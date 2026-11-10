import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from compass import jobs
from compass.config import get_settings
from compass.db import connect, load_series
from compass.rules.store import seed_rules

log = logging.getLogger("compass.worker")


def _job(name: str, fn):
    def wrapped():
        conn = connect()
        try:
            jobs.run(conn, name, lambda: fn(conn))
        finally:
            conn.close()
    wrapped.__name__ = wrapped.__qualname__ = name
    return wrapped


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    conn = connect()
    seed_rules(conn, settings.local_config)
    first_run = not load_series(conn, "FRED:NASDAQ100")
    conn.close()

    def poll_and_evaluate(c):
        detail = jobs.poll_if_due(c, settings)
        if detail is not None:
            jobs.evaluate_rules(c, settings)
        return detail

    def macro_and_evaluate(c):
        detail = jobs.refresh_macro(c, settings)
        jobs.evaluate_rules(c, settings)
        return detail

    def close_and_evaluate(c):
        detail = jobs.poll_ibkr(c, settings, daily_close=True)
        jobs.evaluate_rules(c, settings)
        return detail

    sched = BlockingScheduler(timezone=settings.tz, job_defaults={"coalesce": True,
                                                                  "max_instances": 1,
                                                                  "misfire_grace_time": 600})
    sched.add_job(_job("ibkr_poll", poll_and_evaluate), "interval", minutes=5)
    # US close is 04:00 HKT in summer and 05:00 HKT in winter; 05:30 covers both.
    sched.add_job(_job("daily_close", close_and_evaluate), "cron", day_of_week="tue-sat",
                  hour=5, minute=30)
    sched.add_job(_job("flex_pull", lambda c: jobs.pull_flex(c, settings)), "cron",
                  hour=7, minute=0)
    sched.add_job(_job("macro_refresh", macro_and_evaluate), "cron", hour="7,20", minute=30)
    sched.add_job(_job("ib_history", lambda c: jobs.refresh_ib_history(c, settings)), "cron",
                  day_of_week="sun", hour=8, minute=0)
    sched.add_job(_job("journal_reviews", lambda c: jobs.review_journal(c, settings)), "cron",
                  hour=9, minute=0)
    if first_run:
        sched.add_job(_job("macro_refresh", macro_and_evaluate))
    sched.add_job(_job("ibkr_poll", poll_and_evaluate))
    log.info("worker started (tz %s)", settings.tz)
    sched.start()


if __name__ == "__main__":
    main()
