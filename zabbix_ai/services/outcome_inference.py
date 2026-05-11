"""Outcome inference (v1.5) — did the AI's recommendation actually work?

For every investigation that has been marked resolved (``resolution_at``
is set), this service fetches the relevant host metric history around
the resolution timestamp and checks whether the metric moved in the
"good" direction by more than a fixed threshold. The result is written
to ``investigations.outcome_inferred`` as JSON.

We deliberately *do not* try to match individual suggested-action lines
against specific metrics in v1.5 — that requires semantic parsing of
free-text and is brittle. v1.5 records metric recovery; v1.6 may layer
per-action attribution on top.

Public surface:

* :func:`infer_outcome` — process a single investigation row.
* :func:`run_outcome_inference_loop` — background loop polled every
  10 minutes by ``create_app`` (the integrator wires it in).
* :func:`start_outcome_inference` — convenience start helper.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zabbix_ai.config import Settings
    from zabbix_ai.memory import Memory

_log = logging.getLogger(__name__)


# Three days of context around resolution_at. The Zabbix history.get is
# upper-bound 200 samples per call (see ZabbixClient.get_history), so
# the bin between "before" and "after" stays small enough to be useful.
_HISTORY_WINDOW_SECONDS = 86_400 * 3

# "Good direction" deltas. Tuned conservatively — the goal is to avoid
# false positives more than to maximise coverage. v1.6 can replace this
# with per-metric thresholds learned from history.
_DEFAULT_THRESHOLD_PCT = 10.0


# ─── pattern → metric routing ────────────────────────────────────────────


def _metric_for_pattern(pattern_signature: str, summary: str) -> tuple[str, str] | None:
    """Return a (key_pattern, direction) tuple, or None if we have no
    routing rule.

    ``direction`` is one of:
      * ``"down_good"`` — a drop indicates recovery (e.g. disk %used).
      * ``"up_good"`` — a rise indicates recovery (e.g. memory available).
    """
    # Pattern signatures are SHA-256 hex truncated to 16 chars in
    # memory.compute_pattern_signature; we can't reverse them, so the
    # heuristic looks at the AI's summary text. Callers pass the
    # signature mainly to give us a stable cache key for future work.
    blob = (summary or "").lower()
    if any(tok in blob for tok in ("disk", "fs.size", "filesystem", "pused", "vfs.fs")):
        return ("vfs.fs.size", "down_good")
    if any(tok in blob for tok in ("vm.memory", "memory available",
                                    "memory size[available]", "out of memory")):
        return ("vm.memory.size[available]", "up_good")
    if any(tok in blob for tok in ("cpu utilization", "cpu util", "system.cpu.util",
                                    "high cpu")):
        return ("system.cpu.util", "down_good")
    # Fallback: look for the first key-shaped token in the summary.
    m = re.search(r"\b((?:vfs|system|net|vm|proc|agent)\.[a-zA-Z0-9._\[\],\-]+)",
                   summary or "")
    if m:
        token = m.group(1)
        # Default to "down_good" — most "high X" alerts are about
        # exceeding a threshold from below.
        return (token, "down_good")
    return None


# ─── sample picking ──────────────────────────────────────────────────────


def _pick_sample_before(samples: list[dict], target_clock: int) -> float | None:
    """Largest ``clock`` value strictly < target_clock, or None."""
    best: tuple[int, float] | None = None
    for s in samples:
        c = int(s.get("clock") or 0)
        if c < target_clock:
            try:
                v = float(s.get("value"))
            except (TypeError, ValueError):
                continue
            if best is None or c > best[0]:
                best = (c, v)
    return best[1] if best else None


def _pick_sample_after(samples: list[dict], target_clock: int) -> float | None:
    """Smallest ``clock`` >= target_clock, or None."""
    best: tuple[int, float] | None = None
    for s in samples:
        c = int(s.get("clock") or 0)
        if c >= target_clock:
            try:
                v = float(s.get("value"))
            except (TypeError, ValueError):
                continue
            if best is None or c < best[0]:
                best = (c, v)
    return best[1] if best else None


def _good_delta(before: float, after: float, *, direction: str) -> bool:
    """Did the metric move in the recovery direction by more than the threshold?"""
    if before == 0.0:
        # Avoid division by zero — fall back to absolute change.
        return abs(after - before) > _DEFAULT_THRESHOLD_PCT
    pct = 100.0 * (after - before) / abs(before)
    if direction == "down_good":
        return pct <= -_DEFAULT_THRESHOLD_PCT
    if direction == "up_good":
        return pct >= _DEFAULT_THRESHOLD_PCT
    return False


# ─── single-row inference ────────────────────────────────────────────────


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(ts)
    return None


async def infer_outcome(memory: Memory, client: Any, *,
                         investigation_id: int) -> dict | None:
    """Compute and persist the ``outcome_inferred`` JSON for one row.

    Returns the dict that was written, or ``None`` if the investigation
    can't be evaluated (no resolution_at, no matching metric, no usable
    samples).
    """
    row = await memory.fetchone(
        """SELECT hostid, pattern_signature, suggested_actions,
                  summary, started_at, resolution_at
           FROM investigations WHERE id=?""",
        (investigation_id,),
    )
    if not row:
        return None
    hostid, sig, _actions, summary, _started, resolution_at = row
    if not hostid or not resolution_at:
        return None

    res_dt = _parse_iso(resolution_at)
    if res_dt is None:
        return None

    routing = _metric_for_pattern(sig or "", summary or "")
    if routing is None:
        return None
    key, direction = routing

    try:
        history = await client.get_history(
            int(hostid), [key], range_seconds=_HISTORY_WINDOW_SECONDS,
        )
    except Exception as e:
        _log.debug("infer_outcome: get_history(%s,%s) failed: %s",
                   hostid, key, e)
        return None
    if not history:
        return None

    # Zabbix returns possibly multiple matching items (e.g. one
    # vfs.fs.size[*,pused] per filesystem). Pick the one with the most
    # samples — that's usually the system disk.
    best_key = max(history.keys(),
                    key=lambda k: len(history.get(k) or []), default=None)
    if not best_key:
        return None
    samples = history[best_key]
    if not samples:
        return None

    res_clock = int(res_dt.timestamp())
    before = _pick_sample_before(samples, res_clock)
    # "2h after" is the heuristic the spec calls for; we use 2h
    # symmetrically because most metrics smooth out within that window.
    after_target = res_clock + 2 * 3600
    after = _pick_sample_after(samples, after_target)
    if after is None:
        # No sample after the +2h mark — try anything strictly after
        # res_clock so we can still produce a partial answer.
        after = _pick_sample_after(samples, res_clock + 1)
    if before is None or after is None:
        return None

    delta = after - before
    recovered = _good_delta(before, after, direction=direction)
    payload: dict[str, Any] = {
        "metric": best_key,
        "before": before,
        "after": after,
        "delta": delta,
        "direction": direction,
        "recovered": recovered,
        # v1.5 can't attribute recovery to specific suggested-action
        # indexes; record an empty list so v1.6 consumers can rely on
        # the key being present.
        "effective_action_indexes": [],
        "confidence": "high" if recovered else "low",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "samples_count": len(samples),
    }
    await memory.execute(
        "UPDATE investigations SET outcome_inferred=? WHERE id=?",
        (json.dumps(payload), investigation_id),
    )
    return payload


# ─── background loop ─────────────────────────────────────────────────────


async def _candidates(memory: Memory, *, limit: int) -> list[tuple[int, int]]:
    """Investigations marked resolved in the last 3 days that haven't yet
    been outcome-evaluated. Returns (id, hostid) tuples."""
    cutoff = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    rows = await memory.fetchall(
        """SELECT id, hostid
           FROM investigations
           WHERE resolution_at IS NOT NULL
             AND outcome_inferred IS NULL
             AND resolution_at >= ?
           ORDER BY resolution_at DESC
           LIMIT ?""",
        (cutoff, limit),
    )
    return [(int(r[0]), int(r[1]) if r[1] is not None else 0) for r in rows]


async def run_outcome_inference_loop(
    memory: Memory, settings: Settings, *,
    clients: dict[str, Any] | None = None,
    interval_seconds: int = 600,
    batch_size: int = 50,
) -> None:
    """Polling loop. Pulls up to ``batch_size`` candidates per cycle.

    ``clients`` is a ``{instance_name: ZabbixClient}`` map. The
    investigation's ``instance`` column tells us which client to use; if
    that instance isn't in the map we silently skip the row.
    """
    _ = settings  # currently unused; reserved for future tuning knobs
    while True:
        try:
            rows = await _candidates(memory, limit=batch_size)
            for inv_id, _hostid in rows:
                inst_row = await memory.fetchone(
                    "SELECT instance FROM investigations WHERE id=?",
                    (inv_id,),
                )
                if not inst_row:
                    continue
                instance = inst_row[0]
                client = (clients or {}).get(instance)
                if client is None:
                    _log.debug(
                        "outcome_inference: no client for instance=%r "
                        "(inv #%d) — skipping", instance, inv_id,
                    )
                    continue
                try:
                    await infer_outcome(memory, client,
                                         investigation_id=inv_id)
                except Exception as e:
                    # Loop must survive any single-row failure.
                    _log.warning("outcome_inference: inv %d failed: %s",
                                  inv_id, e)
        except Exception as e:
            _log.warning("outcome_inference loop iteration failed: %s", e)
        await asyncio.sleep(max(30, interval_seconds))


def start_outcome_inference(
    memory: Memory, settings: Settings, *,
    clients: dict[str, Any] | None = None,
    interval_seconds: int = 600,
) -> asyncio.Task:
    """Spawn the background task. Returned task is suitable for
    cancellation on shutdown.
    """
    coro = run_outcome_inference_loop(
        memory, settings, clients=clients,
        interval_seconds=interval_seconds,
    )
    return asyncio.create_task(coro, name="outcome_inference_loop")
