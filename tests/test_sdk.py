"""Protocol + flow-builder tests for the Qefro Python backend SDK."""

import json

import pytest

from qefro_backend import (
    AuthOutcome,
    CustomerProvider,
    FlowError,
    Qefro,
    normalize_lookup,
)
from qefro_backend.model import ToolLookup


def make_app() -> Qefro:
    return Qefro("secret", protocol_version="1")


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_signature_roundtrip():
    app = make_app()
    body = '{"protocol_version":"1","request_id":"x","type":"ping"}'
    ts = 1_800_000_000
    sig = app.sign(ts, body)
    assert sig.startswith("v1=")
    # verify_signature enforces skew, so use a fresh timestamp there
    import time

    now = int(time.time())
    assert app.verify_signature(app.sign(now, body), str(now), body) is True
    assert app.verify_signature("v1=deadbeef", str(now), body) is False
    assert app.verify_signature(None, None, body) is False


# ---------------------------------------------------------------------------
# Protocol dispatch
# ---------------------------------------------------------------------------


async def test_ping():
    app = make_app()
    resp = await app.handle({"protocol_version": "1", "request_id": "x", "type": "ping"})
    assert resp["type"] == "pong"


async def test_protocol_mismatch():
    app = make_app()
    resp = await app.handle({"protocol_version": "9", "request_id": "x", "type": "ping"})
    assert resp["type"] == "error"
    assert resp["code"] == "protocol_mismatch"


async def test_capabilities_list_advertises_tools_and_flows():
    app = make_app()

    @app.tool("order_status_check", auth="none", description="lookup")
    async def _status(ctx):
        return {"found": True}

    (
        app.flow({"id": "track-order", "name": "Track", "intent": ["track order"]})
        .ask("collect", field="order_id", prompt="id?")
        .tool("lookup", tool_ref="order_status_check")
        .condition("check", when="order_status_check.found == true", then="ok", else_="missing")
        .complete("missing", message="not found")
        .complete("ok", message="found")
    )

    resp = await app.handle(
        {"protocol_version": "1", "request_id": "x", "type": "capabilities.list"}
    )
    assert resp["type"] == "capabilities.list"
    assert resp["sdk_name"] == "qefro-backend"
    assert [t["name"] for t in resp["tools"]] == ["order_status_check"]
    flow = resp["flows"][0]
    assert flow["metadata"]["id"] == "track-order"
    assert flow["metadata"]["version"] == 1
    assert [s["type"] for s in flow["steps"]] == ["ask", "tool", "condition", "complete", "complete"]
    # condition step keeps the `else` wire key
    cond = flow["steps"][2]
    assert cond["config"]["else"] == "missing"
    assert cond["config"]["then"] == "ok"


async def test_tools_list_has_no_flows():
    app = make_app()

    @app.tool("t", auth="none")
    async def _t(ctx):
        return {}

    resp = await app.handle({"protocol_version": "1", "request_id": "x", "type": "tools.list"})
    assert resp["type"] == "tools.list"
    assert "flows" not in resp
    assert resp["tools"][0]["auth"] == "none"


# ---------------------------------------------------------------------------
# Tool invocation
# ---------------------------------------------------------------------------


async def test_tool_invoke_returns_output():
    app = make_app()

    @app.tool("echo", auth="none")
    async def echo(ctx):
        return {"echoed": ctx.parameters.get("value")}

    resp = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "echo",
            "parameters": {"value": 42},
        }
    )
    assert resp == {"type": "result", "output": {"echoed": 42}}


async def test_sync_handler_supported():
    app = make_app()

    @app.tool("sync_tool", auth="none")
    def sync_tool(ctx):
        return {"ok": True}

    resp = await app.handle(
        {"protocol_version": "1", "request_id": "x", "type": "tool.invoke", "tool": "sync_tool"}
    )
    assert resp["output"] == {"ok": True}


async def test_unknown_tool():
    app = make_app()
    resp = await app.handle(
        {"protocol_version": "1", "request_id": "x", "type": "tool.invoke", "tool": "nope"}
    )
    assert resp["code"] == "not_found"


# ---------------------------------------------------------------------------
# Customer authorization: challenge -> resume
# ---------------------------------------------------------------------------


class OtpProvider(CustomerProvider):
    async def lookup(self, ctx):
        return {"id": "cust-1"}

    async def authorize(self, ctx) -> AuthOutcome:
        if not ctx.response:
            return ctx.auth.sms_otp("+10000000000", "Enter code.")
        if ctx.response.strip() != "123456":
            return ctx.auth.sms_otp("+10000000000", "Wrong, retry.")
        return ctx.auth.success(ctx.customer, {"type": "bearer_token", "access_token": "tok"})


async def test_auth_required_challenge_then_resume():
    app = make_app()
    app.customer(OtpProvider())

    @app.tool("cancel", auth="required")
    async def cancel(ctx):
        return {"cancelled": True, "customer": ctx.customer.require()["id"]}

    first = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "cancel",
            "conversation_id": "conv-1",
        }
    )
    assert first["type"] == "challenge"
    assert first["challenge"]["type"] == "sms_otp"
    token = first["resume_token"]

    resumed = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "y",
            "type": "tool.resume",
            "tool": "cancel",
            "resume_token": token,
            "challenge_response": "123456",
        }
    )
    assert resumed["type"] == "result"
    assert resumed["output"] == {"cancelled": True, "customer": "cust-1"}
    assert resumed["authentication_context"]["access_token"] == "tok"


async def test_customer_provider_missing_is_configuration_error():
    app = make_app()

    @app.tool("needs_auth", auth="required")
    async def needs_auth(ctx):
        return {}

    resp = await app.handle(
        {"protocol_version": "1", "request_id": "x", "type": "tool.invoke", "tool": "needs_auth"}
    )
    assert resp["code"] == "configuration_error"


# ---------------------------------------------------------------------------
# Flow builder validation + lookup normalization
# ---------------------------------------------------------------------------


def test_duplicate_flow_id_raises():
    app = make_app()
    app.flow({"id": "f"})
    with pytest.raises(ValueError):
        app.flow({"id": "f"})


def test_empty_flow_id_raises():
    app = make_app()
    with pytest.raises(ValueError):
        app.flow({"id": "   "})


def test_duplicate_step_id_raises():
    app = make_app()
    builder = app.flow({"id": "f"}).ask("a", field="x", prompt="p")
    with pytest.raises(FlowError):
        builder.tool("a", tool_ref="t")


def test_normalize_lookup_dedupes():
    assert normalize_lookup(ToolLookup(by="Email", required=["email", "phone"])) == ["email", "phone"]
    assert normalize_lookup(None) == []


async def test_handle_raw_rejects_bad_signature():
    app = make_app()
    body = json.dumps({"protocol_version": "1", "request_id": "x", "type": "ping"})
    resp = await app.handle_raw(body, {"X-Qefro-Signature": "v1=bad", "X-Qefro-Timestamp": "0"})
    assert resp["code"] == "invalid_signature"
