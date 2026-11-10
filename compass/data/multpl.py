import re
from datetime import datetime

import httpx
from selectolax.lexbor import LexborHTMLParser

TABLES = {
    "MULTPL:CAPE": "https://www.multpl.com/shiller-pe/table/by-month",
    "MULTPL:SP500": "https://www.multpl.com/s-p-500-historical-prices/table/by-month",
}

_NUMBER = re.compile(r"-?[\d,]+(?:\.\d+)?")


class MultplError(RuntimeError):
    pass


def parse_table(html: str) -> list[tuple[str, float]]:
    tree = LexborHTMLParser(html)
    table = tree.css_first("table#datatable")
    if table is None:
        raise MultplError("no #datatable on the page")
    points: dict[str, float] = {}
    for row in table.css("tr"):
        cells = row.css("td")
        if len(cells) < 2:
            continue
        date = datetime.strptime(cells[0].text(strip=True), "%b %d, %Y").date().isoformat()
        match = _NUMBER.search(cells[1].text(strip=True))
        if match:
            points[date] = float(match.group().replace(",", ""))
    if not points:
        raise MultplError("table had no rows")
    return sorted(points.items())


def fetch(series_id: str, http: httpx.Client) -> tuple[list[tuple[str, float]], str]:
    url = TABLES[series_id]
    r = http.get(url)
    if r.status_code >= 400:
        raise MultplError(f"multpl {r.status_code} for {url}")
    return parse_table(r.text), url
