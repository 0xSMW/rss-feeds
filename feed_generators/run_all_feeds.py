import os
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def _run_script(script_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["python", script_path], capture_output=True, text=True)


def run_all_feeds():
    """Run all Python scripts in the feed_generators directory (excluding meta feeds)."""
    feed_generators_dir = os.path.dirname(os.path.abspath(__file__))

    # Exclude meta feed from main loop - it will run at the end
    excluded_scripts = {os.path.basename(__file__), "ai_research_meta_feed.py"}

    script_paths: list[str] = [
        os.path.join(feed_generators_dir, filename)
        for filename in sorted(os.listdir(feed_generators_dir))
        if filename.endswith(".py") and filename not in excluded_scripts
    ]

    futures = {}
    with ThreadPoolExecutor() as executor:
        for script_path in script_paths:
            logger.info(f"Running script: {script_path}")
            futures[executor.submit(_run_script, script_path)] = script_path

        for future in as_completed(futures):
            script_path = futures[future]
            result = future.result()
            if result.returncode == 0:
                logger.info(f"Successfully ran script: {script_path}")
            else:
                logger.error(f"Error running script: {script_path}\n{result.stderr}")

    meta_feed_path = os.path.join(feed_generators_dir, "ai_research_meta_feed.py")
    if os.path.exists(meta_feed_path):
        logger.info(f"Running script: {meta_feed_path}")
        result = _run_script(meta_feed_path)
        if result.returncode == 0:
            logger.info(f"Successfully ran script: {meta_feed_path}")
        else:
            logger.error(f"Error running script: {meta_feed_path}\n{result.stderr}")
    
if __name__ == "__main__":
    run_all_feeds()
