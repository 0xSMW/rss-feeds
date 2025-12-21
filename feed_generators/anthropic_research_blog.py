import argparse
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import pytz
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
import xml.etree.ElementTree as ET

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


def setup_selenium_driver():
    """Set up Selenium WebDriver with undetected-chromedriver."""
    options = uc.ChromeOptions()
    options.add_argument("--headless")  # Ensure headless mode is enabled
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
    return uc.Chrome(options=options)

def fetch_research_content_requests(url="https://www.anthropic.com/research"):
    """Fetch the research page HTML using requests."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    logger.info(f"Fetching research content via requests: {url}")
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_research_content_selenium(url="https://www.anthropic.com/research"):
    """Fetch the fully loaded HTML content of the research page using Selenium."""
    driver = None
    try:
        logger.info(f"Fetching content from URL: {url}")
        driver = setup_selenium_driver()
        driver.get(url)

        # Wait for research articles to load by checking for specific elements
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            # Wait for research articles to be present
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/research/']")))
            logger.info("Research articles loaded successfully")
        except:
            logger.warning("Could not confirm articles loaded, proceeding anyway...")

        html_content = driver.page_source
        logger.info("Successfully fetched HTML content")
        return html_content

    except Exception as e:
        logger.error(f"Error fetching content: {e}")
        raise
    finally:
        if driver:
            driver.quit()


def fetch_article_page(url: str) -> str | None:
    """Fetch HTML for a single article page."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url} ({e}); falling back to Selenium")
        try:
            driver = setup_selenium_driver()
            driver.get(url)
            html = driver.page_source
            driver.quit()
            return html
        except Exception as e2:
            logger.warning(f"Selenium fetch failed for {url}: {e2}")
            return None


def _clean_article_html(container, base_url: str) -> str:
    """Clean article container and keep feed-friendly tags."""
    if container is None:
        return ""

    for tag in container.select("script, style, noscript, svg, form, iframe, nav, header, footer"):
        tag.decompose()

    for share_link in container.find_all("a", href=True):
        href = share_link["href"]
        if "twitter.com/intent/tweet" in href or "linkedin.com/shareArticle" in href:
            share_link.decompose()

    for heading in container.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if heading.get_text(" ", strip=True).lower() == "related content":
            for sibling in list(heading.find_all_next()):
                sibling.decompose()
            heading.decompose()
            break

    allowed = {
        "p",
        "a",
        "img",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "blockquote",
        "code",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
    }
    for tag in list(container.find_all(True)):
        if tag.name not in allowed:
            tag.unwrap()
            continue
        attrs = {}
        if tag.name == "a" and tag.get("href"):
            attrs["href"] = tag["href"]
        elif tag.name == "img" and tag.get("src"):
            attrs["src"] = tag["src"]
            if tag.get("alt"):
                attrs["alt"] = tag["alt"]
        tag.attrs = attrs

    for a in container.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(("http://", "https://", "mailto:", "#")):
            a["href"] = urljoin(base_url, href)
    for img in container.find_all("img", src=True):
        src = img["src"]
        if not src.startswith(("http://", "https://", "data:")):
            img["src"] = urljoin(base_url, src)

    return str(container)


def extract_article_content(html: str, page_url: str) -> tuple[str, str]:
    """Extract main article content HTML and a plain-text summary."""
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.select_one("article")
        or soup.select_one("main article")
        or soup.select_one("main")
        or soup.select_one("[class*='content']")
    )
    content_html = _clean_article_html(container, base_url=page_url)

    summary = ""
    if container:
        first_p = container.find("p")
        if first_p:
            summary = first_p.get_text(" ", strip=True)
    if not summary:
        summary = soup.title.get_text(strip=True) if soup.title else ""

    return content_html, summary


def parse_date_string(date_str):
    """Parse various date formats found on Anthropic research pages."""
    if not date_str or not date_str.strip():
        return None

    date_str = date_str.strip()

    # Try different date formats
    date_formats = [
        "%b %d, %Y",  # Mar 27, 2025
        "%B %d, %Y",  # March 27, 2025
        "%Y-%m-%d",  # 2025-03-27
        "%m/%d/%Y",  # 03/27/2025
        "%d %b %Y",  # 27 Mar 2025
        "%d %B %Y",  # 27 March 2025
    ]

    for date_format in date_formats:
        try:
            date = datetime.strptime(date_str, date_format)
            return date.replace(tzinfo=pytz.UTC)
        except ValueError:
            continue

    logger.warning(f"Could not parse date: '{date_str}'")
    return None


def parse_research_html(html_content):
    """Parse the research HTML content and extract article information."""
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        articles = []

        # Look for research article links - updated selectors based on the HTML
        research_links = soup.select("a[href*='/research/']")
        logger.info(f"Found {len(research_links)} research links")

        found_links = set()  # To avoid duplicates

        for link in research_links:
            try:
                href = link.get("href", "")
                if not href or href in found_links:
                    continue

                # Skip non-research URLs
                if not "/research/" in href or href == "/research":
                    continue

                found_links.add(href)

                # Extract title - try multiple approaches
                title = None

                # Try to find title in the link itself
                title_selectors = [
                    "h3",
                    "h2",
                    "h1",
                    ".Card_headline__reaoT",
                    "[class*='headline']",
                    "[class*='title']",
                ]

                for title_sel in title_selectors:
                    title_elem = link.select_one(title_sel)
                    if title_elem and title_elem.text.strip():
                        title = title_elem.text.strip()
                        break

                # If no title found in the link, check parent elements
                if not title:
                    parent = link.parent
                    for _ in range(3):  # Check up to 3 parent levels
                        if parent:
                            for title_sel in title_selectors:
                                title_elem = parent.select_one(title_sel)
                                if title_elem and title_elem.text.strip():
                                    title = title_elem.text.strip()
                                    break
                            if title:
                                break
                            parent = parent.parent

                # If still no title, use the link text itself
                if not title:
                    title = link.text.strip()
                    if len(title) < 5:  # Skip very short titles
                        continue

                # Clean up title
                title = " ".join(title.split())  # Remove extra whitespace

                # Construct full URL
                if href.startswith("https://"):
                    full_url = href
                elif href.startswith("/"):
                    full_url = "https://www.anthropic.com" + href
                else:
                    continue

                # Extract date - try multiple selectors
                date = None
                date_selectors = [
                    ".detail-m.agate",  # Based on the HTML structure
                    "[class*='timestamp']",
                    "[class*='date']",
                    "time",
                    ".PostDetail_post-timestamp__TBJ0Z",
                    ".text-label",
                ]

                # Look for date in the link or its parents
                for date_sel in date_selectors:
                    date_elem = link.select_one(date_sel)
                    if not date_elem and link.parent:
                        # Check parent and sibling elements
                        for parent_level in [link.parent, link.parent.parent if link.parent.parent else None]:
                            if parent_level:
                                date_elem = parent_level.select_one(date_sel)
                                if date_elem:
                                    break

                    if date_elem:
                        date_text = date_elem.text.strip()
                        parsed_date = parse_date_string(date_text)
                        if parsed_date:
                            date = parsed_date
                            break

                # If no date found, don't set a default date - let it be None
                # This avoids the issue of updating dates to "now"

                # Determine category from URL
                category = "Research"
                if "/news/" in href:
                    category = "News"

                # Skip entries without meaningful titles
                if not title or len(title.strip()) < 5:
                    continue

                articles.append(
                    {
                        "title": title,
                        "link": full_url,
                        "date": date,  # This can be None
                        "category": category,
                        "description": title,  # Use title as description
                    }
                )

                logger.info(f"Found article: {title} - {date}")

            except Exception as e:
                logger.warning(f"Skipping article due to parsing error: {str(e)}")
                continue

        # Remove duplicates based on link
        seen_links = set()
        unique_articles = []
        for article in articles:
            if article["link"] not in seen_links:
                seen_links.add(article["link"])
                unique_articles.append(article)

        logger.info(f"Successfully parsed {len(unique_articles)} unique research articles")
        return unique_articles

    except Exception as e:
        logger.error(f"Error parsing HTML content: {str(e)}")
        raise


def generate_rss_feed(articles, feed_name="anthropic_research"):
    """Generate RSS feed from research articles."""
    try:
        fg = FeedGenerator()
        fg.title("Anthropic Research")
        fg.description("Latest research papers and updates from Anthropic")
        fg.link(href="https://www.anthropic.com/research")
        fg.language("en")

        # Set feed metadata
        fg.author({"name": "Anthropic Research Team"})
        fg.logo("https://www.anthropic.com/images/icons/apple-touch-icon.png")
        fg.subtitle("Latest research from Anthropic")
        fg.link(href="https://www.anthropic.com/research", rel="alternate")
        fg.link(href=f"https://anthropic.com/research/feed_{feed_name}.xml", rel="self")

        # Sort articles by date (most recent first), but handle None dates
        # Articles with dates come first, then articles without dates (preserve original order)
        articles_with_date = [a for a in articles if a["date"] is not None]
        articles_without_date = [a for a in articles if a["date"] is None]

        articles_with_date.sort(key=lambda x: x["date"], reverse=True)
        articles_sorted = articles_with_date + articles_without_date

        # Add entries
        for article in articles_sorted:
            fe = fg.add_entry()
            fe.title(article["title"])
            fe.description(article["description"])
            fe.link(href=article["link"])
            if article.get("content_html"):
                fe.content(article["content_html"])

            # Only set published date if we have a valid date
            if article["date"]:
                fe.published(article["date"])

            fe.category(term=article["category"])
            fe.id(article["link"])

        logger.info("Successfully generated RSS feed")
        return fg

    except Exception as e:
        logger.error(f"Error generating RSS feed: {str(e)}")
        raise


def save_rss_feed(feed_generator, feed_name="anthropic_research"):
    """Save the RSS feed to a file in the feeds directory."""
    try:
        # Ensure feeds directory exists and get its path
        feeds_dir = ensure_feeds_directory()

        # Create the output file path
        output_filename = feeds_dir / f"feed_{feed_name}.xml"

        # Save the feed
        feed_generator.rss_file(str(output_filename), pretty=True)
        logger.info(f"Successfully saved RSS feed to {output_filename}")
        return output_filename

    except Exception as e:
        logger.error(f"Error saving RSS feed: {str(e)}")
        raise


def get_existing_entries_from_feed(feed_path):
    """Parse the existing RSS feed and return entries for reuse."""
    entries = []
    if not feed_path.exists():
        return entries
    try:
        tree = ET.parse(feed_path)
        root = tree.getroot()
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        for item in root.findall("./channel/item"):
            link_elem = item.find("link")
            title_elem = item.find("title")
            desc_elem = item.find("description")
            content_elem = item.find("content:encoded", ns)
            date_elem = item.find("pubDate")
            category_elem = item.find("category")

            link = link_elem.text.strip() if link_elem is not None and link_elem.text else None
            if not link:
                continue

            date = None
            if date_elem is not None and date_elem.text:
                try:
                    date = parsedate_to_datetime(date_elem.text.strip())
                except Exception:
                    date = None

            entries.append(
                {
                    "title": title_elem.text.strip() if title_elem is not None and title_elem.text else link,
                    "link": link,
                    "date": date,
                    "category": category_elem.text.strip() if category_elem is not None and category_elem.text else "Research",
                    "description": desc_elem.text if desc_elem is not None and desc_elem.text else "",
                    "content_html": content_elem.text if content_elem is not None and content_elem.text else "",
                }
            )
    except Exception as e:
        logger.warning(f"Failed to parse existing feed entries: {str(e)}")
    return entries


def main(feed_name="anthropic_research", force: bool = False):
    """Main function to generate RSS feed from Anthropic's research page."""
    try:
        feeds_dir = ensure_feeds_directory()
        feed_path = feeds_dir / f"feed_{feed_name}.xml"

        existing_entries = []
        existing_links = set()
        if not force:
            existing_entries = get_existing_entries_from_feed(feed_path)
            existing_links = {entry["link"] for entry in existing_entries}

        articles = []
        try:
            html_content = fetch_research_content_requests()
            articles = parse_research_html(html_content)
        except Exception as e:
            logger.warning(f"Requests fetch failed ({e}); falling back to Selenium")

        if not articles:
            html_content = fetch_research_content_selenium()
            articles = parse_research_html(html_content)

        if not articles:
            logger.warning("No articles found. Please check the HTML structure.")
            return False

        new_articles = [article for article in articles if article["link"] not in existing_links]

        for article in new_articles:
            article_html = fetch_article_page(article["link"])
            if not article_html:
                continue
            content_html, summary = extract_article_content(article_html, article["link"])
            if content_html:
                article["content_html"] = content_html
            if summary:
                article["description"] = summary

        combined_articles = new_articles + existing_entries
        seen_links = set()
        deduped_articles = []
        for article in combined_articles:
            if article["link"] in seen_links:
                continue
            seen_links.add(article["link"])
            deduped_articles.append(article)

        # Generate RSS feed
        feed = generate_rss_feed(deduped_articles, feed_name)

        # Save feed to file
        output_file = save_rss_feed(feed, feed_name)

        logger.info(f"Successfully generated RSS feed with {len(deduped_articles)} articles")
        return True

    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {str(e)}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Anthropic Research RSS feed.")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    main(force=args.force)
