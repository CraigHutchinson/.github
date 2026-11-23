"""Unit tests for local_ci.py: python -m unittest discover -s tools/ci -v"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_ci as lc  # noqa: E402

CONFIG = {
    "version": 1,
    "levels": {
        "quick": {"steps": [
            {"name": "gcc", "compiler": "gcc", "commands": [["cmake", "-B", "{builddir}"], ["ctest", "--test-dir", "{builddir}"]]},
            {"name": "clang", "compiler": "clang", "optional": True, "commands": [["true"]]},
            {"name": "py", "commands": [["{python}", "-c", "pass"]]},
        ]},
        "full": {"steps": []},
    },
}


def fake_detect(found):
    def detect(kind):
        return found.get(kind)
    return detect


GCC = lc.Compiler("gcc", "gcc", "g++", "GCC 16")


class ParseTests(unittest.TestCase):
    def test_ctest_summary(self):
        self.assertEqual(lc.parse_ctest("100% tests passed, 0 tests failed out of 3"), (3, 3))
        self.assertEqual(lc.parse_ctest("67% tests passed, 1 tests failed out of 3"), (2, 3))
        self.assertIsNone(lc.parse_ctest("nothing"))

    def test_remote_urls(self):
        self.assertEqual(lc.parse_remote("https://github.com/ExampleOrg/PrivateThing.git"), ("ExampleOrg", "PrivateThing"))
        self.assertEqual(lc.parse_remote("git@github.com:CraigHutchinson/Sub0Math.git"), ("CraigHutchinson", "Sub0Math"))
        with self.assertRaises(lc.CiError):
            lc.parse_remote("https://example.com/x/y")

    def test_expand(self):
        self.assertEqual(lc.expand("cmake -B {builddir} -j{jobs}", {"builddir": "b", "jobs": 4}), ["cmake", "-B", "b", "-j4"])
        self.assertEqual(lc.expand(["a", "{x}"], {"x": "y"}), ["a", "y"])

    def test_ci_dir_placeholder_points_at_the_tools(self):
        step = lc.Step("s", [["{ci_dir}/hook_check.py"]])
        printed = []
        lc.run_step(step, "quick", Path("."), {}, runner=lambda argv, cwd, env: printed.append(argv) or (0, ""))
        self.assertEqual(Path(printed[0][0]), Path(lc.__file__).resolve().parent / "hook_check.py")

    def test_major_version(self):
        self.assertEqual(lc._major("g++ (GCC) 16.1.0\nCopyright"), "16")
        self.assertEqual(lc._major("garbage"), "?")


class ConfigTests(unittest.TestCase):
    def test_load_and_steps(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".ci").mkdir()
            (Path(d) / ".ci" / "local.json").write_text(json.dumps(CONFIG))
            config = lc.load_config(Path(d))
            self.assertEqual([s.name for s in lc.steps_for(config, "quick")], ["gcc", "clang", "py"])
            with self.assertRaises(lc.CiError):
                lc.steps_for(config, "full")  # declares no steps
            with self.assertRaises(lc.CiError):
                lc.steps_for(config, "nightly")

    def test_missing_config(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(lc.CiError):
                lc.load_config(Path(d))

    def test_bad_compiler(self):
        with self.assertRaises(lc.CiError):
            lc.steps_for({"levels": {"quick": {"steps": [{"name": "x", "compiler": "icc", "commands": []}]}}}, "quick")


class RunTests(unittest.TestCase):
    def runner(self, ctest_output="100% tests passed, 0 tests failed out of 5", fail_on=None):
        calls = []

        def run(argv, cwd, env):
            calls.append((argv, env.get("CC")))
            if fail_on and argv[0] == fail_on:
                return 1, "boom"
            return 0, ctest_output if argv[0] == "ctest" else ""
        run.calls = calls
        return run

    def test_all_pass_and_skip_optional(self):
        run = self.runner()
        results = lc.run_level(CONFIG, "quick", Path("."), detect=fake_detect({"gcc": GCC}), runner=run)
        self.assertEqual([r.status for r in results], ["passed", "skipped", "passed"])
        self.assertEqual(results[0].ctest, (5, 5))
        self.assertEqual(results[0].compiler, "GCC 16")
        self.assertTrue(lc.overall_ok(results))
        self.assertEqual(run.calls[0][1], "gcc")  # CC exported
        self.assertEqual(run.calls[0][0][2], "build-local-quick-gcc")

    def test_missing_required_compiler_fails(self):
        results = lc.run_level(CONFIG, "quick", Path("."), detect=fake_detect({}), runner=self.runner())
        self.assertEqual(results[0].status, "failed")
        self.assertFalse(lc.overall_ok(results))

    def test_command_failure_stops_step(self):
        run = self.runner(fail_on="cmake")
        results = lc.run_level(CONFIG, "quick", Path("."), detect=fake_detect({"gcc": GCC}), runner=run)
        self.assertEqual(results[0].status, "failed")
        self.assertFalse(any(a[0] == "ctest" for a in run.calls))
        self.assertFalse(lc.overall_ok(results))

    def test_only_filter(self):
        results = lc.run_level(CONFIG, "quick", Path("."), only=["py"], detect=fake_detect({}), runner=self.runner())
        self.assertEqual([r.name for r in results], ["py"])
        with self.assertRaises(lc.CiError):
            lc.run_level(CONFIG, "quick", Path("."), only=["nope"], detect=fake_detect({}), runner=self.runner())

    def test_requires_env_skips(self):
        cfg = {"levels": {"quick": {"steps": [{"name": "opt", "requires_env": "LOCAL_CI_TEST_UNSET_VAR", "commands": [["false"]]},
                                              {"name": "py", "commands": [["x"]]}]}}}
        results = lc.run_level(cfg, "quick", Path("."), detect=fake_detect({}), runner=self.runner())
        self.assertEqual([r.status for r in results], ["skipped", "passed"])

    def test_nothing_passed_is_not_ok(self):
        self.assertFalse(lc.overall_ok([lc.StepResult("a", "skipped")]))


class StatusTests(unittest.TestCase):
    def test_description(self):
        rs = [lc.StepResult("gcc", "passed", "GCC 16", (3, 3)), lc.StepResult("clang", "skipped"),
              lc.StepResult("py", "passed")]
        self.assertEqual(lc.describe("full", rs), "GCC 16 gcc: ctest full 3/3; py: ok")

    def test_description_truncated(self):
        rs = [lc.StepResult(f"step-{i}-" + "x" * 20, "passed", "GCC 16", (1, 1)) for i in range(10)]
        d = lc.describe("quick", rs)
        self.assertLessEqual(len(d), lc.DESCRIPTION_LIMIT)
        self.assertTrue(d.endswith("..."))

    def test_post_and_remote_check_use_gh(self):
        calls = []

        def gh(args):
            calls.append(args)
            return 0
        self.assertTrue(lc.commit_on_remote("o", "r", "abc", gh))
        self.assertTrue(lc.post_status("o", "r", "abc", "full", "desc", gh=gh))
        self.assertEqual(calls[0][1], "repos/o/r/commits/abc")
        self.assertEqual(calls[1][1], "repos/o/r/statuses/abc")
        self.assertIn("context=local-ci/full", calls[1])
        self.assertIn("state=success", calls[1])

    def test_missing_commit_not_on_remote(self):
        self.assertFalse(lc.commit_on_remote("o", "r", "abc", lambda a: 1))

    def test_last_run_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            rs = [lc.StepResult("gcc", "passed", "GCC 16", (3, 3))]
            lc.save_last_run(Path(d), "quick", "deadbeef", rs, "desc")
            last = lc.load_last_run(Path(d), "quick")
            self.assertTrue(last["ok"])
            self.assertEqual(last["sha"], "deadbeef")
            with self.assertRaises(lc.CiError):
                lc.load_last_run(Path(d), "full")


class PowerTests(unittest.TestCase):
    BATTERY = lc.ci_power.PowerState("battery", False, "fake")
    SAVER = lc.ci_power.PowerState("ac", True, "fake")
    AC = lc.ci_power.PowerState("ac", False, "fake")

    def test_fake_states(self):
        def state(value):
            return lc.ci_power.power_state(environ={"SUB0PERF_FAKE_POWER": value})
        self.assertEqual(state("battery").state, "battery")
        self.assertTrue(state("saver").untrusted)
        self.assertFalse(state("ac").untrusted)
        self.assertEqual(state("unknown").state, "unknown")
        self.assertFalse(state("unknown").untrusted)  # unknown counts as mains, but is reported

    def test_the_power_mode_is_reported_by_name(self):
        state = lc.ci_power.PowerState("ac", False, "windows", "Best power efficiency", "Balanced")
        self.assertEqual(state.describe(), "ac (windows; power mode Best power efficiency (mains) / Balanced (battery))")
        self.assertEqual(self.AC.describe(), "ac (fake)")  # unknown on other platforms: nothing is said
        self.assertEqual(lc.ci_power._OVERLAYS["961cc777-2547-4f9d-8174-7d86181b8a7a"], "Best power efficiency")
        if sys.platform == "win32":
            ac, dc = lc.ci_power._windows_modes()
            self.assertTrue(ac and dc)

    def test_switches_set(self):
        self.assertEqual(lc.ci_power.switches_set({}), [])
        self.assertEqual(lc.ci_power.switches_set({"SUB0MATH_PERF_ON_BATTERY": "1"}), ["SUB0MATH_PERF_ON_BATTERY=1"])

    def test_exclude_timing(self):
        self.assertEqual(lc.exclude_timing(["ctest", "--test-dir", "b"]), ["ctest", "--test-dir", "b", "-LE", "timing"])
        self.assertEqual(lc.exclude_timing(["/usr/bin/ctest.exe", "-LE", "full"])[-1], "full|timing")
        self.assertEqual(lc.exclude_timing(["ctest", "--label-exclude=full"])[-1], "--label-exclude=full|timing")

    def test_exclude_timing_leaves_other_commands_and_presets_alone(self):
        self.assertIsNone(lc.exclude_timing(["cmake", "--build", "b"]))
        self.assertIsNone(lc.exclude_timing(["ctest", "--preset", "quick"]))  # a CLI filter would replace the preset's own
        self.assertIsNone(lc.exclude_timing(["ctest", "--preset=quick"]))

    def test_battery_run_excludes_timing_from_ctest_only(self):
        run = RunTests().runner()
        results = lc.run_level(CONFIG, "quick", Path("."), detect=fake_detect({"gcc": GCC}), runner=run, untimed=True)
        ctest = [a for a, _ in run.calls if a[0] == "ctest"][0]
        self.assertEqual(ctest[-2:], ["-LE", "timing"])
        self.assertNotIn("-LE", [a for a, _ in run.calls if a[0] == "cmake"][0])
        self.assertIn("-LE timing", results[0].note)

    def test_mains_run_is_unchanged(self):
        run = RunTests().runner()
        lc.run_level(CONFIG, "quick", Path("."), detect=fake_detect({"gcc": GCC}), runner=run)
        self.assertNotIn("-LE", [a for a, _ in run.calls if a[0] == "ctest"][0])

    def test_mark(self):
        self.assertEqual(lc.power_mark(self.AC), "")
        self.assertEqual(lc.power_mark(self.BATTERY, {}), "untimed (on battery)")
        self.assertEqual(lc.power_mark(self.SAVER, {}), "untimed (on battery)")
        self.assertEqual(lc.power_mark(self.BATTERY, {"SUB0MATH_PERF_ON_BATTERY": "1"}), "timed on battery (override)")
        self.assertEqual(lc.power_mark(self.AC, {"SUB0MATH_PERF_ON_BATTERY": "1"}), "")  # ignored on mains

    def test_description_carries_the_mark_even_when_truncated(self):
        rs = [lc.StepResult(f"step-{i}-" + "x" * 20, "passed", "GCC 16", (1, 1)) for i in range(10)]
        d = lc.describe("quick", rs, "untimed (on battery)")
        self.assertLessEqual(len(d), lc.DESCRIPTION_LIMIT)
        self.assertTrue(d.endswith("; untimed (on battery)"))
        self.assertEqual(lc.describe("quick", [lc.StepResult("py", "passed")], "untimed (on battery)"),
                         "py: ok; untimed (on battery)")

    def test_record_carries_the_power_state(self):
        with tempfile.TemporaryDirectory() as d:
            lc.save_last_run(Path(d), "quick", "abc", [lc.StepResult("py", "passed")], "desc",
                             {**self.BATTERY.as_dict(), "mark": "untimed (on battery)"})
            self.assertEqual(lc.load_last_run(Path(d), "quick")["power"]["mark"], "untimed (on battery)")


class TimingStepTests(unittest.TestCase):
    CFG = {"levels": {"full": {"steps": [
        {"name": "py", "commands": [["x"]]},
        {"name": "perf", "timing": True, "commands": [["bench"]]}]}}}

    def run_full(self, untimed):
        calls = []

        def run(argv, cwd, env):
            calls.append(argv)
            return 0, ""
        return lc.run_level(self.CFG, "full", Path("."), detect=fake_detect({}), runner=run, untimed=untimed), calls

    def test_timing_step_skipped_when_untimed(self):
        results, calls = self.run_full(True)
        self.assertEqual([r.status for r in results], ["passed", "skipped"])
        self.assertEqual(results[1].note, "untimed (on battery)")
        self.assertEqual(calls, [["x"]])
        self.assertTrue(lc.overall_ok(results))

    def test_timing_step_runs_on_mains(self):
        results, calls = self.run_full(False)
        self.assertEqual([r.status for r in results], ["passed", "passed"])
        self.assertEqual(calls, [["x"], ["bench"]])

    def test_field_is_parsed(self):
        steps = lc.steps_for(self.CFG, "full")
        self.assertEqual([s.timing for s in steps], [False, True])


class ToolingPlaceholderTests(unittest.TestCase):
    def test_read_from_the_build_directory(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            b = repo / "build-local-quick-s"
            b.mkdir()
            (b / "sub0math_tooling_dir.txt").write_text("/deps/Sub0Math/tools/perf\n")
            step = lc.Step("s", [["{python}", "{sub0math_tooling}/bench_check.py", "gate"]])
            seen = []
            r = lc.run_step(step, "quick", repo, {}, runner=lambda argv, cwd, env: seen.append(argv) or (0, ""))
            self.assertEqual(r.status, "passed")
            self.assertEqual(seen[0][1], "/deps/Sub0Math/tools/perf/bench_check.py")

    def test_missing_file_fails_the_step_with_a_hint(self):
        with tempfile.TemporaryDirectory() as d:
            step = lc.Step("s", [["{sub0math_tooling}/x.py"]])
            seen = []
            r = lc.run_step(step, "quick", Path(d), {}, runner=lambda *a: seen.append(a) or (0, ""))
            self.assertEqual(r.status, "failed")
            self.assertIn("configure the build first", r.note)
            self.assertEqual(seen, [])

    def test_created_by_an_earlier_command_of_the_same_step(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            step = lc.Step("s", [["configure"], ["{sub0math_tooling}/x.py"]])
            seen = []

            def run(argv, cwd, env):
                seen.append(argv)
                if argv == ["configure"]:
                    (repo / "build-local-quick-s").mkdir()
                    (repo / "build-local-quick-s" / "sub0math_tooling_dir.txt").write_text("T")
                return 0, ""
            self.assertEqual(lc.run_step(step, "quick", repo, {}, runner=run).status, "passed")
            self.assertEqual(seen[1], ["T/x.py"])

    def test_dry_run_does_not_need_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            step = lc.Step("s", [["{sub0math_tooling}/x.py"]])
            self.assertEqual(lc.run_step(step, "quick", Path(d), {}, dry_run=True).status, "passed")


class PerfOutcomeTests(unittest.TestCase):
    OUTCOME = {"schema": "sub0perf-outcome-1", "status": "regressed", "exit": 1,
               "counts": {"regressed": 1, "ok": 12, "improved": 0}, "changed": ["bench_a"],
               "report": "build/perf/report.md", "next_action": "run bench_check.py improve bench_a"}

    def write(self, d, data):
        p = Path(d) / "outcome.json"
        p.write_text(json.dumps(data))
        return p

    def test_read(self):
        with tempfile.TemporaryDirectory() as d:
            perf = lc.read_perf_outcome(self.write(d, self.OUTCOME))
            self.assertEqual(perf["status"], "regressed")
            self.assertEqual(perf["changed"], ["bench_a"])
            self.assertEqual(lc.perf_text(perf), "perf regressed (regressed 1, ok 12)")

    def test_absent_or_foreign_schema_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(lc.read_perf_outcome(Path(d) / "nope.json"))
            self.assertIsNone(lc.read_perf_outcome(self.write(d, {"schema": "other-1", "status": "x"})))
            (Path(d) / "bad.json").write_text("{")
            self.assertIsNone(lc.read_perf_outcome(Path(d) / "bad.json"))

    def test_step_folds_the_outcome_into_the_description(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {**self.OUTCOME, "status": "ok", "counts": {"ok": 13}})
            step = lc.Step("perf", [["bench"]], perf_outcome="outcome.json")
            r = lc.run_step(step, "full", Path(d), {}, runner=lambda *a: (0, ""))
            self.assertEqual(r.perf["status"], "ok")
            self.assertEqual(lc.describe("full", [r]), "perf: ok; perf ok (ok 13)")

    def test_missing_outcome_is_noted_not_failed(self):
        with tempfile.TemporaryDirectory() as d:
            step = lc.Step("perf", [["bench"]], perf_outcome="outcome.json")
            r = lc.run_step(step, "full", Path(d), {}, runner=lambda *a: (0, ""))
            self.assertEqual(r.status, "passed")
            self.assertIn("no sub0perf outcome", r.note)


class BriefTests(unittest.TestCase):
    def test_all_green_is_one_line(self):
        rs = [lc.StepResult("gcc", "passed", "GCC 16", (3, 3)), lc.StepResult("clang", "skipped")]
        out = lc.summarise_brief("quick", "abcdef1234567890", rs)
        self.assertEqual(out, "local-ci/quick abcdef123456: OK 1 passed, 1 skipped")

    def test_failures_and_mark(self):
        rs = [lc.StepResult("gcc", "failed", note="exit 1: ctest --preset quick"), lc.StepResult("py", "passed")]
        out = lc.summarise_brief("quick", "abcdef1234567890", rs, "untimed (on battery)").splitlines()
        self.assertEqual(out[0], "local-ci/quick abcdef123456: FAILED 1 passed, 1 failed [untimed (on battery)]")
        self.assertEqual(out[1], "  FAILED gcc: exit 1: ctest --preset quick")
        self.assertEqual(len(out), 2)

    def test_perf_outcome_shows_report_and_next_action(self):
        rs = [lc.StepResult("perf", "failed", note="exit 1: bench", perf={
            "status": "regressed", "exit": 1, "counts": {"regressed": 1}, "changed": ["a"],
            "report": "r.md", "next_action": "improve a"})]
        out = lc.summarise_brief("full", "abcdef1234567890", rs)
        self.assertIn("report: r.md", out)
        self.assertIn("next: improve a", out)
        self.assertIn("changed: a", out)

    def test_quiet_perf_is_not_expanded(self):
        rs = [lc.StepResult("perf", "passed", perf={
            "status": "ok", "exit": 0, "counts": {}, "changed": [], "report": "r.md", "next_action": ""})]
        self.assertEqual(len(lc.summarise_brief("full", "a" * 12, rs).splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
