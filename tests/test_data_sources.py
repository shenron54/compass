from pathlib import Path

import httpx
import pytest
import respx

from compass.data import fred, multpl
from compass.ibkr import flex

FIX = Path(__file__).parent / "fixtures"


def test_fred_csv_skips_missing_values():
    points = fred.parse_csv((FIX / "fred_nasdaq100.csv").read_text())
    assert points[0] == ("1986-01-02", 131.25)
    assert "1986-01-07" not in dict(points) and "1986-01-08" not in dict(points)
    assert points[-1] == ("2026-11-09", 27907.63)


def test_fred_api_parses_observations():
    payload = {"observations": [{"date": "2026-11-06", "value": "3.88"},
                                {"date": "2026-11-07", "value": "."}]}
    assert fred.parse_api(payload) == [("2026-11-06", 3.88)]


def test_fred_api_error_message_surfaces():
    with pytest.raises(fred.FredError, match="api_key"):
        fred.parse_api({"error_message": "Variable api_key is not set."})


@respx.mock
def test_fred_fetch_uses_keyed_api_when_key_set():
    route = respx.get(fred.API_URL).mock(return_value=httpx.Response(
        200, json={"observations": [{"date": "2026-11-06", "value": "1.5"}]}))
    points, url = fred.fetch("T10Y3M", httpx.Client(), api_key="k", start="2026-01-01")
    assert points == [("2026-11-06", 1.5)]
    assert route.calls.last.request.url.params["observation_start"] == "2026-01-01"
    assert url.endswith("/T10Y3M")


@respx.mock
def test_fred_fetch_falls_back_to_csv_without_key():
    respx.get(fred.CSV_URL).mock(return_value=httpx.Response(
        200, text="observation_date,T10Y3M\n2026-11-06,0.5\n"))
    points, _ = fred.fetch("T10Y3M", httpx.Client())
    assert points == [("2026-11-06", 0.5)]


def test_multpl_table_parses_dates_and_values():
    points = multpl.parse_table((FIX / "multpl_cape.html").read_text())
    assert points == sorted(points)
    assert dict(points)["2026-11-09"] == pytest.approx(39.85)
    assert points[0][0] < "1900-01-01"


def test_multpl_missing_table_raises():
    with pytest.raises(multpl.MultplError):
        multpl.parse_table("<html><body>maintenance</body></html>")


def test_flex_report_parses_every_section():
    report = flex.parse_report((FIX / "flex_report.xml").read_text())
    assert [t["trade_id"] for t in report.trades] == ["7001", "7002"]
    assert report.trades[0]["fx_to_base"] == 10.05
    assert [c["date"] for c in report.cash] == ["2026-01-15", "2026-06-16", "2026-06-16"]
    assert report.nav[1] == {"date": "2026-11-09", "total_base": 49500.0, "cash_base": 5000.0,
                             "stock_base": 44500.0}
    assert report.positions[0]["symbol"] == "EQQQ"


def _xml(status, **kw):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in kw.items())
    return f"<FlexStatementResponse><Status>{status}</Status>{body}</FlexStatementResponse>"


@respx.mock
def test_flex_fetch_waits_while_statement_generates():
    get_url = "https://example.ibkr/GetStatement"
    respx.get(flex.SEND_URL).mock(return_value=httpx.Response(
        200, text=_xml("Success", ReferenceCode="123", Url=get_url)))
    respx.get(get_url).mock(side_effect=[
        httpx.Response(200, text=_xml("Warn", ErrorCode="1019",
                                      ErrorMessage="Statement generation in progress")),
        httpx.Response(200, text=(FIX / "flex_report.xml").read_text()),
    ])
    waits = []
    report = flex.fetch_report("tok", "q1", httpx.Client(), sleep=waits.append)
    assert len(report.trades) == 2
    assert waits == [10]


@respx.mock
def test_flex_fetch_raises_on_invalid_token():
    respx.get(flex.SEND_URL).mock(return_value=httpx.Response(
        200, text=_xml("Fail", ErrorCode="1012", ErrorMessage="Token has expired.")))
    with pytest.raises(flex.FlexError, match="Token has expired"):
        flex.fetch_report("tok", "q1", httpx.Client(), sleep=lambda _: None)


def test_flex_requires_credentials():
    with pytest.raises(flex.FlexError, match="FLEX_TOKEN"):
        flex.fetch_report("", "", httpx.Client())
