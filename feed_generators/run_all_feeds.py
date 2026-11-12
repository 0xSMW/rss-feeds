import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _run_script(script_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, script_path], capture_output=True, text=True)


def run_all_feeds() -> int:
    """Run source generators, then the meta-feed; return nonzero on any failure."""
    feed_generators_dir = os.path.dirname(os.path.abspath(__file__))

    # Exclude meta feed from main loop - it will run at the end
    excluded_scripts = {os.path.basename(__file__), "ai_research_meta_feed.py"}

    script_paths: list[str] = [
        os.path.join(feed_generators_dir, filename)
        for filename in sorted(os.listdir(feed_generators_dir))
        if filename.endswith(".py")
        and not filename.startswith("_")
        and filename not in excluded_scripts
    ]

    failed = False
    futures = {}
    # Each generator may spawn a headless Chrome and its own worker threads,
    # so keep the number of concurrent generators small.
    with ThreadPoolExecutor(max_workers=4) as executor:
        for script_path in script_paths:
            logger.info(f"Running script: {script_path}")
            futures[executor.submit(_run_script, script_path)] = script_path

        for future in as_completed(futures):
            script_path = futures[future]
            try:
                result = future.result()
            except Exception:
                failed = True
                logger.exception("Exception running script: %s", script_path)
                continue
            if result.returncode == 0:
                logger.info(f"Successfully ran script: {script_path}")
            else:
                failed = True
                logger.error(
                    "Error running script: %s (exit code %s)\nstdout:\n%s\nstderr:\n%s",
                    script_path,
                    result.returncode,
                    result.stdout,
                    result.stderr,
                )

    if failed:
        logger.error("Skipping meta-feed because source generators failed")
        return 1

    meta_feed_path = os.path.join(feed_generators_dir, "ai_research_meta_feed.py")
    if os.path.exists(meta_feed_path):
        logger.info(f"Running script: {meta_feed_path}")
        try:
            result = _run_script(meta_feed_path)
        except Exception:
            logger.exception("Exception running script: %s", meta_feed_path)
            return 1
        if result.returncode == 0:
            logger.info(f"Successfully ran script: {meta_feed_path}")
        else:
            logger.error(
                "Error running script: %s (exit code %s)\nstdout:\n%s\nstderr:\n%s",
                meta_feed_path,
                result.returncode,
                result.stdout,
                result.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all_feeds())
