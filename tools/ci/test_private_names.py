"""Unit tests for private_names.py: python -m unittest discover -s tools/ci -v

Only made-up names are used here; the real list is local and never published.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import private_names as pn  # noqa: E402

NAMES = ["Zorblax-Secret", "AcmeCorp/Hidden Thing", "quuxwidget"]


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, **kw).stdout


class RepoCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "config", "core.autocrlf", "false")
        self.matcher = pn.Matcher(NAMES)
        self.write("README.md", "hello\nworld\n")
        self.commit("first")

    def write(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD").strip()

    def staged(self):
        git(self.repo, "add", "-A")
        return pn.scan_staged(str(self.repo), self.matcher)


class MatcherTest(unittest.TestCase):
    def test_case_insensitive_plain_text(self):
        m = pn.Matcher(NAMES)
        self.assertEqual(m.in_text("see ZORBLAX-secret here"), ["Zorblax-Secret"])
        self.assertEqual(m.in_text("zorblax.secret"), [])   # not a regular expression
        self.assertEqual(m.in_text("nothing"), [])

    def test_slash_names_match_backslash_spellings(self):
        m = pn.Matcher(NAMES)
        self.assertEqual(m.in_text(r"C:\src\AcmeCorp\Hidden Thing\x"), ["AcmeCorp/Hidden Thing"])

    def test_binary_ascii_and_utf16(self):
        m = pn.Matcher(NAMES)
        self.assertEqual(m.in_bytes(b"\0\0\0QuuxWidget\0"), [("quuxwidget", 3)])
        self.assertEqual(m.in_bytes(b"\0\0" + "QUUXWIDGET".encode("utf-16-le")), [("quuxwidget", 2)])
        self.assertEqual(m.in_bytes(b"\0\0clean\0"), [])


class LoadNamesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def load(self, environ=None, git_config=lambda: [], **kw):
        return pn.load_names(environ or {}, self.home, git_config=git_config, **kw)

    def test_none_configured(self):
        self.assertEqual(self.load(), ([], ""))

    def test_default_file_skips_comments_and_blank_lines(self):
        target = self.home / pn.DEFAULT_NAMES_FILE
        target.parent.mkdir(parents=True)
        target.write_text("# private\n\nZorblax-Secret\n  quuxwidget  \n", encoding="utf-8")
        names, source = self.load()
        self.assertEqual(names, ["Zorblax-Secret", "quuxwidget"])
        self.assertEqual(source, str(target))

    def test_environment_file_beats_the_default(self):
        other = self.home / "other.txt"
        other.write_text("onlyname\n", encoding="utf-8")
        self.assertEqual(self.load({pn.NAMES_FILE_VAR: str(other)})[0], ["onlyname"])

    def test_git_config_is_the_last_resort(self):
        names, source = self.load(git_config=lambda: ["a", "b"])
        self.assertEqual(names, ["a", "b"])
        self.assertIn("kismo.privateNames", source)

    def test_git_config_value_split(self):
        self.assertEqual(pn._split("a; b,c"), ["a", "b", "c"])


class StagedTest(RepoCase):
    def test_clean(self):
        self.write("a.txt", "nothing to see\n")
        self.assertEqual(self.staged(), [])

    def test_added_line_is_reported_with_file_and_line(self):
        self.write("docs/a.md", "one\ntwo\nlinked to Zorblax-Secret now\n")
        hits = self.staged()
        self.assertEqual([h.where for h in hits], ["docs/a.md:3"])

    def test_only_added_lines_count_existing_mentions_do_not_fail_again(self):
        self.write("a.txt", "old zorblax-secret line\n")
        self.commit("old")
        self.write("a.txt", "old zorblax-secret line\nnew clean line\n")
        self.assertEqual(self.staged(), [])

    def test_modified_line_numbers(self):
        self.write("a.txt", "1\n2\n3\n")
        self.commit("three")
        self.write("a.txt", "1\n2\n3\nquuxwidget\n")
        self.assertEqual([h.where for h in self.staged()], ["a.txt:4"])

    def test_a_binary_artefact_is_scanned_by_content(self):
        self.write("site/m.wasm", b"\0asm\1\0\0\0" + b"/src/AcmeCorp/Hidden Thing/x.cpp\0")
        hits = self.staged()
        self.assertEqual(len(hits), 1)
        self.assertIn("site/m.wasm (binary, offset ", hits[0].where)

    def test_a_private_name_in_a_file_path(self):
        self.write("quuxwidget/readme.txt", "clean\n")
        self.assertEqual([h.where for h in self.staged()], ["quuxwidget/readme.txt (path)"])

    def test_deleted_files_are_not_scanned(self):
        self.write("a.txt", "zorblax-secret\n")
        self.commit("old")
        (self.repo / "a.txt").unlink()
        self.assertEqual(self.staged(), [])


class RangeTest(RepoCase):
    def test_a_name_added_then_removed_is_still_found(self):
        base = git(self.repo, "rev-parse", "HEAD").strip()
        self.write("a.txt", "see Zorblax-Secret\n")
        self.commit("add")
        self.write("a.txt", "see nothing\n")
        self.commit("remove")
        hits = pn.scan_range(str(self.repo), self.matcher, [f"{base}..HEAD"])
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].where.endswith("a.txt:1"))

    def test_commit_message(self):
        base = git(self.repo, "rev-parse", "HEAD").strip()
        self.write("a.txt", "clean\n")
        self.commit("port from quuxwidget")
        hits = pn.scan_range(str(self.repo), self.matcher, [f"{base}..HEAD"])
        self.assertEqual([h.where.split(" ", 2)[2] for h in hits], ["(message)"])

    def test_the_first_commit_of_a_new_branch_is_scanned(self):
        self.write("b.txt", "zorblax-secret\n")
        sha = self.commit("two")
        hits = pn.scan_range(str(self.repo), self.matcher, [sha, "--not", "--remotes"])
        self.assertTrue(any(h.where.endswith("b.txt:1") for h in hits))

    def test_pre_push_stdin_forms(self):
        head = git(self.repo, "rev-parse", "HEAD").strip()
        self.assertEqual(pn.pushed_ranges(str(self.repo), ""), [])
        self.assertEqual(pn.pushed_ranges(str(self.repo), f"refs/heads/x {pn.ZERO} refs/heads/x {head}\n"), [])   # a deletion
        new = pn.pushed_ranges(str(self.repo), f"refs/heads/x {head} refs/heads/x {pn.ZERO}\n")
        self.assertEqual(new, [[head, "--not", "--remotes"]])
        known = pn.pushed_ranges(str(self.repo), f"refs/heads/x {head} refs/heads/x {head}\r\n")
        self.assertEqual(known, [[f"{head}..{head}"]])


class FilesAndMainTest(RepoCase):
    def test_scan_files_text_and_binary(self):
        self.write("t.txt", "a\nb quuxwidget\n")
        self.write("b.bin", b"\0\0Zorblax-Secret")
        hits = pn.scan_files(self.matcher, [str(self.repo / "t.txt"), str(self.repo / "b.bin")])
        self.assertTrue(hits[0].where.endswith("t.txt:2"))
        self.assertIn("binary, offset 2", hits[1].where)

    def test_scan_files_checks_the_file_name_not_the_folders(self):
        self.write("quuxwidget/ok.txt", "clean
")
        self.write("plain/quuxwidget.txt", "clean
")
        self.assertEqual(pn.scan_files(self.matcher, [str(self.repo / "quuxwidget" / "ok.txt")]), [])
        self.assertEqual([h.where.endswith("(file name)") for h in pn.scan_files(self.matcher, [str(self.repo / "plain" / "quuxwidget.txt")])], [True])

    def run_main(self, *args, names=None):
        listing = self.repo.parent / "names.txt"
        if names is not None:
            listing.write_text("\n".join(names), encoding="utf-8")
        lines = []
        real = pn.print if hasattr(pn, "print") else None
        pn.print = lambda *a, **k: lines.append(" ".join(map(str, a)))
        try:
            code = pn.main([*args, "--repo", str(self.repo), *(["--names-file", str(listing)] if names is not None else [])])
        finally:
            if real is None:
                del pn.print
        return code, "\n".join(lines)

    def test_no_list_passes_with_a_note(self):
        self.write("a.txt", "zorblax-secret\n")
        git(self.repo, "add", "-A")
        original = pn.load_names
        pn.load_names = lambda **kw: ([], "")
        try:
            code, out = self.run_main("--staged")
            self.assertEqual(code, 0)
            self.assertIn("NOT CHECKED", out)
            self.assertEqual(self.run_main("--staged", "--require-list")[0], 1)
        finally:
            pn.load_names = original

    def test_failure_names_the_file_and_line_and_exits_1(self):
        self.write("docs/x.md", "a\nsee ZORBLAX-SECRET\n")
        git(self.repo, "add", "-A")
        code, out = self.run_main("--staged", names=NAMES)
        self.assertEqual(code, 1)
        self.assertIn("docs/x.md:2", out)

    def test_clean_exits_0(self):
        self.write("docs/x.md", "fine\n")
        git(self.repo, "add", "-A")
        self.assertEqual(self.run_main("--staged", names=NAMES)[0], 0)

    def test_a_git_error_is_exit_2(self):
        self.assertEqual(self.run_main("--range", "nosuchref..alsonone", names=NAMES)[0], 2)


if __name__ == "__main__":
    unittest.main()
