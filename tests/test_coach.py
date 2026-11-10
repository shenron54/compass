from datetime import date

import pytest

from compass.coach import explain
from compass.coach.journal import create_entry, due_reviews
from compass.coach.lessons import all_lessons, lesson_of_week
from compass.coach.llm import BudgetExceeded, Coach, CoachUnavailable
from compass.macro.indicators import BY_ID, Reading


class FakePost:
    def __init__(self):
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(body)
        return {"choices": [{"message": {"content": "An answer."}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}


def test_coach_records_usage_and_cost(conn, settings):
    post = FakePost()
    coach = Coach(conn, settings, post)
    assert coach.complete("t", "hi") == "An answer."
    expected = (1000 * settings.llm_price_in_per_m + 500 * settings.llm_price_out_per_m) / 1e6
    assert coach.month_spend() == pytest.approx(expected)


def test_coach_stops_at_monthly_cap(conn, settings):
    conn.execute("INSERT INTO llm_usage(model, purpose, prompt_tokens, completion_tokens, "
                 "cost_usd) VALUES ('m', 't', 0, 0, ?)", (settings.llm_monthly_cap_usd,))
    with pytest.raises(BudgetExceeded):
        Coach(conn, settings, FakePost()).complete("t", "hi")


def test_cached_answer_costs_nothing(conn, settings):
    post = FakePost()
    coach = Coach(conn, settings, post)
    coach.complete("t", "hi", cache_key="k")
    coach.complete("t", "hi", cache_key="k")
    assert len(post.bodies) == 1


def test_coach_without_key_is_unavailable(conn, settings):
    with pytest.raises(CoachUnavailable):
        Coach(conn, settings).complete("t", "hi")


def test_prompt_never_contains_account_id(conn, settings):
    from tests.test_rules import _view

    view = _view(settings)
    post = FakePost()
    r = Reading("vix", "VIX", "Volatility", "index", "FRED:VIXCLS", "volatility-vix",
                date="2026-11-06", value=25.0, percentile=80, history_start="1990-01-02")
    explain.explain_indicator(Coach(conn, settings, post), r, view)
    prompt = post.bodies[0]["messages"][1]["content"]
    assert view.account not in prompt and "EQQQ" in prompt


def test_every_lesson_links_to_real_indicators():
    lessons = all_lessons()
    assert len(lessons) >= 10
    for lesson in lessons:
        assert lesson.title and lesson.html
        assert all(i in BY_ID for i in lesson.indicators), lesson.slug


def test_lesson_of_week_prefers_stressed_topics_and_sticks(conn):
    readings = [Reading("sahm", "Sahm", "Labour", "pp", "x", "l", value=0.6, tone="stress")]
    first = lesson_of_week(conn, readings, date(2026, 11, 10))
    assert first.slug == "labour-and-sahm"
    assert lesson_of_week(conn, [], date(2026, 11, 12)).slug == first.slug
    assert lesson_of_week(conn, readings, date(2026, 11, 17)).slug != first.slug


def test_journal_reviews_come_due_in_order(conn):
    create_entry(conn, None, title="t", action="buy", symbols="", thesis="because")
    conn.execute("UPDATE journal SET created_at = '2026-01-01T00:00:00+00:00'")
    due = due_reviews(conn, date(2026, 5, 1))
    assert [h for _, h in due] == [30]
    conn.execute("INSERT INTO journal_reviews(entry_id, horizon_days, created_at, text) "
                 "VALUES (1, 30, datetime('now'), 'ok')")
    assert [h for _, h in due_reviews(conn, date(2026, 5, 1))] == [90]
