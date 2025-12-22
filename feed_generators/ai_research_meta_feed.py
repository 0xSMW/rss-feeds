import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from feedgen.feed import FeedGenerator
import logging
from pathlib import Path

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def _parse_pub_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed_xml(feed_path: Path) -> list[dict]:
    """Parse an RSS feed XML file and extract all items."""
    try:
        tree = ET.parse(feed_path)
        root = tree.getroot()

        channel = root.find("channel")
        if channel is None:
            logger.warning(f"No channel found in {feed_path.name}")
            return []

        items = []
        namespaces = {"content": "http://purl.org/rss/1.0/modules/content/"}

        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue

            description = (item.findtext("description") or "").strip() or title
            content_html = item.findtext("content:encoded", default=None, namespaces=namespaces)

            date = _parse_pub_date((item.findtext("pubDate") or "").strip())
            category = (item.findtext("category") or "").strip() or "Research"
            guid = (item.findtext("guid") or "").strip() or link
            author = (item.findtext("author") or "").strip()

            item_data = {
                "title": title,
                "link": link,
                "description": description,
                "date": date,
                "category": category,
                "guid": guid,
            }
            if content_html:
                item_data["content_html"] = content_html
            if author:
                item_data["author"] = author

            items.append(item_data)

        logger.info(f"Parsed {len(items)} items from {feed_path.name}")
        return items

    except Exception as e:
        logger.error(f"Error parsing feed {feed_path}: {e}")
        return []


def collect_all_items(exclude_feeds: list[str] = None) -> list[dict]:
    """Collect all items from all feed files, excluding specified feeds."""
    if exclude_feeds is None:
        exclude_feeds = []
    
    feeds_dir = ensure_feeds_directory()
    all_items = []
    
    # Find all feed XML files
    feed_files = sorted(feeds_dir.glob("feed_*.xml"))
    
    for feed_file in feed_files:
        feed_name = feed_file.stem.replace("feed_", "")
        
        # Skip excluded feeds
        if feed_name in exclude_feeds or any(excluded in feed_name for excluded in exclude_feeds):
            logger.info(f"Skipping excluded feed: {feed_file.name}")
            continue
        
        items = parse_feed_xml(feed_file)
        all_items.extend(items)
    
    # Deduplicate by link/guid (keep first occurrence)
    seen_links = set()
    deduplicated_items = []
    for item in all_items:
        link = item.get("link") or item.get("guid")
        if link and link not in seen_links:
            seen_links.add(link)
            deduplicated_items.append(item)
    
    all_items = deduplicated_items
    
    # Sort by date (newest first), items without dates go last
    # Separate items with and without dates
    items_with_dates = [item for item in all_items if item.get("date")]
    items_without_dates = [item for item in all_items if not item.get("date")]
    
    # Sort items with dates (newest first)
    items_with_dates.sort(key=lambda x: x["date"], reverse=True)
    
    # Combine: items with dates first, then items without dates
    all_items = items_with_dates + items_without_dates
    
    logger.info(f"Collected {len(all_items)} total items from all feeds")
    return all_items


def generate_meta_feed(items: list[dict], feed_name: str = "ai_research") -> FeedGenerator:
    """Generate combined RSS feed from all items."""
    fg = FeedGenerator()
    fg.title("AI Research Feed")
    fg.description("Combined feed of AI research blogs and news from Anthropic, OpenAI, xAI, Mistral, and Thinking Machines")
    fg.link(href="https://github.com/0xSMW/rss-feeds")
    fg.language("en")
    
    fg.author({"name": "AI Research Feed Aggregator"})
    
    # feedgen prepends entries, so iterate in reverse to get newest-first in output
    for item in reversed(items):
        fe = fg.add_entry()
        fe.title(item["title"])
        fe.link(href=item["link"])
        fe.description(item["description"])
        
        if item.get("content_html"):
            fe.content(item["content_html"])
        
        if item.get("date"):
            fe.published(item["date"])
        
        fe.category(term=item["category"])
        
        if item.get("author"):
            fe.author({"name": item["author"]})
        
        fe.id(item["guid"])
    
    logger.info("Meta RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "ai_research") -> Path:
    """Save RSS feed to file."""
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"Meta RSS feed saved to {output_file}")
    return output_file


def main(feed_name: str = "ai_research") -> bool:
    """Main function to generate combined AI Research Feed."""
    try:
        # Exclude Arena Magazine, Pirate Wires, and this meta feed
        exclude_feeds = ["arenamag", "piratewires", feed_name]
        
        items = collect_all_items(exclude_feeds=exclude_feeds)
        if not items:
            logger.warning("No items collected from feeds")
            return False
        
        feed = generate_meta_feed(items, feed_name)
        save_rss_feed(feed, feed_name)
        logger.info(f"Successfully generated AI Research Feed with {len(items)} items")
        return True
    except Exception as e:
        logger.error(f"Failed to generate AI Research Feed: {e}")
        return False


if __name__ == "__main__":
    main()
