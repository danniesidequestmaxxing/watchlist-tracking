"""Phase 13 — Asia equity catalyst adapters (DART/EDINET/MOPS/CNINFO).

Each test mocks the upstream HTTP via `httpx.MockTransport` and exercises the
adapter's normalize → cache → return contract.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.adapters import cninfo, dart, edinet, mops
from src.adapters.cninfo import _ticker_to_market as cn_to_market
from src.adapters.dart import _ticker_to_stock_code as kr_to_code
from src.adapters.edinet import _ticker_to_seccode as jp_to_seccode
from src.adapters.mops import _parse_date as tw_parse_date
from src.adapters.mops import _ticker_to_code as tw_to_code
from src.db import cache
from src.db.schema import init_db
from src.utils.asset_class import detect_asset_class
from src.utils.timestamps import (
    is_cn_market_open,
    is_jp_market_open,
    is_kr_market_open,
    is_tw_market_open,
)


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "asia_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# Asset class detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("005930.KS", "equity_kr"),  # Samsung
        ("035720.KQ", "equity_kr"),  # Kakao on KOSDAQ
        ("7203.T", "equity_jp"),  # Toyota
        ("9984.T", "equity_jp"),  # SoftBank
        ("2330.TW", "equity_tw"),  # TSMC
        ("4904.TWO", "equity_tw"),  # Far EasTone (OTC)
        ("600519.SS", "equity_cn"),  # Kweichow Moutai
        ("000333.SZ", "equity_cn"),  # Midea
        # negatives — ambiguous without suffix
        ("005930", None),  # could be KR or CN
        ("600519", None),
        ("7203", "equity_my"),  # 4 digits without suffix → Bursa
    ],
)
def test_asia_asset_class_detection(ticker: str, expected: str | None) -> None:
    assert detect_asset_class(ticker) == expected


# ---------------------------------------------------------------------------
# Market-hours predicates
# ---------------------------------------------------------------------------


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    "fn,inside,outside",
    [
        # Korea: 00:00-06:30 UTC weekdays
        (is_kr_market_open, _utc(2026, 5, 5, 3, 0), _utc(2026, 5, 5, 7, 0)),
        # Japan: 00:00-06:00 UTC weekdays
        (is_jp_market_open, _utc(2026, 5, 5, 5, 30), _utc(2026, 5, 5, 6, 30)),
        # Taiwan: 01:00-05:30 UTC weekdays
        (is_tw_market_open, _utc(2026, 5, 5, 3, 0), _utc(2026, 5, 5, 0, 30)),
        # China: 01:30-07:00 UTC weekdays
        (is_cn_market_open, _utc(2026, 5, 5, 6, 0), _utc(2026, 5, 5, 1, 0)),
    ],
)
def test_asia_market_hours(fn, inside, outside) -> None:
    assert fn(inside) is True
    assert fn(outside) is False


def test_asia_market_hours_closed_weekend() -> None:
    sat_mid = _utc(2026, 5, 9, 3, 0)
    for fn in (is_kr_market_open, is_jp_market_open, is_tw_market_open, is_cn_market_open):
        assert fn(sat_mid) is False


# ---------------------------------------------------------------------------
# Per-adapter ticker parsers
# ---------------------------------------------------------------------------


def test_kr_ticker_parser() -> None:
    assert kr_to_code("005930.KS") == "005930"
    assert kr_to_code("035720.KQ") == "035720"
    assert kr_to_code("0001") is None


def test_jp_ticker_parser() -> None:
    assert jp_to_seccode("7203.T") == "72030"
    assert jp_to_seccode("AAPL") is None


def test_tw_ticker_parser() -> None:
    assert tw_to_code("2330.TW") == "2330"
    assert tw_to_code("4904.TWO") == "4904"
    assert tw_to_code("2330") == "2330"  # bare 4-digit accepted (Bursa-shape ambiguity)
    assert tw_to_code("AAPL") is None


def test_tw_date_parser_handles_roc_and_western() -> None:
    assert tw_parse_date("113/05/06") == date(2024, 5, 6)
    assert tw_parse_date("2026/05/06") == date(2026, 5, 6)
    assert tw_parse_date("2026-05-06") == date(2026, 5, 6)
    assert tw_parse_date("garbage") is None


def test_cn_ticker_parser() -> None:
    assert cn_to_market("600519.SS") == ("600519", "gssh")
    assert cn_to_market("000333.SZ") == ("000333", "gssz")
    assert cn_to_market("AAPL") is None


# ---------------------------------------------------------------------------
# DART (Korea)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dart_no_api_key_returns_error(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dart, "DART_API_KEY", None)
    out = await dart.fetch_disclosures(db_path, "005930.KS", days_back=30)
    assert out["items"] == []
    assert "DART_API_KEY" in out["error"]


@pytest.mark.asyncio
async def test_dart_invalid_ticker_returns_error(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dart, "DART_API_KEY", "fake")
    out = await dart.fetch_disclosures(db_path, "NOT_A_TICKER", days_back=30)
    assert out["items"] == []
    assert "invalid KR ticker" in out["error"]


@pytest.mark.asyncio
async def test_dart_normalize_filters_by_cutoff(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dart, "DART_API_KEY", "fake")

    # Pre-seed the corp_code cache so we don't need to mock the zip download.
    await cache.write(
        db_path,
        cache_key=dart.CORP_CODE_CACHE_KEY,
        data_type="dart_corp_code",
        payload={"map": {"005930": "00126380"}},
        pulled_at=datetime.now(UTC),
        source_url="https://opendart.fss.or.kr",
    )

    today = datetime.now(UTC).date()
    fresh = today - timedelta(days=2)
    stale = today - timedelta(days=120)

    payload = {
        "status": "000",
        "list": [
            {
                "rcept_dt": fresh.strftime("%Y%m%d"),
                "rcept_no": "20260504000123",
                "report_nm": "Quarterly Report",
            },
            {
                "rcept_dt": stale.strftime("%Y%m%d"),
                "rcept_no": "20260101000000",
                "report_nm": "Old report",
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "list.json" in str(request.url):
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.dart.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    out = await dart.fetch_disclosures(db_path, "005930.KS", days_back=30)
    assert "error" not in out
    titles = [it["description"] for it in out["items"]]
    assert "Quarterly Report" in titles
    assert "Old report" not in titles


@pytest.mark.asyncio
async def test_dart_redacts_api_key_in_errors(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dart, "DART_API_KEY", "SECRET-DART-KEY")

    await cache.write(
        db_path,
        cache_key=dart.CORP_CODE_CACHE_KEY,
        data_type="dart_corp_code",
        payload={"map": {"005930": "00126380"}},
        pulled_at=datetime.now(UTC),
        source_url="https://opendart.fss.or.kr",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Echo the URL (with key) into the response body so an HTTPStatusError
        # would carry it into str(exc).
        return httpx.Response(500, text=f"upstream error fetching {request.url}")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.dart.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    out = await dart.fetch_disclosures(db_path, "005930.KS")
    assert "SECRET-DART-KEY" not in (out.get("error") or "")


# ---------------------------------------------------------------------------
# EDINET (Japan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edinet_walk_filters_by_seccode(db_path: Path, monkeypatch) -> None:
    today = datetime.now(UTC).date()

    def handler(request: httpx.Request) -> httpx.Response:
        # Return one matching record on day 0, none thereafter.
        if request.url.params.get("date") == today.isoformat():
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "secCode": "72030",
                            "submitDateTime": today.isoformat(),
                            "docID": "S100AAAA",
                            "docDescription": "Securities Report (Toyota)",
                            "docTypeCode": "120",
                        },
                        {
                            "secCode": "99990",  # different company
                            "submitDateTime": today.isoformat(),
                            "docID": "S100ZZZZ",
                            "docDescription": "Some other filing",
                        },
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.edinet.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    out = await edinet.fetch_disclosures(db_path, "7203.T", days_back=2)
    assert "error" not in out
    descriptions = [it["description"] for it in out["items"]]
    assert any("Toyota" in d for d in descriptions)
    assert not any("other filing" in d for d in descriptions)


@pytest.mark.asyncio
async def test_edinet_invalid_ticker(db_path: Path) -> None:
    out = await edinet.fetch_disclosures(db_path, "NOT.JP", days_back=2)
    assert out["items"] == []
    assert "invalid JP ticker" in out["error"]


# ---------------------------------------------------------------------------
# MOPS (Taiwan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mops_invalid_ticker(db_path: Path) -> None:
    out = await mops.fetch_disclosures(db_path, "NOT.TW", days_back=30)
    assert out["items"] == []
    assert "invalid TW ticker" in out["error"]


@pytest.mark.asyncio
async def test_mops_html_parses_known_row(db_path: Path, monkeypatch) -> None:
    today = datetime.now(UTC).date()
    html = (
        "<html><body><table>"
        f"<tr><td>{today.year}/{today.month:02d}/{today.day:02d}</td>"
        '<td>2330</td><td><a href="/mops/announcement/123">Q1 results</a></td></tr>'
        "<tr><td>2026/01/01</td><td>2330</td>"
        '<td><a href="/mops/announcement/old">Stale</a></td></tr>'
        "<tr><td>2026/05/06</td><td>1234</td>"
        '<td><a href="/mops/announcement/other">Other co</a></td></tr>'
        "</table></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.mops.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    out = await mops.fetch_disclosures(db_path, "2330.TW", days_back=30)
    titles = [it["description"] for it in out["items"]]
    assert "Q1 results" in titles
    assert "Other co" not in titles
    assert "Stale" not in titles


# ---------------------------------------------------------------------------
# CNINFO (China)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cninfo_invalid_ticker(db_path: Path) -> None:
    out = await cninfo.fetch_disclosures(db_path, "NOT.CN", days_back=30)
    assert out["items"] == []
    assert "invalid CN ticker" in out["error"]


@pytest.mark.asyncio
async def test_cninfo_normalize(db_path: Path, monkeypatch) -> None:
    today = datetime.now(UTC).date()
    payload = {
        "announcements": [
            {
                "announcementId": "111",
                "announcementTitle": "Q1 results announcement",
                "announcementType": "results",
                # Cninfo returns unix-millis
                "announcementTime": int(
                    datetime(today.year, today.month, today.day, tzinfo=UTC).timestamp() * 1000
                ),
            },
            {
                "announcementId": "222",
                "announcementTitle": "Old corporate notice",
                "announcementTime": int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000),
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.cninfo.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    out = await cninfo.fetch_disclosures(db_path, "600519.SS", days_back=30)
    titles = [it["description"] for it in out["items"]]
    assert "Q1 results announcement" in titles
    assert "Old corporate notice" not in titles


# ---------------------------------------------------------------------------
# Disclosure formatter
# ---------------------------------------------------------------------------


def test_format_disclosure_catalyst_renders_items() -> None:
    from src.telegram.formatters import format_disclosure_catalyst

    pulled = datetime.now(UTC).isoformat()
    text = format_disclosure_catalyst(
        "005930.KS",
        [
            {
                "filing_date": "2026-05-04",
                "form": "분기보고서",
                "description": "Quarterly Report",
                "source_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=123",
            }
        ],
        pulled,
        source_label="DART",
        source_domain="opendart.fss.or.kr",
    )
    assert "005930.KS" in text
    assert "DART" in text
    assert "Quarterly Report" in text
    assert "opendart.fss.or.kr" in text


def test_format_disclosure_catalyst_empty() -> None:
    from src.telegram.formatters import format_disclosure_catalyst

    text = format_disclosure_catalyst(
        "7203.T", [], None, source_label="EDINET", source_domain="disclosure.edinet-fsa.go.jp"
    )
    assert "no recent EDINET disclosures" in text
