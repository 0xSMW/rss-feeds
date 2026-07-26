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

from _common import build_feed, feed_self_url, load_cached_entries, save_feed

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
        if not isinstance(payload, str):
            continue

        stories_idx = payload.find('"storiesByFilter"')
        if stories_idx != -1:
            # Current shape: "storiesByFilter":{"top":{...,"posts":[...]}};
            # older shape used "items":[...].
            for key in ('"posts":[', '"items":['):
                items_idx = payload.find(key, stories_idx)
                if items_idx == -1:
                    continue
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
    # No non-social source: prefer an actual X post over a bare profile URL.
    for url in candidates:
        if _is_social_url(url) and "/status/" in urlparse(url).path:
            return url
    for url in candidates:
        if _is_social_url(url):
            return url
    return story_url


def _is_story_page(html_content: str) -> bool:
    """True if this looks like a real SSR story page.

    Digg serves an HTTP-200 'Story unavailable' shell for removed stories;
    real story pages carry NewsArticle ld+json and/or the overview node.
    """
    return bool(html_content) and (
        "NewsArticle" in html_content
        or "data-cluster-detail-tldr" in html_content
    )


def _extract_story_overview(html_content: str) -> str:
    """Extract the story's narrative overview paragraphs.

    The overview is server-rendered in a dedicated prose node
    (div[data-cluster-detail-tldr] / div[data-slot="story-prose"]); extract
    only that node so sidebar widgets (X leaderboard etc.) never leak in.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    node = soup.select_one('[data-cluster-detail-tldr], [data-slot="story-prose"]')
    if node is not None:
        paragraphs = [
            re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            for p in node.find_all("p")
        ]
        paragraphs = [p for p in paragraphs if p]
        if not paragraphs:
            paragraphs = [re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()]
        return " ".join(paragraphs)[:1200].strip()

    # Legacy layout: a "Story Overview" heading followed by the overview text.
    heading = soup.find(
        string=lambda s: s and s.strip().lower() == "story overview"
    )
    if heading is not None and heading.parent is not None:
        parts = []
        for sibling in heading.parent.next_siblings:
            name = getattr(sibling, "name", None)
            if name in ("h1", "h2", "h3", "h4"):
                break
            text = (
                sibling.get_text(" ", strip=True)
                if name
                else str(sibling).strip()
            )
            if text:
                parts.append(re.sub(r"\s+", " ", text))
            if sum(len(p) for p in parts) > 800:
                break
        return " ".join(parts)[:800].strip()
    return ""


def _ld_json_image(data: dict) -> str | None:
    image = data.get("image")
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        image = image.get("url")
    if not isinstance(image, str) or not image.startswith(("http://", "https://")):
        return None
    # The generic site-wide share card is not an article image (per-story
    # cluster share cards are fine as a fallback).
    path = urlparse(image).path.lower()
    if "/opengraph/" in path:
        return None
    return image


def _meta_image(soup: BeautifulSoup) -> str | None:
    """Per-story share-card image from og:/twitter: meta tags."""
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
        meta = soup.find("meta", attrs=attrs)
        image = (meta.get("content") or "").strip() if meta else ""
        if not image.startswith(("http://", "https://")):
            continue
        # Skip the generic site-wide card.
        if "/opengraph/" in urlparse(image).path.lower():
            continue
        return image
    return None


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
                "image": _ld_json_image(data) or _meta_image(soup),
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

    blocks = []
    if article.get("image"):
        alt = html.escape(article.get("title") or "")
        blocks.append(
            f'<p><img src="{html.escape(article["image"])}" alt="{alt}"/></p>'
        )
    blocks.extend(
        [
            f"<p>{html.escape(article.get('description') or article.get('title') or '')}</p>",
            f'<p><strong>Original source:</strong> <a href="{html.escape(article["link"])}">{html.escape(article["link"])}</a></p>',
            f'<p><strong>Digg story:</strong> <a href="{html.escape(article["digg_url"])}">{html.escape(article["digg_url"])}</a></p>',
        ]
    )
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
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        title = (item.get("title") or summary.get("title") or "").strip()
        if not title:
            continue

        digg_url = _story_url(item)
        top_item = item.get("topItem") if isinstance(item.get("topItem"), dict) else {}
        totals = item.get("totals") if isinstance(item.get("totals"), dict) else {}
        rank = item.get("rank") or index
        image = (item.get("thumbnailUrl") or "").strip() or None
        articles.append(
            {
                "title": title,
                "raw_title": title,
                "description": (
                    item.get("tldr") or summary.get("description") or title
                ).strip(),
                "date": _parse_datetime(item.get("createdAt")),
                "category": "Digg AI from X",
                "digg_url": digg_url,
                "link": digg_url,
                "rank": rank,
                "image": image,
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
        if story_html and not _is_story_page(story_html):
            logger.warning(
                f"Digg story page unavailable (removed?): {article['digg_url']}"
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
            if story_metadata.get("image") and not article.get("image"):
                article["image"] = story_metadata["image"]

        article["source_candidates"] = _dedupe_urls(candidates)
        article["link"] = _choose_source_url(
            article["source_candidates"], article["digg_url"]
        )
        article["content_html"] = _build_content_html(article)


_DIGG_STORY_LINK_RE = re.compile(
    r'href="(https://digg\.com/[^"]+)"'
)


def _cached_digg_url(item: dict) -> str | None:
    """The Digg story URL embedded in a cached item's content (its stable id)."""
    match = _DIGG_STORY_LINK_RE.search(item.get("content_html") or "")
    return match.group(1) if match else None


def refresh_cached_item(item: dict, session: requests.Session) -> dict:
    """Re-synthesize a cached item's content from its Digg story page.

    Used with --force so stale cached content (old layouts, missing images)
    gets regenerated. If the story page is gone or unusable the cached item is
    returned unchanged, and the item's link (guid) is never altered.
    """
    digg_url = _cached_digg_url(item)
    if not digg_url:
        return item
    try:
        story_html = fetch_page(digg_url, session=session)
    except Exception as exc:
        logger.warning(
            f"Cached story page unavailable ({digg_url}): {exc}; keeping cached content"
        )
        return item
    if not _is_story_page(story_html):
        logger.warning(
            f"Cached story page removed ({digg_url}); keeping cached content"
        )
        return item

    article = {
        "title": item["title"],
        "raw_title": item["title"],
        "description": item.get("description") or item["title"],
        "date": item.get("date"),
        "category": item.get("category") or "Digg AI from X",
        "digg_url": digg_url,
        "link": item["link"],
        "authors": [],
        "top_text": "",
    }
    article["overview"] = _extract_story_overview(story_html)
    metadata = _extract_story_metadata(story_html)
    if metadata.get("title") and "…" not in metadata["title"]:
        article["title"] = article["raw_title"] = metadata["title"]
    if metadata.get("description"):
        article["description"] = metadata["description"]
    if metadata.get("date"):
        article["date"] = metadata["date"]
    if metadata.get("image"):
        article["image"] = metadata["image"]
    article["content_html"] = _build_content_html(article)
    return article


def generate_rss_feed(articles: list[dict], feed_name: str = "digg_tech"):
    return build_feed(
        title=FEED_TITLE,
        description=FEED_DESCRIPTION,
        site_url=DIGG_TECH_URL,
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
    )


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


def main(
    feed_name: str = "digg_tech",
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> bool:
    try:
        feed_path = ensure_feeds_directory() / f"feed_{feed_name}.xml"

        # Previous feed as fail-safe: a failed fetch/parse must never shrink
        # the feed. Currently ranked items are always re-synthesized from the
        # live payload (metrics change hourly); cached items whose stories
        # dropped off the ranking are kept. --force additionally re-synthesizes
        # cached items from their Digg story pages (keeping any whose pages
        # disappeared).
        existing_items, _cache = load_cached_entries(feed_path)

        session = build_requests_session()
        articles: list[dict] = []
        try:
            html_content = fetch_page(DIGG_TECH_URL, session=session)
            articles = parse_digg_items(html_content, limit=limit)
        except Exception as exc:
            logger.error(f"Failed to fetch/parse Digg tech index: {exc}")
        if not articles:
            logger.warning("No Digg AI Feed from X items parsed; keeping cached items")

        enrich_article_sources(articles, session=session)

        # Drop fresh items with no resolvable external source (their cached
        # copies, if any, are preserved by the merge below). Keeping them
        # would emit digg.com links and fail validation.
        unresolved = [
            a for a in articles
            if urlparse(a["link"]).netloc.lower() in {"digg.com", "www.digg.com"}
        ]
        for article in unresolved:
            logger.warning(
                f"No external source found for '{article['title']}'; skipping"
            )
        articles = [a for a in articles if a not in unresolved]

        # Dedupe cached items against fresh ones by source link AND by Digg
        # story URL (the stable identity, in case source-link selection for a
        # story changes between runs).
        fresh_links = {article["link"] for article in articles}
        fresh_digg_urls = {article["digg_url"] for article in articles}
        cached_only = [
            item
            for item in existing_items
            if item["link"] not in fresh_links
            and _cached_digg_url(item) not in fresh_digg_urls
        ]
        if force and cached_only:
            logger.info(
                f"Force mode: re-synthesizing {len(cached_only)} cached items"
            )
            cached_only = [
                refresh_cached_item(item, session=session) for item in cached_only
            ]
        merged = articles + cached_only
        if not merged:
            logger.error("No articles available (fresh or cached); aborting")
            return False

        feed = generate_rss_feed(merged, feed_name=feed_name)
        save_feed(feed, feed_path)
        validate_feed(feed_path)
        logger.info(
            f"Successfully generated Digg AI Feed from X with {len(merged)} items "
            f"({len(articles)} fresh)"
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refetch all currently ranked stories (cached items that dropped off the ranking are kept).",
    )
    args = parser.parse_args()
    raise SystemExit(0 if main(limit=args.limit, force=args.force) else 1)
