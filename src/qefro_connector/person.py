"""Customer Hub Person — Qefro first-party memory.

Distinct from CustomerContext (external systems / connector auth)::

    ctx.customer  →  external systems
    ctx.person    →  Qefro memory
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

PERSON_EVENTS = {
    "Created": "person.created",
    "Updated": "person.updated",
    "StatusChanged": "person.status.changed",
    "Merged": "person.merged",
    "Assigned": "person.assigned",
    "TagCreated": "person.tag.created",
    "ActivityCreated": "person.activity.created",
}


class PersonEvents:
    Created = "person.created"
    Updated = "person.updated"
    StatusChanged = "person.status.changed"
    Merged = "person.merged"
    Assigned = "person.assigned"
    TagCreated = "person.tag.created"
    ActivityCreated = "person.activity.created"


def person_event_trigger(event: str, when: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "event", "event": event}
    if when is not None:
        out["when"] = when
    return out


def on_person_created(when: Optional[str] = None) -> Dict[str, Any]:
    return person_event_trigger(PersonEvents.Created, when)


def on_person_updated(when: Optional[str] = None) -> Dict[str, Any]:
    return person_event_trigger(PersonEvents.Updated, when)


def on_person_status_changed(when: Optional[str] = None) -> Dict[str, Any]:
    return person_event_trigger(PersonEvents.StatusChanged, when)


def on_person_merged(when: Optional[str] = None) -> Dict[str, Any]:
    return person_event_trigger(PersonEvents.Merged, when)


class PersonContext:
    """Customer Hub Person context. Mutations are queued for the runtime."""

    def __init__(self, snapshot: Optional[Dict[str, Any]], mutations: List[Dict[str, Any]]):
        self._current: Optional[Dict[str, Any]] = None
        if isinstance(snapshot, dict) and isinstance(snapshot.get("id"), str) and snapshot["id"]:
            self._current = dict(snapshot)
        self._mutations = mutations

    def _require_id(self) -> str:
        if not self._current or not self._current.get("id"):
            raise RuntimeError("person_not_found")
        return self._current["id"]

    def get(self) -> Optional[Dict[str, Any]]:
        return self._current

    def require(self) -> Dict[str, Any]:
        if not self._current:
            raise RuntimeError("person_not_found")
        return self._current

    async def note(self, content: str, options: Optional[Dict[str, Any]] = None) -> None:
        self._require_id()
        trimmed = (content or "").strip()
        if not trimmed:
            raise RuntimeError("person_note_empty")
        mut: Dict[str, Any] = {"op": "note", "content": trimmed}
        if options and options.get("author_id") is not None:
            mut["author_id"] = options["author_id"]
        self._mutations.append(mut)

    async def tag(self, name: str, options: Optional[Dict[str, Any]] = None) -> None:
        self._require_id()
        trimmed = (name or "").strip()
        if not trimmed:
            raise RuntimeError("person_tag_empty")
        mut: Dict[str, Any] = {"op": "tag", "name": trimmed}
        color = (options or {}).get("color")
        if color is not None:
            mut["color"] = color
        self._mutations.append(mut)
        tags = list(self._current.get("tags") or []) if self._current else []
        tags.append({"name": trimmed, "color": color})
        if self._current is not None:
            self._current = {**self._current, "tags": tags}

    async def activity(
        self,
        activity_type: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._require_id()
        trimmed = (activity_type or "").strip()
        if not trimmed:
            raise RuntimeError("person_activity_empty")
        opts = options or {}
        mut: Dict[str, Any] = {
            "op": "activity",
            "activity_type": trimmed,
            "source": opts.get("source") or "sdk",
        }
        if opts.get("payload") is not None:
            mut["payload"] = opts["payload"]
        self._mutations.append(mut)

    async def assign(self, to: str, options: Optional[Dict[str, Any]] = None) -> None:
        self._require_id()
        trimmed = (to or "").strip()
        if not trimmed:
            raise RuntimeError("person_assign_empty")
        mut: Dict[str, Any] = {"op": "assign", "to": trimmed}
        if options and "handoff" in options:
            mut["handoff"] = options["handoff"]
        self._mutations.append(mut)

    async def merge(self, into_person_id: str) -> None:
        self._require_id()
        into = (into_person_id or "").strip()
        if not into:
            raise RuntimeError("person_merge_target_empty")
        self._mutations.append({"op": "merge", "into": into})


def build_person_context(
    snapshot: Optional[Dict[str, Any]],
    mutations: List[Dict[str, Any]],
) -> PersonContext:
    return PersonContext(snapshot, mutations)


__all__ = [
    "PERSON_EVENTS",
    "PersonEvents",
    "PersonContext",
    "person_event_trigger",
    "on_person_created",
    "on_person_updated",
    "on_person_status_changed",
    "on_person_merged",
    "build_person_context",
]
