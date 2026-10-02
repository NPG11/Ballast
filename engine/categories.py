"""Which category each symbol belongs to, and what to buy for each category.

Users can override any of this. Unknown symbols are never guessed:
the engine stops and asks instead.
"""
from __future__ import annotations

DEFAULT_CATEGORY_MAP: dict[str, str] = {
    # US stocks
    "VTI": "us_stocks", "VOO": "us_stocks", "SPY": "us_stocks", "IVV": "us_stocks",
    "QQQ": "us_stocks", "SCHB": "us_stocks", "ITOT": "us_stocks",
    "AAPL": "us_stocks", "MSFT": "us_stocks", "NVDA": "us_stocks", "AMZN": "us_stocks",
    "GOOGL": "us_stocks", "META": "us_stocks", "TSLA": "us_stocks",
    # International stocks
    "VXUS": "intl_stocks", "VEA": "intl_stocks", "IXUS": "intl_stocks", "EFA": "intl_stocks",
    # Emerging markets
    "VWO": "emerging_markets", "IEMG": "emerging_markets", "EEM": "emerging_markets",
    # Bonds
    "BND": "bonds", "AGG": "bonds", "SCHZ": "bonds", "VGIT": "bonds", "TLT": "bonds",
    # Inflation-protected bonds
    "TIP": "tips", "SCHP": "tips", "VTIP": "tips",
    # Real estate
    "VNQ": "real_estate", "SCHH": "real_estate", "IYR": "real_estate",
    # Gold / commodities
    "GLD": "commodities", "IAU": "commodities", "DBC": "commodities",
}

DEFAULT_BUY_SYMBOL: dict[str, str] = {
    "us_stocks": "VTI",
    "intl_stocks": "VXUS",
    "emerging_markets": "VWO",
    "bonds": "BND",
    "tips": "SCHP",
    "real_estate": "VNQ",
    "commodities": "GLD",
}


# Broad funds (ETFs). Anything not listed here is treated as an individual stock,
# which matters for the single-stock cap.
KNOWN_FUNDS: set[str] = {
    sym for sym in DEFAULT_CATEGORY_MAP
    if sym not in {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"}
}


def is_fund(symbol: str) -> bool:
    return symbol in KNOWN_FUNDS


def category_of(symbol: str, overrides: dict[str, str] | None = None) -> str | None:
    if overrides and symbol in overrides:
        return overrides[symbol]
    return DEFAULT_CATEGORY_MAP.get(symbol)
