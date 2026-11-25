"""Unit tests for hook_check.py: python -m unittest discover -s tools/ci -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_check as hc  # noqa: E402

# The template that shipped in several repositories: tail's status wins, and the later `$?` test is dead code.
BAD_TEMPLATE = """#!/bin/bash
set -e

echo "Running tests before push..."
cmake --build --preset default --config Release 2>&1 | tail -5
if [ $? -ne 0 ]; then
    echo "BUILD FAILED - push aborted."
    exit 1
fi
ctest --preset default 2>&1
"""

GOOD_TEMPLATE = """#!/usr/bin/env bash
set -euo pipefail
cmake --build build 2>&1 | tail -5
"""

EMPTY_OK = "#!/bin/sh\ncat >/dev/null\nexit 0\n"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class LintTests(unittest.TestCase):
    def test_the_shipped_template_is_flagged(self):
        found = hc.lint_status_masking(BAD_TEMPLATE)
        self.assertEqual([n for n, _ in found], [5])
        self.assertIn("pipefail", found[0][1])

    def test_pipefail_clears_it(self):
        self.assertEqual(hc.lint_status_masking(GOOD_TEMPLATE), [])
        self.assertEqual(hc.lint_status_masking(BAD_TEMPLATE.replace("set -e", "set -e\nset -o pipefail")), [])
        self.assertEqual(hc.lint_status_masking(BAD_TEMPLATE.replace("set -e", "set -eo pipefail")), [])

    def test_status_that_does_not_matter_is_not_flagged(self):
        self.assertEqual(hc.lint_status_masking("#!/bin/sh\ncmake --build . 2>&1 | tail -5\necho done\n"), [])

    def test_a_tested_pipeline_is_flagged_without_set_e(self):
        self.assertEqual(len(hc.lint_status_masking("#!/bin/sh\nbuild | tail -3 || exit 1\n")), 1)
        self.assertEqual(len(hc.lint_status_masking("#!/bin/sh\nif ! build | grep ok; then exit 1; fi\n")), 1)
        self.assertEqual(len(hc.lint_status_masking("#!/bin/sh\nbuild | tee log\n[ $? -eq 0 ] || exit 1\n")), 1)

    def test_a_pipeline_that_ends_in_the_command_that_matters_is_fine(self):
        text = "#!/bin/sh\nset -e\nprintf '%s\\n' \"$refs\" | python gate.py --refs-from-stdin\nrefs=\"$(cat)\" || exit $?\n"
        self.assertEqual(hc.lint_status_masking(text), [])
        self.assertEqual(hc.lint_status_masking("#!/bin/sh\nset -e\necho x | tail -1\n"), [])  # echo cannot fail

    def test_comments_quotes_heredocs_and_or_lists_are_ignored(self):
        text = ("#!/bin/sh\nset -e\n# cmake --build . | tail -5\nmsg='a | tail'\nbuild || tail log\n"
                "cat <<EOF\nbuild | tail -1\nEOF\nexit 0\n")
        self.assertEqual(hc.lint_status_masking(text), [])

    def test_backslash_continuation_is_joined(self):
        self.assertEqual(len(hc.lint_status_masking("#!/bin/sh\nset -e\nbuild \\\n  | tail -5\n")), 1)

    def test_errexit_detection(self):
        self.assertTrue(hc.has_errexit("#!/bin/sh\nset -eu\n"))
        self.assertTrue(hc.has_errexit("#!/bin/sh -e\n"))
        self.assertTrue(hc.has_errexit("#!/bin/sh\nset -o errexit\n"))
        self.assertFalse(hc.has_errexit("#!/bin/sh\nset -u\n# set -e\n"))
        self.assertFalse(hc.has_errexit("#!/bin/sh\nset +e\n"))


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")

    def write_hook(self, text, where=".git/hooks"):
        path = self.repo / where / "pre-push"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)
        return path

    def need_shell(self, path):
        if hc.hook_command(path) is None:
            self.skipTest("no POSIX shell to run hooks")

    def rows(self, **kw):
        return {r.check: r for r in hc.run_checks(self.repo, selftest=False, **kw)}


class ActiveHookTests(RepoCase):
    def test_no_hook_fails(self):
        rows = self.rows()
        self.assertEqual(rows["active hook"].status, hc.FAIL)
        self.assertEqual(rows["empty push"].status, hc.SKIP)

    def test_installed_hook_passes(self):
        self.write_hook(EMPTY_OK)
        self.assertEqual(self.rows()["active hook"].status, hc.PASS)

    def test_committed_but_inactive_hook_is_reported(self):
        self.write_hook(EMPTY_OK, ".githooks")
        git(self.repo, "add", ".githooks/pre-push")
        rows = self.rows()
        self.assertEqual(rows["active hook"].status, hc.FAIL)  # nothing runs: hooksPath is unset
        self.assertEqual(rows["committed hook"].status, hc.WARN)
        self.assertIn("core.hooksPath", rows["committed hook"].detail)

    def test_activating_the_committed_hook_clears_the_report(self):
        self.write_hook(EMPTY_OK, ".githooks")
        git(self.repo, "add", ".githooks/pre-push")
        git(self.repo, "config", "core.hooksPath", ".githooks")
        rows = self.rows()
        self.assertEqual(rows["active hook"].status, hc.PASS)
        self.assertNotIn("committed hook", rows)

    def test_samples_do_not_count(self):
        sample = self.repo / ".git" / "hooks" / "pre-push.sample"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text("#!/bin/sh\n", encoding="utf-8")
        self.assertEqual(self.rows()["active hook"].status, hc.FAIL)


class EmptyPushTests(RepoCase):
    def test_hook_exiting_zero_passes(self):
        path = self.write_hook(EMPTY_OK)
        self.need_shell(path)
        self.assertEqual(self.rows()["empty push"].status, hc.PASS)

    def test_hook_exiting_non_zero_fails(self):
        path = self.write_hook("#!/bin/sh\nexit 3\n")
        self.need_shell(path)
        row = self.rows()["empty push"]
        self.assertEqual(row.status, hc.FAIL)
        self.assertIn("exit 3", row.detail)

    def test_hook_that_keeps_working_times_out(self):
        path = self.write_hook("#!/bin/sh\nsleep 30\n")
        self.need_shell(path)
        row = self.rows(timeout=1.0)["empty push"]
        self.assertEqual(row.status, hc.FAIL)
        self.assertIn("still running", row.detail)

    def test_runs_in_the_repository_with_empty_stdin_and_a_clean_environment(self):
        path = self.write_hook('#!/bin/sh\nrefs="$(cat)"\n[ -z "$refs" ] || exit 4\n[ -d .git ] || exit 5\n'
                               '[ -z "$SUB0PERF_SKIP" ] || exit 6\n[ -z "$GIT_DIR" ] || exit 7\nexit 0\n')
        self.need_shell(path)
        os.environ["SUB0PERF_SKIP"] = "x"
        os.environ["GIT_DIR"] = "elsewhere"
        try:
            row = hc.run_hook_empty(self.repo, path, 20.0)
        finally:
            del os.environ["SUB0PERF_SKIP"]
            del os.environ["GIT_DIR"]
        self.assertEqual(row.status, hc.PASS, row.detail)

    def test_clean_environment_drops_gate_variables(self):
        env = hc.clean_environment({"PATH": "/bin", "GIT_DIR": "x", "SUB0PERF_LEVEL": "full", "X_SKIP": "1", "HOME": "/h"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h"})


class MaskingRowTests(RepoCase):
    def test_active_and_committed_hooks_are_linted(self):
        self.write_hook(GOOD_TEMPLATE)
        self.write_hook(BAD_TEMPLATE, ".githooks")
        git(self.repo, "add", ".githooks/pre-push")
        rows = [r for r in hc.run_checks(self.repo, selftest=False) if r.check == "status masking"]
        self.assertEqual([r.status for r in rows], [hc.PASS, hc.FAIL])
        self.assertIn(".githooks/pre-push:5", rows[1].detail)

    def test_no_hook_to_lint(self):
        self.assertEqual(self.rows()["status masking"].status, hc.SKIP)

    def test_non_shell_hook_is_skipped(self):
        self.write_hook("#!/usr/bin/env python3\nprint('x')\n")
        self.assertEqual(self.rows()["status masking"].status, hc.SKIP)


class SelftestTests(RepoCase):
    def config(self, **kw):
        (self.repo / ".ci").mkdir(exist_ok=True)
        (self.repo / ".ci" / "local.json").write_text(json.dumps({"version": 1, "levels": {}, **kw}), encoding="utf-8")

    def test_no_declaration_is_a_skip(self):
        self.assertEqual(hc.run_selftest(self.repo)[0].status, hc.SKIP)
        self.config()
        self.assertEqual(hc.run_selftest(self.repo)[0].status, hc.SKIP)

    def test_commands_run_in_the_repo_with_placeholders(self):
        self.config(selftest=[["{python}", "-c", "import os,sys; sys.exit(0 if os.path.isdir('.git') else 1)"],
                              "{python} {ci_dir}/hook_check.py --help"])
        rows = hc.run_selftest(self.repo)
        self.assertEqual([r.status for r in rows], [hc.PASS, hc.PASS])

    def test_failing_command_fails_with_its_output(self):
        self.config(selftest=[["{python}", "-c", "print('broken wiring'); raise SystemExit(5)"]])
        row = hc.run_selftest(self.repo)[0]
        self.assertEqual(row.status, hc.FAIL)
        self.assertIn("exit 5", row.detail)
        self.assertIn("broken wiring", row.detail)

    def test_bad_declarations_fail(self):
        self.config(selftest="not a list")
        self.assertEqual(hc.run_selftest(self.repo)[0].status, hc.FAIL)
        (self.repo / ".ci" / "local.json").write_text("{", encoding="utf-8")
        self.assertEqual(hc.run_selftest(self.repo)[0].status, hc.FAIL)

    def test_unrunnable_command_fails(self):
        self.config(selftest=[["no-such-program-xyz"]])
        self.assertEqual(hc.run_selftest(self.repo)[0].status, hc.FAIL)


class TemplateTests(RepoCase):
    """templates/pre-push is the corrected reference: it must pass the checks it exists to satisfy."""
    TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "pre-push"

    def setUp(self):
        super().setUp()
        self.hook = self.write_hook(self.TEMPLATE.read_text(encoding="utf-8"))
        self.need_shell(self.hook)
        for name, text in (("a.cpp", "int main() {}\n"), ("README.md", "x\n")):
            (self.repo / name).write_text(text, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "one")
        self.base = self.sha()

    def sha(self):
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def commit(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", name)

    def push(self, stdin):
        argv = hc.hook_command(self.hook) + ["origin", "url"]
        done = subprocess.run(argv, input=stdin.encode(), cwd=self.repo, capture_output=True, timeout=60)  # bytes: LF, as git sends
        return subprocess.CompletedProcess(argv, done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace"))

    def test_lint_clean_and_pipefail_on(self):
        text = self.TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(hc.lint_status_masking(text), [])
        self.assertTrue(hc.has_pipefail(text))

    def test_passes_the_full_check(self):
        rows = {r.check: r for r in hc.run_checks(self.repo, selftest=False)}
        self.assertEqual([rows[c].status for c in ("active hook", "empty push", "status masking")], [hc.PASS] * 3)

    def test_empty_stdin_and_deletions_skip(self):
        self.assertEqual(self.push("").returncode, 0)
        self.assertEqual(self.push(f"refs/heads/x {'0' * 40} refs/heads/x {self.base}\n").returncode, 0)

    def test_documentation_only_push_skips(self):
        self.commit("docs-note.md", "more\n")
        result = self.push(f"refs/heads/main {self.sha()} refs/heads/main {self.base}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("documentation only", result.stdout)

    def test_a_code_push_runs_the_build_and_its_failure_blocks(self):
        self.commit("b.cpp", "int f() { return 1; }\n")
        result = self.push(f"refs/heads/main {self.sha()} refs/heads/main {self.base}\n")  # no build directory exists
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUILD FAILED", result.stdout + result.stderr)

    def test_a_new_branch_is_treated_as_code(self):
        result = self.push(f"refs/heads/new {self.sha()} refs/heads/new {'0' * 40}\n")
        self.assertNotEqual(result.returncode, 0)

    def stanza_hook(self, gate_exit):
        """The template with the perf stanza uncommented, `build`/`tests` replaced by `true`, and a fake gate that echoes its stdin."""
        text = self.TEMPLATE.read_text(encoding="utf-8")
        start = text.index("# tooling=")
        end = text.index('echo "pre-push: all checks passed."')
        stanza = "\n".join(line[2:] if line.startswith("# ") else line for line in text[start:end].splitlines()) + "\n"
        text = text[:start] + stanza + text[end:]
        text = text.replace("build=(cmake --build build --config Release -j4)", "build=(true)")
        text = text.replace("tests=(ctest --test-dir build --output-on-failure)", "tests=(true)")
        tooling = Path(self.tmp.name) / "tooling"
        tooling.mkdir(exist_ok=True)
        (tooling / "bench_check.py").write_text(
            "import os, sys\nprint('GATE output=' + os.environ.get('SUB0PERF_OUTPUT', '') + ' args=' + ' '.join(sys.argv[1:]))\n"
            "print('STDIN ' + sys.stdin.read().strip())\n" f"sys.exit({gate_exit})\n", encoding="utf-8")
        (self.repo / "build").mkdir(exist_ok=True)
        (self.repo / "build" / "sub0perf_tooling_dir.txt").write_text(tooling.as_posix())
        self.hook = self.write_hook(text)
        return text

    def test_the_perf_stanza_is_commented_out_by_default(self):
        self.commit("b.cpp", "int f() { return 1; }\n")
        text = self.TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("bench_check.py", text)
        self.assertTrue(all(line.startswith("#") for line in text.splitlines() if "bench_check.py" in line))

    def test_the_uncommented_stanza_gets_the_refs_and_brief_output(self):
        text = self.stanza_hook(0)
        self.assertEqual(hc.lint_status_masking(text), [])
        self.commit("b.cpp", "int f() { return 1; }\n")
        refs = f"refs/heads/main {self.sha()} refs/heads/main {self.base}"
        result = self.push(refs + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GATE output=brief args=gate --refs-from-stdin", result.stdout)
        self.assertIn("STDIN " + refs, result.stdout)

    def move_record(self, relative):
        """Moves the recorded tooling directory to <relative>/sub0perf_tooling_dir.txt (a preset's build directory, say)."""
        old = self.repo / "build" / "sub0perf_tooling_dir.txt"
        new = self.repo / relative / "sub0perf_tooling_dir.txt"
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(old.read_text())
        old.unlink()
        return new

    def refs(self):
        return f"refs/heads/main {self.sha()} refs/heads/main {self.base}\n"

    def test_the_stanza_finds_the_record_of_a_preset_build_directory(self):
        self.stanza_hook(0)
        self.commit("b.cpp", "int f() { return 1; }\n")
        self.move_record("build/linux-release")
        result = self.push(self.refs())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GATE output=brief args=gate --refs-from-stdin", result.stdout)

    def test_the_stanza_finds_a_record_in_a_build_prefixed_directory(self):
        self.stanza_hook(0)
        self.commit("b.cpp", "int f() { return 1; }\n")
        self.move_record("build-local-quick")
        self.assertIn("GATE output=brief", self.push(self.refs()).stdout)

    def test_the_stanza_uses_the_newest_record(self):
        self.stanza_hook(0)
        self.commit("b.cpp", "int f() { return 1; }\n")
        stale = Path(self.tmp.name) / "stale-tooling"
        stale.mkdir()
        (stale / "bench_check.py").write_text("print('STALE')\n", encoding="utf-8")
        old = self.repo / "build" / "old" / "sub0perf_tooling_dir.txt"
        old.parent.mkdir(parents=True)
        old.write_text(stale.as_posix())
        os.utime(old, (1, 1))  # long ago: the record written at the start of this test is newer
        result = self.push(self.refs())
        self.assertIn("GATE output=brief", result.stdout)
        self.assertNotIn("STALE", result.stdout)

    def test_the_environment_variable_names_the_tooling(self):
        self.stanza_hook(0)
        self.commit("b.cpp", "int f() { return 1; }\n")
        (self.repo / "build" / "sub0perf_tooling_dir.txt").unlink()
        other = Path(self.tmp.name) / "other-tooling"
        other.mkdir()
        (other / "bench_check.py").write_text("print('GATE from env')\nimport sys; sys.stdin.read()\n", encoding="utf-8")
        argv = hc.hook_command(self.hook) + ["origin", "url"]
        env = {**os.environ, "SUB0PERF_TOOLING_DIR": other.as_posix()}
        done = subprocess.run(argv, input=self.refs().encode(), cwd=self.repo, capture_output=True, timeout=60, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("GATE from env", done.stdout.decode(errors="replace"))

    def test_no_recorded_tooling_skips_the_gate_with_a_message_and_does_not_block(self):
        self.stanza_hook(1)
        self.commit("b.cpp", "int f() { return 1; }\n")
        (self.repo / "build" / "sub0perf_tooling_dir.txt").unlink()
        result = self.push(self.refs())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("perf gate skipped", result.stdout)

    def test_a_failing_gate_blocks_the_push(self):
        self.stanza_hook(1)
        self.commit("b.cpp", "int f() { return 1; }\n")
        result = self.push(f"refs/heads/main {self.sha()} refs/heads/main {self.base}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PERFORMANCE GATE FAILED", result.stdout + result.stderr)

    def test_the_gate_does_not_run_when_there_is_nothing_to_check(self):
        self.stanza_hook(1)
        self.assertEqual(self.push("").returncode, 0)


# A stand-in for the shared perf gate's `bench_check.py power`: the power state is faked through the environment, as the
# real one does (SUB0PERF_FAKE_POWER), so no test depends on the machine's actual power state.
FAKE_BENCH_CHECK = """import os, sys
state = os.environ.get("SUB0PERF_FAKE_POWER", "ac")
print("sub0perf: power: " + state)
sys.exit(3 if state in ("battery", "saver") else 0)
"""
OLD_BENCH_CHECK = "import sys\nprint('usage: bench_check.py {run,check}')\nsys.exit(2)\n"
BENCH_HOOK = "#!/bin/sh\ncat >/dev/null\npython tools/perf/bench_check.py gate --refs-from-stdin\n"


class PowerGuardTests(RepoCase):
    def add_bench_check(self, text):
        path = self.repo / "tools" / "perf" / "bench_check.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def guard(self, fake=None):
        with mock.patch.dict(os.environ):
            os.environ.pop("SUB0PERF_FAKE_POWER", None)
            if fake:
                os.environ["SUB0PERF_FAKE_POWER"] = fake
            return hc.check_power_guard(self.repo, self.write_hook(BENCH_HOOK))[0]

    def test_a_hook_without_bench_check_is_skipped(self):
        row = hc.check_power_guard(self.repo, self.write_hook(EMPTY_OK))[0]
        self.assertEqual(row.status, hc.SKIP)

    def test_mains_and_battery_both_pass(self):
        self.add_bench_check(FAKE_BENCH_CHECK)
        for fake, code in (("ac", 0), ("unknown", 0), ("battery", 3), ("saver", 3)):
            row = self.guard(fake)
            self.assertEqual(row.status, hc.PASS, fake)
            self.assertIn(f"exits {code}", row.detail)

    def test_a_gate_without_the_guard_fails(self):
        self.add_bench_check(OLD_BENCH_CHECK)
        row = self.guard("battery")
        self.assertEqual(row.status, hc.FAIL)
        self.assertIn("lacks the power-state guard", row.detail)

    def test_an_unlocatable_bench_check_warns(self):
        self.assertEqual(self.guard().status, hc.WARN)

    def test_run_checks_includes_the_row(self):
        self.add_bench_check(FAKE_BENCH_CHECK)
        path = self.write_hook(BENCH_HOOK)
        self.need_shell(path)
        with mock.patch.dict(os.environ, {"SUB0PERF_FAKE_POWER": "battery"}):
            self.assertEqual(self.rows()["power guard"].status, hc.PASS)


class PowerSwitchTests(unittest.TestCase):
    def test_nothing_set_is_quiet(self):
        self.assertEqual(hc.check_power_switches({}), [])

    def test_each_switch_warns(self):
        env = {"SUB0PERF_FAKE_POWER": "battery", "SUB0PERF_ON_BATTERY": "1", "SUB0PERF_POWER_STRICT": "1"}
        rows = hc.check_power_switches(env)
        self.assertEqual([r.status for r in rows], [hc.WARN] * 3)
        self.assertIn("faked", rows[0].detail)
        self.assertIn("override", rows[1].detail)
        self.assertIn("refused", rows[2].detail)

    def test_the_empty_push_runs_without_the_switches(self):
        env = hc.clean_environment({"SUB0PERF_FAKE_POWER": "battery", "SUB0PERF_ON_BATTERY": "1", "PATH": "x"})
        self.assertEqual(env, {"PATH": "x"})



# The first lines of a hook sub0perf's hooks.py writes (`hook_body`): a shebang, then `# sub0perf hook: <comment>`.
GENERATED_HOOK = """#!/bin/sh
# sub0perf hook: performance regression gate (tools/perf/bench_check.py gate)
python='/usr/bin/python3'
script='/deps/Sub0Math/tools/perf/bench_check.py'
previous="$(dirname "$0")/pre-push.user"
"""

PERF_STANZA_HOOK = """#!/usr/bin/env bash
set -euo pipefail
ctest --test-dir build
tooling="$(cat build/sub0perf_tooling_dir.txt)"
SUB0PERF_OUTPUT=brief python "$tooling/bench_check.py" gate --refs-from-stdin
"""


class TrackedHooksTests(RepoCase):
    def track(self, name, text, directory=".githooks"):
        path = self.repo / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        git(self.repo, "add", f"{directory}/{name}")
        return path

    def test_hooks_directory_outside_the_worktree_is_skipped(self):
        row = hc.check_tracked_hooks(self.repo)[0]
        self.assertEqual(row.status, hc.SKIP)

    def test_a_hand_written_committed_gate_passes(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.track("pre-push", GOOD_TEMPLATE)
        self.assertEqual(hc.check_tracked_hooks(self.repo)[0].status, hc.PASS)

    def test_a_tracked_pre_push_user_fails(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.track("pre-push", GOOD_TEMPLATE)
        self.track("pre-push.user", EMPTY_OK)
        row = hc.check_tracked_hooks(self.repo)[0]
        self.assertEqual(row.status, hc.FAIL)
        self.assertIn(".githooks/pre-push.user", row.detail)

    def test_a_tracked_generated_sub0perf_hook_fails(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.track("pre-push", GENERATED_HOOK)
        row = hc.check_tracked_hooks(self.repo)[0]
        self.assertEqual(row.status, hc.FAIL)
        self.assertIn("generated by sub0perf", row.detail)

    def test_the_marker_is_what_sub0perf_really_writes(self):
        """The text below is `hooks.hook_body` of Sub0Math's sub0perf (its HOOK_MARK line); the guess `generated by sub0perf` is not in it."""
        self.assertNotIn("generated", GENERATED_HOOK.lower())
        self.assertIsNotNone(hc.GENERATED_RE.search(GENERATED_HOOK))
        self.assertIsNone(hc.GENERATED_RE.search(GOOD_TEMPLATE))
        self.assertIsNone(hc.GENERATED_RE.search("#!/bin/sh\n# runs the sub0perf gate; sub0perf hook is mentioned mid-line\n"))

    def test_a_hand_written_gate_that_mentions_sub0perf_is_not_generated(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.track("pre-push", PERF_STANZA_HOOK + "# the sub0perf gate; generated reports are in build/perf\n")
        self.assertEqual(hc.check_tracked_hooks(self.repo)[0].status, hc.PASS)

    def test_an_untracked_local_hook_is_fine(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.track("pre-push", GOOD_TEMPLATE)
        (self.repo / ".githooks" / "pre-push.user").write_text(EMPTY_OK)  # present, not added
        self.assertEqual(hc.check_tracked_hooks(self.repo)[0].status, hc.PASS)

    def test_run_checks_includes_the_row(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.track("pre-push.user", EMPTY_OK)
        self.assertEqual(self.rows()["tracked hooks"].status, hc.FAIL)


class PerfStanzaGuardTests(RepoCase):
    def test_a_committed_hook_calling_bench_check_gets_the_power_guard_row(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        path = self.repo / ".githooks" / "pre-push"
        path.parent.mkdir()
        path.write_text(PERF_STANZA_HOOK, encoding="utf-8", newline="\n")
        git(self.repo, "add", ".githooks/pre-push")
        row = hc.check_power_guard(self.repo, path)[0]
        self.assertEqual(row.status, hc.WARN)  # tooling dir not recorded yet: located only at run time

    def test_tooling_recorded_at_configure_time_is_found_and_run(self):
        path = self.repo / ".githooks" / "pre-push"
        path.parent.mkdir()
        path.write_text(PERF_STANZA_HOOK, encoding="utf-8", newline="\n")
        tooling = Path(self.tmp.name) / "tooling"
        tooling.mkdir()
        (tooling / "bench_check.py").write_text(FAKE_BENCH_CHECK, encoding="utf-8")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "sub0perf_tooling_dir.txt").write_text(str(tooling))
        with mock.patch.dict(os.environ):
            os.environ.pop("SUB0PERF_FAKE_POWER", None)
            row = hc.check_power_guard(self.repo, path)[0]
        self.assertEqual(row.status, hc.PASS)

class ToolingSearchTests(RepoCase):
    """hook_check finds the tooling the way the pre-push stanza does: SUB0PERF_TOOLING_DIR, then the newest record under build*/ and build/*/."""

    def record(self, relative, tooling, age=None):
        path = self.repo / relative / hc.TOOLING_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(tooling), encoding="utf-8")
        if age is not None:
            os.utime(path, (age, age))
        return path

    def test_every_documented_location_is_searched_newest_first(self):
        self.record("build", "/t/plain", age=100)
        self.record("build-ci", "/t/prefixed", age=300)
        self.record("build/linux-release", "/t/preset", age=200)
        self.assertEqual([p.as_posix() for p in hc.tooling_dirs(self.repo, {})], ["/t/prefixed", "/t/preset", "/t/plain"])

    def test_the_environment_variable_comes_first(self):
        self.record("build/p", "/t/preset")
        found = hc.tooling_dirs(self.repo, {hc.TOOLING_ENV: " /t/env "})
        self.assertEqual([p.as_posix() for p in found], ["/t/env", "/t/preset"])

    def test_an_unrelated_directory_is_not_searched(self):
        self.record("out/x", "/t/other")
        self.record("tools/build/y", "/t/deeper")
        self.assertEqual(hc.tooling_dirs(self.repo, {}), [])

    def test_the_power_guard_finds_bench_check_through_a_preset_build_directory(self):
        path = self.repo / ".githooks" / "pre-push"
        path.parent.mkdir()
        path.write_text(PERF_STANZA_HOOK, encoding="utf-8", newline="\n")
        tooling = Path(self.tmp.name) / "tooling"
        tooling.mkdir()
        (tooling / "bench_check.py").write_text(FAKE_BENCH_CHECK, encoding="utf-8")
        self.record("build/linux-release", tooling.as_posix())
        with mock.patch.dict(os.environ):
            os.environ.pop("SUB0PERF_FAKE_POWER", None)
            row = hc.check_power_guard(self.repo, path)[0]
        self.assertEqual(row.status, hc.PASS)

    def test_the_power_guard_honours_the_environment_variable(self):
        path = self.repo / ".githooks" / "pre-push"
        path.parent.mkdir()
        path.write_text(PERF_STANZA_HOOK, encoding="utf-8", newline="\n")
        tooling = Path(self.tmp.name) / "tooling"
        tooling.mkdir()
        (tooling / "bench_check.py").write_text(FAKE_BENCH_CHECK, encoding="utf-8")
        with mock.patch.dict(os.environ, {hc.TOOLING_ENV: str(tooling)}):
            os.environ.pop("SUB0PERF_FAKE_POWER", None)
            row = hc.check_power_guard(self.repo, path)[0]
        self.assertEqual(row.status, hc.PASS)


class CommandLineTests(RepoCase):
    def run_main(self, *argv):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = hc.main(list(argv))
        return code, out.getvalue()

    def test_exit_code_follows_failures(self):
        code, out = self.run_main("--repo", str(self.repo), "--no-selftest")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  active hook", out)
        path = self.write_hook(EMPTY_OK)
        self.need_shell(path)
        self.assertEqual(self.run_main("--repo", str(self.repo), "--no-selftest")[0], 0)

    def test_not_a_repository_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_main("--repo", d)[0], 2)


if __name__ == "__main__":
    unittest.main()
