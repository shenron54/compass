# compass

A personal, single-user web app for learning how market cycles work and for managing an
Interactive Brokers (IBKR) portfolio against rules written in advance. It has four parts:

- **Portfolio monitor**: positions, cash and NAV in the account's base currency (HKD here), with
  drift from target weights, and *look-through* exposure. Look-through adds up the companies and
  sectors held inside your ETFs as well as the ones you hold directly, so a Nasdaq-100 fund plus a
  single tech stock counts as the concentration it really is.
- **Macro and cycle dashboard**: yield curves, credit spreads, financial conditions, VIX, the Sahm
  rule, jobless claims, inflation, Shiller CAPE, trend and drawdown. Each reading is placed in its
  own history and summarised on three axes: conditions, valuation and trend.
- **Rules**: allocation bands, valuation freezes, drawdown ladders for deploying cash, and
  trim-at-high rules. They are checked after every data refresh, and each one can be replayed over
  decades of history to see when it would have fired and what happened next. **compass never places
  orders.**
- **Coach and journal**: a DeepSeek-backed tutor explains any indicator or rule against current
  data, a written curriculum covers the concepts, and a decision journal is reviewed after 30, 90
  and 365 days.

It's an education and decision-support tool, not financial advice.

## Safety

compass is **read-only** at the broker in three independent ways:

1. IB Gateway runs with `READ_ONLY_API=yes`, so the gateway rejects any order the API sends.
2. The code never calls an order method. `tests/test_no_orders.py` fails the build if one appears,
   and the client connects with `readonly=True`.
3. The gateway logs in as a **secondary IBKR user with no trading permission**. A secondary user
   also stops the gateway from logging you out of IBKR Mobile, because IBKR allows one session per
   username.

The IBKR credentials sit in `ibkr.env`, which only the gateway container reads. The gateway shares
a Docker network only with the worker: the internet-facing web container cannot reach it, and no
gateway port is published. The web app requires a password and an authenticator (TOTP) code.

This repository is public. Holdings, targets, rule thresholds and journal entries never go in git:
they live in the database and in the gitignored `config/local/`.

## Data sources

| Data | Source |
|---|---|
| Positions, cash, NAV, FX to base | IB Gateway via [`ib_async`](https://github.com/ib-api-reloaded/ib_async), every 5 minutes while US or London markets are open |
| Trades, dividends, deposits, daily NAV | IBKR Flex Web Service, nightly |
| Macro series and Nasdaq-100 / S&P 500 daily | [FRED](https://fred.stlouisfed.org) (keyed API if `FRED_API_KEY` is set, otherwise the public CSV) |
| Shiller CAPE and S&P 500 monthly since 1871 | [multpl.com](https://www.multpl.com) |
| Holding price history | IBKR historical bars (weekly; depends on market-data permissions) |
| ETF look-through weights | `compass/defaults/lookthrough.yaml`: approximate index weights with an as-of date, refreshed by hand from fund factsheets |

## Run locally

```bash
uv sync
export COMPASS_DATA_DIR=./data COOKIE_SECURE=false
uv run python -m compass.cli refresh macro     # FRED + multpl, about 20 s
uv run python -m compass.cli seed-demo         # example positions (no IBKR needed)
uv run python -m compass.auth.setup --username me
uv run uvicorn compass.web.app:app --reload
```

Or in Docker: `COMPASS_PORT=8000 docker compose -p compass-local -f deploy/docker-compose.local.yml up --build`.

Checks:

```bash
uv run ruff check .
uv run pytest
uv run python tools/walkthrough.py --base http://127.0.0.1:8000 --user me \
  --password '…' --totp-secret '…' --shots shots/   # headless browser pass over every page
```

## Configure

- `config/local/portfolio.yaml`: base currency, target weights, bands, which sectors count as
  "tech", and the price series to use per symbol. Start from
  `compass/defaults/portfolio.example.yaml`.
- `config/local/rules.yaml` (optional): seeds the rules on first run, from
  `compass/defaults/rules.example.yaml`. After that, edit rules on the Rules page.
- `config/local/lookthrough.yaml` (optional): overrides fund weights.
- `.env` and `ibkr.env`: see `.env.example` and `ibkr.env.example`.

## Deploy (DigitalOcean droplet)

The droplet stack is `deploy/docker-compose.yml`: `ib-gateway` (profile `ibkr`), `web`, `worker` and
`caddy`.

```bash
rsync -az --exclude /.venv --exclude /data --exclude /.git --exclude /config/local --exclude /shots ./ root@HOST:/root/compass/   # anchored: a bare "data" would also skip compass/data/
ssh root@HOST
cd /root/compass && cp .env.example .env && cp ibkr.env.example ibkr.env && chmod 600 .env ibkr.env
echo 'COMPASS_HOST="compass.sanmejora.com, HOST-IP-WITH-DASHES.sslip.io"' > deploy/.env
# fill both files, then:
docker compose -f deploy/docker-compose.yml --profile ibkr up -d --build
docker compose -f deploy/docker-compose.yml exec -it web python -m compass.auth.setup --username aryaman
echo '30 3 * * * /root/compass/deploy/backup.sh' | crontab -
```

The first gateway login, and the weekly re-login IBKR enforces, need the push approval in IBKR
Mobile. IBC handles the daily restart (`AUTO_RESTART_TIME`) without a new 2FA. If the gateway is
down, the Status page shows the error and everything else keeps working on the last snapshot.
