import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from feedgen.feed import FeedGenerator

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://red.anthropic.com"
BLOG_URL = BASE_URL + "/"

DATE_PATTERN = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|"
    r"January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}\b"
)


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory() -> Path:
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
    except Exception as exc:
        logger.warning(f"Failed to fetch article page {url}: {exc}")
        return None


def _parse_date(text: str) -> datetime | None:
    """Parse date string like 'December 18, 2025'."""
    if not text:
        return None
    text = text.strip()
    try:
        dt = dateparser.parse(text)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=pytz.UTC)
            return dt
    except Exception:
        pass
    return None


def _parse_listing_date(text: str) -> datetime | None:
    """Parse month-year strings like 'December 2025'."""
    if not text:
        return None
    match = re.match(r"([A-Za-z]+)\s+(\d{4})", text.strip())
    if not match:
        return None
    month_str, year_str = match.groups()
    for fmt in ("%B %Y", "%b %Y"):
        try:
            dt = datetime.strptime(f"{month_str} {year_str}", fmt)
            return dt.replace(day=1, tzinfo=pytz.UTC)
        except ValueError:
            continue
    return None


def _extract_date_from_article(container) -> tuple[datetime | None, object | None]:
    """Find date paragraph in the article body."""
    if not container:
        return None, None
    for p in container.find_all("p", limit=6):
        text = p.get_text(" ", strip=True)
        if DATE_PATTERN.search(text):
            dt = _parse_date(text)
            if dt:
                return dt, p
    return None, None


def _clean_article_html(container, base_url: str) -> str:
    """Clean article container and keep feed-friendly tags."""
    if container is None:
        return ""

    for tag in container.select("script, style, noscript, svg, form, iframe, nav, header, footer"):
        tag.decompose()

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
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
        "hr",
        "figure",
        "figcaption",
        "sup",
        "sub",
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

    return "".join(str(child) for child in container.contents if str(child).strip())


def extract_article_metadata(html: str, page_url: str) -> dict:
    """Extract article metadata from page: title, date, description, content."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {}

    title_el = soup.select_one("d-title h1") or soup.find("h1")
    if title_el:
        result["title"] = title_el.get_text(strip=True)

    front_matter = soup.find("d-front-matter")
    if front_matter:
        script = front_matter.find("script", type="text/json")
        if script and script.string:
            try:
                data = json.loads(script.string)
                description = data.get("description")
                if description:
                    result["description"] = description.strip()
            except json.JSONDecodeError:
                logger.debug("Failed to parse front matter JSON")

    content_container = soup.find("d-article") or soup.find("article") or soup.find("main")

    date_dt, date_p = _extract_date_from_article(content_container)
    if date_dt:
        result["date"] = date_dt
    if date_p:
        date_p.decompose()

    if content_container:
        first_p = content_container.find("p")
        if first_p and not result.get("description"):
            result["description"] = first_p.get_text(" ", strip=True)[:300]
        result["content_html"] = _clean_article_html(content_container, base_url=page_url)

    return result


def parse_blog_html(html_content: str) -> list[dict]:
    """Parse the red.anthropic.com listing page."""
    soup = BeautifulSoup(html_content, "html.parser")
    articles: list[dict] = []
    seen = set()

    toc = soup.select_one("div.toc")
    if not toc:
        logger.warning("Could not find listing container.")
        return articles

    current_date_text = None

    for child in toc.children:
        if not getattr(child, "name", None):
            continue
        if child.name == "div" and "date" in (child.get("class") or []):
            current_date_text = child.get_text(" ", strip=True)
            continue
        if child.name == "a" and "note" in (child.get("class") or []):
            _append_note_article(child, current_date_text, articles, seen)
            continue
        if child.name == "div":
            for note in child.select("a.note"):
                _append_note_article(note, current_date_text, articles, seen)

    articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)
    logger.info(f"Parsed {len(articles)} articles from listing page")
    return articles


def _append_note_article(note, date_text: str | None, articles: list[dict], seen: set) -> None:
    href = note.get("href", "").strip()
    if not href:
        return
    article_url = urljoin(BASE_URL, href)
    if article_url in seen:
        return
    seen.add(article_url)

    title_el = note.find("h3")
    if not title_el:
        return
    title = title_el.get_text(strip=True)
    description_el = note.select_one("div.description")
    description = description_el.get_text(" ", strip=True) if description_el else title
    date_dt = _parse_listing_date(date_text) if date_text else None

    articles.append(
        {
            "title": title,
            "link": article_url,
            "date": date_dt,
            "category": "Anthropic Red Teaming",
            "description": description,
        }
    )


def generate_rss_feed(articles, feed_name: str = "anthropic_red"):
    """Generate RSS feed from parsed articles."""
    fg = FeedGenerator()
    fg.title("Anthropic Red Teaming")
    fg.description("Research and updates from Anthropic's red teaming work")
    fg.link(href=BASE_URL)
    fg.language("en")

    fg.author({"name": "Anthropic"})
    fg.link(href=BASE_URL, rel="alternate")

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


def save_rss_feed(feed_generator, feed_name: str = "anthropic_red") -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"RSS feed saved to {output_file}")
    return output_file


def main(feed_name: str = "anthropic_red") -> bool:
    try:
        html_content = fetch_page(BLOG_URL)
        articles = parse_blog_html(html_content)
        if not articles:
            logger.warning("No articles parsed. Selectors may need updating.")

        logger.info(f"Fetching full content for {len(articles)} articles...")
        for article in articles:
            article_html = fetch_article_page(article["link"])
            if article_html:
                metadata = extract_article_metadata(article_html, article["link"])
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

        articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)

        feed = generate_rss_feed(articles, feed_name)
        save_rss_feed(feed, feed_name)
        logger.info(f"Successfully generated RSS feed with {len(articles)} articles")
        return True
    except Exception as exc:
        logger.error(f"Failed to generate Anthropic Red RSS: {exc}")
        return False


if __name__ == "__main__":
    main()
