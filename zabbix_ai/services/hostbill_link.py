"""HostBill <-> Zabbix host linker (v1.5).

Given a Zabbix host (instance, hostid) figure out which HostBill service +
client it belongs to. Resolution order:

  1. Cached row in ``host_hostbill_link`` (if linked within 7 days).
  2. Zabbix host tag ``hostbill_service_id`` (highest confidence).
  3. Match any of the host's interface IPs against HostBill services.
  4. Match the Zabbix hostname / display name against a service ``domain``.

Every successful resolution path UPSERTs ``host_hostbill_link``. A *miss*
also writes a row, with ``linked_by='unlinked'`` so we don't re-query
HostBill on every investigation for hosts that have no service.

The HostBill admin API is not yet reachable in production — the linker
treats *any* exception or empty response as "no match" and degrades to an
unlinked row. The orchestrator can safely call this on every investigation
even when HostBill is fully down.
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from zabbix_ai.memory import Memory

_log = logging.getLogger(__name__)

# Re-link cached rows older than this. Cheap to keep — 7d matches the rate
# at which HostBill service↔IP assignments typically change in our fleet.
_CACHE_TTL = timedelta(days=7)


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class HostBillLink:
    """In-memory representation of one row in ``host_hostbill_link``.

    Mirrors the migration 007 column set 1:1. ``linked_by='unlinked'`` is a
    sentinel meaning "we looked and found nothing" — distinguishes a known
    miss from a never-attempted host.
    """

    zabbix_instance: str
    zabbix_hostid: int
    hostbill_service_id: int | None = None
    hostbill_client_id: int | None = None
    hostbill_client_name: str = ""
    hostbill_domain: str = ""
    linked_by: str = "unlinked"   # 'auto:tag'|'auto:ip'|'auto:hostname'|'manual'|'unlinked'
    confidence: str = "low"       # 'high'|'medium'|'low'
    linked_at: str = ""

    @property
    def is_linked(self) -> bool:
        return self.linked_by != "unlinked" and (
            self.hostbill_service_id is not None
            or self.hostbill_client_id is not None
        )


# ── Persistence helpers ───────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_link(row: tuple, instance: str, hostid: int) -> HostBillLink:
    """Map a fetchone() tuple from host_hostbill_link back to a dataclass."""
    return HostBillLink(
        zabbix_instance=instance,
        zabbix_hostid=hostid,
        hostbill_service_id=row[0] if row[0] is not None else None,
        hostbill_client_id=row[1] if row[1] is not None else None,
        hostbill_client_name=row[2] or "",
        hostbill_domain=row[3] or "",
        linked_at=row[4] or "",
        linked_by=row[5] or "unlinked",
        confidence=row[6] or "low",
    )


async def _load_cached(memory: Memory, instance: str,
                       hostid: int) -> HostBillLink | None:
    """Return the cached link row if present, regardless of age."""
    if memory is None:
        return None
    row = await memory.fetchone(
        """SELECT hostbill_service_id, hostbill_client_id, hostbill_client_name,
                  hostbill_domain, linked_at, linked_by, confidence
           FROM host_hostbill_link
           WHERE zabbix_instance=? AND zabbix_hostid=?""",
        (instance, int(hostid)),
    )
    if row is None:
        return None
    return _row_to_link(row, instance, hostid)


def _cache_fresh(link: HostBillLink) -> bool:
    """True if ``linked_at`` is within ``_CACHE_TTL`` of now."""
    if not link.linked_at:
        return False
    try:
        ts = datetime.fromisoformat(link.linked_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return datetime.now(UTC) - ts < _CACHE_TTL


async def _upsert(memory: Memory, link: HostBillLink) -> None:
    """Write ``link`` into ``host_hostbill_link`` (insert-or-replace)."""
    if memory is None:
        return
    link.linked_at = link.linked_at or _now_iso()
    await memory.execute(
        """INSERT INTO host_hostbill_link
           (zabbix_instance, zabbix_hostid, hostbill_service_id,
            hostbill_client_id, hostbill_client_name, hostbill_domain,
            linked_at, linked_by, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(zabbix_instance, zabbix_hostid) DO UPDATE SET
             hostbill_service_id=excluded.hostbill_service_id,
             hostbill_client_id=excluded.hostbill_client_id,
             hostbill_client_name=excluded.hostbill_client_name,
             hostbill_domain=excluded.hostbill_domain,
             linked_at=excluded.linked_at,
             linked_by=excluded.linked_by,
             confidence=excluded.confidence""",
        (
            link.zabbix_instance, int(link.zabbix_hostid),
            link.hostbill_service_id, link.hostbill_client_id,
            link.hostbill_client_name or None,
            link.hostbill_domain or None,
            link.linked_at, link.linked_by, link.confidence,
        ),
    )


# ── Resolution helpers ────────────────────────────────────────────────────────


def _service_to_link_fields(service: dict) -> dict[str, Any]:
    """Extract (service_id, client_id, client_name, domain) from a service row.

    HostBill responses are uneven across versions — accept several common
    key spellings and fall back to empty strings rather than KeyErroring.
    """
    sid = service.get("id") or service.get("service_id")
    cid = service.get("client_id") or service.get("clientid") or service.get(
        "owner_id"
    )
    client_name = (
        service.get("client_name")
        or service.get("clientname")
        or service.get("firstname", "") + " " + service.get("lastname", "")
    ).strip()
    domain = service.get("domain") or service.get("hostname") or ""
    return {
        "service_id": int(sid) if sid else None,
        "client_id": int(cid) if cid else None,
        "client_name": str(client_name or "")[:255],
        "domain": str(domain or "")[:255],
    }


async def _enrich_from_service(hostbill_client: Any,
                               link: HostBillLink) -> None:
    """Fill blank client_name / domain by fetching the full service row."""
    if hostbill_client is None or link.hostbill_service_id is None:
        return
    try:
        service = await hostbill_client.get_service(link.hostbill_service_id)
    except Exception as e:
        _log.debug("get_service(%s) failed: %s", link.hostbill_service_id, e)
        return
    if not service:
        return
    fields = _service_to_link_fields(service)
    if fields["client_id"] and not link.hostbill_client_id:
        link.hostbill_client_id = fields["client_id"]
    if fields["client_name"] and not link.hostbill_client_name:
        link.hostbill_client_name = fields["client_name"]
    if fields["domain"] and not link.hostbill_domain:
        link.hostbill_domain = fields["domain"]


def _extract_tag_service_id(host: dict) -> int | None:
    """Return the int value of host tag ``hostbill_service_id``, or None."""
    tags = host.get("tags") or []
    for t in tags:
        if (t.get("tag") or "").strip().lower() == "hostbill_service_id":
            val = (t.get("value") or "").strip()
            if not val:
                continue
            with contextlib.suppress(ValueError):
                return int(val)
    return None


def _extract_ips(host: dict) -> list[str]:
    """Unique non-empty IPs from host.interfaces."""
    seen: set[str] = set()
    out: list[str] = []
    for iface in host.get("interfaces") or []:
        ip = (iface.get("ip") or "").strip()
        if not ip or ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def _extract_hostnames(host: dict) -> list[str]:
    """Return [host.host, host.name] lowercased, deduped, non-empty."""
    seen: set[str] = set()
    out: list[str] = []
    for k in ("host", "name"):
        v = (host.get(k) or "").strip().lower()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def _try_link_by_tag(host: dict, hostbill_client: Any,
                           link: HostBillLink) -> bool:
    sid = _extract_tag_service_id(host)
    if sid is None or hostbill_client is None:
        return False
    link.hostbill_service_id = sid
    link.linked_by = "auto:tag"
    link.confidence = "high"
    await _enrich_from_service(hostbill_client, link)
    return True


async def _try_link_by_ip(host: dict, hostbill_client: Any,
                          link: HostBillLink) -> bool:
    if hostbill_client is None:
        return False
    ips = _extract_ips(host)
    matches: list[dict] = []
    for ip in ips:
        try:
            rows = await hostbill_client.search_services(ip=ip)
        except Exception as e:
            _log.debug("search_services(ip=%s) failed: %s", ip, e)
            rows = []
        if rows:
            matches.extend(rows)
    if not matches:
        return False
    # Dedup by service_id
    uniq: dict[int, dict] = {}
    for r in matches:
        fields = _service_to_link_fields(r)
        if fields["service_id"] is None:
            continue
        uniq.setdefault(fields["service_id"], r)
    if not uniq:
        return False
    first_id = next(iter(uniq))
    fields = _service_to_link_fields(uniq[first_id])
    link.hostbill_service_id = fields["service_id"]
    link.hostbill_client_id = fields["client_id"]
    link.hostbill_client_name = fields["client_name"]
    link.hostbill_domain = fields["domain"]
    link.linked_by = "auto:ip"
    link.confidence = "high" if len(uniq) == 1 else "low"
    # Make sure client_name/domain are populated even if list response was sparse
    await _enrich_from_service(hostbill_client, link)
    return True


async def _try_link_by_hostname(host: dict, hostbill_client: Any,
                                link: HostBillLink) -> bool:
    if hostbill_client is None:
        return False
    names = _extract_hostnames(host)
    if not names:
        return False
    # Exact match first, endswith fallback
    for name in names:
        try:
            rows = await hostbill_client.search_services(domain=name)
        except Exception as e:
            _log.debug("search_services(domain=%s) failed: %s", name, e)
            rows = []
        for r in rows:
            fields = _service_to_link_fields(r)
            rd = (fields["domain"] or "").lower()
            if rd == name:
                link.hostbill_service_id = fields["service_id"]
                link.hostbill_client_id = fields["client_id"]
                link.hostbill_client_name = fields["client_name"]
                link.hostbill_domain = fields["domain"]
                link.linked_by = "auto:hostname"
                link.confidence = "medium"
                await _enrich_from_service(hostbill_client, link)
                return True
    # endswith fallback (low confidence)
    for name in names:
        try:
            rows = await hostbill_client.search_services(domain=name)
        except Exception as e:
            _log.debug("search_services(domain=%s) endswith failed: %s", name, e)
            rows = []
        for r in rows:
            fields = _service_to_link_fields(r)
            rd = (fields["domain"] or "").lower()
            if rd and (rd.endswith(name) or name.endswith(rd)):
                link.hostbill_service_id = fields["service_id"]
                link.hostbill_client_id = fields["client_id"]
                link.hostbill_client_name = fields["client_name"]
                link.hostbill_domain = fields["domain"]
                link.linked_by = "auto:hostname"
                link.confidence = "low"
                await _enrich_from_service(hostbill_client, link)
                return True
    return False


# ── Public API ────────────────────────────────────────────────────────────────


async def link_zabbix_host(
    *,
    memory: Memory,
    hostbill_client: Any,
    zabbix_client: Any,
    zabbix_instance: str,
    zabbix_hostid: int,
) -> HostBillLink:
    """Look up or create the link row for one Zabbix host.

    The function is idempotent: calling it repeatedly on the same host is
    cheap (cache hit) and safe (no duplicate rows).
    """
    # 1) Cache lookup
    cached = await _load_cached(memory, zabbix_instance, int(zabbix_hostid))
    if cached is not None and _cache_fresh(cached):
        return cached

    link = HostBillLink(
        zabbix_instance=zabbix_instance,
        zabbix_hostid=int(zabbix_hostid),
    )

    # Fetch the host once — every auto path needs at least tags/interfaces/name.
    host: dict = {}
    try:
        host = await zabbix_client.get_host(int(zabbix_hostid))
    except Exception as e:
        _log.debug("link_zabbix_host: get_host(%s) failed: %s",
                   zabbix_hostid, e)

    # 2/3/4) Tag → IP → Hostname resolution
    for fn in (_try_link_by_tag, _try_link_by_ip, _try_link_by_hostname):
        try:
            if await fn(host, hostbill_client, link):
                break
        except Exception as e:
            # Any unexpected exception inside a resolver counts as "no match";
            # the next resolver in the chain still gets a shot.
            _log.warning("link_zabbix_host: %s failed for host %s: %s",
                         fn.__name__, zabbix_hostid, e)

    # Always upsert — even on miss, so we cache the negative result.
    if not link.is_linked:
        link.linked_by = "unlinked"
        link.confidence = "low"
    link.linked_at = _now_iso()
    try:
        await _upsert(memory, link)
    except Exception as e:
        _log.warning("link_zabbix_host: upsert failed for host %s: %s",
                     zabbix_hostid, e)
    return link


async def get_recent_tickets(
    hostbill_client: Any,
    *,
    client_id: int,
    service_id: int | None = None,
    days: int = 30,
) -> list[dict]:
    """Return up to ~100 recent tickets for a client, never raising.

    Returns ``[]`` when ``hostbill_client`` is None, the API is unreachable,
    or returns no data. The caller surfaces this in the briefing as a
    "Recent tickets (Nd): 0 open / 0 closed" line.
    """
    if hostbill_client is None:
        return []
    date_from = (datetime.now(UTC) - timedelta(days=max(1, days))).strftime(
        "%Y-%m-%d"
    )
    try:
        return await hostbill_client.get_tickets(
            client_id=client_id,
            service_id=service_id,
            date_from=date_from,
        )
    except Exception as e:
        _log.debug("get_recent_tickets(client=%s) failed: %s", client_id, e)
        return []


async def refresh_all_links(
    memory: Memory, hostbill_client: Any,
    zabbix_clients: dict[str, Any],
) -> dict[str, int]:
    """Background sync — walk every Zabbix host on every instance.

    Used by the daily ``start_hostbill_sync`` job. Always returns a stats
    dict {"linked": int, "unlinked": int, "errors": int}.
    """
    stats = {"linked": 0, "unlinked": 0, "errors": 0}
    if not zabbix_clients:
        return stats
    for instance, client in zabbix_clients.items():
        try:
            rows = await client.call(
                "host.get",
                {"output": ["hostid"], "limit": 10000},
            )
        except Exception as e:
            _log.warning("refresh_all_links: host.get(%s) failed: %s",
                         instance, e)
            stats["errors"] += 1
            continue
        for row in rows or []:
            try:
                hostid = int(row["hostid"])
            except (KeyError, TypeError, ValueError):
                stats["errors"] += 1
                continue
            try:
                link = await link_zabbix_host(
                    memory=memory,
                    hostbill_client=hostbill_client,
                    zabbix_client=client,
                    zabbix_instance=instance,
                    zabbix_hostid=hostid,
                )
                if link.is_linked:
                    stats["linked"] += 1
                else:
                    stats["unlinked"] += 1
            except Exception as e:
                _log.warning("refresh_all_links: host %s/%s failed: %s",
                             instance, hostid, e)
                stats["errors"] += 1
    return stats


# ── Background scheduler ──────────────────────────────────────────────────────


def start_hostbill_sync(app: Any, settings: Any, memory: Memory) -> None:
    """Schedule ``refresh_all_links`` once a day on the FastAPI lifespan.

    Stashes the task on ``app.state.hostbill_sync_task`` so the GC doesn't
    drop it mid-loop. Caller (create_app) decides whether to invoke this —
    we intentionally don't modify admin/__init__.py here.
    """
    import asyncio

    async def _loop() -> None:
        # Initial delay so we don't slam HostBill at process boot
        await asyncio.sleep(60)
        while True:
            # Resolve clients lazily — they may not exist at scheduling time
            zabbix_clients = getattr(app.state, "zabbix_clients", {}) or {}
            hostbill_client = getattr(app.state, "hostbill_client", None)
            try:
                stats = await refresh_all_links(
                    memory, hostbill_client, zabbix_clients,
                )
                _log.info("hostbill_sync: %s", stats)
            except Exception as e:
                _log.warning("hostbill_sync iteration failed: %s", e)
            await asyncio.sleep(24 * 3600)

    task = asyncio.create_task(_loop())
    # Keep a reference so the task survives until the app shuts down.
    app.state.hostbill_sync_task = task
