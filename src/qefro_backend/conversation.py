"""Conversation slot metadata (ADR-006).

Apps declare harvest fields; Runtime treats ``id`` as opaque. Metadata only —
advertised through ``capabilities.list.conversation_slots``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

SLOT_KINDS = (
    "string",
    "integer",
    "date",
    "time",
    "phone",
    "email",
    "document_id",
    "person_name",
    "choice",
)
CHIP_VALUE_MODES = ("remainder", "last_segment")
IDENTITIES = ("person_name", "email", "phone")


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"conversation_slots: {path} must be a non-empty string")
    return value.strip()


def _optional_str(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"conversation_slots: {path} must be a string")
    trimmed = value.strip()
    return trimmed or None


def _optional_str_list(value: Any, path: str) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"conversation_slots: {path} must be an array of strings")
    out: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"conversation_slots: {path}[{i}] must be a non-empty string")
        out.append(item.strip())
    return out


@dataclass
class OpaqueReference:
    type: str = "opaque"
    value: str = ""

    def to_wire(self) -> Dict[str, Any]:
        return {"type": self.type, "value": self.value}


@dataclass
class CapabilityResult:
    status: Optional[str] = None
    reference: Optional[OpaqueReference] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.status is not None:
            out["status"] = self.status
        if self.reference is not None:
            out["reference"] = self.reference.to_wire()
        return out


@dataclass
class ConversationSlot:
    id: str
    labels: Optional[List[str]] = None
    kind: Optional[str] = None
    aliases: Optional[List[str]] = None
    chip_prefix: Optional[str] = None
    chip_value: Optional[str] = None
    chip_label_slot: Optional[str] = None
    choices: Optional[Dict[str, List[str]]] = None
    quantity_suffixes: Optional[List[str]] = None
    identity: Optional[str] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id}
        if self.labels is not None:
            out["labels"] = self.labels
        if self.kind is not None:
            out["kind"] = self.kind
        if self.aliases is not None:
            out["aliases"] = self.aliases
        if self.chip_prefix is not None:
            out["chip_prefix"] = self.chip_prefix
        if self.chip_value is not None:
            out["chip_value"] = self.chip_value
        if self.chip_label_slot is not None:
            out["chip_label_slot"] = self.chip_label_slot
        if self.choices is not None:
            out["choices"] = self.choices
        if self.quantity_suffixes is not None:
            out["quantity_suffixes"] = self.quantity_suffixes
        if self.identity is not None:
            out["identity"] = self.identity
        return out


def _validate_slot(raw: Any, index: int) -> ConversationSlot:
    if isinstance(raw, ConversationSlot):
        raw = raw.to_wire()
    if not isinstance(raw, dict):
        raise ValueError(f"conversation_slots: [{index}] must be an object")
    slot = ConversationSlot(id=_require_str(raw.get("id"), f"[{index}].id"))
    slot.labels = _optional_str_list(raw.get("labels"), f"[{index}].labels")
    kind = _optional_str(raw.get("kind"), f"[{index}].kind")
    if kind is not None:
        if kind not in SLOT_KINDS:
            raise ValueError(f"conversation_slots: [{index}].kind is not a known kind")
        slot.kind = kind
    slot.aliases = _optional_str_list(raw.get("aliases"), f"[{index}].aliases")
    slot.chip_prefix = _optional_str(raw.get("chip_prefix"), f"[{index}].chip_prefix")
    chip_value = _optional_str(raw.get("chip_value"), f"[{index}].chip_value")
    if chip_value is not None:
        if chip_value not in CHIP_VALUE_MODES:
            raise ValueError(
                f"conversation_slots: [{index}].chip_value must be remainder|last_segment"
            )
        slot.chip_value = chip_value
    slot.chip_label_slot = _optional_str(raw.get("chip_label_slot"), f"[{index}].chip_label_slot")
    choices = raw.get("choices")
    if choices is not None:
        if not isinstance(choices, dict):
            raise ValueError(f"conversation_slots: [{index}].choices must be an object")
        parsed: Dict[str, List[str]] = {}
        for value, synonyms in choices.items():
            parsed[str(value)] = _optional_str_list(
                synonyms, f"[{index}].choices.{value}"
            ) or []
        slot.choices = parsed
    slot.quantity_suffixes = _optional_str_list(
        raw.get("quantity_suffixes"), f"[{index}].quantity_suffixes"
    )
    identity = _optional_str(raw.get("identity"), f"[{index}].identity")
    if identity is not None:
        if identity not in IDENTITIES:
            raise ValueError(
                f"conversation_slots: [{index}].identity must be person_name|email|phone"
            )
        slot.identity = identity
    return slot


def validate_conversation_slots(
    slots: Union[List[ConversationSlot], List[Dict[str, Any]]],
) -> List[ConversationSlot]:
    if not isinstance(slots, list):
        raise ValueError("conversation_slots: must be an array")
    out = [_validate_slot(s, i) for i, s in enumerate(slots)]
    seen = set()
    for slot in out:
        if slot.id in seen:
            raise ValueError(f'conversation_slots: duplicate id "{slot.id}"')
        seen.add(slot.id)
    return out
