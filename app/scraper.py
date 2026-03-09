"""
Hospital bid board scraper.

CSV 설정값에 따라 다섯 가지 스크래핑 패턴을 사용합니다.

  A) table_tag 설정  → HTML에서 <table class="..."> 직접 파싱 (18개 병원)
  B) div_tag 설정    → HTML 컨테이너 탐색 후 <table>|<li>|<a> 파싱 (16개 병원)
  C) cmc_api:{boardNo}  → CMC 계열 Vue.js SPA, /api/article/{boardNo} JSON API (2개)
  D) khmc_api:{boardNo} → 경희의료원 JS SPA, /api/article/{boardNo}.do JSON API (1개)
  E) kumc_api:{boardId}  → 고려대 계열 Vue.js SPA, boardNo 조회 후 JSON API (3개)

  선택자 없음 → 오류 반환 (한양대학교병원)
"""

from __future__ import annotations

import re
import ssl
from datetime import datetime
from typing import List, Optional
from urllib.parse import urljoin, urlparse

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
    # CMC 구조: strong.reduce(타이틀) + em.data(날짜)
    strong = li.find("strong", class_="reduce")
    if strong:
        title = _text(strong)
        if len(title) >= _MIN_TITLE_LEN:
            a = li.find("a", href=True)
            url = _abs_url(a["href"] if a else None, base_url)
            em = li.find("em", class_="data")
            date = _find_date(_text(em)) if em else _find_date(_text(li))
            return BidItem(title=title, date=date, url=url)

    # 기존 범용 로직
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

def _is_ssl_error(exc: BaseException) -> bool:
    """SSL 관련 예외인지 재귀적으로 확인합니다."""
    if isinstance(exc, ssl.SSLError):
        return True
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None:
        return _is_ssl_error(cause)
    return "SSL" in str(exc) or "CERTIFICATE" in str(exc).upper()


def _make_insecure_ssl_context() -> ssl.SSLContext:
    """구형 TLS 서명/암호를 허용하는 SSL 컨텍스트를 생성합니다."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    # Python 3.12+ 에서 사용 가능한 레거시 서버 연결 허용
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


# ---------------------------------------------------------------------------
# Pattern C/D/E: API 기반 패턴 공통 상수
# ---------------------------------------------------------------------------

_CMC_API_PREFIX = "cmc_api:"
_KHMC_API_PREFIX = "khmc_api:"
_KUMC_API_PREFIX = "kumc_api:"


def _is_cmc_api(hospital: HospitalInfo) -> bool:
    return bool(hospital.div_tag and hospital.div_tag.startswith(_CMC_API_PREFIX))


def _cmc_board_no(hospital: HospitalInfo) -> str:
    return hospital.div_tag.split(":", 1)[1]  # type: ignore[union-attr]


def _ts_to_date(ts: int) -> str:
    """Unix timestamp(ms) → YYYY.MM.DD"""
    return datetime.fromtimestamp(ts / 1000).strftime("%Y.%m.%d")


async def _fetch_cmc_api(
    client: httpx.AsyncClient, hospital: HospitalInfo
) -> HospitalBids:
    """CMC 계열 병원 JSON API에서 입찰 목록을 가져옵니다."""
    board_no = _cmc_board_no(hospital)
    base = hospital.url.split("/page/")[0]  # https://www.cmcseoul.or.kr
    api_url = f"{base}/api/article/{board_no}?p=1&s=12"
    view_prefix = f"{base}/page/board/tender/"

    try:
        resp = await client.get(api_url, headers=BROWSER_HEADERS, follow_redirects=True)
        resp.raise_for_status()
        articles = resp.json()
    except Exception as e:
        if not _is_ssl_error(e):
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=[],
                error=str(e),
            )
        # SSL 오류 시 보안 레벨을 낮춘 SSL 컨텍스트로 재시도
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), verify=_make_insecure_ssl_context()
            ) as insecure_client:
                resp = await insecure_client.get(
                    api_url, headers=BROWSER_HEADERS, follow_redirects=True
                )
                resp.raise_for_status()
                articles = resp.json()
        except Exception as e2:
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=[],
                error=str(e2),
            )

    bids: List[BidItem] = []
    for article in articles:
        title = article.get("title")
        if not title or len(title) < _MIN_TITLE_LEN:
            continue
        date = None
        for dt_field in ("recruitClosedDt", "createdDt"):
            ts = article.get(dt_field)
            if ts:
                date = _ts_to_date(ts)
                break
        article_no = article.get("articleNo") or article.get("mixedNo")
        url = f"{view_prefix}{article_no}" if article_no else None
        bids.append(BidItem(title=title, date=date, url=url))

    return HospitalBids(
        hospital=hospital.hospital,
        region=hospital.region,
        source_url=hospital.url,
        bids=bids,
        error=None if bids else "입찰 데이터를 찾을 수 없음",
    )


# --- Pattern D: KHMC (경희의료원) ---


def _is_khmc_api(hospital: HospitalInfo) -> bool:
    return bool(hospital.div_tag and hospital.div_tag.startswith(_KHMC_API_PREFIX))


def _khmc_board_no(hospital: HospitalInfo) -> str:
    return hospital.div_tag.split(":", 1)[1]  # type: ignore[union-attr]


async def _fetch_khmc_api(
    client: httpx.AsyncClient, hospital: HospitalInfo
) -> HospitalBids:
    """경희의료원 JSON API에서 입찰 목록을 가져옵니다."""
    board_no = _khmc_board_no(hospital)
    base = hospital.url.split("/kr/")[0]  # https://www.khmc.or.kr
    api_url = f"{base}/api/article/{board_no}.do"
    view_prefix = f"{base}/kr/introduction-tender/view.do?boardNo={board_no}&articleNo="

    async def _do_fetch(c: httpx.AsyncClient) -> list:
        resp = await c.get(
            api_url,
            params={"startIndex": 1, "pageRow": 10},
            headers=BROWSER_HEADERS,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json().get("list", [])

    try:
        articles = await _do_fetch(client)
    except Exception as e:
        if not _is_ssl_error(e):
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=[],
                error=str(e),
            )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), verify=_make_insecure_ssl_context()
            ) as insecure_client:
                articles = await _do_fetch(insecure_client)
        except Exception as e2:
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=[],
                error=str(e2),
            )

    bids: List[BidItem] = []
    for article in articles:
        title = article.get("title")
        if not title or len(title) < _MIN_TITLE_LEN:
            continue
        date = None
        for dt_field in ("createdDt", "modifiedDt"):
            ts = article.get(dt_field)
            if ts:
                date = _ts_to_date(ts)
                break
        article_no = article.get("articleNo")
        url = f"{view_prefix}{article_no}" if article_no else None
        bids.append(BidItem(title=title, date=date, url=url))

    return HospitalBids(
        hospital=hospital.hospital,
        region=hospital.region,
        source_url=hospital.url,
        bids=bids,
        error=None if bids else "입찰 데이터를 찾을 수 없음",
    )


# --- Pattern E: KUMC (고려대 계열) ---


def _is_kumc_api(hospital: HospitalInfo) -> bool:
    return bool(hospital.div_tag and hospital.div_tag.startswith(_KUMC_API_PREFIX))


def _kumc_board_id(hospital: HospitalInfo) -> str:
    return hospital.div_tag.split(":", 1)[1]  # type: ignore[union-attr]


async def _fetch_kumc_api(
    client: httpx.AsyncClient, hospital: HospitalInfo
) -> HospitalBids:
    """고려대 계열 병원 JSON API에서 입찰 목록을 가져옵니다."""
    board_id = _kumc_board_id(hospital)
    parsed = urlparse(hospital.url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    board_info_url = f"{base}/api/board/{board_id}.do"
    view_url = f"{base}/kr/{board_id}/view.do?article="

    async def _do_fetch(c: httpx.AsyncClient) -> list:
        # 1) boardNo 조회
        resp = await c.get(board_info_url, headers=BROWSER_HEADERS, follow_redirects=True)
        resp.raise_for_status()
        board_no = resp.json()["boardNo"]
        # 2) 글 목록 조회
        resp = await c.get(
            f"{base}/api/article/{board_no}",
            params={"startIndex": 1, "pageRow": 10, "boardNo": board_no},
            headers=BROWSER_HEADERS,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json().get("list", [])

    try:
        articles = await _do_fetch(client)
    except Exception as e:
        if not _is_ssl_error(e):
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=[],
                error=str(e),
            )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), verify=_make_insecure_ssl_context()
            ) as insecure_client:
                articles = await _do_fetch(insecure_client)
        except Exception as e2:
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=[],
                error=str(e2),
            )

    bids: List[BidItem] = []
    for article in articles:
        title = article.get("title")
        if not title or len(title) < _MIN_TITLE_LEN:
            continue
        date = None
        ts = article.get("createdDt")
        if ts:
            date = _ts_to_date(ts)
        article_no = article.get("articleNo")
        url = f"{view_url}{article_no}" if article_no else None
        bids.append(BidItem(title=title, date=date, url=url))

    return HospitalBids(
        hospital=hospital.hospital,
        region=hospital.region,
        source_url=hospital.url,
        bids=bids,
        error=None if bids else "입찰 데이터를 찾을 수 없음",
    )


async def _fetch_raw(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True)
    response.raise_for_status()
    return response.content


async def _fetch_raw_post(client: httpx.AsyncClient, url: str, data: dict) -> bytes:
    response = await client.post(
        url, headers=BROWSER_HEADERS, data=data, follow_redirects=True
    )
    response.raise_for_status()
    return response.content


async def fetch_hospital_bids(client: httpx.AsyncClient, hospital: HospitalInfo) -> HospitalBids:
    # Pattern C: CMC JSON API
    if _is_cmc_api(hospital):
        return await _fetch_cmc_api(client, hospital)

    # Pattern D: KHMC JSON API
    if _is_khmc_api(hospital):
        return await _fetch_khmc_api(client, hospital)

    # Pattern E: KUMC JSON API
    if _is_kumc_api(hospital):
        return await _fetch_kumc_api(client, hospital)

    # No selectors defined → known error
    if not hospital.table_tag and not hospital.div_tag:
        return HospitalBids(
            hospital=hospital.hospital,
            region=hospital.region,
            source_url=hospital.url,
            bids=[],
            error="게시판 선택자 정보 없음 (로드 안됨)",
        )

    # AJAX POST로 목록을 가져와야 하는 페이지 처리
    ajax_post_url: Optional[str] = None
    if hospital.url.endswith("bid-notice2.do"):
        ajax_post_url = hospital.url.replace("bid-notice2.do", "bid-notice2list.do")

    raw: bytes
    try:
        if ajax_post_url:
            raw = await _fetch_raw_post(client, ajax_post_url, {"pageNum": "1"})
            # AJAX 응답은 HTML 조각이므로 body 전체를 컨테이너로 파싱
            soup = _make_soup(raw)
            items: List[BidItem] = []
            for a in soup.find_all("a", class_="inner"):
                subj = a.find("strong", class_="subject")
                if not subj:
                    continue
                title = _text(subj)
                if len(title) < _MIN_TITLE_LEN:
                    continue
                href = _abs_url(a.get("href"), hospital.url)
                date_span = a.find("span", class_="date")
                date = _find_date(_text(date_span)) if date_span else None
                items.append(BidItem(title=title, date=date, url=href))
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=items,
                error=None if items else "입찰 데이터를 찾을 수 없음",
            )
        else:
            raw = await _fetch_raw(client, hospital.url)
    except httpx.TimeoutException:
        return HospitalBids(
            hospital=hospital.hospital,
            region=hospital.region,
            source_url=hospital.url,
            bids=[],
            error="요청 타임아웃",
        )
    except Exception as e:
        if not _is_ssl_error(e):
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=[],
                error=str(e),
            )
        # SSL 오류 시 보안 레벨을 낮춘 SSL 컨텍스트로 재시도
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), verify=_make_insecure_ssl_context()
            ) as insecure_client:
                raw = await _fetch_raw(insecure_client, hospital.url)
        except Exception as e2:
            return HospitalBids(
                hospital=hospital.hospital,
                region=hospital.region,
                source_url=hospital.url,
                bids=[],
                error=str(e2),
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
