"""Organization capability registration (ADR-005 Phase 1).

Apps register metadata only — events, actions, tasks. The platform owns the
capability graph, workflows, and inbox. Templates are organization-owned (Phase 4).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from .customer_hub import env_flag_true


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"organization: {path} must be a non-empty string")
    return value.strip()


def _optional_str(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"organization: {path} must be a string")
    trimmed = value.strip()
    return trimmed or None


def _optional_dict(value: Any, path: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"organization: {path} must be an object")
    return value


def _assert_unique(ids: List[str], kind: str) -> None:
    seen = set()
    for i in ids:
        if i in seen:
            raise ValueError(f'organization: duplicate {kind} id "{i}"')
        seen.add(i)


def _assert_opaque(id_: str, path: str) -> None:
    if "." in id_:
        raise ValueError(
            f'organization: {path} must be an opaque capability id (no \'.\' / app prefix); got "{id_}"'
        )


def _as_dict(raw: Any, index: int, kind: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"organization: {kind}[{index}] must be an object")
    return raw


@dataclass
class OrganizationEvent:
    id: str
    label: Optional[str] = None
    description: Optional[str] = None
    payloadSchema: Optional[Dict[str, Any]] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id}
        if self.label is not None:
            out["label"] = self.label
        if self.description is not None:
            out["description"] = self.description
        if self.payloadSchema is not None:
            out["payloadSchema"] = self.payloadSchema
        return out


@dataclass
class OrganizationAction:
    id: str
    label: Optional[str] = None
    description: Optional[str] = None
    inputSchema: Optional[Dict[str, Any]] = None
    outputSchema: Optional[Dict[str, Any]] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id}
        if self.label is not None:
            out["label"] = self.label
        if self.description is not None:
            out["description"] = self.description
        if self.inputSchema is not None:
            out["inputSchema"] = self.inputSchema
        if self.outputSchema is not None:
            out["outputSchema"] = self.outputSchema
        return out


@dataclass
class OrganizationTaskType:
    id: str
    label: str
    description: Optional[str] = None
    suggested_workspace_type: Optional[str] = None
    suggested_team: Optional[str] = None
    priority: Optional[str] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "label": self.label}
        if self.description is not None:
            out["description"] = self.description
        if self.suggested_workspace_type is not None:
            out["suggested_workspace_type"] = self.suggested_workspace_type
        if self.suggested_team is not None:
            out["suggested_team"] = self.suggested_team
        if self.priority is not None:
            out["priority"] = self.priority
        return out


@dataclass
class OrganizationDefinition:
    version: Optional[int] = None
    events: Optional[List[Any]] = None
    actions: Optional[List[Any]] = None
    tasks: Optional[List[Any]] = None


@dataclass
class OrganizationCapabilities:
    version: int = 1
    events: List[OrganizationEvent] = field(default_factory=list)
    actions: List[OrganizationAction] = field(default_factory=list)
    tasks: List[OrganizationTaskType] = field(default_factory=list)

    def to_capability(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "metadata": {
                "events": [e.to_wire() for e in self.events],
                "actions": [a.to_wire() for a in self.actions],
                "tasks": [t.to_wire() for t in self.tasks],
            },
        }


def _validate_event(raw: Any, index: int) -> OrganizationEvent:
    e = _as_dict(raw, index, "events")
    id_ = _require_str(e.get("id"), f"events[{index}].id")
    _assert_opaque(id_, f"events[{index}].id")
    return OrganizationEvent(
        id=id_,
        label=_optional_str(e.get("label"), f"events[{index}].label"),
        description=_optional_str(e.get("description"), f"events[{index}].description"),
        payloadSchema=_optional_dict(e.get("payloadSchema"), f"events[{index}].payloadSchema"),
    )


def _validate_action(raw: Any, index: int) -> OrganizationAction:
    a = _as_dict(raw, index, "actions")
    id_ = _require_str(a.get("id"), f"actions[{index}].id")
    _assert_opaque(id_, f"actions[{index}].id")
    return OrganizationAction(
        id=id_,
        label=_optional_str(a.get("label"), f"actions[{index}].label"),
        description=_optional_str(a.get("description"), f"actions[{index}].description"),
        inputSchema=_optional_dict(a.get("inputSchema"), f"actions[{index}].inputSchema"),
        outputSchema=_optional_dict(a.get("outputSchema"), f"actions[{index}].outputSchema"),
    )


def _validate_task(raw: Any, index: int) -> OrganizationTaskType:
    t = _as_dict(raw, index, "tasks")
    id_ = _require_str(t.get("id"), f"tasks[{index}].id")
    _assert_opaque(id_, f"tasks[{index}].id")
    suggested = t.get("suggested_workspace_type")
    if suggested is None:
        suggested = t.get("suggestedWorkspaceType")
    team = t.get("suggested_team")
    if team is None:
        team = t.get("suggestedTeam")
    return OrganizationTaskType(
        id=id_,
        label=_require_str(t.get("label"), f"tasks[{index}].label"),
        description=_optional_str(t.get("description"), f"tasks[{index}].description"),
        suggested_workspace_type=_optional_str(suggested, f"tasks[{index}].suggested_workspace_type"),
        suggested_team=_optional_str(team, f"tasks[{index}].suggested_team"),
        priority=_optional_str(t.get("priority"), f"tasks[{index}].priority"),
    )


def validate_organization_definition(
    defn: Union[OrganizationDefinition, Dict[str, Any]],
) -> OrganizationCapabilities:
    """Validate and normalize an organization definition."""
    if isinstance(defn, OrganizationDefinition):
        raw = {
            "version": defn.version,
            "events": defn.events,
            "actions": defn.actions,
            "tasks": defn.tasks,
        }
    elif isinstance(defn, dict):
        raw = defn
    else:
        raise ValueError("organization: definition must be an object")

    if raw.get("templates") is not None:
        raise ValueError(
            "organization: templates are organization-owned assets (Phase 4); do not publish from apps"
        )

    version = raw.get("version")
    if version is None:
        version = 1
    elif not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("organization: version must be a positive integer")

    events_raw = raw.get("events") or []
    actions_raw = raw.get("actions") or []
    tasks_raw = raw.get("tasks") or []

    for name, arr in (
        ("events", events_raw),
        ("actions", actions_raw),
        ("tasks", tasks_raw),
    ):
        if not isinstance(arr, list):
            raise ValueError(f"organization: {name} must be an array")

    events = [_validate_event(e, i) for i, e in enumerate(events_raw)]
    actions = [_validate_action(a, i) for i, a in enumerate(actions_raw)]
    tasks = [_validate_task(t, i) for i, t in enumerate(tasks_raw)]

    _assert_unique([e.id for e in events], "event")
    _assert_unique([a.id for a in actions], "action")
    _assert_unique([t.id for t in tasks], "task")
    _assert_unique(
        [e.id for e in events] + [a.id for a in actions] + [t.id for t in tasks],
        "capability",
    )

    return OrganizationCapabilities(
        version=version,
        events=events,
        actions=actions,
        tasks=tasks,
    )


def to_organization_capability(caps: OrganizationCapabilities) -> Dict[str, Any]:
    """Build the capabilities.list.organization wire object."""
    return caps.to_capability()


def is_organization_enabled() -> bool:
    return env_flag_true("QEFRO_ORGANIZATION_ENABLED", False)


class OrganizationContext:
    """Thin get-own-capabilities client via ``platform.organization``. Registry read only."""

    def __init__(self, platform: Optional[Dict[str, Any]] = None):
        self._platform = platform if isinstance(platform, dict) else {}

    async def get_capabilities(self) -> Optional[Dict[str, Any]]:
        if not is_organization_enabled():
            return None
        binding = self._platform.get("organization") if isinstance(self._platform, dict) else None
        if not isinstance(binding, dict):
            binding = {}
        from_env = (os.environ.get("QEFRO_ORGANIZATION_URL") or "").rstrip("/")
        base_url = (binding.get("base_url") or from_env or "").rstrip("/")
        context = binding.get("context")
        if not base_url or not isinstance(context, dict):
            return None
        if not context.get("tenant_id") or not context.get("workspace_id"):
            return None
        solution_id = context.get("solution_id")
        if not solution_id:
            return None
        token = (
            binding.get("token")
            or os.environ.get("QEFRO_SERVICE_TOKEN")
            or os.environ.get("QEFRO_INTERNAL_TOKEN")
            or os.environ.get("QEFRO_INTERNAL_BEARER")
            or ""
        )
        url = (
            f"{base_url}/v1/internal/organization/capabilities/"
            f"{urllib.parse.quote(str(solution_id), safe='')}"
        )
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers["x-qefro-tenant-id"] = str(context["tenant_id"])
        headers["x-qefro-workspace-id"] = str(context["workspace_id"])
        if context.get("installation_id"):
            headers["x-qefro-installation-id"] = str(context["installation_id"])
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8")
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            text = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"organization.getCapabilities failed ({err.code}): {text}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"organization.getCapabilities failed: {err}") from err
        if status < 200 or status >= 300:
            raise RuntimeError(f"organization.getCapabilities failed ({status}): {text}")
        if not text:
            return None
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None


def build_organization_context(platform: Optional[Dict[str, Any]] = None) -> OrganizationContext:
    return OrganizationContext(platform)


__all__ = [
    "OrganizationEvent",
    "OrganizationAction",
    "OrganizationTaskType",
    "OrganizationDefinition",
    "OrganizationCapabilities",
    "OrganizationContext",
    "validate_organization_definition",
    "to_organization_capability",
    "is_organization_enabled",
    "build_organization_context",
]
