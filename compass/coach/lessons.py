import sqlite3
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib import resources

import markdown
import yaml

from compass.macro.indicators import CAUTION, STRESS, Reading


@dataclass(frozen=True)
class Lesson:
    slug: str
    title: str
    order: int
    summary: str
    indicators: tuple[str, ...]
    body_md: str

    @property
    def html(self) -> str:
        return markdown.markdown(self.body_md, extensions=["tables"])


def _parse(text: str, slug: str) -> Lesson:
    if not text.startswith("---"):
        raise ValueError(f"lesson {slug} has no front matter")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)
    return Lesson(slug=slug, title=meta["title"], order=int(meta.get("order", 99)),
                  summary=meta.get("summary", ""), indicators=tuple(meta.get("indicators", [])),
                  body_md=body.strip())


@lru_cache
def all_lessons() -> tuple[Lesson, ...]:
    folder = resources.files("compass.coach").joinpath("curriculum")
    lessons = [_parse(f.read_text(), f.name.removesuffix(".md"))
               for f in folder.iterdir() if f.name.endswith(".md")]
    return tuple(sorted(lessons, key=lambda lesson: (lesson.order, lesson.slug)))


def get_lesson(slug: str) -> Lesson | None:
    return next((lesson for lesson in all_lessons() if lesson.slug == slug), None)


def lesson_of_week(conn: sqlite3.Connection, readings: list[Reading],
                   today: date | None = None) -> Lesson:
    """The most relevant lesson for this week's readings, not repeated within four weeks."""
    today = today or date.today()
    year, week, _ = today.isocalendar()
    key = f"lotw:{year}-W{week:02d}"
    row = conn.execute("SELECT text FROM coach_notes WHERE key = ?", (key,)).fetchone()
    if row and (picked := get_lesson(row["text"])):
        return picked
    recent = {r["text"] for r in conn.execute(
        "SELECT text FROM coach_notes WHERE key LIKE 'lotw:%' ORDER BY key DESC LIMIT 4")}
    weight = {STRESS: 2, CAUTION: 1}
    by_id = {r.id: r for r in readings if r.available}
    lessons = all_lessons()

    def score(lesson: Lesson) -> int:
        return sum(weight.get(by_id[i].tone, 0) for i in lesson.indicators if i in by_id)

    candidates = [lesson for lesson in lessons if lesson.slug not in recent] or list(lessons)
    best = max(candidates, key=lambda lesson: (score(lesson), -lesson.order))
    if score(best) == 0:
        best = candidates[(year * 53 + week) % len(candidates)]
    conn.execute("INSERT OR REPLACE INTO coach_notes(key, created_at, model, text) "
                 "VALUES (?, datetime('now'), 'rule', ?)", (key, best.slug))
    return best
