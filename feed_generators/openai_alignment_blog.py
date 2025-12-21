import requests
import re
from bs4 import BeautifulSoup
from datetime import datetime
from dateutil import parser as dateparser
import pytz
from feedgen.feed import FeedGenerator
import logging
from pathlib import Path
from urllib.parse import urljoin

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://alignment.openai.com"
BLOG_URL = BASE_URL


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def fetch_page(url: str) -> str:
    """Fetch HTML content."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    logger.info(f"Fetching page: {url}")
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_article_page(url: str) -> str | None:
    """Fetch HTML for a single article page."""
    try:
        logger.debug(f"Fetching article page: {url}")
        return fetch_page(url)
    except Exception as e:
        logger.warning(f"Failed to fetch article page {url}: {e}")
        return None


def _clean_article_html(container, base_url: str) -> str:
    """Clean article container - strip classes, simplify HTML for RSS readers."""
    if container is None:
        return ""

    # Remove noisy elements
    for tag in container.select("script, style, noscript, svg, form, iframe, nav, header, footer"):
        tag.decompose()

    # Remove back link and meta div (date/authors)
    for el in container.select("a.back, div.meta"):
        el.decompose()

    # Make links and media absolute
    for a in container.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(("http://", "https://", "mailto:", "#")):
            a["href"] = urljoin(base_url, href)
    for img in container.find_all("img", src=True):
        src = img["src"]
        if not src.startswith(("http://", "https://", "data:")):
            # Handle relative paths like ./figures/image.png
            img["src"] = urljoin(base_url + "/", src.lstrip("./"))

    # Strip all attributes except essential ones (href, src, alt)
    allowed_attrs = {
        "a": ["href"],
        "img": ["src", "alt"],
    }
    for el in container.find_all(True):
        tag_name = el.name
        if tag_name in allowed_attrs:
            attrs_to_keep = {}
            for attr in allowed_attrs[tag_name]:
                if el.has_attr(attr):
                    attrs_to_keep[attr] = el[attr]
            el.attrs = attrs_to_keep
        else:
            el.attrs = {}

    # Extract content elements in order, including images from wrapper divs
    # First, handle plot-group divs to extract images and their captions
    plot_groups = container.find_all("div", class_=lambda c: c and "plot-group" in c if c else False)
    for plot_group in plot_groups:
        img = plot_group.find("img")
        if img:
            # Replace the plot-group div with just the image
            plot_group.replace_with(img)
    
    # Now extract all content elements in document order
    output_parts = []
    content_tags = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "blockquote", "pre", "hr", "img"]
    
    # Get all content elements, preserving order
    for el in container.find_all(content_tags):
        # Skip if nested inside another content element (will be included with parent)
        parent = el.parent
        if parent and parent.name in content_tags:
            continue
        # Include the element
        output_parts.append(str(el))
    
    return "\n".join(output_parts)


def extract_article_metadata(html: str, page_url: str) -> dict:
    """Extract article metadata from page: title, date, description, content."""
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    
    # Extract title from <h1>
    h1 = soup.find("h1")
    if h1:
        result["title"] = h1.get_text(strip=True)
    
    # Extract date from <div class="meta"> - format: "Dec 18, 2025 · Authors"
    meta_div = soup.select_one("div.meta")
    if meta_div:
        meta_text = meta_div.get_text(strip=True)
        # Extract date part (before the ·)
        date_part = meta_text.split("·")[0].strip()
        dt = _parse_date(date_part)
        if dt:
            result["date"] = dt
    
    # Find main content in <div class="content">
    content_container = soup.select_one("div.content")
    
    if content_container:
        # Extract description from first paragraph
        first_p = content_container.find("p")
        if first_p:
            result["description"] = first_p.get_text(" ", strip=True)[:300]
        
        # Get clean content HTML
        result["content_html"] = _clean_article_html(content_container, base_url=page_url)
    
    return result


def _parse_date(text: str) -> datetime | None:
    """Parse date string like 'Dec 18, 2025'."""
    if not text:
        return None
    text = text.strip()
    try:
        # Try dateutil parser first
        dt = dateparser.parse(text)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=pytz.UTC)
            return dt
    except Exception:
        pass
    
    # Try manual parsing for "Dec 18, 2025" format
    match = re.match(r"(\w+)\s+(\d+),\s+(\d{4})", text)
    if match:
        month_str, day_str, year_str = match.groups()
        try:
            dt = datetime.strptime(f"{month_str} {day_str} {year_str}", "%b %d %Y")
            return dt.replace(tzinfo=pytz.UTC)
        except ValueError:
            pass
    
    return None


def parse_blog_html(html_content: str) -> list[dict]:
    """Parse the Alignment Research Blog listing page."""
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []
    seen = set()

    # Find all post links
    post_links = soup.select("a.post-link[href]")
    
    for link in post_links:
        try:
            href = link.get("href", "").strip()
            if not href:
                continue
            
            # Normalize to absolute link
            article_url = urljoin(BASE_URL, href)
            if article_url in seen:
                continue
            seen.add(article_url)
            
            # Extract title
            title_elem = link.select_one("div.post-title")
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            if not title:
                continue
            
            # Extract subtitle/description
            subtitle_elem = link.select_one("div.post-subtitle")
            description = subtitle_elem.get_text(strip=True) if subtitle_elem else title
            
            # Extract date - format "Dec 18"
            date_elem = link.select_one("div.date")
            date_dt = None
            if date_elem:
                date_text = date_elem.get_text(strip=True)
                # Add current year if not present
                if date_text and "," not in date_text:
                    from datetime import datetime
                    current_year = datetime.now().year
                    date_text = f"{date_text}, {current_year}"
                date_dt = _parse_date(date_text)
            
            articles.append({
                "title": title,
                "link": article_url,
                "date": date_dt,
                "category": "Alignment Research",
                "description": description,
            })
        except Exception as e:
            logger.warning(f"Skipping an article due to parsing error: {e}")
            continue
    
    # Sort by date (newest first)
    articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)
    
    logger.info(f"Parsed {len(articles)} articles from listing page")
    return articles


def generate_rss_feed(articles, feed_name: str = "openai_alignment"):
    """Generate RSS feed from parsed articles."""
    fg = FeedGenerator()
    fg.title("OpenAI Alignment Research Blog")
    fg.description("Informal updates from the OpenAI Alignment and Safety Systems teams")
    fg.link(href=BASE_URL)
    fg.language("en")

    fg.author({"name": "OpenAI Alignment and Safety Systems"})
    fg.link(href=BASE_URL, rel="alternate")

    # feedgen prepends entries, so iterate in reverse to get newest-first in output
    for article in reversed(articles):
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        
        content_html = article.get("content_html")
        summary = article.get("description", article["title"]) or article["title"]
        
        if content_html:
            fe.content(content_html)
        fe.description(summary)
        
        if article.get("date"):
            fe.published(article["date"])
        
        fe.category(term=article["category"])
        fe.id(article["link"])

    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "openai_alignment") -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"RSS feed saved to {output_file}")
    return output_file


def main(feed_name: str = "openai_alignment") -> bool:
    try:
        html_content = fetch_page(BLOG_URL)
        articles = parse_blog_html(html_content)
        if not articles:
            logger.warning("No articles parsed. Selectors may need updating.")
        
        # Fetch full content for each article
        logger.info(f"Fetching full content for {len(articles)} articles...")
        for article in articles:
            article_html = fetch_article_page(article["link"])
            if article_html:
                metadata = extract_article_metadata(article_html, article["link"])
                
                # Update article with extracted metadata
                if metadata.get("title"):
                    article["title"] = metadata["title"]
                if metadata.get("date"):
                    article["date"] = metadata["date"]
                if metadata.get("content_html"):
                    article["content_html"] = metadata["content_html"]
                if metadata.get("description"):
                    article["description"] = metadata["description"]
            else:
                logger.warning(f"Could not fetch content for {article['link']}")
        
        # Re-sort by date now that we have dates from article pages
        articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)
        
        feed = generate_rss_feed(articles, feed_name)
        save_rss_feed(feed, feed_name)
        logger.info(f"Successfully generated RSS feed with {len(articles)} articles")
        return True
    except Exception as e:
        logger.error(f"Failed to generate OpenAI Alignment RSS: {e}")
        return False


if __name__ == "__main__":
    main()

