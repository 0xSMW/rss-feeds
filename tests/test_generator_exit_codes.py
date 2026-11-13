"""Check real CLI footers without running generator network operations."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

GENERATORS = Path(__file__).resolve().parents[1] / "feed_generators"

# Trace only the actual module guard, replacing the main function at that boundary.
# runpy still executes the script's imports, argparse, and original footer itself.
ENTRYPOINT_PROBE = """
import ast
from pathlib import Path
import runpy
import sys

script = Path(sys.argv[1]).resolve()
expected = sys.argv[2] == "true"
tree = ast.parse(script.read_text())
guard = next(node.lineno for node in tree.body if isinstance(node, ast.If)
             and isinstance(node.test, ast.Compare)
             and isinstance(node.test.left, ast.Name)
             and node.test.left.id == "__name__")
called = []
def stub(*args, **kwargs):
    called.append(True)
    return expected

def trace(frame, event, arg):
    if frame.f_code.co_filename != str(script):
        return None
    if event == "line" and frame.f_code.co_filename == str(script) and frame.f_lineno == guard:
        frame.f_globals["main"] = stub
        sys.settrace(None)
        return None
    return trace

sys.path.insert(0, str(script.parent))
sys.argv = [str(script)]
sys.settrace(trace)
try:
    runpy.run_path(str(script), run_name="__main__")
finally:
    sys.settrace(None)
    assert called, "Entrypoint did not call main"
"""


class GeneratorExitCodeTests(unittest.TestCase):
    def test_all_generator_entrypoints_propagate_main_result(self):
        scripts = sorted(
            path
            for path in GENERATORS.glob("*.py")
            if not path.name.startswith("_") and path.name != "run_all_feeds.py"
        )
        self.assertTrue(scripts)
        with tempfile.TemporaryDirectory() as directory:
            for script in scripts:
                for result in (True, False):
                    with self.subTest(script=script.name, main_result=result):
                        completed = subprocess.run(
                            [
                                sys.executable,
                                "-c",
                                ENTRYPOINT_PROBE,
                                str(script),
                                str(result).lower(),
                            ],
                            cwd=directory,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        self.assertEqual(
                            completed.returncode, 0 if result else 1, completed.stderr
                        )

    def test_xai_main_returns_false_when_fetch_raises(self):
        sys.path.insert(0, str(GENERATORS))
        self.addCleanup(sys.path.remove, str(GENERATORS))
        import xai_news

        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with (
                    patch.object(
                        xai_news, "get_project_root", return_value=Path(directory)
                    ),
                    patch.object(sys, "argv", ["xai_news.py"]),
                    patch.object(
                        xai_news,
                        "fetch_news_content",
                        side_effect=RuntimeError("fetch failed"),
                    ),
                ):
                    self.assertIs(xai_news.main(), False)
            finally:
                os.chdir(previous)

    def test_xai_main_returns_true_after_writing_feed(self):
        sys.path.insert(0, str(GENERATORS))
        self.addCleanup(sys.path.remove, str(GENERATORS))
        import xai_news

        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with (
                    patch.object(
                        xai_news, "get_project_root", return_value=Path(directory)
                    ),
                    patch.object(sys, "argv", ["xai_news.py"]),
                    patch.object(
                        xai_news, "fetch_news_content", return_value="<html></html>"
                    ),
                ):
                    self.assertIs(xai_news.main(), True)
                self.assertTrue(Path("feeds/feed_xai_news.xml").is_file())
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
