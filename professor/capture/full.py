"""Capturing a whole site section by following its own hyperlinks.

`mfp -xy --full <url>` treats the page like the root of a site: fetch it,
pull every link it points to, and keep going -- a link found on page two is
walked exactly like one found on page one. There is no separate "recurse"
step; it is the same loop calling itself on whatever it just found, which is
why a visited set rather than a depth counter is what actually stops it. A
finite site runs out of new links on its own; `--max-pages` is the circuit
breaker for one that doesn't (a calendar widget, infinite pagination, a
faceted search).

Everything captured this way lands in one subtopic, named after the origin
page, exactly as if `-xy.<page>` had been typed by hand for every URL found --
so the crawl leaves one ordinary-looking folder behind, not a tree of one-off
topics.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from .fetch import FetchError, fetch
from .paths import slugify

# Extensions that are never worth queuing as a page to capture. Images, styles
# and scripts show up constantly as ordinary <a href> targets (a lightbox link,
# a "view full size" button) and fetching them as pages would just produce
# empty or garbled notes.
_SKIP_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".css", ".js", ".mjs", ".json", ".xml", ".rss", ".atom",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z",
    ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg", ".webm",
    ".woff", ".woff2", ".ttf", ".eot", ".exe", ".dmg",
})

# The real safety valve. Depth is optional and unlimited by default -- the
# visited set is what makes the walk terminate on a normal site -- but nothing
# bounds a pathological one without a hard ceiling on total pages.
MAX_PAGES_DEFAULT = 200


@dataclass
class FullOptions:
    max_depth: int | None = None
    max_pages: int = MAX_PAGES_DEFAULT
    max_tier: str = "T3"


@dataclass
class PageOutcome:
    url: str
    ok: bool
    reason: str | None = None


@dataclass
class FullResult:
    origin_url: str
    pages: list[PageOutcome] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for p in self.pages if p.ok)

    @property
    def failed(self) -> list[PageOutcome]:
        return [p for p in self.pages if not p.ok]


def page_slug(url: str) -> str:
    """A readable subtopic name derived purely from the URL.

    Needs no fetch, so the crawl's destination folder can be resolved before a
    single request goes out. Mirrors capture_slug()'s readable half without
    the hash suffix -- a directory name should read as a name, not an id.
    """
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.").split(":")[0]
    path = parsed.path.strip("/")
    base = f"{host.split('.')[0]}-{path.replace('/', '-')}" if path else host.split(".")[0]
    return slugify(base, max_len=40)


def _same_site(url: str, anchor: str) -> bool:
    return urlparse(url).netloc.removeprefix("www.").lower() == \
        urlparse(anchor).netloc.removeprefix("www.").lower()


_INDEX_SUFFIXES = ("/index.html", "/index.htm", "/index.php")


def _normalize(url: str) -> str:
    """Collapse a URL to the form used for visited-set de-duplication.

    Drops the fragment -- '#section' is the same page, not a new one -- and
    folds an explicit index filename onto its directory, since '/blog/' and
    '/blog/index.html' are the same page on the overwhelming majority of
    sites and a crawl should not capture it twice under two different names.
    """
    stripped = urldefrag(url)[0].rstrip("/")
    lowered = stripped.lower()
    for suffix in _INDEX_SUFFIXES:
        if lowered.endswith(suffix):
            return stripped[: -len(suffix)]
    return stripped


def extract_links(html: str, base_url: str) -> list[str]:
    """Every followable link on the page, made absolute and de-duplicated."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if any(parsed.path.lower().endswith(ext) for ext in _SKIP_EXTENSIONS):
            continue
        normal = _normalize(absolute)
        if normal in seen:
            continue
        seen.add(normal)
        links.append(absolute)
    return links


def crawl(origin_url: str, options: FullOptions, *, on_page) -> FullResult:
    """Breadth-first walk of same-site pages reachable from origin_url.

    `on_page(url, depth, fetched, error)` fires once per attempt -- `fetched`
    is a FetchResult on success, `error` a FetchError on failure, always one of
    the two. The crawler only decides what to fetch next; persisting a page is
    entirely the caller's business, same division of labour as fetch()'s
    on_tier callback.
    """
    origin_norm = _normalize(origin_url)
    visited: set[str] = {origin_norm}
    queue: deque[tuple[str, int]] = deque([(origin_url, 0)])
    result = FullResult(origin_url=origin_url)
    site_anchor = origin_url

    while queue and len(result.pages) < options.max_pages:
        url, depth = queue.popleft()
        try:
            fetched = fetch(url, max_tier=options.max_tier)
        except FetchError as exc:
            result.pages.append(PageOutcome(url, ok=False, reason=exc.reason))
            on_page(url, depth, None, exc)
            continue

        # The origin URL can redirect (bare domain -> www, http -> https);
        # every later same-site check anchors on where it actually landed.
        if depth == 0:
            site_anchor = fetched.final_url

        result.pages.append(PageOutcome(url, ok=True))
        on_page(url, depth, fetched, None)

        if options.max_depth is not None and depth >= options.max_depth:
            continue
        if len(result.pages) >= options.max_pages:
            break

        for link in extract_links(fetched.html, fetched.final_url):
            if not _same_site(link, site_anchor):
                continue
            normal = _normalize(link)
            if normal in visited:
                continue
            if len(visited) >= options.max_pages:
                break
            visited.add(normal)
            queue.append((link, depth + 1))

    return result
