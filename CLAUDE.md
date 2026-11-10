# compass

Aryaman Navale's personal tool for learning market cycles and managing his IBKR (Hong Kong, HKD base)
portfolio against rules he writes himself. Single user. Not a product.

Decisions (10 Nov 2026):
- Python 3.12 with uv, FastAPI, Jinja2, HTMX and vendored ECharts, SQLite in WAL mode.
- APScheduler worker.
- IBKR via a live read-only IB Gateway (gnzsnz image with IBC) plus the Flex Web Service.
- DeepSeek for the coach.
- Alerts on the dashboard only.
- Password + TOTP login.
- Own DigitalOcean droplet in SGP1 at compass.sanmejora.com, separate from grant-scout's.

Hard rules:
- **Never place orders.** No order method may appear anywhere in `compass/`
  (`tests/test_no_orders.py`). The gateway runs with `READ_ONLY_API=yes` and a secondary IBKR user
  without trading rights.
- **Public repo.** No holdings, account ids, targets, rule thresholds, journal text or secrets in
  git. Those live in the DB, `config/local/` (gitignored), `.env` and `ibkr.env` (gitignored, chmod
  600 on the droplet). Demo data and examples use made-up figures.
- Only the gateway container reads `ibkr.env`. Only the worker shares a network with the gateway.
- Coach prompts never include the account id. The monthly spend cap is enforced in
  `compass/coach/llm.py`.
- Source data is shown with its date and source, and staleness is flagged rather than hidden.
- Yahoo Finance refuses cookie-less requests; don't work around it. Use FRED, multpl and IBKR.

Layout:
- `compass/data/` (FRED, multpl)
- `compass/ibkr/` (gateway, flex)
- `compass/portfolio/` (config, view with look-through, history)
- `compass/macro/` (series catalogue and derived series, indicators and regime)
- `compass/rules/` (models, engine, store, replay)
- `compass/coach/` (llm, explain, lessons with `curriculum/*.md`, journal)
- `compass/auth/`
- `compass/web/` (app, templates, static)
- `compass/jobs.py`, `compass/worker.py`, `compass/cli.py`, `compass/demo.py`
- `deploy/`, `tools/walkthrough.py`

Checks before any commit: `uv run ruff check .` and `uv run pytest`. For UI changes, also run the
walkthrough against a local server. Commit messages are plain one-liners.
