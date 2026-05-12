# backwards-compat stub — use market.prices directly
from market.prices import PolymarketPrices, Bracket, EVENT_SLUGS

__all__ = ["PolymarketPrices", "Bracket", "EVENT_SLUGS"]
