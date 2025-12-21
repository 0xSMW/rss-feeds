import os
import subprocess
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def run_all_feeds():
    """Run all Python scripts in the feed_generators directory, then generate meta feed."""
    feed_generators_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Exclude meta feed from main loop - it will run at the end
    excluded_scripts = {os.path.basename(__file__), "ai_research_meta_feed.py"}
    
    for filename in os.listdir(feed_generators_dir):
        if filename.endswith(".py") and filename not in excluded_scripts:
            script_path = os.path.join(feed_generators_dir, filename)
            logger.info(f"Running script: {script_path}")
            result = subprocess.run(["python", script_path], capture_output=True, text=True)
            if result.returncode == 0:
                logger.info(f"Successfully ran script: {script_path}")
            else:
                logger.error(f"Error running script: {script_path}\n{result.stderr}")
    
    # After all individual feeds are generated, create the meta feed
    logger.info("All individual feeds generated. Now generating AI Research meta feed...")
    meta_feed_path = os.path.join(feed_generators_dir, "ai_research_meta_feed.py")
    if os.path.exists(meta_feed_path):
        result = subprocess.run(["python", meta_feed_path], capture_output=True, text=True)
        if result.returncode == 0:
            logger.info("Successfully generated AI Research meta feed")
        else:
            logger.error(f"Error generating AI Research meta feed:\n{result.stderr}")
    else:
        logger.warning(f"Meta feed script not found: {meta_feed_path}")

if __name__ == "__main__":
    run_all_feeds()
