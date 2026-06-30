import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

DIGG_TECH_URL = "https://digg.com/tech"
DIGG_BASE_URL = "https://digg.com"
FEED_TITLE = "Digg AI Feed from X"
FEED_DESCRIPTION = "Top 10 AI stories from Digg's X-ranked tech feed with click-through links to original source content."
DEFAULT_LIMIT = 10

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def get_project_root() -> Path:
    return Path(__file__).parent.parent


def ensure_feeds_directory() -> Path:
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def build_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def fetch_page(url: str, session: requests.Session | None = None) -> str:
    sess = session or build_requests_session()
    logger.info(f"Fetching page: {url}")
    response = sess.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extract_top_items_payload(html_content: str) -> list[dict]:
    soup = BeautifulSoup(html_content, "html.parser")
    decoder = json.JSONDecoder()
    fallback_items: list[dict] = []

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if (
            "storiesByFilter" not in text
            and "data-yesterday-stories-section" not in text
        ):
            continue

        match = re.match(r"self\.__next_f\.push\((.*)\)$", text, flags=re.S)
        if not match:
            continue

        try:
            payload = json.loads(match.group(1))[1]
        except (IndexError, TypeError, json.JSONDecodeError):
            continue

        stories_idx = payload.find('"storiesByFilter"')
        items_idx = payload.find('"items":[', stories_idx)
        if stories_idx != -1 and items_idx != -1:
            array_start = payload.find("[", items_idx)
            try:
                items, _ = decoder.raw_decode(payload[array_start:])
            except json.JSONDecodeError:
                items = []

            if isinstance(items, list) and items:
                logger.info(
                    f"Parsed {len(items)} Digg AI Feed from X ranked items from embedded payload"
                )
                return items

        daily_stories_idx = payload.find('"stories"')
        if daily_stories_idx != -1 and "data-yesterday-stories-section" in payload:
            array_start = payload.find("[", daily_stories_idx)
            try:
                items, _ = decoder.raw_decode(payload[array_start:])
            except json.JSONDecodeError:
                items = []
            if isinstance(items, list) and items:
                fallback_items = items

    if fallback_items:
        logger.info(
            f"Parsed {len(fallback_items)} Digg AI Feed from X daily items from embedded payload"
        )
    return fallback_items


def _story_url(item: dict) -> str:
    cluster_url_id = item.get("clusterUrlId") or item.get("shortId")
    if cluster_url_id:
        return f"{DIGG_BASE_URL}/tech/{cluster_url_id}"
    cluster_id = item.get("clusterId") or item.get("id")
    return f"{DIGG_BASE_URL}/tech/{cluster_id}"


def _is_internal_or_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not parsed.scheme.startswith("http"):
        return True
    if host in {"digg.com", "www.digg.com"}:
        return True
    if host.endswith("public.blob.vercel-storage.com") or host in {
        "pbs.twimg.com",
        "abs.twimg.com",
    }:
        return True
    if path.endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2")
    ):
        return True
    return False


def _is_social_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "t.co"}


def _iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_strings(child)


def _urls_from_text(text: str) -> list[str]:
    urls = []
    for match in re.finditer(r"https?://[^\s<>'\")]+", text):
        url = match.group(0).rstrip(".,;:!?]")
        if not _is_internal_or_asset_url(url):
            urls.append(url)
    return urls


def _candidate_urls_from_payload(item: dict) -> list[str]:
    candidates = []
    for text in _iter_strings(item):
        candidates.extend(_urls_from_text(text))
    return _dedupe_urls(candidates)


def _candidate_urls_from_story_page(html_content: str, story_url: str) -> list[str]:
    soup = BeautifulSoup(html_content, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        url = urljoin(story_url, a["href"])
        if not _is_internal_or_asset_url(url):
            candidates.append(url)
    return _dedupe_urls(candidates)


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def _choose_source_url(candidates: list[str], story_url: str) -> str:
    for url in candidates:
        if not _is_social_url(url):
            return url
    for url in candidates:
        if _is_social_url(url):
            return url
    return story_url


def _extract_story_overview(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text("\n", strip=True)
    match = re.search(
        r"Story Overview\s+(.+?)(?:\n(?:Original post|Digg Deeper|Top post|Related|Sources)\b)",
        text,
        re.S,
    )
    if not match:
        return ""
    overview = re.sub(r"\s+", " ", match.group(1)).strip()
    return overview[:800]


def _extract_story_metadata(html_content: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text() or ""
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "NewsArticle":
            return {
                "title": (data.get("headline") or "").strip(),
                "description": (data.get("description") or "").strip(),
                "date": _parse_datetime(data.get("datePublished")),
            }
    return {}


def _format_number(value) -> str:
    if value is None:
        return ""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,}"


def _build_content_html(article: dict) -> str:
    authors = article.get("authors") or []
    author_items = []
    for author in authors[:5]:
        display = author.get("displayName") or author.get("username") or "Unknown"
        username = author.get("username")
        label = html.escape(display)
        if username:
            href = f"https://x.com/{username}"
            label = f'<a href="{html.escape(href)}">{label}</a>'
        author_items.append(f"<li>{label}</li>")

    metrics = {
        "Views": _format_number(article.get("views")),
        "Likes": _format_number(article.get("likes")),
        "Bookmarks": _format_number(article.get("bookmarks")),
        "Quotes": _format_number(article.get("quotes")),
        "Replies": _format_number(article.get("replies")),
        "Posts": _format_number(article.get("postCount")),
    }
    metric_items = [
        f"<li><strong>{html.escape(k)}:</strong> {html.escape(str(v))}</li>"
        for k, v in metrics.items()
        if v
    ]

    blocks = [
        f"<p>{html.escape(article.get('description') or article.get('title') or '')}</p>",
        f'<p><strong>Original source:</strong> <a href="{html.escape(article["link"])}">{html.escape(article["link"])}</a></p>',
        f'<p><strong>Digg story:</strong> <a href="{html.escape(article["digg_url"])}">{html.escape(article["digg_url"])}</a></p>',
    ]
    if article.get("overview"):
        blocks.append(f"<p>{html.escape(article['overview'])}</p>")
    if metric_items:
        blocks.append("<ul>" + "".join(metric_items) + "</ul>")
    if author_items:
        blocks.append(
            "<p><strong>Top authors</strong></p><ul>" + "".join(author_items) + "</ul>"
        )
    if article.get("top_text"):
        blocks.append(f"<blockquote>{html.escape(article['top_text'])}</blockquote>")
    return "\n".join(blocks)


def parse_digg_items(html_content: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    items = _extract_top_items_payload(html_content)
    articles = []
    for index, item in enumerate(items[:limit], start=1):
        title = (item.get("title") or "").strip()
        if not title:
            continue

        digg_url = _story_url(item)
        top_item = item.get("topItem") if isinstance(item.get("topItem"), dict) else {}
        totals = item.get("totals") if isinstance(item.get("totals"), dict) else {}
        rank = item.get("rank") or index
        articles.append(
            {
                "title": title,
                "raw_title": title,
                "description": (item.get("tldr") or title).strip(),
                "date": _parse_datetime(item.get("createdAt")),
                "category": "Digg AI from X",
                "guid": item.get("clusterId")
                or item.get("id")
                or item.get("clusterUrlId")
                or item.get("shortId")
                or digg_url,
                "digg_url": digg_url,
                "link": digg_url,
                "rank": rank,
                "views": item.get("views") or totals.get("impressions"),
                "likes": item.get("likes") or totals.get("likes"),
                "bookmarks": item.get("bookmarks") or totals.get("bookmarks"),
                "quotes": item.get("quotes") or totals.get("quotes"),
                "replies": item.get("replies")
                or item.get("comments")
                or totals.get("replies"),
                "postCount": item.get("postCount") or item.get("posts"),
                "authors": item.get("authors") or item.get("topAuthors") or [],
                "payload_candidate_urls": _candidate_urls_from_payload(item),
                "top_text": top_item.get("text") or "",
            }
        )
    return articles


def enrich_article_sources(articles: list[dict], session: requests.Session) -> None:
    for article in articles:
        candidates = list(article.get("payload_candidate_urls") or [])
        try:
            story_html = fetch_page(article["digg_url"], session=session)
        except Exception as exc:
            logger.warning(
                f"Failed to fetch Digg story page {article['digg_url']}: {exc}"
            )
            story_html = ""

        if story_html:
            candidates.extend(
                _candidate_urls_from_story_page(story_html, article["digg_url"])
            )
            article["overview"] = _extract_story_overview(story_html)
            story_metadata = _extract_story_metadata(story_html)
            if story_metadata.get("title") and "…" not in story_metadata["title"]:
                article["title"] = story_metadata["title"]
                article["raw_title"] = story_metadata["title"]
            if story_metadata.get("description"):
                article["description"] = story_metadata["description"]
            if story_metadata.get("date"):
                article["date"] = story_metadata["date"]

        article["source_candidates"] = _dedupe_urls(candidates)
        article["link"] = _choose_source_url(
            article["source_candidates"], article["digg_url"]
        )
        article["content_html"] = _build_content_html(article)


def generate_rss_feed(
    articles: list[dict], feed_name: str = "digg_tech"
) -> FeedGenerator:
    fg = FeedGenerator()
    fg.title(FEED_TITLE)
    fg.description(FEED_DESCRIPTION)
    fg.link(href=DIGG_TECH_URL)
    fg.language("en")

    for article in reversed(articles):
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        fe.description(article.get("description") or article["raw_title"])
        fe.content(article["content_html"])
        if article.get("date"):
            fe.published(article["date"])
        fe.category(term=article.get("category") or "Digg AI from X")
        fe.id(article.get("guid") or article["digg_url"])

    return fg


def save_rss_feed(feed_generator: FeedGenerator, feed_name: str = "digg_tech") -> Path:
    output_file = ensure_feeds_directory() / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"Digg AI Feed from X RSS feed saved to {output_file}")
    return output_file


def validate_feed(feed_path: Path) -> None:
    tree = ET.parse(feed_path)
    root = tree.getroot()
    items = root.findall("./channel/item")
    if not items:
        raise ValueError("Generated feed has no items")
    for item in items:
        link = (item.findtext("link") or "").strip()
        if not link:
            raise ValueError("Generated item is missing a link")
        if urlparse(link).netloc.lower() in {"digg.com", "www.digg.com"}:
            raise ValueError(
                f"Generated item links to Digg instead of source content: {link}"
            )


def main(feed_name: str = "digg_tech", limit: int = DEFAULT_LIMIT) -> bool:
    try:
        session = build_requests_session()
        html_content = fetch_page(DIGG_TECH_URL, session=session)
        articles = parse_digg_items(html_content, limit=limit)
        if not articles:
            logger.warning("No Digg AI Feed from X items parsed")
            return False

        enrich_article_sources(articles, session=session)
        feed = generate_rss_feed(articles, feed_name=feed_name)
        output_file = save_rss_feed(feed, feed_name=feed_name)
        validate_feed(output_file)
        logger.info(
            f"Successfully generated Digg AI Feed from X with {len(articles)} items"
        )
        return True
    except Exception as exc:
        logger.exception(f"Failed to generate Digg AI Feed from X: {exc}")
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Digg AI Feed from X RSS feed."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Number of ranked Digg AI Feed from X items to include.",
    )
    args = parser.parse_args()
    raise SystemExit(0 if main(limit=args.limit) else 1)
