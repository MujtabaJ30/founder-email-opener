"""Read a small set of company website pages from a trusted URL on the CLI."""

import argparse
import json
import re
from dataclasses import dataclass, field
from heapq import heappop, heappush
from ipaddress import ip_address
from itertools import count
import socket
from time import monotonic
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


MAX_PAGE_BYTES = 2_500_000
MAX_PAGE_CHARS = 16_000
MAX_DEPTH = 2
FILE_SUFFIX = re.compile(r"\.(?:pdf|zip|png|jpe?g|gif|svg|webp|mp[34]|xml|ico)$", re.I)
SKIP_PATH = re.compile(
    r"(?:^|/)(?:login|log-in|signin|sign-in|signup|sign-up|register|logout|"
    r"checkout|contact|privacy|terms|search|feed|rss|tag|category|cart)(?:/|$)", re.I
)
LINK_SIGNALS = (
    (8, r"\b(?:annual|year in review|retrospective|roadmap|behind the scenes|postmortem|incident report)\b"),
    (6, r"\b(?:founder|migration|onboarding|customer support|how we|build data|operating)\b"),
    (5, r"\b(?:about|company|team|press)\b"),
    (4, r"\b(?:blog|news|archive|changelog|journal|updates)\b"),
    (3, r"\b(?:pricing|help|support|docs|launch|analytics|case stud(?:y|ies)|import|getting started)\b"),
)
SECTION_NAMES = {"blog", "news", "archive", "changelog", "journal", "updates"}
SITEMAP_ALTERNATIVE_SECTIONS = SECTION_NAMES | {"compare", "comparison-guides"}
WORKFLOW_PATH = re.compile(r"\b(?:import|export|migrat\w*|onboard\w*|activation|cancell?ation|getting started)\b")


class RetrievalError(Exception):
    pass


class PublicSiteAdapter(requests.adapters.HTTPAdapter):
    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, tls = super().build_connection_pool_key_attributes(request, verify, cert)
        hostname = host["host"]
        port = host["port"] or (443 if host["scheme"] == "https" else 80)
        try:
            addresses = [ip_address(info[4][0]) for info in socket.getaddrinfo(
                hostname, port, type=socket.SOCK_STREAM
            )]
        except OSError as error:
            raise RetrievalError("Could not resolve a public address for this website.") from error
        if not addresses or any(not address.is_global or address.is_multicast for address in addresses):
            raise RetrievalError("This page does not resolve exclusively to public addresses.")
        # Use the checked address for the socket and keep the site name for HTTP and TLS.
        host["host"] = str(min(addresses, key=lambda address: address.version))
        request.headers["Host"] = hostname
        if host["scheme"] == "https":
            tls.update(assert_hostname=hostname, server_hostname=hostname)
        return host, tls


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: dict[str, str]
    primary_navigation: set[str] = field(default_factory=set)


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
    ):
        raise ValueError("Use an HTTP or HTTPS website URL without credentials or a custom port.")
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.hostname.lower(), path, "", ""))


def company_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def page_key(url: str) -> tuple[str, str]:
    return company_host(url), urlsplit(url).path.rstrip("/") or "/"


def compact(text: str) -> str:
    return " ".join(text.split())


def parse_page(url: str, html: bytes) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    title = compact(soup.title.get_text(" ")) if soup.title else ""
    links = {}
    primary_navigation = set()
    for anchor in soup.select("a[href]"):
        try:
            target = canonical_url(urljoin(url, anchor["href"]))
        except ValueError:
            continue
        path = unquote(urlsplit(target).path)
        if page_key(target) == page_key(url) or company_host(target) != company_host(url):
            continue
        if SKIP_PATH.search(path) or FILE_SUFFIX.search(path):
            continue
        label = compact(anchor.get_text(" ") or anchor.get("aria-label", ""))
        if label and "/" not in path.strip("/") and anchor.find_parent(["header", "nav"]):
            primary_navigation.add(target)
        if target not in links or link_score(target, label) > link_score(target, links[target]):
            links[target] = label

    for node in soup.select("script, style, noscript, svg, nav, footer, form, [hidden], [aria-hidden=true]"):
        node.decompose()

    content = soup.find("main") or soup.find("article") or soup.body or soup
    blocks = []
    seen = set()
    for node in content.select("h1,h2,h3,h4,p,li,dt,dd,tr,summary,blockquote"):
        if node.find_parent(["li", "tr", "blockquote"]):
            continue
        line = compact(node.get_text(" ", strip=True))
        key = line.casefold()
        if line and key not in seen:
            seen.add(key)
            blocks.append(line)
    text = "\n".join(blocks)
    if len(text) < 250:
        text = compact(content.get_text(" ", strip=True))
    return Page(url, title, text[:MAX_PAGE_CHARS], links, primary_navigation)


def link_score(url: str, label: str) -> int:
    path = unquote(urlsplit(url).path)
    segments = path.strip("/").lower().split("/")
    within_section = len(segments) > 1 and segments[0] in SECTION_NAMES
    # An article does not earn relevance just because its URL starts with /blog.
    path_clue = "/".join(segments[1:]) if within_section else path
    words = re.sub(r"[^\w ]|_", " ", f"{path_clue} {label[:180]}").lower()
    scores = [weight for weight, pattern in LINK_SIGNALS if re.search(pattern, words)]
    return max(scores, default=1 if within_section else 0)


def read_document(
    session: requests.Session, url: str, host: str, deadline: float, content_types: tuple[str, ...]
) -> tuple[str, bytes, str]:
    for _ in range(5):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RetrievalError("Website reading reached its time limit.")
        with session.get(url, stream=True, allow_redirects=False, timeout=min(remaining, 5)) as response:
            if response.is_redirect:
                url = canonical_url(urljoin(url, response.headers["Location"]))
                if company_host(url) != host:
                    raise RetrievalError("Redirect led to a different host. Use the destination website URL.")
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if not any(kind in content_type for kind in content_types):
                raise RetrievalError(f"Unexpected document type: {content_type or 'no content type'}.")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=16_384):
                if monotonic() > deadline:
                    raise RetrievalError("Website reading reached its time limit.")
                body.extend(chunk)
                if len(body) > MAX_PAGE_BYTES:
                    raise RetrievalError("Page exceeds the HTML size limit.")
            return url, bytes(body), content_type
    raise RetrievalError("Too many redirects.")


def read_page(session: requests.Session, url: str, host: str, deadline: float) -> Page:
    url, body, content_type = read_document(
        session, url, host, deadline, ("text/html", "application/xhtml+xml", "text/markdown")
    )
    if content_type.startswith("text/markdown"):
        text = body.decode("utf-8", errors="replace").strip()
        title = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        return Page(url, title.group(1).strip() if title else "", text[:MAX_PAGE_CHARS], {})
    return parse_page(url, body)


def sitemap_workflow(
    session: requests.Session, origin: str, host: str, deadline: float, seen: set[tuple[str, str]]
) -> str | None:
    # Read the main sitemap and, if it is an index, its first same-host sitemap.
    address = urljoin(origin, "/sitemap.xml")
    for _ in range(2):
        try:
            _, body, _ = read_document(session, address, host, deadline, ("xml", "text/plain"))
            xml = body.decode("utf-8-sig")
            if "<!DOCTYPE" in xml:
                return None
            root = ElementTree.fromstring(xml)
        except (requests.RequestException, ValueError, RetrievalError, ElementTree.ParseError):
            return None

        urls = []
        for entry in root.findall("./*/{*}loc"):
            if not entry.text:
                continue
            try:
                target = canonical_url(entry.text.strip())
            except ValueError:
                continue
            if company_host(target) == host and page_key(target) not in seen:
                urls.append(target)

        kind = root.tag.rsplit("}", 1)[-1]
        if kind == "sitemapindex" and urls:
            address = urls[0]
            continue
        if kind != "urlset":
            return None

        candidates = []
        for target in urls:
            path = unquote(urlsplit(target).path).strip("/").lower()
            section, _, leaf = path.rpartition("/")
            words = re.sub(r"[-_]", " ", leaf)
            if SKIP_PATH.search(path) or FILE_SUFFIX.search(path) or not WORKFLOW_PATH.search(words):
                continue
            # Written setup instructions and specific article titles outrank broad index pages.
            docs = any(part in {"help", "support", "docs"} for part in section.split("/"))
            candidates.append((not docs, -min(len(words.split()), 6), path.count("/"), target))
        return min(candidates)[-1] if candidates else None
    return None


def website_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    elif "://" not in url:
        url = f"https://{url}"
    return canonical_url(url)


def collect(url: str, max_pages: int, timeout: float, fetch=None, *, public_only: bool = False) -> dict:
    url = website_url(url)
    host = company_host(url)
    started = monotonic()
    deadline = started + timeout
    pages, failures, queue = [], [], []
    seen_urls, seen_text = set(), set()
    sequence = count()
    heappush(queue, (0, next(sequence), 0, url))
    attempts = 0
    origin = url
    with requests.Session() as session:
        if public_only:
            session.trust_env = False
            adapter = PublicSiteAdapter()
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        session.headers["User-Agent"] = "OpenerResearch/0.1 (company website reader)"
        reader = fetch or read_page
        while queue and attempts < max_pages and monotonic() < deadline:
            priority, _, depth, link = heappop(queue)
            key = page_key(link)
            if key in seen_urls:
                continue
            section = urlsplit(link).path.strip("/").split("/", 1)[0]
            now = monotonic()
            if (
                max_pages >= 4
                and attempts == max_pages - 1
                and priority > -6
                and section in SITEMAP_ALTERNATIVE_SECTIONS
                and deadline - now > 2
            ):
                candidate = sitemap_workflow(session, origin, host, min(deadline - 1, now + 2), seen_urls)
                if candidate:
                    link, key = candidate, page_key(candidate)
            seen_urls.add(key)
            attempts += 1
            try:
                page = reader(session, link, host, deadline)
            except (requests.RequestException, ValueError, RetrievalError) as error:
                failures.append({"url": link, "reason": str(error)})
                continue
            seen_urls.add(page_key(page.url))
            if depth == 0:
                origin = page.url
            if len(page.text) < 100:
                failures.append({"url": page.url, "reason": "Too little readable HTML text."})
            elif page.text not in seen_text:
                seen_text.add(page.text)
                pages.append({"url": page.url, "title": page.title, "text": page.text})
            if depth < MAX_DEPTH:
                added_navigation_candidate = False
                for target, label in page.links.items():
                    score = link_score(target, label)
                    if (
                        depth == 0
                        and not score
                        and not added_navigation_candidate
                        and target in page.primary_navigation
                    ):
                        # Make room for one product link described in the site's own terms.
                        score = 6
                        added_navigation_candidate = True
                    if score:
                        heappush(queue, (-score, next(sequence), depth + 1, target))
    return {
        "input_url": url,
        "pages": pages,
        "failures": failures,
        "pages_attempted": attempts,
        "seconds": round(monotonic() - started, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--max-pages", type=int, choices=range(1, 13), default=7, metavar="1-12")
    parser.add_argument("--timeout", type=float, default=22, help="Time budget in seconds.")
    args = parser.parse_args()
    if not 0 < args.timeout <= 120:
        parser.error("--timeout must be above zero and at most 120 seconds")
    try:
        result = collect(args.url, args.max_pages, args.timeout)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["pages"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
