"""Business Events an SDK connection may emit into the Qefro event bus."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+$")


def is_business_event_type(name: str) -> bool:
    t = (name or "").strip().lower()
    return bool(_EVENT_TYPE.match(t)) and "(" not in t


@dataclass
class BusinessEventField:
    path: str
    label: str
    type: Optional[str] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"path": self.path, "label": self.label}
        if self.type is not None:
            out["type"] = self.type
        return out


@dataclass
class BusinessEventDefinition:
    event_type: str
    version: int = 1
    label: Optional[str] = None
    description: Optional[str] = None
    schema: Optional[Dict[str, Any]] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"event_type": self.event_type, "version": self.version}
        if self.label is not None:
            out["label"] = self.label
        if self.description is not None:
            out["description"] = self.description
        if self.schema is not None:
            out["schema"] = self.schema
        return out


def normalize_business_event(def_: Any) -> BusinessEventDefinition:
    if isinstance(def_, BusinessEventDefinition):
        raw = {
            "event_type": def_.event_type,
            "version": def_.version,
            "label": def_.label,
            "description": def_.description,
            "schema": def_.schema,
        }
    elif isinstance(def_, dict):
        raw = def_
    else:
        raise ValueError("businessEvent() requires an object")

    event_type = str(raw.get("event_type") or "").strip().lower()
    if not is_business_event_type(event_type):
        raise ValueError(
            'businessEvent() requires a Business Event name such as quotation.created '
            f'(not a capability like createQuotation); got "{raw.get("event_type")}"'
        )
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        version = 1
    label = raw.get("label")
    if isinstance(label, str):
        label = label.strip() or None
    else:
        label = None
    description = raw.get("description")
    if isinstance(description, str):
        description = description.strip() or None
    else:
        description = None
    return BusinessEventDefinition(
        event_type=event_type,
        version=version,
        label=label,
        description=description,
        schema=raw.get("schema"),
    )


def stable_event_id(event_type: str, entity_id: Any) -> str:
    """Stable id within a source: ``{event_type}:{entity_id}``."""
    type_ = str(event_type or "").strip().lower()
    id_ = str(entity_id if entity_id is not None else "").strip()
    if not id_:
        raise ValueError("stableEventId requires an entity id")
    if not type_:
        return id_
    prefix = f"{type_}:"
    return id_ if id_.startswith(prefix) else f"{prefix}{id_}"


def normalize_emitted_event(
    event: Dict[str, Any],
    declared: Optional[BusinessEventDefinition] = None,
) -> Dict[str, Any]:
    """Normalize an emit into a durable Business Event envelope."""
    event_type = str((event or {}).get("event_type") or "").strip().lower()
    if not is_business_event_type(event_type):
        raise ValueError(
            "ctx.emit() requires a Business Event such as quotation.created, "
            f'not a capability; got "{(event or {}).get("event_type")}"'
        )
    version = event.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        pass
    elif declared is not None and declared.version > 0:
        version = declared.version
    else:
        version = 1
    raw_id = event.get("event_id")
    raw_id = raw_id.strip() if isinstance(raw_id, str) else ""
    event_id = stable_event_id(event_type, raw_id) if raw_id else f"evt_{uuid.uuid4()}"
    out = dict(event)
    out["event_type"] = event_type
    out["version"] = version
    out["event_id"] = event_id
    return out


__all__ = [
    "BusinessEventField",
    "BusinessEventDefinition",
    "is_business_event_type",
    "normalize_business_event",
    "stable_event_id",
    "normalize_emitted_event",
]
