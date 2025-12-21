import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
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


def parse_feed_xml(feed_path: Path) -> list[dict]:
    """Parse an RSS feed XML file and extract all items."""
    try:
        with open(feed_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        soup = BeautifulSoup(content, "xml")
        items = []
        
        for item in soup.find_all("item"):
            item_data = {}
            
            # Extract title
            title_elem = item.find("title")
            if title_elem:
                item_data["title"] = title_elem.get_text(strip=True)
            else:
                continue
            
            # Extract link
            link_elem = item.find("link")
            if link_elem:
                item_data["link"] = link_elem.get_text(strip=True)
            else:
                continue
            
            # Extract description
            desc_elem = item.find("description")
            if desc_elem:
                item_data["description"] = desc_elem.get_text(strip=True)
            else:
                item_data["description"] = item_data["title"]
            
            # Extract content:encoded
            content_elem = item.find("content:encoded")
            if content_elem:
                item_data["content_html"] = content_elem.get_text()
            
            # Extract date
            pub_date_elem = item.find("pubDate")
            if pub_date_elem:
                date_str = pub_date_elem.get_text(strip=True)
                try:
                    # Parse RFC 822 date format
                    dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z")
                    item_data["date"] = dt
                except ValueError:
                    try:
                        # Try without timezone
                        dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %Z")
                        dt = dt.replace(tzinfo=pytz.UTC)
                        item_data["date"] = dt
                    except ValueError:
                        item_data["date"] = None
            else:
                item_data["date"] = None
            
            # Extract category
            category_elem = item.find("category")
            if category_elem:
                item_data["category"] = category_elem.get_text(strip=True)
            else:
                item_data["category"] = "Research"
            
            # Extract guid/id
            guid_elem = item.find("guid")
            if guid_elem:
                item_data["guid"] = guid_elem.get_text(strip=True)
            else:
                item_data["guid"] = item_data["link"]
            
            # Extract author if present
            author_elem = item.find("author")
            if author_elem:
                item_data["author"] = author_elem.get_text(strip=True)
            
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
    feed_files = list(feeds_dir.glob("feed_*.xml"))
    
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
        # Exclude Arena Magazine feeds
        exclude_feeds = ["arenamag"]
        
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

