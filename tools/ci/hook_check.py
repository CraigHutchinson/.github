#!/usr/bin/env python3
"""Hook self-check: prove a repository's local push gate does what it should, without a push.

    python hook_check.py --repo DIR [--timeout 30] [--no-selftest]

See docs/ci-strategy.md, "Local gates must self-test". Checks, each a row of a pass/fail table:

    active hook     a pre-push hook is active (`core.hooksPath` resolved, else `.git/hooks`); a committed
                    `.githooks/pre-push` that is not the active one is reported (git ignores it until
                    `git config core.hooksPath .githooks`)
    empty push      the active hook, run as git runs it for an up-to-date push (empty stdin, the repository as the
                    working directory, a clean environment), exits 0 within --timeout seconds: a hook that works on a
                    push with nothing to push is building or measuring what it should skip
    status masking  a static lint of the shell hooks: a pipeline whose status matters (`set -e`, a `$?` check, `||`)
                    but ends in a filter such as `tail` returns the filter's status, so a failed build passes, unless
                    `set -o pipefail` is on
    deep self-test  the commands of the `"selftest"` list in `.ci/local.json`, the repository's own deep check (for
                    example one that drives its gate with synthetic pushes); a command that exits non-zero fails

A row is PASS, WARN (worth a look, does not fail the run), FAIL or SKIP (nothing to check). The exit code is 1 on any
FAIL, 2 on a usage error. In `.ci/local.json`, `"selftest"` commands take the placeholders of local_ci.py
({python} {repo} {ci_dir}); they run in the repository. Standard library only; Windows and Linux.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_ci  # noqa: E402  (the config path and command expansion are shared with the local CI runner)

HOOK = "pre-push"
DEFAULT_TIMEOUT = 30.0
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
# Commands that pass their input through: a pipeline ending in one returns its status, not the producer's.
FILTERS = {"tail", "head", "tee", "grep", "egrep", "fgrep", "sed", "awk", "sort", "uniq", "cut", "tr", "wc", "less", "more",
           "cat", "xargs", "column", "fold", "nl", "rev"}
PRODUCERS_THAT_CANNOT_FAIL = {"echo", "printf"}


@dataclass
class Row:
    status: str
    check: str
    detail: str


# ---------------------------------------------------------------------------------------------------------
# Git


def _git(repo: Path, *args: str) -> Optional[str]:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def hooks_dir(repo: Path) -> Path:
    """The directory git runs hooks from: `core.hooksPath` if set, else the repository's hooks directory."""
    found = _git(repo, "rev-parse", "--git-path", "hooks")
    path = Path(found or ".git/hooks")
    return path if path.is_absolute() else repo / path


def is_real_hook(path: Path) -> bool:
    return path.is_file() and path.suffix != ".sample"


def committed_hooks(repo: Path) -> list[Path]:
    """Shell hooks the repository tracks under .githooks/."""
    listed = _git(repo, "ls-files", ".githooks") or ""
    return [repo / line for line in listed.splitlines() if line]


# ---------------------------------------------------------------------------------------------------------
# Check 1: active hook


def check_active(repo: Path) -> tuple[list[Row], Optional[Path]]:
    d = hooks_dir(repo)
    active = d / HOOK
    configured = _git(repo, "config", "--get", "core.hooksPath")
    where = f"core.hooksPath={configured}" if configured else "core.hooksPath unset"
    rows: list[Row] = []
    found = active if is_real_hook(active) else None
    if found is None:
        rows.append(Row(FAIL, "active hook", f"no {HOOK} hook in {d} ({where}): git runs nothing on push"))
    else:
        rows.append(Row(PASS, "active hook", f"{found} ({where})"))
    for path in committed_hooks(repo):
        if path.name != HOOK:
            continue
        try:
            same = found is not None and path.resolve() == found.resolve()
        except OSError:
            same = False
        if not same:
            rows.append(Row(WARN, "committed hook",
                            f"{path.relative_to(repo).as_posix()} is committed but not the active hook ({where}); git ignores it "
                            f"until `git config core.hooksPath .githooks`" + ("" if found else ", so nothing gates the push")))
    return rows, found


# ---------------------------------------------------------------------------------------------------------
# Check 2: empty push


def shebang_interpreter(path: Path) -> Optional[str]:
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("#!"):
        return None
    parts = first[2:].split()
    if not parts:
        return None
    name = Path(parts[0]).name
    return Path(parts[1]).name if name == "env" and len(parts) > 1 else name


def _git_sh() -> Optional[str]:
    """A POSIX shell on Windows: the one on PATH, else the one bundled with Git for Windows."""
    found = shutil.which("sh") or shutil.which("bash")
    if found:
        return found
    git = shutil.which("git")
    if git:
        for rel in ("../usr/bin/sh.exe", "../../usr/bin/sh.exe", "../bin/sh.exe"):
            candidate = (Path(git).resolve().parent / rel).resolve()
            if candidate.is_file():
                return str(candidate)
    return None


def hook_command(path: Path, which: Callable = shutil.which) -> Optional[list[str]]:
    """The argv that runs a hook the way git does: its shebang's interpreter on Windows, the file itself on POSIX."""
    interpreter = shebang_interpreter(path)
    if os.name == "posix" and os.access(path, os.X_OK):
        return [str(path)]
    if interpreter in (None, "sh", "bash", "dash", "zsh", "ksh"):
        shell = which(interpreter) if interpreter and interpreter != "sh" and which(interpreter) else _git_sh()
        return [shell, str(path)] if shell else None
    if interpreter and interpreter.startswith("python"):
        return [sys.executable, str(path)]
    found = which(interpreter) if interpreter else None
    return [found, str(path)] if found else None


def clean_environment(environ: dict) -> dict:
    """The environment git gives a hook, minus what changes a gate's behaviour: GIT_* and bypass or tuning variables."""
    drop = ("GIT_", "SUB0")
    return {k: v for k, v in environ.items() if not k.upper().startswith(drop) and not k.upper().endswith("_SKIP")}


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        proc.kill()


def run_hook_empty(repo: Path, hook: Path, timeout: float, environ: Optional[dict] = None) -> Row:
    argv = hook_command(hook)
    if argv is None:
        return Row(WARN, "empty push", f"cannot run {hook.name}: no shell for its shebang on this machine")
    argv += ["origin", "https://example.invalid/empty.git"]  # the remote name and url git passes
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = subprocess.Popen(argv, cwd=repo, env=clean_environment(os.environ if environ is None else environ),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", **kwargs)
    except OSError as e:
        return Row(FAIL, "empty push", f"cannot run {hook}: {e}")
    try:
        output, _ = proc.communicate(input="", timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        return Row(FAIL, "empty push", f"still running after {timeout:g} s with nothing to push: it builds or measures "
                                       f"instead of skipping an up-to-date push")
    tail = " | ".join(output.strip().splitlines()[-2:])
    if proc.returncode != 0:
        return Row(FAIL, "empty push", f"exit {proc.returncode} on an up-to-date push (should be 0)" + (f": {tail}" if tail else ""))
    return Row(PASS, "empty push", f"exit 0 with empty stdin" + (f" ({tail})" if tail else ""))


# ---------------------------------------------------------------------------------------------------------
# Check 3: exit-status masking (static)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """(first line number, text) with comments removed, backslash continuations joined and here-document bodies dropped."""
    out: list[tuple[int, str]] = []
    pending, start, heredoc = "", 0, None
    for number, raw in enumerate(text.splitlines(), 1):
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        line = _strip_comment(raw)
        if not pending:
            start = number
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        line = pending + line
        pending = ""
        if line.strip():
            out.append((start, line.strip()))
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        if m:
            heredoc = m.group(1)
    return out


def _strip_comment(line: str) -> str:
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _split_unquoted(line: str, separators: tuple[str, ...]) -> list[tuple[str, str]]:
    """[(segment, separator that followed it)] splitting on the longest separator found outside quotes."""
    parts, quote, i, start = [], None, 0, 0
    ordered = sorted(separators, key=len, reverse=True)
    while i < len(line):
        c = line[i]
        if quote:
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        hit = next((s for s in ordered if line.startswith(s, i)), None)
        if hit == "|" and line.startswith("||", i):  # `||` is a list separator, not a pipe
            i += 2
            continue
        if hit:
            parts.append((line[start:i].strip(), hit))
            i += len(hit)
            start = i
            continue
        i += 1
    parts.append((line[start:].strip(), ""))
    return [p for p in parts if p[0]]


def _command_name(stage: str) -> str:
    words = stage.split()
    while words and (words[0] in ("!", "if", "while", "until", "then", "do", "exec", "time", "command") or
                     re.match(r"^\w+=", words[0])):
        words = words[1:]
    return Path(words[0].strip("'\"(")).name.lower().removesuffix(".exe") if words else ""


def has_errexit(text: str) -> bool:
    """True when the script turns on `set -e` (also `-eu`, `-o errexit`, or a `#!/bin/sh -e` line)."""
    first = text.splitlines()[0] if text else ""
    if re.match(r"^#!\S+(?:\s+\S+)*?\s+-\w*e", first):
        return True
    return any(re.match(r"^set\s+(?:-\w*e\w*|.*-o\s+errexit)(?:\s|$)", line) for _, line in _logical_lines(text))


def has_pipefail(text: str) -> bool:
    return any(re.match(r"^set\s+.*pipefail", line) for _, line in _logical_lines(text))


def lint_status_masking(text: str) -> list[tuple[int, str]]:
    """(line number, message) for each pipeline whose status matters but is the status of a trailing filter.

    A pipeline's status is its last command's, unless `set -o pipefail`. So `cmake --build . 2>&1 | tail -5` succeeds
    when the build fails; under `set -e` the script goes on, and an `if [ $? -ne 0 ]` after it never fires. Status
    "matters" when `set -e` is on, when `$?` is read on the same or the next line, or the pipeline is tested with
    `!`, `if`, `||` or `&&`.
    """
    if has_pipefail(text):
        return []
    errexit = has_errexit(text)
    findings: list[tuple[int, str]] = []
    lines = _logical_lines(text)
    for index, (number, line) in enumerate(lines):
        for segment, following in _split_unquoted(line, ("&&", "||", ";")):
            stages = _split_unquoted(segment, ("|",))
            if len(stages) < 2:
                continue
            first, last = _command_name(stages[0][0]), _command_name(stages[-1][0])
            if last not in FILTERS or first in PRODUCERS_THAT_CANNOT_FAIL:
                continue
            next_line = lines[index + 1][1] if index + 1 < len(lines) else ""
            tested = (re.match(r"^(!|if\b|while\b|until\b)", segment) is not None or following in ("||", "&&")
                      or "$?" in line or "PIPESTATUS" in line or re.match(r"^(if\s+)?\[+\s*\"?\$\?", next_line) is not None)
            if errexit or tested:
                why = " and ".join(r for r, on in (("`set -e` is on", errexit), ("its status is tested", tested)) if on)
                findings.append((number, f"`{first} ... | {last}` returns {last}'s status, so a failure of {first} passes ({why}); "
                                         f"add `set -o pipefail`"))
    return findings


def check_masking(repo: Path, active: Optional[Path]) -> list[Row]:
    files: list[Path] = []
    for path in ([active] if active else []) + committed_hooks(repo):
        if path.is_file() and path not in files and not any(path.resolve() == f.resolve() for f in files):
            files.append(path)
    if not files:
        return [Row(SKIP, "status masking", "no shell hook to lint")]
    rows = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            rows.append(Row(FAIL, "status masking", f"cannot read {path}: {e}"))
            continue
        name = path.relative_to(repo).as_posix() if repo in path.parents else str(path)
        if shebang_interpreter(path) not in (None, "sh", "bash", "dash", "zsh", "ksh"):
            rows.append(Row(SKIP, "status masking", f"{name} is not a shell script"))
            continue
        found = lint_status_masking(text)
        if found:
            rows += [Row(FAIL, "status masking", f"{name}:{n}: {message}") for n, message in found]
        else:
            rows.append(Row(PASS, "status masking", f"{name}: no masked pipeline status"))
    return rows


# ---------------------------------------------------------------------------------------------------------
# Check 4: deep self-test


def selftest_commands(repo: Path) -> list:
    path = repo / local_ci.CONFIG_PATH
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise local_ci.CiError(f"{path}: invalid JSON: {e}") from e
    found = data.get("selftest", [])
    if not isinstance(found, list):
        raise local_ci.CiError(f"{path}: 'selftest' must be a list of commands")
    return found


def run_selftest(repo: Path, timeout: float = 600.0, runner: Callable = None) -> list[Row]:
    try:
        commands = selftest_commands(repo)
    except local_ci.CiError as e:
        return [Row(FAIL, "deep self-test", str(e))]
    if not commands:
        return [Row(SKIP, "deep self-test", f'no "selftest" list in {local_ci.CONFIG_PATH.as_posix()}')]
    values = {"python": sys.executable, "repo": str(repo), "ci_dir": str(Path(__file__).resolve().parent)}
    rows = []
    for command in commands:
        argv = local_ci.expand(command, values)
        label = " ".join(Path(a).name if os.path.isabs(a) and i == 0 else a for i, a in enumerate(argv))
        if runner is not None:
            code, output = runner(argv, repo)
        else:
            code, output = _run(argv, repo, timeout)
        if code == 0:
            rows.append(Row(PASS, "deep self-test", label))
        else:
            tail = " | ".join(output.strip().splitlines()[-3:])
            rows.append(Row(FAIL, "deep self-test", f"exit {code}: {label}" + (f": {tail}" if tail else "")))
        if output.strip() and code == 0:
            rows[-1].detail += "\n" + "\n".join("      " + l for l in output.rstrip().splitlines())
    return rows


def _run(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except OSError as e:
        return 127, f"cannot run {argv[0]}: {e}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g} s"
    return p.returncode, p.stdout + p.stderr


# ---------------------------------------------------------------------------------------------------------
# Command line


def render(rows: list[Row], repo: Path) -> str:
    width = max(len(r.check) for r in rows)
    lines = [f"hook_check: {repo}", ""]
    for r in rows:
        first, *rest = r.detail.split("\n")
        lines.append(f"{r.status:5} {r.check:{width}}  {first}")
        lines += [f"{'':5} {'':{width}}  {l}" for l in rest]
    counts = {s: sum(1 for r in rows if r.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    lines += ["", ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n) or "nothing checked"]
    return "\n".join(lines)


def run_checks(repo: Path, timeout: float = DEFAULT_TIMEOUT, selftest: bool = True) -> list[Row]:
    rows, active = check_active(repo)
    if active:
        rows.append(run_hook_empty(repo, active, timeout))
    else:
        rows.append(Row(SKIP, "empty push", "no active hook to run"))
    rows += check_masking(repo, active)
    if selftest:
        rows += run_selftest(repo)
    return rows


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repository to check (default: current directory)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="seconds the hook may take on an empty push (default %(default)s)")
    ap.add_argument("--no-selftest", action="store_true", help="skip the repository's own deep self-test")
    args = ap.parse_args(argv)
    top = _git(Path(args.repo), "rev-parse", "--show-toplevel")
    if top is None:
        print(f"hook_check: {args.repo} is not a git repository", file=sys.stderr)
        return 2
    repo = Path(top)
    rows = run_checks(repo, args.timeout, not args.no_selftest)
    print(render(rows, repo))
    return 1 if any(r.status == FAIL for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
