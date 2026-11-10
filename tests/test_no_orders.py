import re
from pathlib import Path

import compass

FORBIDDEN = re.compile(r"\b(placeOrder|cancelOrder|reqGlobalCancel|whatIfOrder|bracketOrder|"
                       r"MarketOrder|LimitOrder|StopOrder)\b")


def test_package_never_references_order_methods():
    root = Path(compass.__file__).parent
    hits = [f"{p.relative_to(root)}:{i}" for p in root.rglob("*.py")
            for i, line in enumerate(p.read_text().splitlines(), 1) if FORBIDDEN.search(line)]
    assert hits == []


def test_gateway_connects_read_only():
    source = (Path(compass.__file__).parent / "ibkr" / "gateway.py").read_text()
    assert "readonly=True" in source
