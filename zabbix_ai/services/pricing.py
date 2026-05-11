"""Anthropic model pricing — USD per million tokens, with INR conversion.

Pricing as of 2026 per anthropic.com/pricing. Update if Anthropic publishes
new rates. The dashboard reads these constants but overrides are honoured
from settings (admin/connections/system page).
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_USD_TO_INR = 83.0


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    # Anthropic also bills cache_read at ~0.1x input and cache_write at
    # ~1.25x input. We ignore those for now (tracked separately would
    # require schema changes).


MODEL_PRICING: dict[str, ModelPrice] = {
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00),
    "claude-haiku-4-5-20251001": ModelPrice(0.80, 4.00),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    p = MODEL_PRICING.get(model) or MODEL_PRICING["claude-sonnet-4-6"]
    return (tokens_in / 1_000_000) * p.input_usd_per_mtok \
         + (tokens_out / 1_000_000) * p.output_usd_per_mtok


def cost_inr(model: str, tokens_in: int, tokens_out: int,
             fx_usd_to_inr: float = DEFAULT_USD_TO_INR) -> float:
    return cost_usd(model, tokens_in, tokens_out) * fx_usd_to_inr
