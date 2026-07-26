"""Shared utilities for feed generators.

Provides a single article-HTML normalizer and feed-building helpers so every
generator emits reader-ready RSS:

- ``clean_article_html``: sanitize a scraped article container down to the HTML
  subset that major RSS readers (Miniflux, Feedbin, NetNewsWire/Reeder) render
  reliably, while preserving structure (headings, lists, figures with captions,
  tables, code blocks) and resolving lazy-loaded images to concrete URLs.
- ``extract_summary``: derive a short item description from page metadata.
- ``build_feed`` / ``save_feed``: consistent channel metadata (self link first,
  site link last so the channel <link> is correct), CDATA content:encoded,
  stable permalink guids, a media:content lead image, item caps, and
  byte-stable output so unchanged feeds are not rewritten.
- ``load_cached_entries``: read a previously generated feed back as the cache.
"""

import copy
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pytz
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from dateutil import parser as dateparser
from feedgen.feed import FeedGenerator

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITEMS = 50

# Where the generated feeds are actually served from (see README).
FEED_BASE_URL = "https://raw.githubusercontent.com/0xSMW/rss-feeds/main/feeds"


def feed_self_url(feed_filename):
    """Public URL of a generated feed file, for atom:link rel=self."""
    return f"{FEED_BASE_URL}/{feed_filename}"

# Tags removed together with their contents.
REMOVE_TAGS = [
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "label",
    "svg",
    "canvas",
    "dialog",
    "object",
    "embed",
    "link",
    "meta",
    "title",
]

# Allowed output tags and the attributes kept on each. Everything else is
# unwrapped (children preserved). This is roughly the intersection of the
# Miniflux and Feedbin sanitizer allowlists.
ALLOWED_TAGS = {
    "p": set(),
    "h2": {"id"},
    "h3": {"id"},
    "h4": {"id"},
    "h5": {"id"},
    "h6": {"id"},
    "ul": set(),
    "ol": set(),
    "li": set(),
    "blockquote": set(),
    "pre": set(),
    "code": set(),
    "em": set(),
    "strong": set(),
    "b": set(),
    "i": set(),
    "u": set(),
    "s": set(),
    "del": set(),
    "ins": set(),
    "small": set(),
    "sup": {"id"},
    "sub": set(),
    "br": set(),
    "hr": set(),
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "figure": set(),
    "figcaption": set(),
    "table": set(),
    "caption": set(),
    "thead": set(),
    "tbody": set(),
    "tfoot": set(),
    "tr": set(),
    "td": {"rowspan", "colspan"},
    "th": {"rowspan", "colspan"},
    "dl": set(),
    "dt": set(),
    "dd": set(),
    "cite": set(),
    "q": {"cite"},
    "kbd": set(),
    "samp": set(),
    "var": set(),
    "time": {"datetime"},
    "audio": {"src", "controls"},
    "iframe": {"src", "width", "height", "allowfullscreen", "frameborder"},
}

# Embedded players readers actually render; other iframes are dropped.
IFRAME_HOSTS = (
    "youtube.com",
    "youtube-nocookie.com",
    "player.vimeo.com",
    "vimeo.com",
    "dailymotion.com",
    "open.spotify.com",
    "w.soundcloud.com",
    "soundcloud.com",
)

SHARE_URL_MARKERS = (
    "linkedin.com/sharing",
    "linkedin.com/sharearticle",
    "facebook.com/sharer",
    "facebook.com/sharer.php",
    "twitter.com/intent",
    "twitter.com/share",
    "x.com/intent",
    "x.com/share",
    "reddit.com/submit",
    "pinterest.com/pin/create",
    "news.ycombinator.com/submitlink",
    "mailto:?",
)

# Site-chrome class/id tokens. Matched against whole dash/underscore-separated
# tokens so e.g. "navigation" matches via "nav"-prefixed tokens but prose
# classes like "canvas" do not.
CHROME_TOKEN_RE = re.compile(
    r"(?:^|[-_\s])("
    r"share|sharing|social|socials|related|newsletter|subscribe|subscription|"
    r"promo|promotion|breadcrumb|breadcrumbs|sidebar|pagination|paginate|"
    r"cookie|banner|signup|cta|ctas|toc|menu|footer|header|masthead|modal|"
    r"popup|overlay|skip|progressbar|eyebrow|badge|chip|pill|avatar|byline|"
    r"comments|disqus|advert|advertisement|sponsor|sponsored|recirc|"
    r"recommended|trending|readnext|prevnext|backtotop"
    r")(?:$|[-_\s])",
    re.IGNORECASE,
)

RELATED_HEADING_MARKERS = (
    "related articles",
    "related posts",
    "related content",
    "related stories",
    "more articles",
    "more from",
    "more stories",
    "read next",
    "keep reading",
    "you might also like",
    "other articles you might like",
    "further reading",
    "explore more",
    "table of contents",
)

# Standalone anchors (or single-link paragraphs) whose entire text matches one
# of these are navigation/CTA buttons, not article content.
DEFAULT_CTA_PATTERNS = (
    r"try(\s+\S+){0,4}",
    r"get started(\s+\S+){0,3}",
    r"start (building|now|free|using\s+\S+)",
    r"sign (up|in)(\s+\S+){0,3}",
    r"log ?in",
    r"subscribe(\s+\S+){0,3}",
    r"download(\s+\S+){0,3}",
    r"learn more",
    r"read more",
    r"see more",
    r"view all(\s+\S+){0,2}",
    r"see all(\s+\S+){0,2}",
    r"explore(\s+\S+){0,3}",
    r"contact (sales|us)",
    r"(book|request|schedule) a demo",
    r"talk to (an expert|sales|us)",
    r"join(\s+\S+){0,3}",
    r"share(\s+\S+){0,3}",
    r"copy (link|page)(\s+\S+){0,2}",
    r"back to(\s+\S+){0,2}",
    r"add to(\s+\S+){0,3}",
    r"create (an )?(api key|account)",
    r"get (access|the app|api access)",
    r"open",
    r"get\s+\S+",
    r"build (on|with)(\s+\S+){0,3}",
    r"watch (video|now|the video)",
    r"apply now",
    r"skip to content",
    r"read the docs",
    r"read the \S+(\s+\S+){0,2}",
    r"view (docs|documentation|pricing)",
)

MONTH_NAME_RE = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
)
# A paragraph that is nothing but a date (optionally with a reading-time note).
DATE_ONLY_RE = re.compile(
    rf"^(?:published\s+)?(?:on\s+)?(?:{MONTH_NAME_RE}\s+\d{{1,2}},?\s+\d{{4}}"
    rf"|\d{{1,2}}\s+{MONTH_NAME_RE},?\s+\d{{4}}"
    r"|\d{4}-\d{2}-\d{2})"
    r"(?:\s*[·•|-]?\s*\d+\s*min(?:ute)?s?(?:\s+read)?)?$",
    re.IGNORECASE,
)
READING_TIME_RE = re.compile(r"^\d+\s*min(?:ute)?s?(?:\s+read)?$", re.IGNORECASE)

LAZY_SRC_ATTRS = (
    "data-src",
    "data-original",
    "data-orig",
    "data-url",
    "data-orig-file",
    "data-large-file",
    "data-medium-file",
    "data-lazy-src",
)

# XML 1.0 illegal control characters (lxml CDATA rejects them).
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _compile_cta_re(extra_patterns=()):
    patterns = list(DEFAULT_CTA_PATTERNS) + list(extra_patterns)
    return re.compile(
        r"^(?:" + "|".join(patterns) + r")[\s!.…→↗]*$", re.IGNORECASE
    )


def _absolutize(url, base_url):
    url = (url or "").strip()
    if not url or url.startswith(("http://", "https://", "mailto:", "#", "data:")):
        return url
    return urljoin(base_url, url)


def pick_srcset_candidate(srcset):
    """Return the widest URL from a srcset string.

    Parses via non-whitespace runs so URLs containing commas (e.g. Cloudinary
    transform params) are not shredded.
    """
    if not srcset:
        return None
    width_candidates = re.findall(r"(\S+)\s+(\d+)w", srcset)
    if width_candidates:
        return max(width_candidates, key=lambda c: int(c[1]))[0]
    density_candidates = re.findall(r"(\S+)\s+\d+(?:\.\d+)?x", srcset)
    if density_candidates:
        return density_candidates[-1]
    first = srcset.split(",")[0].strip().split()
    return first[0] if first else None


def _is_placeholder_src(src):
    if not src:
        return True
    if src.startswith("data:"):
        # Tiny inline placeholders (blur-up/1px gifs); real inline images are rare.
        return len(src) < 512
    return False


def _resolve_img(img, base_url):
    """Resolve a real absolute src for an <img>, keeping alt/title/width/height."""
    src = (img.get("src") or "").strip()
    if _is_placeholder_src(src):
        for attr in LAZY_SRC_ATTRS:
            candidate = (img.get(attr) or "").strip()
            if candidate and not _is_placeholder_src(candidate):
                src = candidate
                break
    if _is_placeholder_src(src):
        candidate = pick_srcset_candidate(
            img.get("srcset") or img.get("data-srcset") or ""
        )
        if candidate:
            src = candidate
    if _is_placeholder_src(src):
        img.decompose()
        return
    src = _absolutize(src, base_url)
    if not src.startswith(("http://", "https://", "data:")):
        img.decompose()
        return

    kept = {"src": src}
    for attr in ("alt", "title"):
        if img.get(attr):
            kept[attr] = img[attr]
    for attr in ("width", "height"):
        value = str(img.get(attr) or "")
        if value.isdigit() and int(value) > 1:
            kept[attr] = value
    # 1px trackers
    if kept.get("width") is None and str(img.get("width")) in ("0", "1"):
        img.decompose()
        return
    img.attrs = kept


def _resolve_picture(picture, base_url):
    """Replace <picture> with a plain resolved <img>."""
    img = picture.find("img")
    if img is None:
        source = picture.find("source")
        candidate = None
        if source is not None:
            candidate = pick_srcset_candidate(
                source.get("srcset") or source.get("data-srcset") or ""
            ) or source.get("src")
        if not candidate:
            picture.decompose()
            return
        img = BeautifulSoup("", "html.parser").new_tag("img", src=candidate)
    else:
        img = img.extract()
        if _is_placeholder_src((img.get("src") or "").strip()):
            source = picture.find("source")
            if source is not None:
                candidate = pick_srcset_candidate(
                    source.get("srcset") or source.get("data-srcset") or ""
                )
                if candidate:
                    img["src"] = candidate
    picture.replace_with(img)
    _resolve_img(img, base_url)


def _replace_video(video, base_url):
    """Replace <video> with poster image + link; drop it if there is nothing usable."""
    src = (video.get("src") or "").strip()
    if not src:
        source = video.find("source")
        if source is not None:
            src = (source.get("src") or "").strip()
    poster = (video.get("poster") or "").strip()

    soup = BeautifulSoup("", "html.parser")
    replacements = []
    if poster:
        img = soup.new_tag("img", src=_absolutize(poster, base_url))
        img["alt"] = "Video preview"
        replacements.append(img)
    if src and not src.startswith(("blob:", "data:")):
        p = soup.new_tag("p")
        a = soup.new_tag("a", href=_absolutize(src, base_url))
        a.string = "▶ Watch video"
        p.append(a)
        replacements.append(p)

    if not replacements:
        video.decompose()
        return
    for piece in reversed(replacements):
        video.insert_after(piece)
    video.decompose()


def _filter_iframe(iframe, base_url):
    src = _absolutize(iframe.get("src") or "", base_url)
    host = urlparse(src).netloc.lower()
    if src.startswith("https://") and any(
        host == h or host.endswith("." + h) for h in IFRAME_HOSTS
    ):
        kept = {"src": src, "allowfullscreen": ""}
        for attr in ("width", "height"):
            value = str(iframe.get(attr) or "")
            if value.isdigit():
                kept[attr] = value
        iframe.attrs = kept
        return
    iframe.decompose()


def _chrome_token(tag):
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    # Skip Tailwind variant/arbitrary-value tokens (contain ':', '[' or '(').
    # e.g. scroll-mt-[calc(var(--toc-button-h))] would otherwise register as
    # "toc" chrome and delete real headings.
    classes = [c for c in classes if not re.search(r"[:\[\(]", c)]
    return " ".join(classes) + " " + (tag.get("id") or "")


def _remove_related_sections(container):
    """Remove 'Related articles'-style headings and everything after them."""
    for heading in container.find_all(["h2", "h3", "h4", "p", "strong"]):
        if heading.decomposed or heading.parent is None:
            continue
        text = heading.get_text(" ", strip=True).lower().rstrip(":")
        if text in RELATED_HEADING_MARKERS:
            for sibling in list(heading.next_siblings):
                if isinstance(sibling, Tag):
                    sibling.decompose()
                else:
                    sibling.extract()
            heading.decompose()


def _normalize_text(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _collapse_duplicate_images(container):
    """Collapse adjacent <img> siblings with the same src (lazy-load pairs)."""
    for img in list(container.find_all("img")):
        if img.decomposed or img.parent is None:
            continue
        prev = img.previous_sibling
        while isinstance(prev, NavigableString) and not prev.strip():
            prev = prev.previous_sibling
        if isinstance(prev, Tag) and prev.name == "img" and prev.get("src") == img.get("src"):
            img.decompose()


def _drop_short_paragraph_runs(container, min_run=4, max_len=30):
    """Drop runs of >= min_run consecutive short, punctuation-free paragraphs.

    These are the residue of interactive UI mockups/widgets after unwrapping;
    real prose paragraphs are longer or end in sentence punctuation.
    """
    run = []

    def flush():
        if len(run) >= min_run:
            for p in run:
                p.decompose()
        run.clear()

    for child in list(container.children):
        if isinstance(child, Tag) and child.name == "p":
            text = child.get_text(" ", strip=True)
            if len(text) <= max_len and not re.search(r"[.!?…]$", text) and not child.find("img"):
                run.append(child)
                continue
        flush()
    flush()


def clean_article_html(
    container,
    base_url,
    *,
    title=None,
    strip_selectors=(),
    extra_cta_patterns=(),
):
    """Normalize a scraped article container into reader-safe HTML.

    Returns the inner HTML (no wrapper element) with structure preserved:
    headings, lists, figures/captions, tables, and code blocks survive; site
    chrome, CTA buttons, share links, and lazy-load indirection do not.
    """
    if container is None:
        return ""
    container = copy.copy(container)
    cta_re = _compile_cta_re(extra_cta_patterns)

    for tag in container.find_all(REMOVE_TAGS):
        tag.decompose()

    # HTML comments (framework hydration markers like <!--[0--> leak as text).
    for comment in container.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Decorative/interactive widgets: anything aria-hidden, presentational, or
    # explicitly marked non-article (Tailwind's .not-prose). Real images inside
    # such blocks (custom figure embeds) are preserved.
    for el in container.select('[aria-hidden="true"], [role="presentation"], [role="none"]'):
        el.decompose()
    for el in container.select('.not-prose, div[role="img"], span[role="img"]'):
        if el.decomposed or el.parent is None:
            continue
        preserved = [
            n
            for n in el.find_all(["figure", "pre", "table", "blockquote"])
            if not n.find_parent(["figure", "pre", "table", "blockquote"])
        ]
        if not preserved:
            preserved = el.find_all(["img", "picture"])
        for piece in preserved:
            el.insert_before(piece.extract())
        el.decompose()

    # Site chrome by class/id token, plus caller-specified selectors.
    for tag in container.find_all(True):
        if tag.decomposed or tag.parent is None:
            continue
        if CHROME_TOKEN_RE.search(_chrome_token(tag)):
            tag.decompose()
    for selector in strip_selectors:
        for el in container.select(selector):
            el.decompose()

    _remove_related_sections(container)

    # Share links by URL.
    for a in container.find_all("a", href=True):
        href = a["href"].lower()
        if any(marker in href for marker in SHARE_URL_MARKERS):
            a.decompose()

    # Media normalization.
    for picture in container.find_all("picture"):
        _resolve_picture(picture, base_url)
    for img in container.find_all("img"):
        if not img.decomposed and img.parent is not None:
            _resolve_img(img, base_url)
    for video in container.find_all("video"):
        _replace_video(video, base_url)
    for iframe in container.find_all("iframe"):
        _filter_iframe(iframe, base_url)
    _collapse_duplicate_images(container)

    # Headings: demote h1, drop the one duplicating the item title, and turn
    # same-page anchor links inside headings into plain text.
    title_norm = _normalize_text(title) if title else None
    for h1 in container.find_all("h1"):
        h1.name = "h2"
    seen_first_heading = False
    for heading in container.find_all(["h2", "h3", "h4", "h5", "h6"]):
        if heading.decomposed or heading.parent is None:
            continue
        for a in heading.find_all("a", href=True):
            if a["href"].startswith("#"):
                a.unwrap()
        if (
            not seen_first_heading
            and title_norm
            and _normalize_text(heading.get_text(" ", strip=True)) == title_norm
        ):
            heading.decompose()
            continue
        seen_first_heading = True

    # Allowlist pass: prune attributes, absolutize URLs, unwrap everything else.
    for tag in list(container.find_all(True)):
        if tag.decomposed or tag.parent is None:
            continue
        name = tag.name
        if name in ALLOWED_TAGS:
            allowed_attrs = ALLOWED_TAGS[name]
            tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_attrs}
            if name == "a" and tag.get("href"):
                tag["href"] = _absolutize(tag["href"].strip(), base_url)
            if name == "q" and tag.get("cite"):
                tag["cite"] = _absolutize(tag["cite"], base_url)
            if name == "audio" and tag.get("src"):
                tag["src"] = _absolutize(tag["src"], base_url)
        elif name != "img":
            tag.unwrap()

    # Invalid <a><p>…</p></a> nesting (block content hoisted into links upstream).
    for a in container.find_all("a"):
        for block in a.find_all(["p", "div", "h2", "h3", "h4"]):
            block.unwrap()

    # Stray top-level text nodes left behind by unwrapping: drop date/reading
    # time leftovers, wrap real prose in <p>.
    helper_soup = BeautifulSoup("", "html.parser")
    for child in list(container.children):
        if isinstance(child, NavigableString):
            text = child.strip()
            if not text:
                continue
            if DATE_ONLY_RE.match(text) or READING_TIME_RE.match(text):
                child.extract()
                continue
            p = helper_soup.new_tag("p")
            child.wrap(p)

    # Date-only / reading-time paragraphs anywhere.
    for p in container.find_all("p"):
        text = p.get_text(" ", strip=True)
        if text and (DATE_ONLY_RE.match(text) or READING_TIME_RE.match(text)):
            p.decompose()

    # Backstop for UI widgets that unwrapped into fragments: a run of several
    # consecutive tiny non-sentence paragraphs is interface text, not prose.
    _drop_short_paragraph_runs(container)

    # Standalone CTA links, bare table-of-contents fragment links, and
    # standalone repeats of links already present in prose.
    inline_hrefs = set()
    for p in container.find_all(["p", "li", "figcaption", "td", "th"]):
        own_text = p.get_text(" ", strip=True)
        for a in p.find_all("a", href=True):
            if a.get_text(" ", strip=True) != own_text:
                inline_hrefs.add(a["href"])

    seen_standalone = set()
    for a in list(container.find_all("a", href=True)):
        if a.decomposed or a.parent is None:
            continue
        parent = a.parent
        holder = None
        if parent is container:
            holder = a
        elif (
            parent.name in ("p", "li", "figure")
            and parent.parent is container
            and parent.get_text(" ", strip=True) == a.get_text(" ", strip=True)
            and not parent.find("img")
        ):
            holder = parent
        if holder is None:
            continue
        text = a.get_text(" ", strip=True)
        href = a["href"]
        if (
            href.startswith("#")
            or cta_re.match(text)
            or href in inline_hrefs
            or (href, text) in seen_standalone
            or (not text and not a.find("img"))
        ):
            holder.decompose()
            continue
        seen_standalone.add((href, text))

    # Empty-element pruning (two passes for nesting). A lone <br> does not
    # count as content, so <p><br/></p> spacers are dropped too.
    for _ in range(2):
        for tag in container.find_all(
            ["p", "a", "li", "ul", "ol", "figure", "figcaption", "blockquote",
             "em", "strong", "b", "i", "h2", "h3", "h4", "h5", "h6", "audio"]
        ):
            if tag.decomposed or tag.parent is None:
                continue
            if tag.name == "audio":
                if not tag.get("src"):
                    tag.decompose()
                continue
            if not tag.get_text(strip=True) and not tag.find(["img", "iframe", "audio"]):
                tag.decompose()

    parts = []
    for child in container.children:
        if isinstance(child, NavigableString):
            if child.strip():
                parts.append(str(child))
            continue
        parts.append(str(child))
    html = "\n".join(parts)
    html = CONTROL_CHAR_RE.sub("", html)
    html = html.replace("]]>", "]]&gt;")
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def extract_summary(soup, container=None, title=None, min_length=60):
    """Item description: og:description, then meta description, then first real paragraph."""
    for finder in (
        lambda: soup.find("meta", property="og:description"),
        lambda: soup.find("meta", attrs={"name": "description"}),
        lambda: soup.find("meta", attrs={"name": "twitter:description"}),
    ):
        meta = finder()
        content = (meta.get("content") or "").strip() if meta else ""
        if len(content) > 20:
            return re.sub(r"\s+", " ", content)

    title_norm = _normalize_text(title) if title else None
    if container is not None:
        for p in container.find_all("p"):
            text = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
            if len(text) < min_length:
                continue
            if DATE_ONLY_RE.match(text):
                continue
            if title_norm and _normalize_text(text) == title_norm:
                continue
            return text
    return (title or "").strip()


def first_image_url(content_html):
    if not content_html:
        return None
    match = re.search(r'<img[^>]+src="([^"]+)"', content_html)
    if match:
        url = match.group(1)
        if url.startswith(("http://", "https://")):
            return url
    return None


def _image_mime(url):
    path = urlparse(url).path.lower()
    for ext, mime in (
        (".png", "image/png"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
        (".avif", "image/avif"),
        (".svg", "image/svg+xml"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
    ):
        if path.endswith(ext):
            return mime
    return "image/jpeg"


def build_feed(
    *,
    title,
    description,
    site_url,
    feed_url,
    items,
    language="en",
    author=None,
    max_items=DEFAULT_MAX_ITEMS,
):
    """Build a FeedGenerator from item dicts.

    Each item dict: title, link, date (aware datetime), and optionally
    description, content_html, category, author.

    Items are sorted newest-first and capped at ``max_items``. lastBuildDate is
    pinned to the newest item so output bytes are stable when nothing changed.
    """
    fg = FeedGenerator()
    fg.load_extension("media")
    fg.load_extension("dc")
    fg.title(title)
    fg.description(description)
    fg.language(language)
    if author:
        fg.author(author)
    # Self link FIRST, site link LAST: feedgen sets the channel <link> to the
    # most recently added href, so this order keeps <link> = site URL while
    # still emitting <atom:link rel="self">.
    fg.link(href=feed_url, rel="self", type="application/rss+xml")
    fg.link(href=site_url, rel="alternate")

    epoch = datetime.min.replace(tzinfo=pytz.UTC)
    items = sorted(items, key=lambda a: a.get("date") or epoch, reverse=True)
    if max_items:
        items = items[:max_items]

    if items and items[0].get("date"):
        fg.lastBuildDate(items[0]["date"])

    for art in items:
        fe = fg.add_entry(order="append")
        fe.title(art["title"])
        fe.link(href=art["link"])
        fe.guid(art["link"], permalink=True)

        summary = (art.get("description") or art["title"]).strip() or art["title"]
        fe.description(summary, isSummary=True)

        content_html = art.get("content_html")
        if content_html:
            fe.content(content_html, type="CDATA")
            lead_image = art.get("image") or first_image_url(content_html)
            if lead_image:
                fe.media.content(
                    url=lead_image,
                    medium="image",
                    type=_image_mime(lead_image),
                    group=None,
                )
        if art.get("date"):
            fe.published(art["date"])
        if art.get("category"):
            fe.category(term=art["category"])
        if art.get("author"):
            # feedgen only emits RSS <author> when an email is present, so
            # name-only bylines go out as dc:creator (what readers display).
            fe.dc.dc_creator(art["author"])
    return fg


def save_feed(fg, feed_path):
    """Write the feed only when its bytes changed; returns True if written."""
    feed_path = Path(feed_path)
    feed_path.parent.mkdir(exist_ok=True)
    xml = fg.rss_str(pretty=True)
    if feed_path.exists() and feed_path.read_bytes() == xml:
        logger.info(f"Feed unchanged, skipping write: {feed_path}")
        return False
    feed_path.write_bytes(xml)
    logger.info(f"Feed saved: {feed_path}")
    return True


def load_cached_entries(feed_path):
    """Read a previously generated feed as (items, cache_by_link).

    cache_by_link maps link -> {description, content_html} so generators can
    skip refetching articles they already captured.

    Note for --force implementations: the returned items also carry their
    cached content_html/description. A force rebuild that keeps item skeletons
    must null those fields explicitly, not just ignore the cache dict.
    """
    items, cache = [], {}
    feed_path = Path(feed_path)
    if not feed_path.exists():
        return items, cache
    try:
        soup = BeautifulSoup(feed_path.read_text(encoding="utf-8"), "xml")
        for item in soup.find_all("item"):
            link_tag = item.find("link")
            if not link_tag or not link_tag.text:
                continue
            link = link_tag.text.strip()
            title = item.find("title").text.strip() if item.find("title") else link
            desc_tag = item.find("description")
            description = desc_tag.text.strip() if desc_tag and desc_tag.text else title
            content_tag = item.find("content:encoded") or item.find("encoded")
            content_html = content_tag.text if content_tag and content_tag.text else None

            date_obj = None
            pub = item.find("pubDate")
            if pub and pub.text:
                try:
                    date_obj = dateparser.parse(pub.text)
                    if date_obj and date_obj.tzinfo is None:
                        date_obj = date_obj.replace(tzinfo=pytz.UTC)
                except (ValueError, OverflowError):
                    date_obj = None

            cat_tag = item.find("category")
            creator_tag = item.find("dc:creator") or item.find("creator")
            entry = {
                "title": title,
                "link": link,
                "date": date_obj,
                "category": cat_tag.text.strip() if cat_tag and cat_tag.text else None,
                "description": description,
                "content_html": content_html,
                "author": creator_tag.text.strip() if creator_tag and creator_tag.text else None,
            }
            items.append(entry)
            cache[link] = {"description": description, "content_html": content_html}
    except Exception as e:
        logger.warning(f"Failed to load existing feed from {feed_path}: {e}")
    return items, cache
