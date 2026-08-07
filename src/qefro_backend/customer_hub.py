"""Optional Customer Hub client — mirrors ``@qefro-ai/backend`` 1.7.0.

Hub participation is gated by ``QEFRO_CUSTOMER_HUB_ENABLED`` (default false) and
``QEFRO_CUSTOMER_HUB_OPTIONAL`` (default true). When disabled/optional, hub
methods soft-skip (``None`` / no-op) instead of failing the tool. ``ctx.storage``
(when present) remains independent — Hub is never the sole path.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

__all__ = [
    "env_flag_true",
    "is_customer_hub_enabled",
    "is_customer_hub_optional",
    "hub_customer_from_person",
    "pick_identity",
    "read_identity_phone",
    "hub_call",
    "TimelineContext",
    "MembershipContext",
    "ConsentContext",
]


def env_flag_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    switch = raw.strip().lower()
    if switch in ("1", "true", "yes", "on"):
        return True
    if switch in ("0", "false", "no", "off"):
        return False
    return default


def is_customer_hub_enabled() -> bool:
    """Master switch — when false, hub methods soft-skip (never call Hub)."""
    return env_flag_true("QEFRO_CUSTOMER_HUB_ENABLED", False)


def is_customer_hub_optional() -> bool:
    """When true (default), missing hub config returns None / no-ops."""
    return env_flag_true("QEFRO_CUSTOMER_HUB_OPTIONAL", True)


def read_identity_phone(identity: Dict[str, Any]) -> Optional[str]:
    for key in ("phone", "phone_number", "whatsapp_number", "whatsapp"):
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def hub_customer_from_person(person: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(person, dict):
        return None
    person_id = person.get("id")
    if not isinstance(person_id, str) or not person_id.strip():
        return None

    phone = None
    if isinstance(person.get("phone_number"), str) and person["phone_number"]:
        phone = person["phone_number"]
    elif isinstance(person.get("phone"), str) and person["phone"]:
        phone = person["phone"]

    whatsapp = None
    if isinstance(person.get("whatsapp_number"), str) and person["whatsapp_number"]:
        whatsapp = person["whatsapp_number"]
    else:
        whatsapp = phone

    display = None
    if isinstance(person.get("display_name"), str) and person["display_name"]:
        display = person["display_name"]
    elif isinstance(person.get("name"), str) and person["name"]:
        display = person["name"]

    email = person.get("email") if isinstance(person.get("email"), str) else person.get("email", None)
    if email is not None and not isinstance(email, str):
        email = None

    out = dict(person)
    out["id"] = person_id.strip()
    out["phone_number"] = phone
    out["whatsapp_number"] = whatsapp
    out["display_name"] = display
    out["email"] = email
    return out


def pick_identity(
    input_data: Optional[Dict[str, Any]] = None,
    identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = {**(identity or {}), **(input_data or {})}
    phone = None
    if isinstance(merged.get("phone_number"), str) and merged["phone_number"]:
        phone = merged["phone_number"]
    elif isinstance(merged.get("phone"), str) and merged["phone"]:
        phone = merged["phone"]

    whatsapp = merged["whatsapp_number"] if isinstance(merged.get("whatsapp_number"), str) else None
    email = merged["email"] if isinstance(merged.get("email"), str) else None

    display = None
    if isinstance(merged.get("display_name"), str) and merged["display_name"]:
        display = merged["display_name"]
    elif isinstance(merged.get("name"), str) and merged["name"]:
        display = merged["name"]

    if isinstance(merged.get("channel"), str) and merged["channel"]:
        channel = merged["channel"]
    elif whatsapp:
        channel = "whatsapp"
    elif phone:
        channel = "sms"
    elif email:
        channel = "email"
    else:
        channel = "api"

    if isinstance(merged.get("identifier"), str) and merged["identifier"]:
        identifier = merged["identifier"]
    else:
        identifier = whatsapp or phone or email or (
            merged["id"] if isinstance(merged.get("id"), str) else None
        )

    return {
        "id": merged["id"] if isinstance(merged.get("id"), str) else None,
        "phone_number": phone,
        "whatsapp_number": whatsapp,
        "email": email,
        "display_name": display,
        "channel": channel,
        "identifier": identifier,
    }


def _resolve_hub_endpoint(platform: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    from_env = (os.environ.get("QEFRO_CUSTOMER_HUB_URL") or "").rstrip("/")
    customer = (platform or {}).get("customer") if isinstance(platform, dict) else None
    if not isinstance(customer, dict):
        customer = {}
    base_url = (customer.get("base_url") or from_env or "").rstrip("/")
    context = customer.get("context")
    if not base_url or not isinstance(context, dict):
        return None
    if not context.get("tenant_id") or not context.get("workspace_id"):
        return None
    token = (
        customer.get("token")
        or os.environ.get("QEFRO_SERVICE_TOKEN")
        or os.environ.get("QEFRO_INTERNAL_TOKEN")
        or os.environ.get("QEFRO_INTERNAL_BEARER")
        or ""
    )
    return {"base_url": base_url, "token": token, "context": context}


def hub_call(
    platform: Optional[Dict[str, Any]],
    op: str,
    body: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """POST ``/v1/internal/customer-hub/{op}``. Soft-skip or hard-fail per flags."""
    if not is_customer_hub_enabled():
        if is_customer_hub_optional():
            return None
        raise RuntimeError("customer_hub_disabled")

    endpoint = _resolve_hub_endpoint(platform)
    if not endpoint:
        if is_customer_hub_optional():
            return None
        raise RuntimeError("customer_hub_unavailable")

    payload = {**body, "context": endpoint["context"]}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if endpoint["token"]:
        headers["Authorization"] = f"Bearer {endpoint['token']}"

    req = urllib.request.Request(
        f"{endpoint['base_url']}/v1/internal/customer-hub/{op}",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8", errors="replace")
        status = err.code
        if is_customer_hub_optional() and (status == 404 or status >= 500):
            return None
        raise RuntimeError(f"customer_hub.{op} failed ({status}): {text}") from err
    except urllib.error.URLError as err:
        if is_customer_hub_optional():
            return None
        raise RuntimeError(f"customer_hub.{op} failed: {err}") from err

    if status < 200 or status >= 300:
        if is_customer_hub_optional() and (status == 404 or status >= 500):
            return None
        raise RuntimeError(f"customer_hub.{op} failed ({status}): {text}")

    if not text:
        return {}
    return json.loads(text)


class TimelineContext:
    def __init__(self, *, platform: Optional[Dict[str, Any]], state: Any):
        self._platform = platform
        self._state = state

    async def append(self, input_data: Dict[str, Any]) -> None:
        event_type = str(input_data.get("event_type") or "").strip()
        if not event_type:
            raise RuntimeError("timeline_event_empty")
        customer_id = input_data.get("customer_id")
        if not customer_id and isinstance(self._state.current, dict):
            customer_id = self._state.current.get("id")
        if not customer_id:
            if is_customer_hub_optional():
                return
            raise RuntimeError("customer_not_found")
        await _async_hub_call(
            self._platform,
            "timeline_append",
            {
                "customer_id": customer_id,
                "event_type": event_type,
                "payload": input_data.get("payload") or {},
                "source": input_data.get("source") or "sdk",
            },
        )


class MembershipContext:
    def __init__(
        self,
        *,
        platform: Optional[Dict[str, Any]],
        state: Any,
        solution_id: Optional[str] = None,
    ):
        self._platform = platform
        self._state = state
        self._solution_id = solution_id

    def _customer_id_or_optional(self, input_data: Optional[Dict[str, Any]]) -> Optional[str]:
        customer_id = (input_data or {}).get("customer_id")
        if not customer_id and isinstance(self._state.current, dict):
            customer_id = self._state.current.get("id")
        if customer_id:
            return customer_id
        if is_customer_hub_optional():
            return None
        raise RuntimeError("customer_not_found")

    async def attach(self, input_data: Optional[Dict[str, Any]] = None) -> None:
        customer_id = self._customer_id_or_optional(input_data)
        if not customer_id:
            return
        data = input_data or {}
        await _async_hub_call(
            self._platform,
            "membership_attach",
            {
                "customer_id": customer_id,
                "solution_id": data.get("solution_id") or self._solution_id,
                "role": data.get("role"),
                "metadata": data.get("metadata") or {},
            },
        )

    async def detach(self, input_data: Optional[Dict[str, Any]] = None) -> None:
        customer_id = self._customer_id_or_optional(input_data)
        if not customer_id:
            return
        data = input_data or {}
        await _async_hub_call(
            self._platform,
            "membership_detach",
            {
                "customer_id": customer_id,
                "solution_id": data.get("solution_id") or self._solution_id,
                "role": data.get("role"),
                "metadata": data.get("metadata") or {},
            },
        )


class ConsentContext:
    def __init__(self, *, platform: Optional[Dict[str, Any]], state: Any):
        self._platform = platform
        self._state = state

    def _customer_id_or_optional(self, input_data: Dict[str, Any]) -> Optional[str]:
        customer_id = input_data.get("customer_id")
        if not customer_id and isinstance(self._state.current, dict):
            customer_id = self._state.current.get("id")
        if customer_id:
            return customer_id
        if is_customer_hub_optional():
            return None
        raise RuntimeError("customer_not_found")

    async def grant(self, input_data: Dict[str, Any]) -> None:
        purpose = str(input_data.get("purpose") or "").strip()
        if not purpose:
            raise RuntimeError("consent_purpose_empty")
        customer_id = self._customer_id_or_optional(input_data)
        if not customer_id:
            return
        await _async_hub_call(
            self._platform,
            "consent_grant",
            {
                "customer_id": customer_id,
                "purpose": purpose,
                "metadata": input_data.get("metadata") or {},
            },
        )

    async def revoke(self, input_data: Dict[str, Any]) -> None:
        purpose = str(input_data.get("purpose") or "").strip()
        if not purpose:
            raise RuntimeError("consent_purpose_empty")
        customer_id = self._customer_id_or_optional(input_data)
        if not customer_id:
            return
        await _async_hub_call(
            self._platform,
            "consent_revoke",
            {
                "customer_id": customer_id,
                "purpose": purpose,
                "metadata": input_data.get("metadata") or {},
            },
        )


async def _async_hub_call(
    platform: Optional[Dict[str, Any]],
    op: str,
    body: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    # hub_call is sync (urllib); wrap so handlers stay async-friendly.
    return hub_call(platform, op, body)
