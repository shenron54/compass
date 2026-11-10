import re

import pyotp
import pytest
from fastapi.testclient import TestClient

from compass.auth.core import (
    authenticate,
    create_user,
    make_session,
    read_session,
    revoke_sessions,
    too_many_failures,
)
from compass.demo import seed
from tests.conftest import daily, put

PASSWORD = "correct horse battery"
SECRET = pyotp.random_base32()


def _code():
    return pyotp.TOTP(SECRET).now()


def test_password_too_short_rejected(conn):
    with pytest.raises(ValueError):
        create_user(conn, "me", "short")


def test_wrong_totp_rejected(conn):
    create_user(conn, "me", PASSWORD, SECRET)
    assert authenticate(conn, "me", PASSWORD, "000000", "1.1.1.1") is None
    assert authenticate(conn, "me", PASSWORD, _code(), "1.1.1.1") is not None


def test_unknown_user_rejected(conn):
    assert authenticate(conn, "ghost", PASSWORD, _code(), "1.1.1.1") is None


def test_five_failures_lock_the_ip(conn):
    create_user(conn, "me", PASSWORD, SECRET)
    for _ in range(5):
        authenticate(conn, "me", "wrong password!", _code(), "2.2.2.2")
    assert too_many_failures(conn, "2.2.2.2")
    assert not too_many_failures(conn, "3.3.3.3")


def test_revoked_session_no_longer_valid(conn, settings):
    create_user(conn, "me", PASSWORD, SECRET)
    user = conn.execute("SELECT * FROM users").fetchone()
    token = make_session(settings.session_secret, user)
    assert read_session(conn, settings.session_secret, token)["username"] == "me"
    revoke_sessions(conn, user["id"])
    assert read_session(conn, settings.session_secret, token) is None


def test_tampered_session_rejected(conn, settings):
    assert read_session(conn, settings.session_secret, "forged.token.value") is None


@pytest.fixture
def client(conn, settings):
    from compass.web.app import create_app

    create_user(conn, "me", PASSWORD, SECRET)
    return TestClient(create_app())


@pytest.fixture
def logged_in(client):
    r = client.post("/login", data={"username": "me", "password": PASSWORD, "code": _code()},
                    follow_redirects=False)
    assert r.status_code == 303
    return client


def test_pages_require_login(client):
    r = client.get("/portfolio", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_htmx_request_without_session_gets_401(client):
    r = client.post("/coach/ask", data={"question": "x"}, headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_login_wrong_code_is_401(client):
    r = client.post("/login", data={"username": "me", "password": PASSWORD, "code": "123456"})
    assert r.status_code == 401


def test_cross_site_post_refused(logged_in):
    r = logged_in.post("/rules/x/delete", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_security_headers_set(logged_in):
    r = logged_in.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


@pytest.fixture
def with_data(conn):
    put(conn, "FRED:NASDAQ100", daily([100 + i * 0.1 for i in range(700)], start="2024-12-01"))
    put(conn, "FRED:SP500", daily([50 + i * 0.05 for i in range(700)], start="2024-12-01"))
    put(conn, "MULTPL:CAPE", [(f"{1900 + i}-01-01", 10 + i * 0.2) for i in range(127)])
    put(conn, "FRED:T10Y3M", daily([0.5, -0.2], start="2026-11-01"))
    seed(conn, days=120)


PAGES = ["/", "/portfolio", "/macro", "/macro/cape", "/macro/vix", "/rules", "/rules/new",
         "/coach", "/coach/lessons/yield-curve", "/journal", "/status"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(logged_in, with_data, path):
    r = logged_in.get(path)
    assert r.status_code == 200, r.text[:500]
    assert "compass" in r.text


def test_rule_history_and_edit_render(logged_in, with_data):
    logged_in.get("/rules")
    for rule_id in ("ndx-ladder", "cape-freeze", "tech-cap", "single-stock-trim"):
        assert logged_in.get(f"/rules/{rule_id}/history").status_code == 200
    assert logged_in.get("/rules/ndx-ladder/edit").status_code == 200


def test_invalid_rule_yaml_shows_error(logged_in):
    r = logged_in.post("/rules/save", data={"text": "id: Bad Id\nname: x\ntype: nope\n"})
    assert r.status_code == 422 and "error" in r.text


def test_rule_saved_and_toggled(logged_in, conn):
    text = "id: cash\nname: Cash floor\ntype: allocation_band\nmeasure: cash_weight\nmin: 0.05\n"
    assert logged_in.post("/rules/save", data={"text": text},
                          follow_redirects=False).status_code == 303
    logged_in.post("/rules/cash/toggle")
    assert conn.execute("SELECT enabled FROM rules WHERE id='cash'").fetchone()[0] == 0


def test_journal_entry_created_with_prices(logged_in, with_data, conn):
    r = logged_in.post("/journal", data={"title": "Add on correction", "action": "buy",
                                         "symbols": "eqqq", "thesis": "Ladder tranche 1"},
                       follow_redirects=False)
    assert r.status_code == 303
    page = logged_in.get(r.headers["location"])
    assert "Ladder tranche 1" in page.text and "EQQQ" in page.text


def test_journal_requires_thesis(logged_in):
    r = logged_in.post("/journal", data={"title": "x", "action": "buy", "thesis": " "})
    assert r.status_code == 422


def test_coach_without_key_explains_why(logged_in, with_data):
    r = logged_in.post("/coach/explain/indicator/cape")
    assert "DEEPSEEK_API_KEY" in r.text


def test_chart_json_cannot_close_script_tag(logged_in, conn):
    text = "id: evil\nname: '</script><b>x'\ntype: allocation_band\nmeasure: cash_weight\nmin: 0.1\n"
    logged_in.post("/rules/save", data={"text": text})
    page = logged_in.get("/rules/evil/history").text
    assert not re.search(r"</script><b>", page)
