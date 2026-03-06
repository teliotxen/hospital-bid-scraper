"""
Hospital bid board scraper.

Two main patterns driven by the CSV configuration:
  A) table_tag  → <table class="...">  (direct table)
  B) div_tag    → container element → inner <table> | <li> | <a>
  C) neither    → skip / error
"""

from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urljoin

import chardet
import httpx
from bs4 import BeautifulSoup, Tag

from app.models import BidItem, HospitalBids, HospitalInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DATE_RE = re.compile(
    r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}"          # 2024.03.06 / 2024-03-06
    r"|\d{4}년\s*\d{1,2}월\s*\d{1,2}일"           # 2024년 3월 6일
)

# Minimum characters to consider a cell text a meaningful title
_MIN_TITLE_LEN = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_encoding(raw: bytes) -> str:
    result = chardet.detect(raw)
    enc = result.get("encoding") or "utf-8"
    # Korean sites often mislabelled; prefer euc-kr when chardet guesses iso-8859
    if enc.lower().startswith("iso-8859"):
        enc = "euc-kr"
    return enc


def _make_soup(raw: bytes) -> BeautifulSoup:
    enc = _detect_encoding(raw)
    try:
        html = raw.decode(enc, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = raw.decode("utf-8", errors="replace")
    return BeautifulSoup(html, "lxml")


def _abs_url(href: Optional[str], base: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith("javascript") or href == "#":
        return None
    return urljoin(base, href)


def _find_date(text: str) -> Optional[str]:
    m = DATE_RE.search(text)
    return m.group(0) if m else None


def _text(tag: Tag) -> str:
    return tag.get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# Row / item extraction
# ---------------------------------------------------------------------------

def _extract_from_tr(tr: Tag, base_url: str) -> Optional[BidItem]:
    """Extract bid item from a <tr> row."""
    cells = tr.find_all(["td", "th"])
    if not cells:
        return None

    title: Optional[str] = None
    url: Optional[str] = None
    date: Optional[str] = None

    # Title: prefer the cell that contains an <a> tag with text
    for cell in cells:
        a = cell.find("a", href=True)
        if a:
            candidate = _text(a)
            if len(candidate) >= _MIN_TITLE_LEN:
                title = candidate
                url = _abs_url(a["href"], base_url)
                break

    # Fallback title: first non-empty cell text
    if not title:
        for cell in cells:
            t = _text(cell)
            if len(t) >= _MIN_TITLE_LEN:
                title = t
                break

    # Date: scan every cell
    for cell in cells:
        d = _find_date(_text(cell))
        if d:
            date = d
            break

    if not title:
        return None
    return BidItem(title=title, date=date, url=url)


def _extract_from_li(li: Tag, base_url: str) -> Optional[BidItem]:
    """Extract bid item from a <li> element."""
    a = li.find("a", href=True)
    title = _text(a) if a else _text(li)
    if len(title) < _MIN_TITLE_LEN:
        return None
    url = _abs_url(a["href"] if a else None, base_url)
    date = _find_date(_text(li))
    return BidItem(title=title, date=date, url=url)


def _extract_from_a(a: Tag, base_url: str) -> Optional[BidItem]:
    """Extract bid item from a standalone <a> element."""
    title = _text(a)
    if len(title) < _MIN_TITLE_LEN:
        return None
    url = _abs_url(a.get("href"), base_url)
    parent = a.parent
    date = _find_date(_text(parent)) if parent else None
    return BidItem(title=title, date=date, url=url)


# ---------------------------------------------------------------------------
# Core parse strategies
# ---------------------------------------------------------------------------

def _parse_table(table: Tag, base_url: str) -> List[BidItem]:
    """Parse all <tr> rows of a table, skipping pure header rows."""
    items: List[BidItem] = []
    rows = table.find_all("tr")
    for tr in rows:
        # Skip header rows (all th cells)
        if tr.find("th") and not tr.find("td"):
            continue
        item = _extract_from_tr(tr, base_url)
        if item:
            items.append(item)
    return items


def _parse_list(container: Tag, base_url: str) -> List[BidItem]:
    """Parse <li> items inside a container."""
    items: List[BidItem] = []
    for li in container.find_all("li"):
        item = _extract_from_li(li, base_url)
        if item:
            items.append(item)
    return items


def _parse_links(container: Tag, base_url: str) -> List[BidItem]:
    """Fallback: parse all meaningful <a> tags inside a container."""
    items: List[BidItem] = []
    seen_urls: set = set()
    for a in container.find_all("a", href=True):
        item = _extract_from_a(a, base_url)
        if item and item.url not in seen_urls:
            seen_urls.add(item.url or "")
            items.append(item)
    return items


# ---------------------------------------------------------------------------
# Pattern A: table_tag → direct <table class="...">
# ---------------------------------------------------------------------------

def _scrape_by_table_tag(soup: BeautifulSoup, table_tag: str, base_url: str) -> List[BidItem]:
    classes = table_tag.split()
    table = soup.find("table", class_=classes)
    if not table:
        # Try matching any single class
        for cls in classes:
            table = soup.find("table", class_=cls)
            if table:
                break
    if not table:
        return []
    return _parse_table(table, base_url)


# ---------------------------------------------------------------------------
# Pattern B: div_tag → container → detect inner structure
# ---------------------------------------------------------------------------

def _find_container(soup: BeautifulSoup, div_tag: str) -> Optional[Tag]:
    classes = div_tag.split()
    for tag_name in ("div", "ul", "section", "tbody", "table", "article"):
        el = soup.find(tag_name, class_=classes)
        if el:
            return el
    # Fallback: any element
    return soup.find(class_=classes)


def _scrape_by_div_tag(soup: BeautifulSoup, div_tag: str, base_url: str) -> List[BidItem]:
    # Special case: entire body
    if div_tag.strip().lower() == "body":
        container = soup.find("body")
    else:
        container = _find_container(soup, div_tag)

    if not container:
        return []

    # Prefer table inside container
    table = container.find("table")
    if table:
        items = _parse_table(table, base_url)
        if items:
            return items

    # Try list items
    if container.find("li"):
        items = _parse_list(container, base_url)
        if items:
            return items

    # Fallback: any meaningful links
    return _parse_links(container, base_url)


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_html(html: bytes, hospital: HospitalInfo) -> List[BidItem]:
    soup = _make_soup(html)
    base_url = hospital.url

    # Pattern A: table_tag takes priority
    if hospital.table_tag:
        items = _scrape_by_table_tag(soup, hospital.table_tag, base_url)
        if items:
            return items

    # Pattern B: div_tag
    if hospital.div_tag:
        items = _scrape_by_div_tag(soup, hospital.div_tag, base_url)
        if items:
            return items

    # Last resort: try any table on the page
    for table in soup.find_all("table"):
        items = _parse_table(table, base_url)
        if len(items) >= 2:
            return items

    return []


# ---------------------------------------------------------------------------
# Async fetch + parse
# ---------------------------------------------------------------------------

async def fetch_hospital_bids(client: httpx.AsyncClient, hospital: HospitalInfo) -> HospitalBids:
    # No selectors defined → known error
    if not hospital.table_tag and not hospital.div_tag:
        return HospitalBids(
            hospital=hospital.hospital,
            region=hospital.region,
            source_url=hospital.url,
            bids=[],
            error="게시판 선택자 정보 없음 (로드 안됨)",
        )

    try:
        response = await client.get(hospital.url, headers=BROWSER_HEADERS, follow_redirects=True)
        response.raise_for_status()
        raw = response.content
    except httpx.TimeoutException:
        return HospitalBids(
            hospital=hospital.hospital,
            region=hospital.region,
            source_url=hospital.url,
            bids=[],
            error="요청 타임아웃",
        )
    except Exception as e:
        return HospitalBids(
            hospital=hospital.hospital,
            region=hospital.region,
            source_url=hospital.url,
            bids=[],
            error=str(e),
        )

    try:
        bids = parse_html(raw, hospital)
    except Exception as e:
        return HospitalBids(
            hospital=hospital.hospital,
            region=hospital.region,
            source_url=hospital.url,
            bids=[],
            error="파싱 오류: " + str(e),
        )

    return HospitalBids(
        hospital=hospital.hospital,
        region=hospital.region,
        source_url=hospital.url,
        bids=bids,
        error=None if bids else "입찰 데이터를 찾을 수 없음",
    )
