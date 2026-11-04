#!/usr/bin/env python3
"""Guard: a public repository must never name (or link) a private one.

    python private_names.py --staged                    # the staged diff (pre-commit)
    python private_names.py --range <a>..<b>            # every commit in a range, as pushed
    python private_names.py --pre-push                  # reads git's pre-push stdin (refs being pushed)
    python private_names.py --files PATH [PATH ...]     # whole files, e.g. a built .wasm or an archive
    python private_names.py --tree                      # every tracked file at HEAD: content and path (the whole-tree sweep)
    options: --repo DIR (default .), --names-file FILE, --require-list

The forbidden names are read from a LOCAL, UNTRACKED source and are never published (not in this repository, not in a CI
secret, not in the documentation). First found wins:

    1. $KISMO_PRIVATE_NAMES_FILE                     (a path)
    2. ~/.config/kismo/private-names.txt             (%USERPROFILE%\\.config\\kismo\\private-names.txt on Windows)
    3. `git config --global kismo.privateNames`      (one or more values; each may hold several names separated by `;` or `,`)

A names file has one name per line; blank lines and lines starting with `#` are ignored. Matching is case-insensitive and
plain text (not a regular expression); a name containing `/` also matches the same name with `\\`.

What is scanned: the lines ADDED by the diff (text files, reported as file:line), the whole content of every added or
changed binary file (reported as file: binary at offset N; ASCII and UTF-16 spellings), the changed file PATHS, and for a pushed
range the commit messages. In a range every commit is scanned, not the net change: a name added in one commit and removed in the
next is still in the history that gets published.

`--tree` scans the whole tracked tree at HEAD, not a diff: every file path, and every file's content (text lines as file:line,
binary files by ASCII and UTF-16 spelling). It finds mentions committed before the guard existed. History is not scanned.

With no list configured the check passes and says so (a contributor without access to the private list is not blocked); give
`--require-list` where a missing list should fail. Exit status: 0 clean (or no list), 1 a name found, 2 usage or git error.
Standard library only; Windows and Linux.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

NAMES_FILE_VAR = "KISMO_PRIVATE_NAMES_FILE"
DEFAULT_NAMES_FILE = Path(".config") / "kismo" / "private-names.txt"
GIT_CONFIG_KEY = "kismo.privateNames"
ZERO = "0" * 40
MAX_BLOB = 256 * 1024 * 1024   # larger blobs are skipped with a warning (a scan holds one blob in memory)


@dataclass
class Hit:
    where: str      # "file:line", "file (binary, offset N)", "file (path)" or "commit abc1234 (message)"
    name: str       # the entry of the list that matched (shown locally only)

    def __str__(self) -> str:
        return f"{self.where}: names '{self.name}' from your private list"


def _split(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;,\n]", text) if part.strip()]


def load_names(environ=None, home: Path | None = None, names_file: str | None = None, git_config=None) -> tuple[list[str], str]:
    """(names, where they came from). An empty list means none is configured."""
    environ = os.environ if environ is None else environ
    candidates = [names_file] if names_file else []
    candidates.append(environ.get(NAMES_FILE_VAR))
    home = home if home is not None else Path(environ.get("USERPROFILE") or environ.get("HOME") or Path.home())
    candidates.append(str(home / DEFAULT_NAMES_FILE))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            lines = Path(candidate).read_text(encoding="utf-8-sig").splitlines()
            names = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
            if names:
                return names, str(candidate)
    configured = (git_config or _git_global_config)()
    if configured:
        return configured, f"git config --global {GIT_CONFIG_KEY}"
    return [], ""


def _git_global_config() -> list[str]:
    try:
        out = subprocess.run(["git", "config", "--global", "--get-all", GIT_CONFIG_KEY], capture_output=True, text=True).stdout
    except OSError:
        return []
    return [name for value in out.splitlines() for name in _split(value)]


class Matcher:
    def __init__(self, names: list[str]):
        variants = []   # (needle in lower case, the list entry it stands for)
        for name in names:
            lower = name.lower()
            for spelling in {lower, lower.replace("/", "\\"), lower.replace("/", "\\\\")}:
                variants.append((spelling, name))
        self.text = variants
        self.binary = [(spelling.encode("utf-8"), name) for spelling, name in variants] + \
                      [(spelling.encode("utf-16-le"), name) for spelling, name in variants]

    def in_text(self, text: str) -> list[str]:
        lower = text.lower()
        return sorted({name for needle, name in self.text if needle in lower})

    def in_bytes(self, data: bytes) -> list[tuple[str, int]]:
        lower = data.lower()   # bytes.lower folds ASCII only, which is right for ASCII and UTF-16 spellings of ASCII names
        found = {}
        for needle, name in self.binary:
            at = lower.find(needle)
            if at >= 0 and (name not in found or at < found[name]):
                found[name] = at
        return sorted(found.items(), key=lambda item: item[1])


class GitError(Exception):
    pass


def git(repo: str, *args: str, stdin: bytes | None = None, text: bool = True) -> str | bytes:
    proc = subprocess.run(["git", "-C", repo, *args], input=stdin, capture_output=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "replace") if text else proc.stdout


HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,\d+)? @@")


def added_lines(diff: str):
    """Yields (path, line number, text) for each line a unified diff (-U0) adds; binary files have none."""
    path, number = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = None if line.endswith("/dev/null") else line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("@@"):
            match = HUNK.match(line)
            number = int(match.group(1)) if match else 0
        elif path is not None and line.startswith("+") and not line.startswith("+++"):
            yield path, number, line[1:]
            number += 1


def changed_blobs(raw: bytes):
    """Yields (path, blob sha) from `git diff --raw -z` for the added and modified entries."""
    parts = raw.split(b"\0")
    i = 0
    while i < len(parts) and parts[i].startswith(b":"):
        meta = parts[i].split()
        status = meta[4].decode()[:1]
        path = parts[i + 1].decode("utf-8", "replace")
        i += 2
        if status in ("R", "C"):
            i += 1
            path = parts[i - 1].decode("utf-8", "replace")
        sha = meta[3].decode()
        if status != "D" and sha != ZERO:
            yield path, sha


def scan_diff(repo: str, matcher: Matcher, diff_args: list[str], label: str = "", warn=print) -> list[Hit]:
    """Scans what one diff (`diff --cached`, or `diff-tree <commit>`) adds. `label` prefixes the location (a commit)."""
    common = ["--no-ext-diff", "--no-textconv", "--no-renames", "-U0"]
    hits: list[Hit] = []
    seen_text = set()
    diff = git(repo, *diff_args, "-p", *common)
    for path, number, text in added_lines(diff):
        for name in matcher.in_text(text):
            hits.append(Hit(f"{label}{path}:{number}", name))
            seen_text.add(path)
    raw = git(repo, *diff_args, "--raw", "-z", "--no-renames", text=False)
    for path, sha in changed_blobs(raw):
        for name in matcher.in_text(path):
            hits.append(Hit(f"{label}{path} (path)", name))
        size = int(git(repo, "cat-file", "-s", sha).strip() or 0)
        if size > MAX_BLOB:
            warn(f"private-names: WARNING: {path} is {size:,} bytes; too large to scan, skipped")
            continue
        blob = git(repo, "cat-file", "blob", sha, text=False)
        if b"\0" not in blob[:8000] and not blob.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue   # text: its added lines were scanned above (file:line)
        for name, offset in matcher.in_bytes(blob):
            hits.append(Hit(f"{label}{path} (binary, offset {offset})", name))
    return hits


def scan_staged(repo: str, matcher: Matcher) -> list[Hit]:
    return scan_diff(repo, matcher, ["diff", "--cached"])


def scan_range(repo: str, matcher: Matcher, revs: list[str], warn=print) -> list[Hit]:
    """Every commit `git rev-list <revs>` lists, oldest first: its message, then what it adds."""
    hits: list[Hit] = []
    for sha in git(repo, "rev-list", "--reverse", *revs).split():
        short = sha[:9]
        message = git(repo, "show", "-s", "--format=%an <%ae>%n%B", sha)
        for name in matcher.in_text(message):
            hits.append(Hit(f"commit {short} (message)", name))
        hits += scan_diff(repo, matcher, ["diff-tree", "--root", "-r", sha], label=f"{short} ", warn=warn)
    return hits


def scan_files(matcher: Matcher, paths: list[str]) -> list[Hit]:
    hits = []
    for path in paths:
        for name in matcher.in_text(Path(path).name):   # the file name, not the folders it sits in
            hits.append(Hit(f"{path} (file name)", name))
        data = Path(path).read_bytes()
        if b"\0" in data[:8000]:
            hits += [Hit(f"{path} (binary, offset {offset})", name) for name, offset in matcher.in_bytes(data)]
        else:
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                hits += [Hit(f"{path}:{number}", name) for name in matcher.in_text(line)]
    return hits


def scan_tree(repo: str, matcher: Matcher, warn=print) -> list[Hit]:
    """Every file tracked at HEAD: its path, then its content (text by line, binary by ASCII and UTF-16 offset)."""
    hits: list[Hit] = []
    listing = git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD", text=False)
    entries = []   # (path, blob sha); symlinks and submodules hold no scannable content
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, kind, sha = meta.decode().split()
        path = raw_path.decode("utf-8", "replace")
        for name in matcher.in_text(path):
            hits.append(Hit(f"{path} (path)", name))
        if kind == "blob" and mode != "120000":
            entries.append((path, sha))
    for path, sha in entries:
        size = int(git(repo, "cat-file", "-s", sha).strip() or 0)
        if size > MAX_BLOB:
            warn(f"private-names: WARNING: {path} is {size:,} bytes; too large to scan, skipped")
            continue
        blob = git(repo, "cat-file", "blob", sha, text=False)
        if b"\0" in blob[:8000] or blob.startswith((b"\xff\xfe", b"\xfe\xff")):
            hits += [Hit(f"{path} (binary, offset {offset})", name) for name, offset in matcher.in_bytes(blob)]
        else:
            for number, line in enumerate(blob.decode("utf-8", "replace").splitlines(), 1):
                hits += [Hit(f"{path}:{number}", name) for name in matcher.in_text(line)]
    return hits


def pushed_ranges(repo: str, stdin_text: str) -> list[list[str]]:
    """The rev-list arguments for the refs git is pushing (its pre-push stdin: `<local ref> <local sha> <remote ref> <remote sha>`).
    A new branch, or a remote commit we lack, covers the commits no remote-tracking branch has."""
    ranges = []
    for line in stdin_text.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[1] == ZERO:
            continue   # blank, or a deletion: nothing is pushed
        local, remote = fields[1], fields[3]
        known = remote != ZERO and subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{remote}^{{commit}}"], capture_output=True).returncode == 0
        ranges.append([f"{remote}..{local}"] if known else [local, "--not", "--remotes"])
    return ranges


def report(hits: list[Hit], names_from: str, scanned: str, out=None) -> int:
    out = out or print
    if not hits:
        out(f"private-names: clean ({scanned}; list from {names_from})")
        return 0
    out(f"private-names: FAILED - {len(hits)} mention(s) of a private repository name in {scanned}:")
    for hit in hits:
        out(f"  {hit}")
    out("Remove them (rewrite the commit if they are already committed); the names list stays local and must not be published.")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="the staged diff")
    mode.add_argument("--range", metavar="REVS", help="a rev-list range such as origin/main..HEAD")
    mode.add_argument("--pre-push", action="store_true", help="read git's pre-push stdin")
    mode.add_argument("--tree", action="store_true", help="every tracked file at HEAD (whole-tree sweep)")
    mode.add_argument("--files", nargs="+", metavar="PATH", help="scan whole files")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--names-file", help="read the names from this file instead of the usual places")
    ap.add_argument("--require-list", action="store_true", help="fail, instead of pass with a note, when no list is configured")
    args = ap.parse_args(argv)

    names, names_from = load_names(names_file=args.names_file)
    if not names:
        print("private-names: no private-names list configured (see tools/ci/private_names.py); NOT CHECKED")
        return 1 if args.require_list else 0
    matcher = Matcher(names)
    try:
        if args.staged:
            return report(scan_staged(args.repo, matcher), names_from, "the staged changes")
        if args.range:
            return report(scan_range(args.repo, matcher, args.range.split()), names_from, f"range {args.range}")
        if args.tree:
            return report(scan_tree(args.repo, matcher), names_from, "the tracked tree at HEAD")
        if args.files:
            return report(scan_files(matcher, args.files), names_from, f"{len(args.files)} file(s)")
        ranges = pushed_ranges(args.repo, sys.stdin.read())
        hits = [hit for revs in ranges for hit in scan_range(args.repo, matcher, revs)]
        return report(hits, names_from, f"{len(ranges)} pushed ref(s)")
    except (GitError, OSError) as error:
        print(f"private-names: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
