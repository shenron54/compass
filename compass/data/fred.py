import csv
import io

import httpx

API_URL = "https://api.stlouisfed.org/fred/series/observations"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


class FredError(RuntimeError):
    pass


def parse_csv(text: str) -> list[tuple[str, float]]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise FredError(f"unexpected FRED CSV header: {header!r}")
    points = []
    for row in reader:
        if len(row) < 2 or row[1] in ("", "."):
            continue
        points.append((row[0], float(row[1])))
    return points


def parse_api(payload: dict) -> list[tuple[str, float]]:
    if "observations" not in payload:
        raise FredError(payload.get("error_message", "no observations in FRED response"))
    return [(o["date"], float(o["value"])) for o in payload["observations"]
            if o.get("value") not in (None, "", ".")]


def fetch(series_id: str, http: httpx.Client, api_key: str = "",
          start: str | None = None) -> tuple[list[tuple[str, float]], str]:
    """Return (points, source_url). The keyed API is preferred; the graph CSV needs no key."""
    if api_key:
        params = {"series_id": series_id, "api_key": api_key, "file_type": "json"}
        if start:
            params["observation_start"] = start
        r = http.get(API_URL, params=params)
        if r.status_code >= 400:
            raise FredError(f"FRED API {r.status_code} for {series_id}: {r.text[:200]}")
        return parse_api(r.json()), f"https://fred.stlouisfed.org/series/{series_id}"
    params = {"id": series_id}
    if start:
        params["cosd"] = start
    r = http.get(CSV_URL, params=params)
    if r.status_code >= 400:
        raise FredError(f"FRED CSV {r.status_code} for {series_id}")
    return parse_csv(r.text), f"https://fred.stlouisfed.org/series/{series_id}"
