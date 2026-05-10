import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.tools import dispatch
from zabbix_ai.tools.forecast import register_tools


def _hist(values: list[tuple[float, float]]) -> dict:
    return {"k": [{"clock": str(c), "value": str(v)} for c, v in values]}


@pytest.fixture
def context():
    c = MagicMock()
    c.get_history = AsyncMock(return_value={})
    return {"clients": {"monitoring": c}}


# ─── forecast.linear ───

async def test_linear_forecast_growing_metric(context):
    """Linear extrapolation on a clean +1/day growing metric."""
    now = time.time()
    points = [(now - 86400 * (5 - i), 50 + i) for i in range(6)]  # 50→55 over 5 days
    context["clients"]["monitoring"].get_history = AsyncMock(
        return_value=_hist(points),
    )
    register_tools()
    out = await dispatch(
        "forecast.linear",
        {"hostid": 1, "instance": "monitoring", "key": "k",
         "history_seconds": 86400 * 7, "horizon_seconds": 86400 * 5,
         "threshold": 60.0},
        context=context,
    )
    assert out["samples"] == 6
    assert 0.95 <= out["slope_per_day"] <= 1.05  # ~+1/day
    # current ≈ 55, projected at +5 days ≈ 60 (so threshold should hit ~now+5d)
    assert 59 <= out["projected_value_at_horizon"] <= 61
    assert out["time_to_threshold_seconds"] is not None
    # ~5 days
    assert 86400 * 4 <= out["time_to_threshold_seconds"] <= 86400 * 6


async def test_linear_forecast_flat_metric(context):
    now = time.time()
    points = [(now - 86400 * (5 - i), 100.0) for i in range(6)]
    context["clients"]["monitoring"].get_history = AsyncMock(
        return_value=_hist(points),
    )
    register_tools()
    out = await dispatch(
        "forecast.linear",
        {"hostid": 1, "instance": "monitoring", "key": "k",
         "threshold": 200.0},
        context=context,
    )
    assert abs(out["slope_per_day"]) < 0.001
    assert out["time_to_threshold_human"] == "never (flat)"


async def test_linear_forecast_insufficient_history(context):
    context["clients"]["monitoring"].get_history = AsyncMock(
        return_value=_hist([(1.0, 50.0)]),
    )
    register_tools()
    out = await dispatch(
        "forecast.linear",
        {"hostid": 1, "instance": "monitoring", "key": "k"},
        context=context,
    )
    assert "error" in out


# ─── anomaly.iqr ───

async def test_iqr_flags_outlier(context):
    """Most points around 50, one extreme at 500."""
    now = time.time()
    points = [(now - 60 * (20 - i), 50.0) for i in range(20)]
    points.append((now, 500.0))
    context["clients"]["monitoring"].get_history = AsyncMock(
        return_value=_hist(points),
    )
    register_tools()
    out = await dispatch(
        "anomaly.iqr",
        {"hostid": 1, "instance": "monitoring", "key": "k"},
        context=context,
    )
    assert out["outlier_count"] >= 1
    assert any(o["value"] == 500.0 for o in out["top_outliers"])


async def test_iqr_no_outliers_in_uniform_series(context):
    points = [(i * 60.0, 50.0 + (i % 3)) for i in range(50)]
    context["clients"]["monitoring"].get_history = AsyncMock(
        return_value=_hist(points),
    )
    register_tools()
    out = await dispatch(
        "anomaly.iqr",
        {"hostid": 1, "instance": "monitoring", "key": "k"},
        context=context,
    )
    assert out["outlier_count"] == 0


# ─── anomaly.zscore ───

async def test_zscore_flags_extreme_value(context):
    points = [(i * 60.0, 100.0 + (i % 5)) for i in range(40)]
    points.append((40 * 60.0, 1000.0))
    context["clients"]["monitoring"].get_history = AsyncMock(
        return_value=_hist(points),
    )
    register_tools()
    out = await dispatch(
        "anomaly.zscore",
        {"hostid": 1, "instance": "monitoring", "key": "k",
         "threshold_z": 3.0},
        context=context,
    )
    assert out["outlier_count"] >= 1
    # latest value (1000) should have very high z
    assert abs(out["latest_z"]) > 3.0


async def test_zscore_zero_variance(context):
    points = [(i * 60.0, 42.0) for i in range(20)]
    context["clients"]["monitoring"].get_history = AsyncMock(
        return_value=_hist(points),
    )
    register_tools()
    out = await dispatch(
        "anomaly.zscore",
        {"hostid": 1, "instance": "monitoring", "key": "k"},
        context=context,
    )
    assert out["stdev"] == 0.0
    assert "note" in out
