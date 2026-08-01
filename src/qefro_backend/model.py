"""Data model for the Qefro backend SDK: tool/flow definitions, the flow
builder, and the customer-authorization primitives.

Everything here is plain data plus a fluent :class:`FlowBuilder`. The HTTP
protocol and orchestration live in :mod:`qefro_backend.app`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional

__all__ = [
    "ToolLookup",
    "ToolDefinition",
    "BusinessFlowMetadata",
    "FlowTrigger",
    "EventHandlerDefinition",
    "ChallengePayload",
    "AuthenticationContext",
    "AuthOutcome",
    "AuthBuilder",
    "ChallengeSignal",
    "FlowError",
    "FlowBuilder",
    "normalize_lookup",
    "normalize_flow_trigger",
]

ToolAuthMode = Literal["none", "optional", "required"]
ChallengeType = Literal["email_otp", "sms_otp", "login", "custom"]


# ---------------------------------------------------------------------------
# Tool + flow metadata
# ---------------------------------------------------------------------------


@dataclass
class ToolLookup:
    """Identity attributes the Qefro Runtime must resolve before ``tool.invoke``.

    ``by`` is shorthand for a single attribute (e.g. ``"email"``); ``required``
    is the explicit list. Both are merged and deduped on the wire.
    """

    by: Optional[str] = None
    required: Optional[List[str]] = None


@dataclass
class ToolDefinition:
    """Describes a Business Tool advertised through ``capabilities.list``."""

    name: str
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    authentication_methods: Optional[List[str]] = None
    auth: ToolAuthMode = "optional"
    permissions: Optional[List[str]] = None
    timeout: Optional[int] = None
    default_auth_method: Optional[str] = None
    lookup: Optional[ToolLookup] = None


@dataclass
class FlowTrigger:
    """How a Business Flow is entered (Phase 3).

    ``type`` is one of ``conversation`` | ``event`` | ``schedule`` | ``webhook``.
    Optional ``when`` is a payload predicate for event/webhook triggers.
    """

    type: str = "conversation"
    event: Optional[str] = None
    cron: Optional[str] = None
    name: Optional[str] = None
    when: Optional[str] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type}
        if self.type == "event" and self.event:
            out["event"] = self.event
        if self.type == "schedule" and self.cron:
            out["cron"] = self.cron
        if self.type == "webhook" and self.name:
            out["name"] = self.name
        if self.when:
            out["when"] = self.when
        return out


def normalize_flow_trigger(trigger: Any) -> Optional[Dict[str, Any]]:
    """Validate a flow trigger; return wire dict or None for default conversation."""
    if trigger is None:
        return None
    if isinstance(trigger, FlowTrigger):
        t = trigger
    elif isinstance(trigger, dict):
        t = FlowTrigger(
            type=str(trigger.get("type") or "conversation"),
            event=trigger.get("event"),
            cron=trigger.get("cron"),
            name=trigger.get("name"),
            when=trigger.get("when"),
        )
    else:
        raise ValueError(f"Invalid flow trigger: {trigger!r}")
    kind = (t.type or "conversation").strip().lower()
    if kind == "conversation":
        return {"type": "conversation"}
    if kind == "event":
        event = (t.event or "").strip()
        if not event:
            raise ValueError("trigger.type=event requires a non-empty event name")
        if "." not in event:
            raise ValueError(
                "trigger.event must be namespaced (e.g. shopify.order.created)"
            )
        out: Dict[str, Any] = {"type": "event", "event": event}
        when = (t.when or "").strip() if getattr(t, "when", None) else ""
        if when:
            out["when"] = when
        return out
    if kind == "schedule":
        cron = (t.cron or "").strip()
        if not cron:
            raise ValueError("trigger.type=schedule requires a non-empty cron expression")
        return {"type": "schedule", "cron": cron}
    if kind == "webhook":
        out = {"type": "webhook"}  # type: Dict[str, Any]
        name = (t.name or "").strip()
        if name:
            out["name"] = name
        when = (t.when or "").strip() if getattr(t, "when", None) else ""
        if when:
            out["when"] = when
        return out
    raise ValueError(f"Unknown flow trigger type: {kind}")


@dataclass
class EventHandlerDefinition:
    """Standalone event / webhook / schedule handler advertised via capabilities.list."""

    name: str
    description: Optional[str] = None
    cron: Optional[str] = None
    handler_type: str = "event"

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "type": self.handler_type}
        if self.description is not None:
            out["description"] = self.description
        if self.cron is not None:
            out["cron"] = self.cron
        return out


@dataclass
class BusinessFlowMetadata:
    """Immutable identity + descriptive metadata for a Business Flow.

    ``id`` is the identity key: renaming ``name`` never creates a new flow.
    """

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    version: int = 1
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    intent: Optional[List[str]] = None
    inputs: Optional[List[str]] = None
    outputs: Optional[List[str]] = None
    trigger: Optional[Any] = None


# ---------------------------------------------------------------------------
# Auth primitives
# ---------------------------------------------------------------------------


@dataclass
class ChallengePayload:
    """A verification challenge relayed to the customer by the Runtime."""

    type: ChallengeType
    message: str
    destination_hint: Optional[str] = None
    login_url: Optional[str] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type, "message": self.message}
        if self.destination_hint is not None:
            out["destination_hint"] = self.destination_hint
        if self.login_url is not None:
            out["login_url"] = self.login_url
        return out


@dataclass
class AuthenticationContext:
    """Credential material returned to the Runtime after a successful authorize."""

    type: Optional[Literal["bearer_token", "jwt", "cookie"]] = None
    access_token: Optional[str] = None
    credential: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    customer_id: Optional[str] = None

    def to_wire(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class AuthOutcome:
    """Result of a customer authorization attempt.

    Build these with :class:`AuthBuilder` (``ctx.auth``) rather than by hand.
    """

    kind: Literal["success", "challenge", "denied", "not_found"]
    customer: Any = None
    auth: Optional[Dict[str, Any]] = None
    challenge: Optional[Dict[str, Any]] = None


class ChallengeSignal(Exception):
    """Raised to pause a tool and ask the customer a verification challenge.

    The SDK catches it and returns a ``challenge`` protocol response with a
    resume token; the Runtime relays the prompt and resumes with the answer.
    """

    def __init__(self, challenge: Dict[str, Any]):
        super().__init__(str(challenge.get("message", "challenge required")))
        self.challenge = challenge


def _mask(value: str) -> str:
    if len(value) <= 4:
        return value
    return f"{value[:2]}***{value[-2:]}"


def _coerce_auth_payload(token: Any, customer: Any) -> Dict[str, Any]:
    if isinstance(token, AuthenticationContext):
        payload = token.to_wire()
    elif isinstance(token, dict):
        payload = {k: v for k, v in token.items() if v is not None}
    else:  # pragma: no cover - defensive
        raise TypeError("auth token must be a dict or AuthenticationContext")
    if payload.get("customer_id") is None and isinstance(customer, dict):
        cid = customer.get("id")
        if isinstance(cid, str):
            payload["customer_id"] = cid
    return payload


class AuthBuilder:
    """Fluent helpers for building :class:`AuthOutcome` values.

    Exposed as ``ctx.auth`` inside :meth:`CustomerProvider.authorize`. ``response``
    carries the customer's latest challenge answer (``None`` on the first call).
    """

    def __init__(self, response: Optional[str] = None):
        self.response = response

    def success(self, customer: Any, token: Any) -> AuthOutcome:
        return AuthOutcome(
            kind="success",
            customer=customer,
            auth=_coerce_auth_payload(token, customer),
        )

    def denied(self) -> AuthOutcome:
        return AuthOutcome(kind="denied")

    def not_found(self) -> AuthOutcome:
        return AuthOutcome(kind="not_found")

    def email_otp(self, email: str, message: Optional[str] = None) -> AuthOutcome:
        return AuthOutcome(
            kind="challenge",
            challenge=ChallengePayload(
                type="email_otp",
                message=message or "Enter the OTP sent to your email.",
                destination_hint=_mask(email),
            ).to_wire(),
        )

    def sms_otp(self, phone: str, message: Optional[str] = None) -> AuthOutcome:
        return AuthOutcome(
            kind="challenge",
            challenge=ChallengePayload(
                type="sms_otp",
                message=message or "Enter the OTP sent to your phone.",
                destination_hint=_mask(phone),
            ).to_wire(),
        )

    def login(self, url: str, message: Optional[str] = None) -> AuthOutcome:
        return AuthOutcome(
            kind="challenge",
            challenge=ChallengePayload(
                type="login",
                message=message or "Please continue in your login page.",
                login_url=url,
            ).to_wire(),
        )

    def custom(self, challenge: Any) -> AuthOutcome:
        if isinstance(challenge, ChallengePayload):
            challenge = challenge.to_wire()
        return AuthOutcome(kind="challenge", challenge=challenge)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


class FlowError(ValueError):
    """A malformed flow declaration (empty/duplicate flow or step id)."""


def normalize_lookup(lookup: Optional[ToolLookup]) -> List[str]:
    """Merge ``lookup.by`` / ``lookup.required`` into a deduped, lowercased list."""
    if lookup is None:
        return []
    raw: List[str] = list(lookup.required or [])
    if lookup.by:
        raw.append(lookup.by)
    seen: set = set()
    out: List[str] = []
    for item in raw:
        key = str(item or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


@dataclass
class _FlowRegistration:
    metadata: BusinessFlowMetadata
    steps: List[Dict[str, Any]] = field(default_factory=list)


class FlowBuilder:
    """Fluent builder returned by :meth:`Qefro.flow`.

    Steps are recorded into the flow registry as they are declared; the SDK
    never executes them. A duplicate/empty step id raises :class:`FlowError`.
    """

    def __init__(self, registration: _FlowRegistration):
        self._registration = registration

    def ask(self, id: str, field: str, prompt: str) -> "FlowBuilder":
        return self._push(id, "ask", {"field": field, "prompt": prompt})

    def tool(self, id: str, tool_ref: str) -> "FlowBuilder":
        return self._push(id, "tool", {"tool_ref": tool_ref})

    def challenge(self, id: str, message: Optional[str] = None) -> "FlowBuilder":
        return self._push(id, "challenge", {"message": message})

    def upload(
        self,
        id: str,
        field: Optional[str] = None,
        prompt: Optional[str] = None,
        accept: Optional[List[str]] = None,
    ) -> "FlowBuilder":
        return self._push(id, "upload", {"field": field, "prompt": prompt, "accept": accept})

    def condition(
        self,
        id: str,
        when: str,
        then: Optional[str] = None,
        else_: Optional[str] = None,
    ) -> "FlowBuilder":
        return self._push(id, "condition", {"when": when, "then": then, "else": else_})

    def delay(self, id: str, duration_seconds: int) -> "FlowBuilder":
        return self._push(id, "delay", {"duration_seconds": duration_seconds})

    def approval(self, id: str, prompt: Optional[str] = None) -> "FlowBuilder":
        return self._push(id, "approval", {"prompt": prompt})

    def complete(self, id: str, message: Optional[str] = None) -> "FlowBuilder":
        return self._push(id, "complete", {"message": message})

    def _push(self, id: str, type_: str, config: Dict[str, Any]) -> "FlowBuilder":
        step_id = id.strip() if isinstance(id, str) else ""
        if not step_id:
            raise FlowError(
                f'Flow "{self._registration.metadata.id}": every step requires a non-empty id'
            )
        if any(s["id"] == step_id for s in self._registration.steps):
            raise FlowError(
                f'Flow "{self._registration.metadata.id}": duplicate step id "{step_id}"'
            )
        cleaned = {k: v for k, v in config.items() if v is not None}
        self._registration.steps.append({"id": step_id, "type": type_, "config": cleaned})
        return self
