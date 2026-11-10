import html
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import markdown
import yaml
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from compass.auth.core import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    authenticate,
    make_session,
    read_session,
    revoke_sessions,
    too_many_failures,
)
from compass.coach import explain
from compass.coach.journal import (
    ACTIONS,
    create_entry,
    entries,
    get_entry,
    price_changes,
    review_entry,
)
from compass.coach.lessons import all_lessons, get_lesson, lesson_of_week
from compass.coach.llm import Coach, CoachError
from compass.config import Settings, get_settings
from compass.db import connect
from compass.macro.indicators import BY_ID, INDICATORS, fmt_value, read, read_all, regime
from compass.macro.series import describe, downsample, get_series
from compass.portfolio.config import load_lookthrough, load_portfolio_config
from compass.portfolio.history import drawdowns, nav_series, net_contributions
from compass.portfolio.view import PortfolioView, load_view
from compass.rules.engine import Context, evaluate
from compass.rules.models import dump_rule, parse_rules_file
from compass.rules.replay import replay
from compass.rules.store import (
    active_alerts,
    delete_rule,
    dismiss,
    events_for,
    load_rules,
    save_rule,
    seed_rules,
    set_enabled,
)

HERE = Path(__file__).parent
PUBLIC_PATHS = ("/login", "/static/", "/healthz")

templates = Jinja2Templates(directory=str(HERE / "templates"))


def _money(v: float | None, ccy: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}" + (f" {ccy}" if ccy else "")


def _pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _signed_pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _ago(ts: str | None) -> str:
    if not ts:
        return "never"
    when = datetime.fromisoformat(ts.replace(" ", "T"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    minutes = (datetime.now(UTC) - when).total_seconds() / 60
    if minutes < 2:
        return "just now"
    if minutes < 120:
        return f"{minutes:.0f} min ago"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} h ago"
    return f"{minutes / 1440:.0f} days ago"


def _md(text: str) -> str:
    # Escape first: model output is shown as formatted text, never as HTML.
    return markdown.markdown(html.escape(text), extensions=["tables"])


templates.env.filters.update(money=_money, pct=_pct, signed_pct=_signed_pct, ago=_ago,
                             fmt_value=fmt_value, md=_md, tojson_attr=json.dumps)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


class Page:
    """What most pages need: settings, config and the current portfolio view."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn, self.settings = conn, settings
        self.cfg = load_portfolio_config(settings.local_config)
        self.lt = load_lookthrough(settings.local_config)
        self.view: PortfolioView | None = load_view(conn, self.cfg, self.lt)


def chart(series: list[dict], **opts) -> str:
    # The JSON sits inside a <script> tag, so a "</" in a rule name must not close it.
    return json.dumps({"series": series, **opts}, separators=(",", ":")).replace("</", "<\\/")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="compass", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("cross-site request refused", status_code=403)
        if not path.startswith(PUBLIC_PATHS):
            conn = connect()
            try:
                user = read_session(conn, settings.session_secret,
                                    request.cookies.get(SESSION_COOKIE))
            finally:
                conn.close()
            if user is None:
                if request.headers.get("hx-request"):
                    return Response(status_code=401, headers={"HX-Redirect": "/login"})
                return RedirectResponse("/login", status_code=303)
            request.state.user = user["username"]
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("user", getattr(request.state, "user", None))
        ctx.setdefault("nav", name.split(".")[0])
        return templates.TemplateResponse(request, name, ctx)

    def page(conn: sqlite3.Connection = Depends(get_conn)) -> Page:
        return Page(conn, settings)

    # --- auth --------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...),
              code: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)):
        ip = request.client.host if request.client else "unknown"
        if too_many_failures(conn, ip):
            resp = render(request, "login.html", error="Too many failed attempts. Wait 15 "
                          "minutes.")
            resp.status_code = 429
            return resp
        user = authenticate(conn, username.strip(), password, code, ip)
        if user is None:
            resp = render(request, "login.html", error="Wrong username, password or code.")
            resp.status_code = 401
            return resp
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, make_session(settings.session_secret, user),
                        max_age=SESSION_MAX_AGE, httponly=True, secure=settings.cookie_secure,
                        samesite="strict")
        return resp

    @app.post("/logout")
    def logout(request: Request, everywhere: str = Form(""),
               conn: sqlite3.Connection = Depends(get_conn)):
        if everywhere:
            row = conn.execute("SELECT id FROM users WHERE username = ?",
                               (request.state.user,)).fetchone()
            if row:
                revoke_sessions(conn, row["id"])
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- dashboard ---------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, p: Page = Depends(page)):
        seed_rules(p.conn, settings.local_config)
        readings = read_all(p.conn)
        axes = regime(readings)
        navs = nav_series(p.conn, settings.tz)
        day_change = None
        if len(navs) >= 2 and navs[-2][1]:
            day_change = navs[-1][1] / navs[-2][1] - 1
        notable = [r for r in readings if r.available and r.tone != "calm"][:6]
        jobs = {r["job"]: dict(r) for r in p.conn.execute("SELECT * FROM job_runs")}
        return render(
            request, "dashboard.html", view=p.view, axes=axes, alerts=active_alerts(p.conn),
            notable=notable, lesson=lesson_of_week(p.conn, readings), day_change=day_change,
            nav_chart=chart([{"name": "NAV", "points": downsample(
                [(d, v) for d, v, _ in navs], 300)}], y="money"),
            journal=entries(p.conn)[:3], jobs=jobs)

    # --- portfolio ---------------------------------------------------------------------------

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request, p: Page = Depends(page)):
        navs = nav_series(p.conn, settings.tz)
        points = [(d, v) for d, v, _ in navs]
        contrib = net_contributions(p.conn)
        sources = {s for _, _, s in navs}
        return render(
            request, "portfolio.html", view=p.view, cfg=p.cfg, sources=sources,
            nav_chart=chart([{"name": "NAV", "points": downsample(points, 500)},
                             {"name": "Net deposits", "points": contrib, "step": True}],
                            y="money"),
            dd_chart=chart([{"name": "Drawdown from peak", "points": downsample(
                [(d, v * 100) for d, v in drawdowns(points)], 500), "area": True}], y="pct"))

    # --- macro -------------------------------------------------------------------------------

    @app.get("/macro", response_class=HTMLResponse)
    def macro(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        readings = read_all(conn)
        groups: dict[str, list] = {}
        for r in readings:
            groups.setdefault(r.group, []).append(r)
        meta = {r["series_id"]: dict(r) for r in conn.execute("SELECT * FROM series_meta")}
        return render(request, "macro.html", axes=regime(readings), groups=groups, meta=meta,
                      spark=lambda r: chart([{"name": r.name, "points": r.spark}], spark=True))

    @app.get("/macro/{indicator_id}", response_class=HTMLResponse)
    def indicator(request: Request, indicator_id: str,
                  conn: sqlite3.Connection = Depends(get_conn)):
        ind = BY_ID.get(indicator_id)
        if ind is None:
            raise HTTPException(404)
        r = read(conn, ind)
        pts = get_series(conn, ind.series)
        lesson = get_lesson(ind.lesson)
        base = ind.series.split(":")[0] if ":" in ind.series else None
        meta_key = ind.series if base else None
        meta = conn.execute("SELECT * FROM series_meta WHERE series_id = ?",
                            (meta_key,)).fetchone() if meta_key else None
        return render(request, "indicator.html", r=r, lesson=lesson, meta=meta,
                      description=describe(ind.series), nav="macro",
                      history=chart([{"name": r.name, "points": downsample(pts, 1200)}],
                                    zoom=True))

    # --- rules -------------------------------------------------------------------------------

    @app.get("/rules", response_class=HTMLResponse)
    def rules(request: Request, p: Page = Depends(page)):
        seed_rules(p.conn, settings.local_config)
        ctx = Context(p.conn, p.view, p.cfg)
        items = [(rule, text, evaluate(rule, ctx)) for rule, text in load_rules(p.conn)]
        return render(request, "rules.html", items=items, alerts=active_alerts(p.conn))

    @app.get("/rules/new", response_class=HTMLResponse)
    def rule_new(request: Request):
        examples = {r.type: dump_rule(r) for r in reversed(parse_rules_file(
            (HERE.parent / "defaults" / "rules.example.yaml").read_text()))}
        return render(request, "rule_edit.html", rule_id=None, text="", error=None,
                      examples=examples, nav="rules")

    @app.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
    def rule_edit(request: Request, rule_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        row = conn.execute("SELECT yaml FROM rules WHERE id = ?", (rule_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        return render(request, "rule_edit.html", rule_id=rule_id, text=row["yaml"], error=None,
                      examples={}, nav="rules")

    @app.post("/rules/save")
    def rule_save(request: Request, text: str = Form(...), original_id: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn)):
        try:
            rule = save_rule(conn, text, original_id or None)
        except (ValidationError, ValueError, TypeError, yaml.YAMLError) as e:
            resp = render(request, "rule_edit.html", rule_id=original_id or None, text=text,
                          error=str(e), examples={}, nav="rules")
            resp.status_code = 422
            return resp
        return RedirectResponse(f"/rules#{rule.id}", status_code=303)

    @app.post("/rules/{rule_id}/toggle")
    def rule_toggle(rule_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        row = conn.execute("SELECT enabled FROM rules WHERE id = ?", (rule_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        set_enabled(conn, rule_id, not row["enabled"])
        return RedirectResponse(f"/rules#{rule_id}", status_code=303)

    @app.post("/rules/{rule_id}/delete")
    def rule_delete(rule_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        delete_rule(conn, rule_id)
        return RedirectResponse("/rules", status_code=303)

    @app.get("/rules/{rule_id}/history", response_class=HTMLResponse)
    def rule_history(request: Request, rule_id: str, p: Page = Depends(page)):
        found = next(((r, t) for r, t in load_rules(p.conn) if r.id == rule_id), None)
        if found is None:
            raise HTTPException(404)
        rule, text = found
        rp = replay(p.conn, rule, p.cfg)
        ev = evaluate(rule, Context(p.conn, p.view, p.cfg))
        return render(request, "rule_history.html", rule=rule, text=text, rp=rp, ev=ev,
                      events=events_for(p.conn, rule_id), nav="rules",
                      chart_json=chart([{"name": rp.series, "points": rp.chart}],
                                       markers=rp.markers, bands=rp.bands, zoom=True))

    @app.post("/alerts/{event_id}/dismiss")
    def alert_dismiss(event_id: int, request: Request,
                      conn: sqlite3.Connection = Depends(get_conn)):
        dismiss(conn, event_id)
        back = request.headers.get("referer") or "/"
        return RedirectResponse(urlsplit(back).path or "/", status_code=303)

    # --- coach -------------------------------------------------------------------------------

    def coach_fragment(fn) -> HTMLResponse:
        try:
            text = fn()
        except CoachError as e:
            return HTMLResponse(f'<div class="coach-error">Coach unavailable: '
                                f'{html.escape(str(e))}</div>')
        return HTMLResponse(f'<div class="coach-answer">{_md(text)}</div>')

    @app.post("/coach/explain/indicator/{indicator_id}", response_class=HTMLResponse)
    def explain_indicator(indicator_id: str, p: Page = Depends(page)):
        ind = BY_ID.get(indicator_id)
        if ind is None:
            raise HTTPException(404)
        r = read(p.conn, ind)
        return coach_fragment(lambda: explain.explain_indicator(Coach(p.conn, settings), r,
                                                                p.view))

    @app.post("/coach/explain/rule/{rule_id}", response_class=HTMLResponse)
    def explain_rule(rule_id: str, p: Page = Depends(page)):
        found = next(((r, t) for r, t in load_rules(p.conn) if r.id == rule_id), None)
        if found is None:
            raise HTTPException(404)
        rule, text = found
        ev = evaluate(rule, Context(p.conn, p.view, p.cfg))
        summary = replay(p.conn, rule, p.cfg).summary
        return coach_fragment(lambda: explain.explain_rule(Coach(p.conn, settings), ev, text,
                                                           summary, p.view))

    @app.post("/coach/ask", response_class=HTMLResponse)
    def coach_ask(question: str = Form(...), p: Page = Depends(page)):
        if not question.strip():
            return HTMLResponse('<div class="coach-error">Type a question first.</div>')
        readings = read_all(p.conn)
        return coach_fragment(lambda: explain.ask(
            Coach(p.conn, settings), question[:2000], readings, regime(readings), p.view,
            active_alerts(p.conn)))

    @app.post("/coach/review", response_class=HTMLResponse)
    def coach_review(p: Page = Depends(page)):
        readings = read_all(p.conn)
        return coach_fragment(lambda: explain.portfolio_review(
            Coach(p.conn, settings), readings, regime(readings), p.view, active_alerts(p.conn),
            date.today().isoformat()))

    @app.get("/coach", response_class=HTMLResponse)
    def coach(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        c = Coach(conn, settings)
        usage = conn.execute(
            "SELECT purpose, COUNT(*) AS n, SUM(cost_usd) AS cost FROM llm_usage WHERE "
            "ts >= datetime('now', 'start of month') GROUP BY purpose ORDER BY cost DESC"
        ).fetchall()
        return render(request, "coach.html", lessons=all_lessons(), available=c.available,
                      spent=c.month_spend(), cap=settings.llm_monthly_cap_usd, usage=usage,
                      lesson=lesson_of_week(conn, read_all(conn)))

    @app.get("/coach/lessons/{slug}", response_class=HTMLResponse)
    def lesson(request: Request, slug: str):
        found = get_lesson(slug)
        if found is None:
            raise HTTPException(404)
        related = [BY_ID[i] for i in found.indicators if i in BY_ID]
        return render(request, "lesson.html", lesson=found, related=related, nav="coach")

    # --- journal -----------------------------------------------------------------------------

    @app.get("/journal", response_class=HTMLResponse)
    def journal(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        rule_ids = [r["id"] for r in conn.execute("SELECT id FROM rules ORDER BY id")]
        return render(request, "journal.html", entries=entries(conn), actions=ACTIONS,
                      rule_ids=rule_ids, error=None, form={})

    @app.post("/journal")
    def journal_create(request: Request, title: str = Form(""), action: str = Form("buy"),
                       symbols: str = Form(""), thesis: str = Form(""), trigger: str = Form(""),
                       rule_id: str = Form(""), expected: str = Form(""),
                       p: Page = Depends(page)):
        form = {"title": title, "action": action, "symbols": symbols, "thesis": thesis,
                    "trigger": trigger, "rule_id": rule_id, "expected": expected}
        try:
            entry_id = create_entry(p.conn, p.view, **form)
        except ValueError as e:
            rule_ids = [r["id"] for r in p.conn.execute("SELECT id FROM rules ORDER BY id")]
            resp = render(request, "journal.html", entries=entries(p.conn), actions=ACTIONS,
                          rule_ids=rule_ids, error=str(e), form=form)
            resp.status_code = 422
            return resp
        return RedirectResponse(f"/journal/{entry_id}", status_code=303)

    @app.get("/journal/{entry_id}", response_class=HTMLResponse)
    def journal_entry(request: Request, entry_id: int, p: Page = Depends(page)):
        e = get_entry(p.conn, entry_id)
        if e is None:
            raise HTTPException(404)
        return render(request, "journal_entry.html", e=e, nav="journal",
                      changes=price_changes(p.conn, e, p.view))

    @app.post("/journal/{entry_id}/review", response_class=HTMLResponse)
    def journal_review(entry_id: int, p: Page = Depends(page)):
        e = get_entry(p.conn, entry_id)
        if e is None:
            raise HTTPException(404)
        age = (date.today() - datetime.fromisoformat(e["created_at"]).date()).days
        return coach_fragment(lambda: review_entry(Coach(p.conn, settings), p.conn, e,
                                                   max(age, 0), p.view))

    # --- status ------------------------------------------------------------------------------

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request, p: Page = Depends(page)):
        jobs = [dict(r) for r in p.conn.execute("SELECT * FROM job_runs ORDER BY job")]
        meta = [dict(r) | {"points": p.conn.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM series WHERE series_id = ?",
            (r["series_id"],)).fetchone()} for r in p.conn.execute(
            "SELECT * FROM series_meta ORDER BY series_id")]
        return render(request, "status.html", jobs=jobs, meta=meta, view=p.view,
                      cfg=p.cfg, settings=settings,
                      indicators=[(i.id, i.series) for i in INDICATORS])

    return app


app = create_app()
