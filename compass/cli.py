"""compass command line.

    python -m compass.cli refresh macro|ibkr|flex|history|rules|journal|all
    python -m compass.cli seed-demo
    python -m compass.cli status
"""
import argparse
import logging
import sys

from compass import demo, jobs
from compass.config import get_settings
from compass.db import connect

STEPS = {
    "macro": jobs.refresh_macro,
    "ibkr": jobs.poll_ibkr,
    "flex": jobs.pull_flex,
    "history": jobs.refresh_ib_history,
    "rules": jobs.evaluate_rules,
    "journal": jobs.review_journal,
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="compass")
    sub = parser.add_subparsers(dest="cmd", required=True)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("what", choices=[*STEPS, "all"])
    sub.add_parser("seed-demo")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    settings = get_settings()
    conn = connect()
    if args.cmd == "refresh":
        names = list(STEPS) if args.what == "all" else [args.what]
        ok = True
        for name in names:
            result = jobs.run(conn, name, lambda n=name: STEPS[n](conn, settings))
            row = conn.execute("SELECT * FROM job_runs WHERE job = ?", (name,)).fetchone()
            print(f"{name}: {'ok' if result else 'FAILED'} "
                  f"{row['detail'] if result else row['last_error']}")
            ok = ok and result
        return 0 if ok else 1
    if args.cmd == "seed-demo":
        try:
            n = demo.seed(conn)
        except demo.DemoRefused as e:
            print(e, file=sys.stderr)
            return 1
        jobs.evaluate_rules(conn, settings)
        print(f"{n} demo daily snapshots written")
        return 0
    for row in conn.execute("SELECT * FROM job_runs ORDER BY job"):
        print(f"{row['job']:16} ok={row['last_ok']} error={row['last_error_at']} "
              f"{row['last_error'] or row['detail'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
