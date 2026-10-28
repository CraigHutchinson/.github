#!/usr/bin/env python3
"""Local CI: run a repository's declared local checks and attest the result as a commit status.

See docs/ci-strategy.md. The target repository declares its checks in `.ci/local.json`:

    {
      "version": 1,
      "levels": {
        "quick": {"steps": [STEP, ...]},
        "full":  {"steps": [STEP, ...]}
      }
    }

A STEP is an object:

    name        label shown in the summary and in the commit status description (required)
    compiler    "gcc" | "clang" | "msvc" | null. The step runs only if that compiler is found; CC/CXX are set
                for gcc and clang. Without a compiler the step always runs (python tests, scripts).
    optional    true: a missing compiler skips the step. false (default): a missing compiler fails the run.
    env         extra environment variables
    requires_env  name of an environment variable that must be set (non-empty) for the step to run; otherwise
                the step is skipped (for slow or opt-in checks, e.g. a reference simulator build)
    ctest       true when the step's commands include a ctest run whose "N tests passed" line should be
                counted into the status description (default: any command starting with "ctest")
    commands    list of commands; each is an argv list or a string (split with shlex). Placeholders:
                {builddir} build-local-<level>-<step name>   {cc} {cxx} compiler executables
                {python} this interpreter   {jobs} CPU count   {repo} repository root

On success a commit status `local-ci/<level>` is posted with `gh api repos/{owner}/{repo}/statuses/{sha}`, but
only when the commit exists on the remote and the working tree is clean. Otherwise it says how to post after
pushing (`--post-only`). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

CONFIG_PATH = Path(".ci") / "local.json"
LAST_RUN_TEMPLATE = ".ci/last-{level}.json"
LEVELS = ("quick", "full")
DESCRIPTION_LIMIT = 140  # GitHub's cap on a commit status description


class CiError(Exception):
    """A configuration or usage error (distinct from a failed check)."""


# ---------------------------------------------------------------------------------------------------------
# Configuration


@dataclass
class Step:
    name: str
    commands: list
    compiler: Optional[str] = None
    optional: bool = False
    env: dict = field(default_factory=dict)
    ctest: Optional[bool] = None
    requires_env: Optional[str] = None


def load_config(repo: Path) -> dict:
    path = repo / CONFIG_PATH
    if not path.is_file():
        raise CiError(f"{path} not found: declare the repository's local checks there (docs/ci-strategy.md)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CiError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(data.get("levels"), dict):
        raise CiError(f"{path}: missing 'levels' object")
    return data


def steps_for(config: dict, level: str) -> list[Step]:
    levels = config["levels"]
    if level not in levels:
        raise CiError(f"level '{level}' is not declared in {CONFIG_PATH} (have: {', '.join(sorted(levels))})")
    steps = []
    for raw in levels[level].get("steps", []):
        for key in ("name", "commands"):
            if key not in raw:
                raise CiError(f"level '{level}': a step lacks '{key}': {raw}")
        if raw.get("compiler") not in (None, "gcc", "clang", "msvc"):
            raise CiError(f"step '{raw['name']}': unknown compiler '{raw.get('compiler')}'")
        steps.append(Step(raw["name"], raw["commands"], raw.get("compiler"), bool(raw.get("optional", False)),
                          dict(raw.get("env", {})), raw.get("ctest"), raw.get("requires_env")))
    if not steps:
        raise CiError(f"level '{level}' declares no steps")
    return steps


# ---------------------------------------------------------------------------------------------------------
# Compilers


@dataclass
class Compiler:
    kind: str       # gcc | clang | msvc
    cc: str         # C compiler executable (empty for msvc: the generator finds it)
    cxx: str
    version: str    # e.g. "GCC 16", "Clang 22", "MSVC"


_VERSION_RE = re.compile(r"(\d+)(?:\.\d+)+")


def _major(version_output: str) -> str:
    m = _VERSION_RE.search(version_output)
    return m.group(1) if m else "?"


def detect_compiler(kind: str, which: Callable[[str], Optional[str]] = shutil.which,
                    run: Callable[[list], str] = None) -> Optional[Compiler]:
    """Find a compiler of this kind on PATH; None if absent."""
    run = run or _version_output
    if kind == "gcc":
        for cc, cxx in (("gcc", "g++"),):
            if which(cc) and which(cxx):
                return Compiler("gcc", cc, cxx, f"GCC {_major(run([cxx, '--version']))}")
    elif kind == "clang":
        if which("clang") and which("clang++"):
            return Compiler("clang", "clang", "clang++", f"Clang {_major(run(['clang++', '--version']))}")
    elif kind == "msvc":
        if which("cl") or which("vswhere") or _vswhere_path():
            return Compiler("msvc", "", "", "MSVC")
    return None


def _vswhere_path() -> Optional[str]:
    pf = os.environ.get("ProgramFiles(x86)")
    if pf:
        p = Path(pf) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if p.is_file():
            return str(p)
    return None


def _version_output(argv: list) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------------------------------------
# Running


def expand(command, values: dict) -> list[str]:
    argv = shlex.split(command, posix=True) if isinstance(command, str) else list(command)
    out = []
    for a in argv:
        for k, v in values.items():
            a = a.replace("{" + k + "}", str(v))
        out.append(a)
    return out


_CTEST_RE = re.compile(r"(\d+)% tests passed, (\d+) tests? failed out of (\d+)")


def parse_ctest(output: str) -> Optional[tuple[int, int]]:
    """(passed, total) from a ctest summary line, else None."""
    m = _CTEST_RE.search(output)
    if not m:
        return None
    failed, total = int(m.group(2)), int(m.group(3))
    return total - failed, total


@dataclass
class StepResult:
    name: str
    status: str                  # passed | failed | skipped
    compiler: str = ""           # e.g. "GCC 16"
    ctest: Optional[tuple] = None
    seconds: float = 0.0
    note: str = ""


def run_command(argv: list[str], cwd: Path, env: dict, echo: bool = True) -> tuple[int, str]:
    """Run, streaming output to the console while capturing it. Returns (exit code, output)."""
    if echo:
        print("  $ " + " ".join(shlex.quote(a) for a in argv), flush=True)
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except OSError as e:
        return 127, f"cannot run {argv[0]}: {e}\n"
    captured = []
    assert proc.stdout is not None
    for line in proc.stdout:
        captured.append(line)
        sys.stdout.write("    " + line)
    return proc.wait(), "".join(captured)


def run_step(step: Step, level: str, repo: Path, compilers: dict, dry_run: bool = False,
             runner: Callable = run_command) -> StepResult:
    comp = None
    if step.requires_env and not os.environ.get(step.requires_env):
        return StepResult(step.name, "skipped", note=f"set {step.requires_env} to run")
    if step.compiler:
        comp = compilers.get(step.compiler)
        if comp is None:
            status = "skipped" if step.optional else "failed"
            return StepResult(step.name, status, note=f"compiler '{step.compiler}' not found")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", step.name)
    values = {"builddir": f"build-local-{level}-{safe}", "python": sys.executable, "jobs": os.cpu_count() or 2,
              "repo": str(repo), "cc": comp.cc if comp else "", "cxx": comp.cxx if comp else ""}
    env = dict(os.environ)
    if comp and comp.kind in ("gcc", "clang"):
        env["CC"], env["CXX"] = comp.cc, comp.cxx
    env.update({k: str(v) for k, v in step.env.items()})
    result = StepResult(step.name, "passed", compiler=comp.version if comp else "")
    started = time.monotonic()
    totals = [0, 0]
    counted = False
    print(f"== {step.name}" + (f" [{comp.version}]" if comp else ""), flush=True)
    for command in step.commands:
        argv = expand(command, values)
        if dry_run:
            print("  (dry-run) $ " + " ".join(shlex.quote(a) for a in argv))
            continue
        code, output = runner(argv, repo, env)
        is_ctest = step.ctest if step.ctest is not None else Path(argv[0]).stem == "ctest"
        if is_ctest:
            counts = parse_ctest(output)
            if counts:
                totals[0] += counts[0]
                totals[1] += counts[1]
                counted = True
        if code != 0:
            result.status = "failed"
            result.note = f"exit {code}: {' '.join(argv[:3])}"
            break
    result.seconds = time.monotonic() - started
    if counted:
        result.ctest = (totals[0], totals[1])
    return result


def run_level(config: dict, level: str, repo: Path, only: Optional[list] = None, dry_run: bool = False,
              detect: Callable = detect_compiler, runner: Callable = run_command) -> list[StepResult]:
    steps = steps_for(config, level)
    if only:
        steps = [s for s in steps if s.name in only]
        if not steps:
            raise CiError(f"--only matched no step in level '{level}'")
    compilers = {}
    for kind in {s.compiler for s in steps if s.compiler}:
        c = detect(kind)
        if c:
            compilers[kind] = c
    return [run_step(s, level, repo, compilers, dry_run, runner) for s in steps]


# ---------------------------------------------------------------------------------------------------------
# Summary and status


def overall_ok(results: list[StepResult]) -> bool:
    return all(r.status != "failed" for r in results) and any(r.status == "passed" for r in results)


def summarise(results: list[StepResult]) -> str:
    lines = []
    for r in results:
        detail = f"ctest {r.ctest[0]}/{r.ctest[1]}" if r.ctest else ""
        extra = " ".join(x for x in (r.compiler, detail, r.note) if x)
        lines.append(f"  {r.status.upper():8} {r.name:30} {extra} ({r.seconds:.0f}s)")
    return "\n".join(lines)


def describe(level: str, results: list[StepResult]) -> str:
    """Status description, e.g. 'GCC 16 gcc: ctest full 3/3; Clang 22 clang-fma: ctest full 3/3'."""
    parts = []
    for r in results:
        if r.status != "passed":
            continue
        label = f"{r.compiler} {r.name}".strip() if r.compiler else r.name
        parts.append(f"{label}: ctest {level} {r.ctest[0]}/{r.ctest[1]}" if r.ctest else f"{label}: ok")
    text = "; ".join(parts) or "no steps"
    return text if len(text) <= DESCRIPTION_LIMIT else text[:DESCRIPTION_LIMIT - 3] + "..."


# ---------------------------------------------------------------------------------------------------------
# Git and GitHub


def _git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise CiError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def parse_remote(url: str) -> tuple[str, str]:
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise CiError(f"cannot parse a GitHub owner/repo from remote url '{url}'")
    return m.group(1), m.group(2)


def commit_on_remote(owner: str, repo: str, sha: str, gh: Callable = None) -> bool:
    gh = gh or _gh
    return gh(["api", f"repos/{owner}/{repo}/commits/{sha}", "--silent"]) == 0


def post_status(owner: str, repo: str, sha: str, level: str, description: str, target_url: str = "",
                gh: Callable = None) -> bool:
    gh = gh or _gh
    args = ["api", f"repos/{owner}/{repo}/statuses/{sha}", "-f", "state=success",
            "-f", f"context=local-ci/{level}", "-f", f"description={description}"]
    if target_url:
        args += ["-f", f"target_url={target_url}"]
    return gh(args) == 0


def _gh(args: list) -> int:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True).returncode
    except OSError:
        return 127


def save_last_run(repo: Path, level: str, sha: str, results: list[StepResult], description: str) -> Path:
    path = repo / LAST_RUN_TEMPLATE.format(level=level)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "sha": sha, "level": level, "ok": overall_ok(results), "description": description,
        "results": [r.__dict__ for r in results]}, indent=2), encoding="utf-8")
    return path


def load_last_run(repo: Path, level: str) -> dict:
    path = repo / LAST_RUN_TEMPLATE.format(level=level)
    if not path.is_file():
        raise CiError(f"no recorded {level} run ({path}); run without --post-only first")
    return json.loads(path.read_text(encoding="utf-8"))


def attest(repo: Path, level: str, sha: str, description: str, allow_dirty: bool, gh: Callable = None) -> int:
    """Post local-ci/<level> if the commit is on the remote and the tree is clean. Returns an exit code."""
    owner, name = parse_remote(_git(repo, "remote", "get-url", "origin"))
    if not allow_dirty and _git(repo, "status", "--porcelain", "--untracked-files=no"):
        print("Not posting: the working tree has uncommitted changes, so the result does not describe a commit.")
        return 0
    if not commit_on_remote(owner, name, sha, gh):
        print(f"Not posting: {sha[:12]} is not on {owner}/{name} yet. After pushing, post the attestation with:\n"
              f"  python {Path(__file__).as_posix()} --level {level} --post-only")
        return 0
    if post_status(owner, name, sha, level, description, gh=gh):
        print(f"Posted local-ci/{level} on {owner}/{name}@{sha[:12]}: {description}")
        return 0
    print(f"Could not post local-ci/{level} (is `gh` authenticated with repo:status access?)", file=sys.stderr)
    return 3


# ---------------------------------------------------------------------------------------------------------
# Command line


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", choices=LEVELS, default="quick")
    ap.add_argument("--repo-dir", default=".", help="repository to check (default: current directory)")
    ap.add_argument("--only", action="append", help="run only the named step(s)")
    ap.add_argument("--no-post", action="store_true", help="run and summarise; never post a status")
    ap.add_argument("--post-only", action="store_true",
                    help="do not run; post the status for the last successful run of this level")
    ap.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    ap.add_argument("--allow-dirty", action="store_true", help="post even with uncommitted changes")
    ap.add_argument("--list", action="store_true", help="list the steps of the level and exit")
    args = ap.parse_args(argv)

    try:
        top = _git(Path(args.repo_dir), "rev-parse", "--show-toplevel")
        repo = Path(top)
        sha = _git(repo, "rev-parse", "HEAD")
        config = load_config(repo)
        if args.list:
            for s in steps_for(config, args.level):
                print(f"{s.name} (compiler: {s.compiler or '-'}{', optional' if s.optional else ''})")
            return 0
        if args.post_only:
            last = load_last_run(repo, args.level)
            if not last["ok"]:
                raise CiError("the recorded run did not succeed; nothing to attest")
            if last["sha"] != sha:
                raise CiError(f"the recorded run verified {last['sha'][:12]} but HEAD is {sha[:12]}; run again")
            return attest(repo, args.level, sha, last["description"], args.allow_dirty)
        results = run_level(config, args.level, repo, args.only, args.dry_run)
    except CiError as e:
        print(f"local_ci: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        return 0
    print(f"\nlocal-ci/{args.level} at {sha[:12]}:\n{summarise(results)}")
    ok = overall_ok(results)
    description = describe(args.level, results)
    save_last_run(repo, args.level, sha, results, description)
    if not ok:
        print("\nFAILED: nothing posted.", file=sys.stderr)
        return 1
    if args.no_post:
        print("\nOK (--no-post: nothing posted).")
        return 0
    try:
        return attest(repo, args.level, sha, description, args.allow_dirty)
    except CiError as e:
        print(f"local_ci: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
