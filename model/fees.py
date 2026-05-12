from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Polymarket taker fee rates by documented category. Makers are fee-free.
# Source: https://docs.polymarket.com/trading/fees
POLYMARKET_TAKER_FEE_RATES: dict[str, float] = {
    "crypto": 0.072,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "general": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
    "world": 0.0,
}


def _clamp_price(price: float) -> float:
    return max(0.0, min(1.0, float(price)))


def fee_rate_for_category(category: str | None, override_rate: float | None = None) -> float:
    """Return the taker fee rate for a Polymarket category.

    `override_rate` is useful when the CLOB market payload provides a specific
    fee rate. If absent, we use the documented category table.
    """
    if override_rate is not None:
        return max(0.0, float(override_rate))
    key = str(category or "other").strip().lower().replace(" / ", "/")
    key = key.replace("other/general", "other")
    return POLYMARKET_TAKER_FEE_RATES.get(key, POLYMARKET_TAKER_FEE_RATES["other"])


def taker_fee_per_share(price: float, fee_rate: float) -> float:
    """Polymarket taker fee per share at match time.

    Formula: fee = shares * feeRate * p * (1 - p)
    This helper returns the per-share term: feeRate * p * (1 - p).
    """
    p = _clamp_price(price)
    return max(0.0, float(fee_rate) * p * (1.0 - p))


def taker_fee_usdc(
    shares: float,
    price: float,
    fee_rate: float,
    precision: int = 5,
) -> float:
    """Return total taker fee in USDC, rounded like the protocol docs.

    Fees are rounded to 5 decimal places and tiny fees can round to zero.
    """
    fee = max(0.0, float(shares)) * taker_fee_per_share(price, fee_rate)
    return round(fee, int(precision))


@dataclass(frozen=True)
class FeeModel:
    """Small value object used by live paper trading and research sweeps."""

    category: str = "mentions"
    taker_fee_rate: float | None = None
    maker_fee_rate: float = 0.0
    precision: int = 5

    @property
    def rate(self) -> float:
        return fee_rate_for_category(self.category, self.taker_fee_rate)

    def per_share(self, price: float, liquidity_role: str = "taker") -> float:
        if str(liquidity_role).lower() == "maker":
            return 0.0
        return taker_fee_per_share(price, self.rate)

    def fee_usdc(
        self,
        shares: float,
        price: float,
        liquidity_role: str = "taker",
    ) -> float:
        if str(liquidity_role).lower() == "maker":
            return 0.0
        return taker_fee_usdc(shares, price, self.rate, precision=self.precision)

    def describe(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "taker_fee_rate": self.rate,
            "maker_fee_rate": self.maker_fee_rate,
            "precision": self.precision,
        }
