"""Unit tests for zabbix_ai.services.pricing — token cost math."""
from __future__ import annotations

from zabbix_ai.services.pricing import (
    DEFAULT_USD_TO_INR,
    MODEL_PRICING,
    cost_inr,
    cost_usd,
)


def test_sonnet_input_cost_per_mtok() -> None:
    # 1M input tokens on sonnet → $3.00
    assert abs(cost_usd("claude-sonnet-4-6", 1_000_000, 0) - 3.00) < 0.01


def test_sonnet_output_cost_per_mtok() -> None:
    # 1M output tokens on sonnet → $15.00
    assert abs(cost_usd("claude-sonnet-4-6", 0, 1_000_000) - 15.00) < 0.01


def test_haiku_pricing() -> None:
    # 1M in + 1M out on haiku → $0.80 + $4.00
    assert abs(cost_usd("claude-haiku-4-5-20251001",
                        1_000_000, 1_000_000) - 4.80) < 0.01


def test_cost_inr_matches_usd_times_default_fx() -> None:
    usd = cost_usd("claude-sonnet-4-6", 500_000, 500_000)
    inr = cost_inr("claude-sonnet-4-6", 500_000, 500_000)
    assert abs(inr - usd * DEFAULT_USD_TO_INR) < 0.01


def test_cost_inr_custom_fx_rate() -> None:
    usd = cost_usd("claude-sonnet-4-6", 1_000_000, 0)  # $3.00
    inr = cost_inr("claude-sonnet-4-6", 1_000_000, 0, fx_usd_to_inr=90.0)
    assert abs(inr - usd * 90.0) < 0.01


def test_unknown_model_falls_back_to_sonnet() -> None:
    # Should not raise; falls back to sonnet pricing.
    expected = cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
    actual = cost_usd("some-future-model-xyz", 1_000_000, 1_000_000)
    assert abs(actual - expected) < 0.01


def test_zero_tokens_zero_cost() -> None:
    assert cost_usd("claude-sonnet-4-6", 0, 0) == 0.0
    assert cost_inr("claude-sonnet-4-6", 0, 0) == 0.0
    assert cost_usd("claude-haiku-4-5-20251001", 0, 0) == 0.0


def test_model_pricing_table_has_required_models() -> None:
    assert "claude-sonnet-4-6" in MODEL_PRICING
    assert "claude-haiku-4-5-20251001" in MODEL_PRICING
