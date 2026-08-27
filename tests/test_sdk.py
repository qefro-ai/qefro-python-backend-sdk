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


# ---------------------------------------------------------------------------
# Customer Hub (optional, mirrors @qefro-ai/backend 1.7.0)
# ---------------------------------------------------------------------------


ENV_KEYS = (
    "QEFRO_CUSTOMER_HUB_ENABLED",
    "QEFRO_CUSTOMER_HUB_OPTIONAL",
    "QEFRO_CUSTOMER_HUB_URL",
)


@pytest.fixture(autouse=True)
def _clear_hub_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def test_hub_flags_defaults():
    from qefro_backend import (
        env_flag_true,
        is_customer_hub_enabled,
        is_customer_hub_optional,
    )

    assert is_customer_hub_enabled() is False
    assert is_customer_hub_optional() is True
    assert env_flag_true("QEFRO_CUSTOMER_HUB_ENABLED", False) is False
    assert env_flag_true("QEFRO_CUSTOMER_HUB_OPTIONAL", True) is True


def test_hub_flags_parse_variants(monkeypatch):
    from qefro_backend import is_customer_hub_enabled, is_customer_hub_optional

    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_ENABLED", "on")
    assert is_customer_hub_enabled() is True
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_ENABLED", "off")
    assert is_customer_hub_enabled() is False
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_OPTIONAL", "0")
    assert is_customer_hub_optional() is False


def test_hub_customer_from_person():
    from qefro_backend import hub_customer_from_person

    hub = hub_customer_from_person(
        {"id": "p1", "name": "Ada", "phone": "+1555", "email": "a@b.c"}
    )
    assert hub["id"] == "p1"
    assert hub["display_name"] == "Ada"
    assert hub["phone_number"] == "+1555"
    assert hub["whatsapp_number"] == "+1555"
    assert hub["email"] == "a@b.c"
    assert hub_customer_from_person({"name": "x"}) is None
    assert hub_customer_from_person(None) is None


async def test_hub_resolve_soft_skip_when_disabled(monkeypatch):
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_ENABLED", "false")
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_OPTIONAL", "true")
    app = make_app()

    @app.tool("hub_probe", auth="none")
    async def hub_probe(ctx):
        return {"customer": await ctx.customer.resolve({"phone_number": "+1"})}

    resp = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "hub_probe",
        }
    )
    assert resp["type"] == "result"
    assert resp["output"]["customer"] is None


async def test_hub_resolve_hard_fail_when_required(monkeypatch):
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_ENABLED", "true")
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_OPTIONAL", "false")
    app = make_app()

    @app.tool("hub_probe", auth="none")
    async def hub_probe(ctx):
        return {"customer": await ctx.customer.resolve({"phone_number": "+1"})}

    resp = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "hub_probe",
        }
    )
    assert resp["type"] == "error"
    assert "customer_hub_unavailable" in resp["message"]


async def test_timeline_membership_consent_noop_optional(monkeypatch):
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_ENABLED", "true")
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_OPTIONAL", "true")
    app = make_app()

    @app.tool("hub_side", auth="none")
    async def hub_side(ctx):
        await ctx.timeline.append({"event_type": "x.y"})
        await ctx.membership.attach()
        await ctx.consent.grant({"purpose": "marketing"})
        return {"ok": True}

    resp = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "hub_side",
        }
    )
    assert resp == {"type": "result", "output": {"ok": True}}


async def test_person_seed_exposes_hub_properties():
    app = make_app()

    @app.tool("who", auth="none")
    async def who(ctx):
        return {
            "id": ctx.customer.id,
            "phone_number": ctx.customer.phone_number,
            "display_name": ctx.customer.display_name,
        }

    resp = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "who",
            "person": {
                "id": "cust-1",
                "phone": "+1999",
                "name": "Sam",
            },
        }
    )
    assert resp["output"] == {
        "id": "cust-1",
        "phone_number": "+1999",
        "display_name": "Sam",
    }


async def test_hub_resolve_posts_internal_route(monkeypatch):
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_ENABLED", "true")
    monkeypatch.setenv("QEFRO_CUSTOMER_HUB_OPTIONAL", "false")

    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload.encode()
            self.status = 200

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=30):  # noqa: ARG001
        body = json.loads(req.data.decode())
        calls.append({"url": req.full_url, "body": body})
        return _Resp(
            json.dumps(
                {
                    "id": "hub-99",
                    "display_name": "Priya",
                    "phone_number": "+15551002001",
                    "whatsapp_number": "+15551002001",
                }
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    app = make_app()

    @app.tool("resolve_hub", auth="none")
    async def resolve_hub(ctx):
        out = await ctx.customer.resolve(
            {"whatsapp_number": "+15551002001", "display_name": "Priya"}
        )
        await ctx.timeline.append(
            {"event_type": "reservation.created", "payload": {"code": "R-1001"}}
        )
        return {"id": out["id"], "prop": ctx.customer.id}

    platform = {
        "customer": {
            "base_url": "http://hub.local",
            "token": "t",
            "context": {
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "workspace_id": "00000000-0000-0000-0000-000000000002",
                "solution_id": "restaurant-pro",
            },
        }
    }
    resp = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "resolve_hub",
            "platform": platform,
        }
    )
    assert resp["type"] == "result"
    assert resp["output"] == {"id": "hub-99", "prop": "hub-99"}
    assert len(calls) == 2
    assert calls[0]["url"].endswith("/v1/internal/customer-hub/resolve")
    assert calls[0]["body"]["whatsapp_number"] == "+15551002001"
    assert calls[0]["body"]["context"]["solution_id"] == "restaurant-pro"
    assert calls[1]["url"].endswith("/v1/internal/customer-hub/timeline_append")
    assert calls[1]["body"]["customer_id"] == "hub-99"


# ---------------------------------------------------------------------------
# Marketing (ADR-004 Phase 1)
# ---------------------------------------------------------------------------


def test_validate_marketing_definition():
    from qefro_backend import validate_marketing_definition

    reg = validate_marketing_definition(
        {
            "version": 1,
            "audiences": [
                {
                    "id": "vip",
                    "label": "VIP",
                    "source": "customer_hub",
                    "customerHub": {"tags": ["vip"], "consentPurpose": "marketing"},
                }
            ],
            "variables": [
                {
                    "id": "guest_name",
                    "label": "Guest name",
                    "type": "string",
                    "source": "customer_hub",
                    "path": "display_name",
                }
            ],
            "actions": [
                {
                    "id": "book",
                    "label": "Book",
                    "kind": "url",
                    "landingPageId": "booking",
                }
            ],
            "landingPages": [
                {"id": "booking", "label": "Booking", "path": "/booking", "host": "app"}
            ],
            "channels": [
                {"id": "whatsapp", "provider": "meta", "enabled": True},
                {"id": "email", "provider": "sendgrid", "enabled": True},
            ],
        }
    )
    assert reg.version == 1
    assert reg.channels[0].provider == "meta"
    cap = reg.to_capability()
    assert cap["version"] == 1
    assert "audiences" in cap["metadata"]
    assert "audiences" not in cap


def test_validate_marketing_rejects_bad_source():
    from qefro_backend import validate_marketing_definition
    import pytest

    with pytest.raises(ValueError, match="source"):
        validate_marketing_definition(
            {"audiences": [{"id": "a", "label": "A", "source": "crm"}]}
        )


async def test_capabilities_list_includes_marketing():
    app = make_app()
    app.marketing(
        {
            "audiences": [{"id": "vip", "label": "VIP", "source": "customer_hub"}],
            "channels": [
                {"id": "whatsapp", "provider": "meta", "enabled": True},
                {"id": "email", "provider": "sendgrid", "enabled": True},
                {"id": "website_widget", "enabled": True},
            ],
        }
    )
    resp = await app.handle(
        {"protocol_version": "1", "request_id": "x", "type": "capabilities.list"}
    )
    assert resp["type"] == "capabilities.list"
    assert resp["marketing"]["version"] == 1
    assert resp["marketing"]["metadata"]["audiences"][0]["id"] == "vip"
    assert resp["marketing"]["metadata"]["channels"][0]["provider"] == "meta"
    assert "audiences" not in resp["marketing"]


def test_marketing_only_once():
    import pytest

    app = make_app()
    app.marketing({"channels": [{"id": "email"}]})
    with pytest.raises(ValueError, match="only be called once"):
        app.marketing({"channels": [{"id": "whatsapp"}]})


async def test_marketing_context_disabled_without_flag():
    from qefro_backend import (
        build_marketing_context,
        is_marketing_enabled,
        to_marketing_capability,
        validate_marketing_definition,
    )

    assert is_marketing_enabled() is False
    ctx = build_marketing_context({"marketing": {"base_url": "https://example.invalid"}})
    assert await ctx.get_registration() is None
    cap = to_marketing_capability(validate_marketing_definition({"channels": [{"id": "email"}]}))
    assert cap["version"] == 1
    assert "metadata" in cap


# ---------------------------------------------------------------------------
# Organization (ADR-005)
# ---------------------------------------------------------------------------


async def test_capabilities_list_includes_organization():
    app = make_app()
    app.organization(
        {
            "events": [{"id": "purchase_requested", "label": "Purchase requested"}],
            "actions": [{"id": "create_purchase_request", "label": "Create"}],
            "tasks": [
                {
                    "id": "purchase_approval",
                    "label": "Purchase approval",
                    "suggested_workspace_type": "finance",
                    "priority": "high",
                }
            ],
        }
    )
    resp = await app.handle(
        {"protocol_version": "1", "request_id": "x", "type": "capabilities.list"}
    )
    assert resp["organization"]["version"] == 1
    assert resp["organization"]["metadata"]["events"][0]["id"] == "purchase_requested"
    assert "templates" not in resp["organization"]["metadata"]


def test_organization_rejects_prefixed_ids():
    app = make_app()
    with pytest.raises(ValueError, match="opaque capability id"):
        app.organization({"events": [{"id": "restaurant.purchase_requested"}]})


async def test_organization_context_disabled_without_flag():
    from qefro_backend import (
        build_organization_context,
        is_organization_enabled,
        to_organization_capability,
        validate_organization_definition,
    )

    assert is_organization_enabled() is False
    ctx = build_organization_context({"organization": {"base_url": "https://example.invalid"}})
    assert await ctx.get_capabilities() is None
    cap = to_organization_capability(
        validate_organization_definition({"events": [{"id": "purchase_requested"}]})
    )
    assert cap["version"] == 1
    assert cap["metadata"]["events"][0]["id"] == "purchase_requested"


async def test_conversation_slots_advertised_on_capabilities_list():
    app = make_app()
    app.conversation_slots(
        [
            {
                "id": "spaceship_registration_code",
                "labels": ["spaceship registration code", "registration code"],
                "kind": "document_id",
                "chip_prefix": "ship",
            },
            {
                "id": "quantum_fuel_quantity",
                "labels": ["quantum fuel", "fuel quantity"],
                "kind": "integer",
                "quantity_suffixes": ["units"],
            },
        ]
    )
    resp = await app.handle(
        {"protocol_version": "1", "request_id": "x", "type": "capabilities.list"}
    )
    slots = resp["conversation_slots"]
    assert [s["id"] for s in slots] == [
        "spaceship_registration_code",
        "quantum_fuel_quantity",
    ]
    assert slots[0]["chip_prefix"] == "ship"
    assert "name" not in slots[0]
    assert "chipPrefix" not in slots[0]


def test_conversation_slots_rejects_duplicate_ids():
    app = make_app()
    with pytest.raises(ValueError, match="duplicate id"):
        app.conversation_slots([{"id": "same"}, {"id": "same"}])


# ---------------------------------------------------------------------------
# Business Events + Person + protocol extras
# ---------------------------------------------------------------------------


def test_stable_event_id_is_unique_per_type():
    from qefro_backend import stable_event_id

    assert stable_event_id("quotation.created", "Q-1001") == "quotation.created:Q-1001"
    assert stable_event_id("order.created", "Q-1001") == "order.created:Q-1001"
    assert (
        stable_event_id("quotation.created", "quotation.created:Q-1001")
        == "quotation.created:Q-1001"
    )


async def test_ctx_emit_persists_versioned_event(tmp_path):
    app = Qefro("secret", protocol_version="1", event_outbox_dir=str(tmp_path))
    app.business_event(
        {"event_type": "quotation.created", "version": 1, "label": "Quotation created"}
    )

    @app.tool("createQuotation", auth="none")
    async def create_quotation(ctx):
        ctx.emit(
            {
                "event_type": "quotation.created",
                "event_id": "Q-1001",
                "data": {"amount": 125000},
            }
        )
        return {"id": "Q-1001"}

    result = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "r-emit",
            "type": "tool.invoke",
            "tool": "createQuotation",
            "conversation_id": "c1",
            "parameters": {},
        }
    )
    assert result["type"] == "result"
    assert result["events"][0]["event_type"] == "quotation.created"
    assert result["events"][0]["event_id"] == "quotation.created:Q-1001"
    assert result["events"][0]["version"] == 1

    caps = await app.handle(
        {"protocol_version": "1", "request_id": "r-caps", "type": "capabilities.list"}
    )
    assert caps["business_events"][0]["version"] == 1


async def test_person_mutations_and_trace_id():
    app = make_app()

    @app.tool("tag_person", auth="none")
    async def tag_person(ctx):
        await ctx.person.tag("vip")
        return {"trace": ctx.trace_id, "id": ctx.person.require()["id"]}

    resp = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "tag_person",
            "person": {"id": "p1", "name": "Ada"},
        },
        headers={"x-qefro-trace-id": "trace-1"},
    )
    assert resp["type"] == "result"
    assert resp["output"] == {"trace": "trace-1", "id": "p1"}
    assert resp["person_mutations"] == [{"op": "tag", "name": "vip"}]


async def test_person_not_found_without_snapshot():
    app = make_app()

    @app.tool("need_person", auth="none")
    async def need_person(ctx):
        ctx.person.require()
        return {}

    resp = await app.handle(
        {
            "protocol_version": "1",
            "request_id": "x",
            "type": "tool.invoke",
            "tool": "need_person",
        }
    )
    assert resp["code"] == "person_not_found"


async def test_tool_chat_false_advertised():
    app = make_app()

    @app.tool("approve", auth="none", chat=False)
    async def approve(ctx):
        return {"ok": True}

    resp = await app.handle(
        {"protocol_version": "1", "request_id": "x", "type": "tools.list"}
    )
    assert resp["tools"][0]["chat"] is False


def test_person_event_helpers():
    from qefro_backend import PersonEvents, on_person_created

    assert PersonEvents.Created == "person.created"
    assert on_person_created() == {"type": "event", "event": "person.created"}


def test_flow_person_hub_steps():
    app = make_app()
    (
        app.flow({"id": "welcome", "trigger": {"type": "event", "event": "person.created"}})
        .message("hi", message="Hi {{person.name}}")
        .tag("vip", name="vip")
        .assign("sales", to="sales")
        .activity("note", activity_type="note.logged")
        .complete("done")
    )
    steps = app._list_flows()[0]["steps"]
    assert [s["type"] for s in steps] == ["message", "tag", "assign", "activity", "complete"]

