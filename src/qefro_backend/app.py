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
    SDK_VERSION = "1.3.0"

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

    Attribute access falls through to the resolved customer, so ``ctx.customer.id``
    works when the customer is a mapping.
    """

    def __init__(self, app: "Qefro", *, identity, parameters, conversation_id, channel, state, auth_response):
        self._app = app
        self._identity = identity
        self._parameters = parameters
        self._conversation_id = conversation_id
        self._channel = channel
        self._state: _CustomerState = state
        self._auth_response = auth_response

    async def lookup(self) -> Any:
        provider = self._app._require_provider()
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
        self._state.current = customer if customer is not None else None
        self._state.lookup_completed = True
        return customer

    async def lookup_by_phone(self, phone: Optional[str] = None) -> Any:
        provider = self._app._require_provider()
        source = phone or self._read_identity_phone()
        if not source:
            self._state.lookup_completed = True
            self._state.current = None
            return None
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
        self._state.current = customer if customer is not None else None
        self._state.lookup_completed = True
        return customer

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

    def _read_identity_phone(self) -> Optional[str]:
        phone = self._identity.get("phone")
        if isinstance(phone, str) and phone.strip():
            return phone
        return None

    def __getattr__(self, item: str) -> Any:
        # Only called for attributes not found normally.
        state = self.__dict__.get("_state")
        current = state.current if state else None
        if isinstance(current, dict):
            return current.get(item)
        raise AttributeError(item)


class ToolContext:
    """Passed to every tool handler."""

    def __init__(self, *, identity, parameters, conversation_id, channel, authentication, customer):
        self.identity: Dict[str, Any] = identity
        self.parameters: Dict[str, Any] = parameters
        self.conversation = {"id": conversation_id}
        self.conversation_id: str = conversation_id
        self.channel: Optional[str] = channel
        self.authentication: Optional[Dict[str, Any]] = authentication
        self.logger = logger
        self.customer: CustomerContext = customer


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


class QefroConfig:
    def __init__(
        self,
        signing_secret: str,
        *,
        protocol_version: str = "1",
        max_timestamp_skew_seconds: int = 300,
        endpoint_path: str = "/qefro",
    ):
        self.signing_secret = signing_secret
        self.protocol_version = protocol_version
        self.max_timestamp_skew_seconds = max_timestamp_skew_seconds
        self.endpoint_path = endpoint_path


class Qefro:
    """A Qefro backend app: register tools + flows, then :meth:`run` the webhook."""

    def __init__(
        self,
        signing_secret: Union[str, QefroConfig],
        *,
        protocol_version: str = "1",
        max_timestamp_skew_seconds: int = 300,
        endpoint_path: str = "/qefro",
    ):
        if isinstance(signing_secret, QefroConfig):
            config = signing_secret
        else:
            config = QefroConfig(
                signing_secret,
                protocol_version=protocol_version,
                max_timestamp_skew_seconds=max_timestamp_skew_seconds,
                endpoint_path=endpoint_path,
            )
        self._signing_secret = config.signing_secret
        self._protocol_version = config.protocol_version
        self._max_skew = config.max_timestamp_skew_seconds
        self._endpoint_path = config.endpoint_path

        self._tools: Dict[str, tuple[ToolDefinition, ToolHandler]] = {}
        self._flows: Dict[str, _FlowRegistration] = {}
        self._events: Dict[str, EventHandlerDefinition] = {}
        self._webhooks: Dict[str, EventHandlerDefinition] = {}
        self._schedules: Dict[str, EventHandlerDefinition] = {}
        self._event_handlers: Dict[str, Any] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._auth_by_conversation: Dict[str, Dict[str, Any]] = {}
        self._provider: Optional[CustomerProvider] = None
        self._lock = threading.Lock()

    # -- registration ------------------------------------------------------

    def customer(self, provider: Any) -> Any:
        """Register the customer provider (instance, or a class to instantiate)."""
        if isinstance(provider, type):
            provider = provider()
        self._provider = provider
        return provider

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
        return await self.handle(json.loads(body))

    async def handle(self, req: Dict[str, Any]) -> Dict[str, Any]:
        if req.get("protocol_version") != self._protocol_version:
            return {"type": "error", "code": "protocol_mismatch", "message": "Unsupported protocol version"}

        req_type = req.get("type")

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
            return {
                "type": "capabilities.list",
                "tools": self._list_tools(),
                "flows": self._list_flows(),
                "events": [d.to_wire() for d in self._events.values()],
                "webhooks": [d.to_wire() for d in self._webhooks.values()],
                "schedules": [d.to_wire() for d in self._schedules.values()],
                "protocol_version": self._protocol_version,
                "sdk_version": SDK_VERSION,
                "sdk_name": SDK_NAME,
            }

        if req_type == "tool.resume":
            resume_token = req.get("resume_token")
            challenge_response = req.get("challenge_response")
            if not resume_token or not challenge_response:
                return {
                    "type": "error",
                    "code": "invalid_request",
                    "message": "resume_token and challenge_response are required",
                }
            with self._lock:
                pending = self._pending.pop(resume_token, None)
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
        customer_ctx = CustomerContext(
            self,
            identity=identity or {},
            parameters=parameters,
            conversation_id=conversation_id,
            channel=channel,
            state=state,
            auth_response=auth_response,
        )
        ctx = ToolContext(
            identity=identity or {},
            parameters=parameters,
            conversation_id=conversation_id,
            channel=channel,
            authentication=authentication,
            customer=customer_ctx,
        )

        try:
            if definition.auth == "required":
                await customer_ctx.authorize(method=definition.default_auth_method)

            output = await _maybe_await(handler(ctx))

            latest = self._get_valid_auth(conversation_id)
            result: Dict[str, Any] = {"type": "result", "output": output}
            if latest is not None:
                result["authentication_context"] = latest["auth"]
            return result
        except ChallengeSignal as sig:
            resume_token = str(uuid.uuid4())
            with self._lock:
                self._pending[resume_token] = {
                    "tool": tool_name,
                    "conversation_id": conversation_id,
                    "parameters": parameters,
                    "identity": identity,
                    "channel": channel,
                }
            return {"type": "challenge", "resume_token": resume_token, "challenge": sig.challenge}
        except _QefroSignal as sig:
            code = str(sig)
            if code == "denied":
                return {"type": "error", "code": "denied", "message": "Authentication denied"}
            if code == "customer_not_found":
                return {"type": "error", "code": "customer_not_found", "message": "Customer not found"}
            if code == "customer_provider_missing":
                return {
                    "type": "error",
                    "code": "configuration_error",
                    "message": "Tool requires customer provider. Configure app.customer(...) first.",
                }
            return {"type": "error", "code": "internal_error", "message": code}
        except Exception as err:  # noqa: BLE001 - surface handler errors as protocol errors
            return {"type": "error", "code": "internal_error", "message": str(err)}

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
