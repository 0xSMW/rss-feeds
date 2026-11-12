"""Exercise the runner entry point against isolated, local generator scripts."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "feed_generators" / "run_all_feeds.py"


class RunAllFeedsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)
        self.runner = self.directory / RUNNER.name
        shutil.copyfile(RUNNER, self.runner)

    def script(self, name, source):
        (self.directory / name).write_text(source, encoding="utf-8")

    def run_runner(self, *, launch_error_for=None, empty_path=False):
        env = os.environ.copy()
        bin_dir = self.directory / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").symlink_to(sys.executable)
        env["PATH"] = "" if empty_path else str(bin_dir)
        if launch_error_for:
            # Inject a launch-boundary failure while keeping all other child calls real.
            hooks_dir = self.directory / "hooks"
            hooks_dir.mkdir()
            (hooks_dir / "sitecustomize.py").write_text(
                "import subprocess\n"
                "original_run = subprocess.run\n"
                "def run(args, *positional, **kwargs):\n"
                f"    if args[-1].endswith({launch_error_for!r}):\n"
                "        raise OSError('simulated launch failure')\n"
                "    return original_run(args, *positional, **kwargs)\n"
                "subprocess.run = run\n",
            )
            env["PYTHONPATH"] = str(hooks_dir)
        return subprocess.run(
            [sys.executable, str(self.runner)],
            cwd=self.directory,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def marker_script(self, marker):
        return f"from pathlib import Path\nPath({marker!r}).touch()\n"

    def test_success_uses_current_interpreter_and_runs_meta_last(self):
        self.script("source.py", self.marker_script("source-ran"))
        self.script(
            "ai_research_meta_feed.py",
            "from pathlib import Path\n"
            "assert Path('source-ran').exists()\n"
            "Path('meta-ran').touch()\n",
        )
        result = self.run_runner(empty_path=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.directory / "meta-ran").exists(), result.stderr)

    def test_child_failure_is_nonzero_continues_sources_and_skips_meta(self):
        self.script(
            "a_failure.py",
            "import sys\nprint('source detail', file=sys.stderr)\nsys.exit(7)\n",
        )
        self.script("b_failure.py", "import sys\nprint('second detail')\nsys.exit(8)\n")
        self.script("z_success.py", self.marker_script("other-ran"))
        self.script("ai_research_meta_feed.py", self.marker_script("meta-ran"))
        result = self.run_runner()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("source detail", result.stderr)
        self.assertIn("exit code 7", result.stderr)
        self.assertIn("second detail", result.stderr)
        self.assertIn("exit code 8", result.stderr)
        self.assertTrue((self.directory / "other-ran").exists(), result.stderr)
        self.assertFalse((self.directory / "meta-ran").exists(), result.stderr)

    def test_launch_exception_is_reported_continues_sources_and_skips_meta(self):
        self.script("a_failure.py", "pass\n")
        self.script("z_success.py", self.marker_script("other-ran"))
        self.script("ai_research_meta_feed.py", self.marker_script("meta-ran"))
        result = self.run_runner(launch_error_for="a_failure.py")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("simulated launch failure", result.stderr)
        self.assertIn(
            "Successfully ran script: " + str(self.directory / "z_success.py"),
            result.stderr,
        )
        self.assertTrue((self.directory / "other-ran").exists(), result.stderr)
        self.assertFalse((self.directory / "meta-ran").exists(), result.stderr)

    def test_meta_failure_is_nonzero_and_preserves_diagnostics(self):
        self.script("source.py", "pass\n")
        self.script(
            "ai_research_meta_feed.py",
            "import sys\nprint('meta detail', file=sys.stderr)\nsys.exit(9)\n",
        )
        result = self.run_runner()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("meta detail", result.stderr)
        self.assertIn("exit code 9", result.stderr)

    def test_meta_launch_exception_is_nonzero_and_reported(self):
        self.script("source.py", "pass\n")
        self.script("ai_research_meta_feed.py", "pass\n")
        result = self.run_runner(launch_error_for="ai_research_meta_feed.py")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("simulated launch failure", result.stderr)
        self.assertIn("ai_research_meta_feed.py", result.stderr)

    def test_success_without_meta(self):
        self.script("source.py", self.marker_script("source-ran"))
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.directory / "source-ran").exists(), result.stderr)


if __name__ == "__main__":
    unittest.main()
