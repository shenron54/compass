import logging
import sqlite3
import time
from collections.abc import Callable

import httpx

from compass.config import Settings

log = logging.getLogger(__name__)

API_URL = "https://api.deepseek.com/chat/completions"
RETRY_WAITS = (5, 20, 60)

SYSTEM_PROMPT = """You are the tutor inside compass, a personal tool for one long-term investor
who lives in Hong Kong, keeps his account in HKD at Interactive Brokers, and is tilted towards
US technology. He wants to understand market cycles: macro conditions, valuation, trend, and
how his own written rules behave.

How to answer:
- Teach. Explain what a number measures, why it moves, how it has behaved in past cycles, and
  what it does not tell you. Give concrete historical examples with years.
- Use only the figures supplied in the context for current values. If a figure is missing or
  marked stale, say so instead of guessing.
- Never tell him to buy or sell anything. You may restate what his own rules say, and you may
  describe the trade-offs of the choices in front of him.
- Indicators are probabilistic. Point out when signals disagree.
- Plain English, short paragraphs, no hype. About 200-300 words unless asked for more.
- End with one question he could think about, prefixed "To think about:".
This is education, not financial advice."""


class CoachError(RuntimeError):
    pass


class CoachUnavailable(CoachError):
    pass


class BudgetExceeded(CoachError):
    pass


Post = Callable[[dict], dict]


def deepseek_post(api_key: str, timeout: float = 120,
                  sleep: Callable[[float], None] = time.sleep) -> Post:
    http = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"})

    def post(body: dict) -> dict:
        for wait in (*RETRY_WAITS, None):
            try:
                r = http.post(API_URL, json=body)
            except httpx.TransportError as e:
                if wait is None:
                    raise CoachError(f"DeepSeek unreachable: {type(e).__name__}") from e
                sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504) and wait is not None:
                sleep(wait)
                continue
            if r.status_code == 402:
                raise BudgetExceeded("DeepSeek balance is empty (402); top up the account")
            if r.status_code >= 400:
                raise CoachError(f"DeepSeek {r.status_code}: {r.text[:200]}")
            return r.json()
        raise CoachError("DeepSeek kept failing")

    return post


class Coach:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, post: Post | None = None):
        self.conn, self.settings = conn, settings
        if post is None and settings.deepseek_api_key:
            post = deepseek_post(settings.deepseek_api_key)
        self._post = post

    @property
    def available(self) -> bool:
        return self._post is not None

    def month_spend(self) -> float:
        return self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage "
            "WHERE ts >= datetime('now', 'start of month')").fetchone()[0]

    def cached(self, key: str) -> str | None:
        row = self.conn.execute("SELECT text FROM coach_notes WHERE key = ?", (key,)).fetchone()
        return row["text"] if row else None

    def complete(self, purpose: str, user: str, deep: bool = False, max_tokens: int = 900,
                 cache_key: str | None = None) -> str:
        if cache_key and (hit := self.cached(cache_key)):
            return hit
        if self._post is None:
            raise CoachUnavailable("DEEPSEEK_API_KEY is not set")
        spent, cap = self.month_spend(), self.settings.llm_monthly_cap_usd
        if spent >= cap:
            raise BudgetExceeded(f"monthly coach budget used: ${spent:.2f} of ${cap:.2f}")
        model = self.settings.deep_model if deep else self.settings.fast_model
        resp = self._post({
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        })
        try:
            text = resp["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as e:
            raise CoachError(f"unexpected DeepSeek response: {str(resp)[:200]}") from e
        usage = resp.get("usage") or {}
        p_tok, c_tok = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        cost = (p_tok * self.settings.llm_price_in_per_m
                + c_tok * self.settings.llm_price_out_per_m) / 1e6
        self.conn.execute(
            "INSERT INTO llm_usage(model, purpose, prompt_tokens, completion_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?)", (model, purpose, p_tok, c_tok, cost))
        if cache_key and text:
            self.conn.execute(
                "INSERT OR REPLACE INTO coach_notes(key, created_at, model, text) "
                "VALUES (?, datetime('now'), ?, ?)", (cache_key, model, text))
        return text
