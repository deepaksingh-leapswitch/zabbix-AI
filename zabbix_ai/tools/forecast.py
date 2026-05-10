"""forecast.* and anomaly.* tools — pure-Python predictive analytics.

The AI uses these to answer "is this metric trending toward a problem?"
or "is this current value abnormal compared to its history?". All
implementations are short, dependency-free (no scipy/numpy/sklearn), and
operate on the metric history that `zabbix.get_history` already returns.

Tools provided:
  - forecast.linear      — least-squares linear extrapolation
  - anomaly.iqr          — Tukey IQR outlier detection
  - anomaly.zscore       — z-score outlier detection

Holt-Winters is NOT included in v0.8 — for the on-demand investigation
pattern the AI uses these for, linear extrapolation gives a useful
first answer ("disk fills in ~6 days at current growth") and the AI can
narrate trends from the history directly when seasonality matters.
"""
from __future__ import annotations

import statistics
import time
from typing import Any

from zabbix_ai.tools import register


def _client(ctx: dict, instance: str):
    clients = ctx.get("clients") or {}
    if instance not in clients:
        raise ValueError(f"unknown instance '{instance}'")
    return clients[instance]


def _to_floats(history: list[dict]) -> list[tuple[float, float]]:
    """Convert Zabbix history rows to (epoch_seconds, value) pairs.

    Drops rows whose value can't be parsed as float.
    """
    out: list[tuple[float, float]] = []
    for row in history:
        clock = row.get("clock")
        value = row.get("value")
        if clock is None or value is None:
            continue
        try:
            out.append((float(clock), float(value)))
        except (TypeError, ValueError):
            continue
    return out


def _linear_fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Return slope (units / second) + intercept for least-squares fit."""
    n = len(points)
    if n < 2:
        return 0.0, points[0][1] if points else 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0, mean_y
    slope = num / den
    intercept = mean_y - slope * mean_x
    return slope, intercept


def register_tools() -> None:

    @register(
        "forecast.linear",
        description=(
            "Linear least-squares extrapolation of a Zabbix item's history. "
            "Returns the current value, slope (units per day), projected "
            "value at the requested horizon, and time-to-threshold if a "
            "threshold is given. Use this for "
            "'when does disk hit 90%' / 'memory growth rate' / "
            "'queue trend' style questions."
        ),
        schema={
            "type": "object",
            "properties": {
                "hostid": {"type": "integer"},
                "instance": {"type": "string"},
                "key": {"type": "string"},
                "history_seconds": {
                    "type": "integer", "default": 86400,
                    "description": "How far back to look (default 24h).",
                },
                "horizon_seconds": {
                    "type": "integer", "default": 86400 * 7,
                    "description": "How far forward to project (default 7d).",
                },
                "threshold": {
                    "type": "number",
                    "description": "Optional value to compute time-until-cross "
                                   "(e.g. 90 for disk %used).",
                },
            },
            "required": ["hostid", "instance", "key"],
        },
    )
    async def _forecast_linear(*, hostid: int, instance: str, key: str,
                                history_seconds: int = 86400,
                                horizon_seconds: int = 86400 * 7,
                                threshold: float | None = None,
                                _ctx: dict) -> dict[str, Any]:
        client = _client(_ctx, instance)
        history = await client.get_history(hostid, [key], history_seconds)
        rows = history.get(key, [])
        points = _to_floats(rows)
        if len(points) < 2:
            return {
                "key": key, "samples": len(points),
                "error": "insufficient history (need >=2 samples)",
            }
        slope_per_sec, intercept = _linear_fit(points)
        slope_per_day = slope_per_sec * 86400
        now = time.time()
        current = slope_per_sec * now + intercept
        projected = slope_per_sec * (now + horizon_seconds) + intercept
        result: dict[str, Any] = {
            "key": key,
            "samples": len(points),
            "history_seconds": history_seconds,
            "current_value": round(current, 4),
            "slope_per_day": round(slope_per_day, 6),
            "horizon_seconds": horizon_seconds,
            "projected_value_at_horizon": round(projected, 4),
        }
        if threshold is not None:
            if slope_per_sec == 0:
                result["time_to_threshold_seconds"] = None
                result["time_to_threshold_human"] = "never (flat)"
            else:
                # Solve for t: slope * t + intercept = threshold
                t_cross = (threshold - intercept) / slope_per_sec
                delta = t_cross - now
                result["time_to_threshold_seconds"] = int(delta)
                if delta < 0:
                    result["time_to_threshold_human"] = (
                        f"already crossed ({_human_duration(-delta)} ago)"
                    )
                else:
                    result["time_to_threshold_human"] = _human_duration(delta)
        return result

    @register(
        "anomaly.iqr",
        description=(
            "Tukey IQR-based outlier detection on a Zabbix item's history. "
            "Flags samples outside [Q1 - 1.5*IQR, Q3 + 1.5*IQR]. Returns "
            "summary stats, count of outliers, and the most extreme samples. "
            "Use when an alert says a metric is 'high' / 'low' to confirm "
            "the value is genuinely anomalous vs normal variation."
        ),
        schema={
            "type": "object",
            "properties": {
                "hostid": {"type": "integer"},
                "instance": {"type": "string"},
                "key": {"type": "string"},
                "history_seconds": {"type": "integer", "default": 86400 * 7},
            },
            "required": ["hostid", "instance", "key"],
        },
    )
    async def _anomaly_iqr(*, hostid: int, instance: str, key: str,
                            history_seconds: int = 86400 * 7,
                            _ctx: dict) -> dict[str, Any]:
        client = _client(_ctx, instance)
        history = await client.get_history(hostid, [key], history_seconds)
        rows = history.get(key, [])
        points = _to_floats(rows)
        if len(points) < 4:
            return {"key": key, "samples": len(points),
                    "error": "insufficient history (need >=4 samples)"}
        values = sorted(p[1] for p in points)
        n = len(values)
        q1 = values[n // 4]
        q3 = values[(3 * n) // 4]
        iqr = q3 - q1
        low_bound = q1 - 1.5 * iqr
        high_bound = q3 + 1.5 * iqr
        outliers = [(c, v) for c, v in points
                    if v < low_bound or v > high_bound]
        outliers.sort(key=lambda cv: abs(cv[1] - statistics.median(values)),
                       reverse=True)
        return {
            "key": key,
            "samples": n,
            "min": values[0],
            "max": values[-1],
            "median": statistics.median(values),
            "q1": q1, "q3": q3, "iqr": iqr,
            "low_bound": low_bound, "high_bound": high_bound,
            "outlier_count": len(outliers),
            "top_outliers": [
                {"clock": int(c), "value": v} for c, v in outliers[:10]
            ],
        }

    @register(
        "anomaly.zscore",
        description=(
            "Z-score outlier detection on a Zabbix item's history. Returns "
            "mean, stddev, current value's z-score, and the highest-z "
            "samples. Use when you want to quantify 'how unusual is this "
            "value' on a normally-distributed metric."
        ),
        schema={
            "type": "object",
            "properties": {
                "hostid": {"type": "integer"},
                "instance": {"type": "string"},
                "key": {"type": "string"},
                "history_seconds": {"type": "integer", "default": 86400 * 7},
                "threshold_z": {"type": "number", "default": 3.0},
            },
            "required": ["hostid", "instance", "key"],
        },
    )
    async def _anomaly_zscore(*, hostid: int, instance: str, key: str,
                               history_seconds: int = 86400 * 7,
                               threshold_z: float = 3.0,
                               _ctx: dict) -> dict[str, Any]:
        client = _client(_ctx, instance)
        history = await client.get_history(hostid, [key], history_seconds)
        rows = history.get(key, [])
        points = _to_floats(rows)
        if len(points) < 4:
            return {"key": key, "samples": len(points),
                    "error": "insufficient history (need >=4 samples)"}
        values = [p[1] for p in points]
        mean = statistics.fmean(values)
        stdev = statistics.pstdev(values)
        if stdev == 0:
            return {
                "key": key, "samples": len(values), "mean": mean,
                "stdev": 0.0,
                "note": "all samples identical — no z-score possible",
            }
        scored = [(c, v, (v - mean) / stdev) for c, v in points]
        outliers = [s for s in scored if abs(s[2]) >= threshold_z]
        outliers.sort(key=lambda cvz: abs(cvz[2]), reverse=True)
        latest = scored[-1] if scored else None
        return {
            "key": key,
            "samples": len(values),
            "mean": round(mean, 4),
            "stdev": round(stdev, 4),
            "threshold_z": threshold_z,
            "outlier_count": len(outliers),
            "top_outliers": [
                {"clock": int(c), "value": v, "z": round(z, 2)}
                for c, v, z in outliers[:10]
            ],
            "latest_value": round(latest[1], 4) if latest else None,
            "latest_z": round(latest[2], 2) if latest else None,
        }


def _human_duration(seconds: float) -> str:
    """Render seconds as a human-readable duration (e.g. '6 d 4 h')."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        return f"{h} h {rem // 60} m"
    if seconds < 86400 * 30:
        d, rem = divmod(seconds, 86400)
        return f"{d} d {rem // 3600} h"
    months = seconds // (86400 * 30)
    return f"~{months} mo"
