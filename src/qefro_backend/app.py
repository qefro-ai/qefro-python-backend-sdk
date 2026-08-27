"""Qefro backend framework: register Business Tools and Business Flows, handle
the signed webhook protocol, and orchestrate customer authorization.

Business Flows are metadata only — the SDK advertises them through
``capabilities.list`` and the Qefro Runtime executes them, calling back into
the registered tools over the same signed webhook when a ``tool`` step runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from .business_events import (
    BusinessEventDefinition,
    is_business_event_type,
    normalize_business_event,
    normalize_emitted_event,
)
from .customer_hub import (
    ConsentContext,
    MembershipContext,
    TimelineContext,
    hub_call,
    hub_customer_from_person,
    is_customer_hub_enabled,
    is_customer_hub_optional,
    pick_identity,
    read_identity_phone,
)
from .event_outbox import EventOutbox, default_outbox_dir
from .marketing import (
    MarketingDefinition,
    MarketingRegistration,
    validate_marketing_definition,
)
from .organization import (
    OrganizationCapabilities,
    OrganizationDefinition,
    validate_organization_definition,
)
from .conversation import ConversationSlot, validate_conversation_slots
from .person import PersonContext, build_person_context
from .storage import StorageContext, build_storage_context
from .model import (
    AuthBuilder,
    AuthOutcome,
    BusinessFlowMetadata,
    ChallengeSignal,
    EventHandlerDefinition,
    FlowBuilder,
    FlowError,
    ToolDefinition,
    ToolLookup,
    _FlowRegistration,
    normalize_flow_trigger,
    normalize_lookup,
)

try:  # version is best-effort; falls back when running from source
    from importlib.metadata import version as _pkg_version

    SDK_VERSION = _pkg_version("qefro-backend")
except Exception:  # pragma: no cover
    SDK_VERSION = "1.2.0"

SDK_NAME = "qefro-backend"

logger = logging.getLogger("qefro")

ToolHandler = Callable[["ToolContext"], Union[Any, Awaitable[Any]]]

__all__ = [
    "Qefro",
    "QefroConfig",
    "CustomerProvider",
    "CustomerLookupContext",
    "CustomerAuthorizeContext",
    "CustomerContext",
    "ToolContext",
    "TimelineContext",
    "MembershipContext",
    "ConsentContext",
    "ServerHandle",
    "SDK_NAME",
    "SDK_VERSION",
]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


class CustomerProvider:
    """Base class for resolving and authorizing customers.

    Subclass and override :meth:`lookup` and :meth:`authorize`. Both may be
    sync or ``async``. Any object exposing these two methods is also accepted.
    """

    async def lookup(self, ctx: "CustomerLookupContext") -> Any:  # pragma: no cover
        raise NotImplementedError

    async def authorize(self, ctx: "CustomerAuthorizeContext") -> AuthOutcome:  # pragma: no cover
        raise NotImplementedError


class CustomerLookupContext:
    def __init__(self, *, identity, parameters, conversation_id, channel):
        self.identity: Dict[str, Any] = identity
        self.parameters: Dict[str, Any] = parameters
        self.conversation = {"id": conversation_id}
        self.conversation_id: str = conversation_id
        self.channel: Optional[str] = channel
        self.logger = logger


class CustomerAuthorizeContext:
    def __init__(self, *, customer, method, response, identity, parameters, conversation_id, channel):
        self.customer: Any = customer
        self.method: Optional[str] = method
        self.response: Optional[str] = response
        self.identity: Dict[str, Any] = identity
        self.parameters: Dict[str, Any] = parameters
        self.conversation = {"id": conversation_id}
        self.conversation_id: str = conversation_id
        self.channel: Optional[str] = channel
        self.logger = logger
        #: Fluent builder for success/challenge/denied/not_found outcomes.
        self.auth = AuthBuilder(response)


class _CustomerState:
    __slots__ = ("current", "lookup_completed")

    def __init__(self, current: Any = None, lookup_completed: bool = False):
        self.current = current
        self.lookup_completed = lookup_completed


class CustomerContext:
    """In-handler customer helpers, available as ``ctx.customer``.

    Hub methods (``resolve`` / ``create`` / ``update`` / ``note`` / ``tag``)
    talk to Customer Hub when ``QEFRO_CUSTOMER_HUB_ENABLED``. External
    ``CustomerProvider`` auth (``authorize`` / provider ``lookup``) is unchanged.

    Attribute access falls through to the resolved customer, so ``ctx.customer.id``
    works when the customer is a mapping.
    """

    def __init__(
        self,
        app: "Qefro",
        *,
        identity,
        parameters,
        conversation_id,
        channel,
        state,
        auth_response,
        platform: Optional[Dict[str, Any]] = None,
    ):
        self._app = app
        self._identity = identity
        self._parameters = parameters
        self._conversation_id = conversation_id
        self._channel = channel
        self._state: _CustomerState = state
        self._auth_response = auth_response
        self._platform = platform

    def _set_current(self, customer: Any) -> Any:
        self._state.current = customer if customer is not None else None
        self._state.lookup_completed = True
        return customer

    def _require_current_id(self) -> str:
        cur = self._state.current
        if isinstance(cur, dict) and isinstance(cur.get("id"), str) and cur["id"]:
            return cur["id"]
        raise _QefroSignal("customer_not_found")

    async def resolve(self, input_data: Optional[Dict[str, Any]] = None) -> Any:
        """Resolve-or-create Customer Hub identity (preferred for apps)."""
        identity = pick_identity(input_data, self._identity)
        out = hub_call(
            self._platform,
            "resolve",
            {
                **{k: v for k, v in identity.items() if v is not None},
                "channel": self._channel or identity.get("channel"),
                "conversation_id": self._conversation_id,
            },
        )
        hub = hub_customer_from_person(out)
        if hub:
            self._set_current(hub)
        return hub

    async def lookup(self, input_data: Optional[Dict[str, Any]] = None) -> Any:
        """Lookup only (no create). Hub when enabled / args given; else provider."""
        provider = self._app._provider
        # Explicit hub lookup when identity args or hub-only mode.
        if input_data is not None or provider is None:
            if self._state.lookup_completed and input_data is None and self._state.current is not None:
                return self._state.current
            identity = pick_identity(input_data, self._identity)
            out = hub_call(
                self._platform,
                "lookup",
                {
                    **{k: v for k, v in identity.items() if v is not None},
                    "channel": self._channel or identity.get("channel"),
                    "conversation_id": self._conversation_id,
                },
            )
            hub = hub_customer_from_person(out)
            self._set_current(hub)
            return hub

        if self._state.lookup_completed:
            return self._state.current
        customer = await _maybe_await(
            provider.lookup(
                CustomerLookupContext(
                    identity=self._identity,
                    parameters=self._parameters,
                    conversation_id=self._conversation_id,
                    channel=self._channel,
                )
            )
        )
        return self._set_current(customer if customer is not None else None)

    async def lookup_by_phone(self, phone: Optional[str] = None) -> Any:
        source = phone or read_identity_phone(self._identity)
        if not source:
            self._state.lookup_completed = True
            self._state.current = None
            return None
        provider = self._app._provider
        if provider is None or is_customer_hub_enabled():
            return await self.lookup({"phone_number": source, "whatsapp_number": source})
        customer = await _maybe_await(
            provider.lookup(
                CustomerLookupContext(
                    identity={**self._identity, "phone": source},
                    parameters=self._parameters,
                    conversation_id=self._conversation_id,
                    channel=self._channel,
                )
            )
        )
        return self._set_current(customer if customer is not None else None)

    async def create(self, input_data: Dict[str, Any]) -> Any:
        identity = pick_identity(input_data, self._identity)
        out = hub_call(
            self._platform,
            "create",
            {
                **{k: v for k, v in identity.items() if v is not None},
                "channel": self._channel or identity.get("channel"),
                "conversation_id": self._conversation_id,
            },
        )
        hub = hub_customer_from_person(out)
        if hub:
            self._set_current(hub)
        return hub

    async def update(self, input_data: Dict[str, Any]) -> Any:
        customer_id = input_data.get("id") if isinstance(input_data.get("id"), str) else None
        if not customer_id and isinstance(self._state.current, dict):
            customer_id = self._state.current.get("id")
        if not customer_id:
            if is_customer_hub_optional():
                return None
            raise _QefroSignal("customer_not_found")
        identity = pick_identity(input_data, self._identity)
        out = hub_call(
            self._platform,
            "update",
            {**{k: v for k, v in identity.items() if v is not None}, "id": customer_id},
        )
        hub = hub_customer_from_person(out)
        if hub:
            self._set_current(hub)
        return hub

    async def note(self, content: str, options: Optional[Dict[str, Any]] = None) -> None:
        trimmed = (content or "").strip()
        if not trimmed:
            raise RuntimeError("customer_note_empty")
        try:
            customer_id = self._require_current_id()
        except _QefroSignal:
            if is_customer_hub_optional():
                return
            raise
        hub_call(
            self._platform,
            "note",
            {
                "customer_id": customer_id,
                "content": trimmed,
                "author_id": (options or {}).get("author_id"),
            },
        )

    async def tag(self, name: str, options: Optional[Dict[str, Any]] = None) -> None:
        trimmed = (name or "").strip()
        if not trimmed:
            raise RuntimeError("customer_tag_empty")
        try:
            customer_id = self._require_current_id()
        except _QefroSignal:
            if is_customer_hub_optional():
                return
            raise
        hub_call(
            self._platform,
            "tag",
            {
                "customer_id": customer_id,
                "name": trimmed,
                "color": (options or {}).get("color"),
            },
        )

    async def authorize(self, method: Optional[str] = None) -> Any:
        provider = self._app._require_provider()

        existing = self._app._get_valid_auth(self._conversation_id)
        if existing is not None:
            self._state.current = existing["customer"]
            self._state.lookup_completed = True
            return existing["customer"]

        customer = await self.lookup()
        if not customer:
            raise _QefroSignal("customer_not_found")

        outcome = await _maybe_await(
            provider.authorize(
                CustomerAuthorizeContext(
                    customer=customer,
                    method=method,
                    response=self._auth_response,
                    identity=self._identity,
                    parameters=self._parameters,
                    conversation_id=self._conversation_id,
                    channel=self._channel,
                )
            )
        )
        return self._app._consume_auth_outcome(outcome, self._conversation_id, self._state)

    def get(self) -> Any:
        return self._state.current

    def require(self) -> Any:
        if self._state.current is None:
            raise _QefroSignal("customer_not_found")
        return self._state.current

    def __getattr__(self, item: str) -> Any:
        # Only called for attributes not found normally.
        state = self.__dict__.get("_state")
        current = state.current if state else None
        if isinstance(current, dict):
            return current.get(item)
        raise AttributeError(item)


class ToolContext:
    """Passed to every tool handler."""

    def __init__(
        self,
        *,
        identity,
        parameters,
        conversation_id,
        channel,
        authentication,
        customer,
        timeline: Optional[TimelineContext] = None,
        membership: Optional[MembershipContext] = None,
        consent: Optional[ConsentContext] = None,
        platform: Optional[Dict[str, Any]] = None,
        person: Optional[PersonContext] = None,
        storage: Optional[StorageContext] = None,
        settings: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        emit_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.identity: Dict[str, Any] = identity
        self.parameters: Dict[str, Any] = parameters
        self.conversation = {"id": conversation_id}
        self.conversation_id: str = conversation_id
        self.channel: Optional[str] = channel
        self.authentication: Optional[Dict[str, Any]] = authentication
        self.logger = logger
        self.customer: CustomerContext = customer
        self.timeline: TimelineContext = timeline or TimelineContext(platform=platform, state=customer._state)
        self.membership: MembershipContext = membership or MembershipContext(
            platform=platform, state=customer._state
        )
        self.consent: ConsentContext = consent or ConsentContext(platform=platform, state=customer._state)
        self.platform = platform
        self.person: PersonContext = person or build_person_context(None, [])
        self.storage: StorageContext = storage or build_storage_context(platform)
        self.settings: Optional[Dict[str, Any]] = settings
        self.install_settings: Optional[Dict[str, Any]] = settings
        self.trace_id: Optional[str] = trace_id
        self._emit_fn = emit_fn

    def emit(self, event: Dict[str, Any]) -> None:
        if self._emit_fn is None:
            raise RuntimeError("ctx.emit is not available")
        self._emit_fn(event)

    async def require_customer(self, resolver: Callable[[AuthBuilder], Any]) -> Any:
        builder = AuthBuilder(self.customer._auth_response)
        outcome = await _maybe_await(resolver(builder))
        return self.customer._app._consume_auth_outcome(
            outcome, self.conversation_id, self.customer._state
        )

    authorize_customer = require_customer
    require_authentication = require_customer


class _QefroSignal(Exception):
    """Internal control-flow error mapped to a protocol error code."""


# ---------------------------------------------------------------------------
# Server handle
# ---------------------------------------------------------------------------


class ServerHandle:
    def __init__(self, url: str, httpd: ThreadingHTTPServer, thread: threading.Thread):
        self.url = url
        self._httpd = httpd
        self._thread = thread

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Config + Qefro
# ---------------------------------------------------------------------------


PENDING_TTL_SECS = 15 * 60


class QefroConfig:
    def __init__(
        self,
        signing_secret: str,
        *,
        protocol_version: str = "1",
        max_timestamp_skew_seconds: int = 300,
        endpoint_path: str = "/qefro",
        event_outbox_dir: Optional[str] = None,
        event_ingest_url: Optional[str] = None,
    ):
        self.signing_secret = signing_secret
        self.protocol_version = protocol_version
        self.max_timestamp_skew_seconds = max_timestamp_skew_seconds
        self.endpoint_path = endpoint_path
        self.event_outbox_dir = event_outbox_dir
        self.event_ingest_url = event_ingest_url


class Qefro:
    """A Qefro backend app: register tools + flows, then :meth:`run` the webhook."""

    def __init__(
        self,
        signing_secret: Union[str, QefroConfig],
        *,
        protocol_version: str = "1",
        max_timestamp_skew_seconds: int = 300,
        endpoint_path: str = "/qefro",
        event_outbox_dir: Optional[str] = None,
        event_ingest_url: Optional[str] = None,
    ):
        if isinstance(signing_secret, QefroConfig):
            config = signing_secret
        else:
            config = QefroConfig(
                signing_secret,
                protocol_version=protocol_version,
                max_timestamp_skew_seconds=max_timestamp_skew_seconds,
                endpoint_path=endpoint_path,
                event_outbox_dir=event_outbox_dir,
                event_ingest_url=event_ingest_url,
            )
        self._signing_secret = config.signing_secret
        self._protocol_version = config.protocol_version
        self._max_skew = config.max_timestamp_skew_seconds
        self._endpoint_path = config.endpoint_path
        ingest = (config.event_ingest_url or "").strip()
        self._event_ingest_url: Optional[str] = ingest or None
        self._outbox = EventOutbox(config.event_outbox_dir or default_outbox_dir(self._signing_secret))
        self._flush_lock = threading.Lock()
        self._flush_in_flight = False

        self._tools: Dict[str, tuple[ToolDefinition, ToolHandler]] = {}
        self._flows: Dict[str, _FlowRegistration] = {}
        self._events: Dict[str, EventHandlerDefinition] = {}
        self._webhooks: Dict[str, EventHandlerDefinition] = {}
        self._schedules: Dict[str, EventHandlerDefinition] = {}
        self._event_handlers: Dict[str, Any] = {}
        self._business_events: Dict[str, BusinessEventDefinition] = {}
        self._marketing: Optional[MarketingRegistration] = None
        self._organization: Optional[OrganizationCapabilities] = None
        self._conversation_slots: Optional[List[ConversationSlot]] = None
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._auth_by_conversation: Dict[str, Dict[str, Any]] = {}
        self._provider: Optional[CustomerProvider] = None
        self._middlewares: List[Callable] = []
        self._before_hooks: List[Callable] = []
        self._after_hooks: List[Callable] = []
        self._lock = threading.Lock()

    # -- registration ------------------------------------------------------

    def customer(self, provider: Any) -> Any:
        """Register the customer provider (instance, or a class to instantiate)."""
        if isinstance(provider, type):
            provider = provider()
        self._provider = provider
        return provider

    def marketing(self, definition: Union[MarketingDefinition, Dict[str, Any]]) -> "Qefro":
        """Register Marketing metadata (ADR-004 Phase 1).

        Metadata only — advertised through ``capabilities.list.marketing``.
        The platform owns campaigns, delivery, and analytics.
        """
        if self._marketing is not None:
            raise ValueError("marketing() may only be called once")
        self._marketing = validate_marketing_definition(definition)
        return self

    def organization(
        self, definition: Union[OrganizationDefinition, Dict[str, Any]]
    ) -> "Qefro":
        """Register Organization capability metadata (ADR-005 Phase 1).

        Metadata only — advertised through ``capabilities.list.organization``.
        The platform owns the capability graph, workflows, and inbox.
        """
        if self._organization is not None:
            raise ValueError("organization() may only be called once")
        self._organization = validate_organization_definition(definition)
        return self

    def conversation_slots(
        self, slots: Union[List[ConversationSlot], List[Dict[str, Any]]]
    ) -> "Qefro":
        """Register conversation slot metadata (ADR-006).

        Metadata only — advertised through ``capabilities.list.conversation_slots``.
        Runtime harvests declared ids as opaque app vocabulary.
        """
        if self._conversation_slots is not None:
            raise ValueError("conversation_slots() may only be called once")
        self._conversation_slots = validate_conversation_slots(slots)
        return self

    def use(self, middleware: Callable) -> "Qefro":
        self._middlewares.append(middleware)
        return self

    def before(self, hook: Callable) -> "Qefro":
        self._before_hooks.append(hook)
        return self

    def after(self, hook: Callable) -> "Qefro":
        self._after_hooks.append(hook)
        return self

    def business_event(self, definition: Union[BusinessEventDefinition, Dict[str, Any]]) -> "Qefro":
        """Declare a Business Event this connector may emit into the Qefro bus."""
        normalized = normalize_business_event(definition)
        if normalized.event_type in self._business_events:
            raise ValueError(f'businessEvent "{normalized.event_type}" is already registered')
        self._business_events[normalized.event_type] = normalized
        return self

    def tool(
        self,
        name_or_def: Union[str, ToolDefinition, Dict[str, Any]],
        handler: Optional[ToolHandler] = None,
        **metadata: Any,
    ):
        """Register a Business Tool.

        Usable directly (``app.tool("name", fn, auth="none")``) or as a
        decorator (``@app.tool("name", auth="none")``).
        """

        def register(fn: ToolHandler) -> ToolHandler:
            definition = self._build_definition(name_or_def, metadata)
            self._tools[definition.name] = (definition, fn)
            return fn

        if handler is not None:
            register(handler)
            return None
        return register

    def flow(self, metadata: Union[BusinessFlowMetadata, Dict[str, Any]]) -> FlowBuilder:
        """Register a Business Flow and return a :class:`FlowBuilder`.

        Optional ``metadata.trigger`` selects the entry path (conversation /
        event / schedule / webhook). Events are triggers into the Qefro runtime
        — not a second execution engine.
        """
        if isinstance(metadata, dict):
            metadata = BusinessFlowMetadata(**metadata)
        flow_id = metadata.id.strip() if isinstance(metadata.id, str) else ""
        if not flow_id:
            raise ValueError("flow() requires a non-empty metadata.id")
        if flow_id in self._flows:
            raise ValueError(f'Flow "{flow_id}" is already registered')
        metadata.id = flow_id
        if metadata.version is None:
            metadata.version = 1
        if metadata.trigger is not None:
            metadata.trigger = normalize_flow_trigger(metadata.trigger)
        registration = _FlowRegistration(metadata=metadata)
        self._flows[flow_id] = registration
        return FlowBuilder(registration)

    def event(self, definition: Union[EventHandlerDefinition, Dict[str, Any]], handler=None):
        """Register a standalone event handler (advertised via capabilities.list)."""
        return self._register_named("event", self._events, definition, handler)

    def webhook(self, definition: Union[EventHandlerDefinition, Dict[str, Any]], handler=None):
        """Register a webhook alias (normalized to an orchestration event at ingest)."""
        return self._register_named("webhook", self._webhooks, definition, handler)

    def schedule(self, definition: Union[EventHandlerDefinition, Dict[str, Any]], handler=None):
        """Register a cron schedule. The runtime scheduler emits the named event."""
        return self._register_named("schedule", self._schedules, definition, handler, require_cron=True)

    def _register_named(self, kind, store, definition, handler=None, require_cron=False):
        if isinstance(definition, dict):
            handler = handler or definition.get("handler")
            definition = EventHandlerDefinition(
                name=definition.get("name") or "",
                description=definition.get("description"),
                cron=definition.get("cron"),
                handler_type=kind,
            )
        else:
            definition.handler_type = kind
        name = (definition.name or "").strip()
        if not name:
            raise ValueError(f"{kind}() requires a non-empty name")
        if require_cron and not (definition.cron or "").strip():
            raise ValueError("schedule() requires a non-empty cron expression")
        if name in store:
            raise ValueError(f'{kind} "{name}" is already registered')
        if handler is None:
            raise ValueError(f"{kind}() requires a handler")
        store[name] = definition
        self._event_handlers[f"{kind}:{name}"] = handler
        return self

    def _build_definition(
        self, name_or_def: Union[str, ToolDefinition, Dict[str, Any]], metadata: Dict[str, Any]
    ) -> ToolDefinition:
        if isinstance(name_or_def, ToolDefinition):
            definition = name_or_def
        elif isinstance(name_or_def, dict):
            definition = ToolDefinition(**name_or_def)
        else:
            lookup = metadata.pop("lookup", None)
            if isinstance(lookup, dict):
                lookup = ToolLookup(**lookup)
            definition = ToolDefinition(name=name_or_def, lookup=lookup, **metadata)
        if definition.auth is None:
            definition.auth = "optional"
        return definition

    # -- wire helpers ------------------------------------------------------

    def _list_tools(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for definition, _ in self._tools.values():
            lookup = normalize_lookup(definition.lookup)
            entry: Dict[str, Any] = {
                "name": definition.name,
                "auth": definition.auth or "optional",
                "permissions": definition.permissions or [],
                "authentication_methods": definition.authentication_methods or [],
            }
            if definition.description is not None:
                entry["description"] = definition.description
            if definition.input_schema is not None:
                entry["input_schema"] = definition.input_schema
            if definition.timeout is not None:
                entry["timeout"] = definition.timeout
            if lookup:
                entry["lookup"] = {"required": lookup}
            if definition.chat is not None:
                entry["chat"] = definition.chat
            out.append(entry)
        return out

    def _list_flows(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for reg in self._flows.values():
            meta = {k: v for k, v in reg.metadata.__dict__.items() if v is not None}
            out.append({"metadata": meta, "steps": reg.steps})
        return out

    # -- signing -----------------------------------------------------------

    def sign(self, timestamp: Union[int, str], body: str) -> str:
        """Compute the ``v1=`` signature for a body (useful for tests/clients)."""
        payload = f"v1:{timestamp}:{body}"
        digest = hmac.new(self._signing_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"v1={digest}"

    def verify_signature(self, signature: Optional[str], timestamp: Optional[str], body: str) -> bool:
        if not signature or not timestamp:
            return False
        try:
            ts = int(float(timestamp))
        except (TypeError, ValueError):
            return False
        now = int(time.time())
        if abs(now - ts) > self._max_skew:
            return False
        expected = self.sign(ts, body)
        return hmac.compare_digest(expected, signature)

    # -- protocol ----------------------------------------------------------

    async def handle_raw(self, body: str, headers: Dict[str, Optional[str]]) -> Dict[str, Any]:
        lower = {k.lower(): v for k, v in headers.items()}
        if not self.verify_signature(
            lower.get("x-qefro-signature"), lower.get("x-qefro-timestamp"), body
        ):
            return {"type": "error", "code": "invalid_signature", "message": "Invalid Qefro signature"}
        protocol_header = lower.get("x-qefro-protocol") or lower.get("x-qefro-protocol-version")
        if protocol_header and protocol_header != self._protocol_version:
            return {
                "type": "error",
                "code": "protocol_mismatch",
                "message": f"Unsupported protocol version {protocol_header}",
            }
        resp = await self.handle(json.loads(body), headers=lower)
        self._schedule_flush()
        return resp

    async def handle(
        self, req: Dict[str, Any], headers: Optional[Dict[str, Optional[str]]] = None
    ) -> Dict[str, Any]:
        if req.get("protocol_version") != self._protocol_version:
            return {"type": "error", "code": "protocol_mismatch", "message": "Unsupported protocol version"}

        req_type = req.get("type")
        trace_id = None
        if headers:
            raw_trace = headers.get("x-qefro-trace-id")
            if isinstance(raw_trace, str) and raw_trace.strip():
                trace_id = raw_trace.strip()

        if req_type == "ping":
            return {"type": "pong", "protocol_version": self._protocol_version, "sdk_version": SDK_VERSION}

        if req_type == "tools.list":
            return {
                "type": "tools.list",
                "tools": self._list_tools(),
                "protocol_version": self._protocol_version,
                "sdk_version": SDK_VERSION,
            }

        if req_type == "capabilities.list":
            out: Dict[str, Any] = {
                "type": "capabilities.list",
                "tools": self._list_tools(),
                "flows": self._list_flows(),
                "events": [d.to_wire() for d in self._events.values()],
                "webhooks": [d.to_wire() for d in self._webhooks.values()],
                "schedules": [d.to_wire() for d in self._schedules.values()],
                "business_events": [d.to_wire() for d in self._business_events.values()],
                "protocol_version": self._protocol_version,
                "sdk_version": SDK_VERSION,
                "sdk_name": SDK_NAME,
            }
            if self._marketing is not None:
                out["marketing"] = self._marketing.to_capability()
            if self._organization is not None:
                out["organization"] = self._organization.to_capability()
            if self._conversation_slots is not None:
                out["conversation_slots"] = [s.to_wire() for s in self._conversation_slots]
            return out

        if req_type == "tool.resume":
            resume_token = req.get("resume_token")
            challenge_response = req.get("challenge_response")
            if not resume_token or not challenge_response:
                return {
                    "type": "error",
                    "code": "invalid_request",
                    "message": "resume_token and challenge_response are required",
                }
            pending = self._take_pending(resume_token)
            if not pending:
                return {"type": "error", "code": "not_found", "message": "resume token not found or expired"}
            return await self._invoke_tool(
                pending["tool"],
                pending["parameters"],
                pending["conversation_id"],
                pending.get("identity"),
                pending.get("channel"),
                auth_response=challenge_response,
                authentication=req.get("authentication"),
                platform=req.get("platform") or pending.get("platform"),
                person=req.get("person") or pending.get("person"),
                settings=req.get("settings") or pending.get("settings"),
                trace_id=trace_id,
            )

        if not req.get("tool"):
            return {"type": "error", "code": "invalid_request", "message": "tool is required"}

        return await self._invoke_tool(
            req["tool"],
            req.get("parameters") or {},
            req.get("conversation_id") or str(uuid.uuid4()),
            req.get("identity"),
            req.get("channel"),
            auth_response=None,
            authentication=req.get("authentication"),
            platform=req.get("platform"),
            person=req.get("person"),
            settings=req.get("settings"),
            trace_id=trace_id,
        )

    async def _invoke_tool(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        conversation_id: str,
        identity: Optional[Dict[str, Any]],
        channel: Optional[str],
        *,
        auth_response: Optional[str],
        authentication: Optional[Dict[str, Any]],
        platform: Optional[Dict[str, Any]] = None,
        person: Optional[Dict[str, Any]] = None,
        settings: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        registration = self._tools.get(tool_name)
        if not registration:
            return {"type": "error", "code": "not_found", "message": f"Unknown tool: {tool_name}"}
        definition, handler = registration

        stored = self._get_valid_auth(conversation_id)
        state = _CustomerState(
            current=stored["customer"] if stored else None,
            lookup_completed=bool(stored),
        )

        # Seed hub customer from Person snapshot when present (native chat path).
        if isinstance(person, dict) and isinstance(person.get("id"), str) and person["id"]:
            state.current = {
                "id": person["id"],
                "phone_number": person.get("phone"),
                "whatsapp_number": person.get("phone"),
                "display_name": person.get("name"),
                "email": person.get("email"),
                "status": person.get("status"),
                "workspace_id": person.get("workspace_id"),
            }
            state.lookup_completed = True

        customer_ctx = CustomerContext(
            self,
            identity=identity or {},
            parameters=parameters,
            conversation_id=conversation_id,
            channel=channel,
            state=state,
            auth_response=auth_response,
            platform=platform,
        )

        solution_id = None
        if isinstance(platform, dict):
            cust = platform.get("customer") or {}
            storage = platform.get("storage") or {}
            ctx_block = cust.get("context") if isinstance(cust, dict) else None
            storage_ctx = storage.get("context") if isinstance(storage, dict) else None
            if isinstance(ctx_block, dict):
                solution_id = ctx_block.get("solution_id")
            if not solution_id and isinstance(storage_ctx, dict):
                solution_id = storage_ctx.get("solution_id")

        timeline = TimelineContext(platform=platform, state=state)
        membership = MembershipContext(platform=platform, state=state, solution_id=solution_id)
        consent = ConsentContext(platform=platform, state=state)
        person_mutations: List[Dict[str, Any]] = []
        person_ctx = build_person_context(person if isinstance(person, dict) else None, person_mutations)
        emitted: List[Dict[str, Any]] = []

        def emit_fn(event: Dict[str, Any]) -> None:
            event_type = str((event or {}).get("event_type") or "").strip()
            if not is_business_event_type(event_type):
                raise ValueError(
                    "ctx.emit() requires a Business Event such as quotation.created, "
                    f'not a capability; got "{(event or {}).get("event_type")}"'
                )
            if self._business_events and event_type not in self._business_events:
                raise ValueError(f'ctx.emit("{event_type}") is not declared via app.business_event()')
            durable = normalize_emitted_event(event, self._business_events.get(event_type))
            self._outbox.put(durable)
            emitted.append(durable)
            self._schedule_flush()

        ctx = ToolContext(
            identity=identity or {},
            parameters=parameters,
            conversation_id=conversation_id,
            channel=channel,
            authentication=authentication,
            customer=customer_ctx,
            timeline=timeline,
            membership=membership,
            consent=consent,
            platform=platform,
            person=person_ctx,
            storage=build_storage_context(platform),
            settings=settings,
            trace_id=trace_id,
            emit_fn=emit_fn,
        )

        try:
            for hook in self._before_hooks:
                await _maybe_await(hook(ctx))

            if definition.auth == "required":
                await customer_ctx.authorize(method=definition.default_auth_method)

            output = await self._run_middlewares(ctx, handler)
            for hook in self._after_hooks:
                output = await _maybe_await(hook(ctx, output))

            latest = self._get_valid_auth(conversation_id)
            events = self._merge_pending_events(emitted)
            result: Dict[str, Any] = {"type": "result", "output": output}
            if latest is not None:
                result["authentication_context"] = latest["auth"]
            if person_mutations:
                result["person_mutations"] = person_mutations
            if events:
                result["events"] = events
            return result
        except ChallengeSignal as sig:
            resume_token = str(uuid.uuid4())
            self._prune_expired_pending()
            with self._lock:
                self._pending[resume_token] = {
                    "tool": tool_name,
                    "conversation_id": conversation_id,
                    "parameters": parameters,
                    "identity": identity,
                    "channel": channel,
                    "platform": platform,
                    "person": person,
                    "settings": settings,
                    "created_at": time.time(),
                }
            return {"type": "challenge", "resume_token": resume_token, "challenge": sig.challenge}
        except _QefroSignal as sig:
            return self._map_signal(str(sig))
        except RuntimeError as err:
            mapped = self._map_signal(str(err))
            if mapped["code"] != "internal_error" or str(err) in {
                "person_not_found",
                "customer_not_found",
                "denied",
                "customer_provider_missing",
            }:
                if str(err) == "person_not_found":
                    return {
                        "type": "error",
                        "code": "person_not_found",
                        "message": "No Customer Hub Person is linked to this conversation.",
                    }
                return mapped
            return {"type": "error", "code": "internal_error", "message": str(err)}
        except Exception as err:  # noqa: BLE001 - surface handler errors as protocol errors
            if str(err) == "person_not_found":
                return {
                    "type": "error",
                    "code": "person_not_found",
                    "message": "No Customer Hub Person is linked to this conversation.",
                }
            return {"type": "error", "code": "internal_error", "message": str(err)}

    def _map_signal(self, code: str) -> Dict[str, Any]:
        if code == "denied":
            return {"type": "error", "code": "denied", "message": "Authentication denied"}
        if code == "customer_not_found":
            return {"type": "error", "code": "customer_not_found", "message": "Customer not found"}
        if code == "person_not_found":
            return {
                "type": "error",
                "code": "person_not_found",
                "message": "No Customer Hub Person is linked to this conversation.",
            }
        if code == "customer_provider_missing":
            return {
                "type": "error",
                "code": "configuration_error",
                "message": "Tool requires customer provider. Configure app.customer(...) first.",
            }
        return {"type": "error", "code": "internal_error", "message": code}

    async def _run_middlewares(self, ctx: ToolContext, handler: ToolHandler) -> Any:
        index = {"i": -1}

        async def dispatch(i: int) -> Any:
            if i <= index["i"]:
                raise RuntimeError("next() called multiple times")
            index["i"] = i
            if i == len(self._middlewares):
                return await _maybe_await(handler(ctx))
            mw = self._middlewares[i]

            async def nxt():
                return await dispatch(i + 1)

            return await _maybe_await(mw(ctx, nxt))

        return await dispatch(0)

    def _take_pending(self, token: str) -> Optional[Dict[str, Any]]:
        self._prune_expired_pending()
        with self._lock:
            return self._pending.pop(token, None)

    def _prune_expired_pending(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                k for k, v in self._pending.items() if now - v.get("created_at", now) > PENDING_TTL_SECS
            ]
            for key in expired:
                self._pending.pop(key, None)

    def _merge_pending_events(self, session: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        by_id: Dict[str, Dict[str, Any]] = {}
        for event in self._outbox.pending():
            eid = event.get("event_id")
            if isinstance(eid, str) and eid:
                by_id[eid] = event
        for event in session:
            eid = event.get("event_id")
            if isinstance(eid, str) and eid:
                by_id[eid] = event
        return list(by_id.values())

    def _schedule_flush(self) -> None:
        if not self._event_ingest_url:
            return
        with self._flush_lock:
            if self._flush_in_flight:
                return
            self._flush_in_flight = True
        threading.Thread(target=self._flush_outbox, daemon=True).start()

    def _flush_outbox(self) -> None:
        try:
            url = self._event_ingest_url
            if not url:
                return
            for event in self._outbox.pending():
                if self._post_ingest(url, event) and event.get("event_id"):
                    self._outbox.ack(str(event["event_id"]))
        finally:
            with self._flush_lock:
                self._flush_in_flight = False

    def _post_ingest(self, url: str, event: Dict[str, Any]) -> bool:
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "name": event.get("event_type"),
                "event_type": event.get("event_type"),
                "event_id": event.get("event_id"),
                "payload": {
                    "customer": event.get("customer") or {},
                    "data": event.get("data") or {},
                    "event_version": event.get("version") or 1,
                    "external_event_id": event.get("event_id"),
                    **({"timestamp": event["timestamp"]} if event.get("timestamp") else {}),
                },
            },
            separators=(",", ":"),
        )
        ts = str(int(time.time()))
        signature = self.sign(ts, body)
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-qefro-signature": signature,
                "x-qefro-timestamp": ts,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = getattr(resp, "status", 200)
            return 200 <= status < 300 or status == 409
        except urllib.error.HTTPError as err:
            return err.code == 409
        except Exception:  # noqa: BLE001
            return False

    # -- auth state --------------------------------------------------------

    def _require_provider(self) -> CustomerProvider:
        if self._provider is None:
            raise _QefroSignal("customer_provider_missing")
        return self._provider

    def _get_valid_auth(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            stored = self._auth_by_conversation.get(conversation_id)
            if stored and stored["expires_at"] > time.time():
                return stored
            if stored:
                self._auth_by_conversation.pop(conversation_id, None)
        return None

    def _consume_auth_outcome(self, outcome: Any, conversation_id: str, state: _CustomerState) -> Any:
        if isinstance(outcome, dict):
            outcome = AuthOutcome(
                kind=outcome.get("kind"),
                customer=outcome.get("customer"),
                auth=outcome.get("auth"),
                challenge=outcome.get("challenge"),
            )
        if not isinstance(outcome, AuthOutcome):
            raise _QefroSignal("internal_error: authorize() must return an AuthOutcome")

        if outcome.kind == "success":
            expires_in = max(1, (outcome.auth or {}).get("expires_in") or 900)
            with self._lock:
                self._auth_by_conversation[conversation_id] = {
                    "customer": outcome.customer,
                    "auth": outcome.auth or {},
                    "expires_at": time.time() + expires_in,
                }
            state.current = outcome.customer
            state.lookup_completed = True
            return outcome.customer
        if outcome.kind == "challenge":
            raise ChallengeSignal(outcome.challenge or {})
        if outcome.kind == "denied":
            raise _QefroSignal("denied")
        raise _QefroSignal("customer_not_found")

    # -- server ------------------------------------------------------------

    def listen(self, port: int, *, host: str = "0.0.0.0", path: Optional[str] = None) -> ServerHandle:
        """Start the webhook server in a background thread and return a handle."""
        endpoint = path or self._endpoint_path
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence default logging
                pass

            def _send(self, status: int, payload: Dict[str, Any]):
                # Compact separators keep the wire bytes identical to the JS and
                # Rust SDKs (no spaces after ':' or ',').
                body = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Qefro-Protocol", app._protocol_version)
                self.send_header("X-Qefro-Protocol-Version", app._protocol_version)
                self.send_header("X-Qefro-SDK", SDK_NAME)
                self.send_header("X-Qefro-Version", SDK_VERSION)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                if self.path != endpoint:
                    self._send(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                headers = {k: v for k, v in self.headers.items()}
                try:
                    response = asyncio.run(app.handle_raw(body, headers))
                    status = 401 if response.get("code") == "invalid_signature" else 200
                    if response.get("code") == "protocol_mismatch":
                        status = 400
                    self._send(status, response)
                except Exception as err:  # noqa: BLE001
                    self._send(500, {"type": "error", "code": "internal_error", "message": str(err)})

        httpd = ThreadingHTTPServer((host, port), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return ServerHandle(f"http://{host}:{port}{endpoint}", httpd, thread)

    def run(self, port: int, *, host: str = "0.0.0.0", path: Optional[str] = None) -> None:
        """Start the server and block until interrupted (Ctrl-C)."""
        handle = self.listen(port, host=host, path=path)
        logger.info("Qefro Python SDK listening at %s", handle.url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            handle.close()
