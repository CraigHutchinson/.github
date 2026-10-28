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
        self.assertEqual(lc.parse_remote("https://github.com/KismoTech/BikeDynamics.git"), ("KismoTech", "BikeDynamics"))
        self.assertEqual(lc.parse_remote("git@github.com:CraigHutchinson/Sub0Math.git"), ("CraigHutchinson", "Sub0Math"))
        with self.assertRaises(lc.CiError):
            lc.parse_remote("https://example.com/x/y")

    def test_expand(self):
        self.assertEqual(lc.expand("cmake -B {builddir} -j{jobs}", {"builddir": "b", "jobs": 4}), ["cmake", "-B", "b", "-j4"])
        self.assertEqual(lc.expand(["a", "{x}"], {"x": "y"}), ["a", "y"])

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


if __name__ == "__main__":
    unittest.main()
