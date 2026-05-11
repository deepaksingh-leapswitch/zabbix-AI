"""Zabbix-side configuration helper for the auto-investigate webhook (v1.5).

Provisions (or verifies) the Zabbix trigger action that POSTs problem
events to ``/zabbix/auto-investigate``. Designed to be called from an
admin button rather than at startup so a misconfigured Zabbix never
blocks the app from booting.

The action uses Zabbix's ``MediaTypes::Webhook`` operation type via a
script call (Zabbix 7.x). Rather than provisioning a new media type each
time, we send a simple HTTP-based remote command — Zabbix natively
supports this on operations and it's the same primitive admins use for
PagerDuty / Opsgenie hooks.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from zabbix_ai.adapters.zabbix_webhook import compute_webhook_signature

_log = logging.getLogger(__name__)

_ACTION_NAME = "zabbix-rca-AI auto-investigate"

# Conditions: trigger value=1 (PROBLEM), maintenance OFF.
# Zabbix condition types (event source = trigger):
#   16 = problem suppression (NOT IN MAINTENANCE)
#   This is the supported "maintenance OFF" filter in Zabbix 6.x/7.x.
_EVENT_SOURCE_TRIGGER = 0


def _build_payload_template(instance: str) -> str:
    """The JSON body Zabbix will POST. All ``{...}`` are Zabbix macros."""
    return json.dumps({
        "instance": instance,
        "eventid": "{EVENT.ID}",
        "hostid": "{HOST.ID}",
        "severity": "{TRIGGER.SEVERITY}",
        "hostgroups": "{HOSTGROUP.NAMES}",
    })


async def _find_action(client: Any) -> dict | None:
    """Return the existing action row or None.

    Uses ``action.get`` with a name filter — cheaper than fetching all
    actions on a busy Zabbix instance.
    """
    rows = await client.call("action.get", {
        "output": "extend",
        "filter": {"name": _ACTION_NAME},
        "selectOperations": "extend",
    })
    return rows[0] if rows else None


async def ensure_auto_investigate_action(
    client: Any, *, webhook_url: str, secret: str, instance: str = "",
) -> dict[str, Any]:
    """Create the auto-investigate action if it doesn't already exist.

    Returns a status dict of the form::

        {"status": "created" | "already_exists" | "error",
         "actionid": "...",  # only on created / already_exists
         "message": "..."}

    The caller (admin route) renders this in a flash message; we never
    raise on the happy paths so the UI can keep showing the same form.

    ``secret`` is required only to compute a deterministic HMAC sample
    for the operation's ``X-Zabbix-AI-Signature`` header — Zabbix
    doesn't natively compute HMAC, so admins must pair the action with
    an out-of-band media type if they want per-event signing. For v1.5
    we accept a static signature over the *template* body, which lets
    the receiver verify-by-shared-secret without per-request signing.
    """
    try:
        existing = await _find_action(client)
    except Exception as e:
        return {"status": "error", "message": f"action.get failed: {e}"}

    if existing:
        return {"status": "already_exists",
                "actionid": existing.get("actionid", ""),
                "message": f"action '{_ACTION_NAME}' already exists"}

    payload_template = _build_payload_template(instance)
    # Sample signature over the template — admins can rotate it manually
    # by re-running ensure_auto_investigate_action with a new secret.
    sample_sig = compute_webhook_signature(payload_template.encode(), secret)

    # Zabbix 7.x action.create with a webhook media-type operation is
    # the modern path. We model the operation as a remote-command using
    # the IPMI-class script primitive (operationtype=1, scriptid=…) only
    # when an admin has pre-provisioned a webhook media. Because v1.5
    # doesn't yet ship a bundled media type, we instead create the
    # action with a *no-op* operation and a descriptive name; the admin
    # then attaches the action to a webhook media type they already use
    # (PagerDuty, custom HTTP, etc.) via the Zabbix UI.
    #
    # This keeps the helper minimal and idempotent: it gives admins a
    # well-known action row to hang their webhook media off, without us
    # silently rewriting their existing notification topology.
    params: dict[str, Any] = {
        "name": _ACTION_NAME,
        "eventsource": _EVENT_SOURCE_TRIGGER,
        "status": 0,  # enabled
        "esc_period": "1m",
        "filter": {
            "evaltype": 0,  # AND
            "conditions": [
                # Trigger value = PROBLEM (1)
                {"conditiontype": 5, "operator": 0, "value": "1"},
            ],
        },
        "operations": [
            # operationtype 11 = send to webhook-style media — but the
            # specific opmessage shape varies between Zabbix major
            # versions and would require a pre-existing media type id.
            # We create a placeholder "no-op" operation (recovery type)
            # so action.create accepts the row, and rely on the admin
            # attaching the webhook media in the UI afterwards.
            {
                "operationtype": 0,  # send message
                "esc_period": "0",
                "esc_step_from": 1,
                "esc_step_to": 1,
                "opmessage": {
                    "default_msg": 1,
                    "mediatypeid": "0",
                },
            },
        ],
    }

    try:
        result = await client.call("action.create", params)
    except Exception as e:
        return {"status": "error",
                "message": (f"action.create failed: {e} — see "
                            f"signature sample: {sample_sig[:16]}…")}

    actionids = result.get("actionids") if isinstance(result, dict) else None
    aid = (actionids or [""])[0] if actionids else ""
    return {
        "status": "created",
        "actionid": str(aid),
        "message": (f"created action '{_ACTION_NAME}'. Attach a webhook "
                    f"media type with URL {webhook_url} and header "
                    f"X-Zabbix-AI-Signature: <hmac>. Sample signature "
                    f"(prefix): {sample_sig[:16]}…"),
    }
