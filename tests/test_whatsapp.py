import hashlib
import hmac
import json
import time
import pytest

from qefro_connector import (
    Qefro,
    WhatsAppFlowDefinition,
    WhatsAppFlowScreen,
    WhatsAppTemplateComponent,
    WhatsAppTemplateDefinition,
    WHATSAPP_SEND_TEMPLATE_TOOL,
    WHATSAPP_LAUNCH_FLOW_TOOL,
    WHATSAPP_GET_TEMPLATE_STATUS_TOOL,
    WHATSAPP_GET_FLOW_STATUS_TOOL,
    clean_recipient_phone,
    mask_phone_number,
    resolve_whatsapp_credentials,
    validate_whatsapp_definition,
)
from qefro_connector.whatsapp import (
    execute_send_template,
    execute_launch_flow,
)


def sign(secret: str, body: bytes, ts: int) -> str:
    payload = f"v1:{ts}:{body.decode('utf-8')}".encode("utf-8")
    return "v1=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


async def handle_signed(app: Qefro, req: dict, secret: str = "test-secret") -> dict:
    if "protocol_version" not in req:
        req["protocol_version"] = "1"
    body_str = json.dumps(req)
    ts = int(time.time())
    sig = sign(secret, body_str.encode("utf-8"), ts)
    headers = {
        "x-qefro-protocol": "1",
        "x-qefro-timestamp": str(ts),
        "x-qefro-signature": sig,
    }
    return await app.handle_raw(body_str, headers)


class MockContext:
    def __init__(self, parameters=None, identity=None, settings=None):
        self.parameters = parameters or {}
        self.identity = identity or {}
        self.settings = settings
        self.conversation = {"id": "test-conv"}
        self.conversation_id = "test-conv"
        self.channel = "whatsapp"


# ============================================================================
# 1. Validation & Schema Tests
# ============================================================================


def test_schema_validation_success():
    reg = validate_whatsapp_definition(
        {
            "version": 1,
            "templates": [
                {
                    "name": "order_update",
                    "language": "en_US",
                    "category": "UTILITY",
                    "components": [
                        {
                            "type": "BODY",
                            "variables": ["order_id", "status"],
                        }
                    ],
                }
            ],
            "flows": [
                {
                    "id": "book_viewing",
                    "provider_flow_id": "flow_12345",
                    "mode": "published",
                    "cta": "Book Now",
                    "screens": [{"id": "SCREEN_ONE", "title": "First Screen", "terminal": False}],
                }
            ],
        }
    )
    assert reg.version == 1
    assert len(reg.templates) == 1
    assert reg.templates[0].name == "order_update"
    assert len(reg.flows) == 1
    assert reg.flows[0].id == "book_viewing"
    assert reg.flows[0].screens[0].id == "SCREEN_ONE"


def test_schema_validation_rejects_empty_name_and_duplicates():
    with pytest.raises(ValueError, match="non-empty name"):
        validate_whatsapp_definition({"templates": [{"name": "   "}]})

    with pytest.raises(ValueError, match="duplicate whatsapp template name"):
        validate_whatsapp_definition(
            {"templates": [{"name": "welcome"}, {"name": "welcome"}]}
        )

    with pytest.raises(ValueError, match="non-empty id"):
        validate_whatsapp_definition({"flows": [{"id": ""}]})

    with pytest.raises(ValueError, match="duplicate whatsapp flow id"):
        validate_whatsapp_definition({"flows": [{"id": "flow_1"}, {"id": "flow_1"}]})


def test_phone_sanitization_and_masking():
    assert clean_recipient_phone("+1 (555) 123-4567") == "+15551234567"
    assert clean_recipient_phone("15551234567") == "15551234567"
    assert clean_recipient_phone("invalid") == ""
    assert mask_phone_number("+15551234567") == "+15*******67"
    assert mask_phone_number("123") == "***"


# ============================================================================
# 2. Capabilities Advertising & Discovery
# ============================================================================


@pytest.mark.asyncio
async def test_capabilities_omitted_when_not_registered():
    app = Qefro("test-secret")
    resp = await handle_signed(app, {"type": "capabilities.list"})
    assert "whatsapp" not in resp


@pytest.mark.asyncio
async def test_capabilities_advertised_when_registered():
    app = Qefro("test-secret")
    app.whatsapp(
        {
            "version": 1,
            "templates": [
                {
                    "name": "appointment_reminder",
                    "language": "en_US",
                    "category": "UTILITY",
                    "description": "Remind customer of appointment",
                }
            ],
            "flows": [
                {
                    "id": "collect_feedback",
                    "name": "Collect Feedback",
                    "cta": "Give Feedback",
                    "screens": [{"id": "RATING", "terminal": True}],
                }
            ],
        }
    )

    resp = await handle_signed(app, {"type": "capabilities.list"})
    assert "whatsapp" in resp
    wa = resp["whatsapp"]
    assert wa["version"] == 1
    assert len(wa["templates"]) == 1
    assert wa["templates"][0]["name"] == "appointment_reminder"
    assert len(wa["flows"]) == 1
    assert wa["flows"][0]["id"] == "collect_feedback"

    # All 4 tools must be present
    tools = {t["name"] for t in resp["tools"]}
    assert WHATSAPP_SEND_TEMPLATE_TOOL in tools
    assert WHATSAPP_LAUNCH_FLOW_TOOL in tools
    assert WHATSAPP_GET_TEMPLATE_STATUS_TOOL in tools
    assert WHATSAPP_GET_FLOW_STATUS_TOOL in tools


@pytest.mark.asyncio
async def test_incremental_registration():
    app = Qefro("test-secret")
    app.whatsapp_template(
        WhatsAppTemplateDefinition(name="tpl_1", language="en_US")
    )
    app.whatsapp_flow(
        WhatsAppFlowDefinition(id="flow_1", name="Flow 1")
    )

    resp = await handle_signed(app, {"type": "capabilities.list"})
    assert "whatsapp" in resp
    assert len(resp["whatsapp"]["templates"]) == 1
    assert len(resp["whatsapp"]["flows"]) == 1


# ============================================================================
# 3. Invocation & Normalization (Unit & Mock Provider)
# ============================================================================


@pytest.mark.asyncio
async def test_send_template_missing_parameters():
    app = Qefro("test-secret")
    app.whatsapp({"templates": [{"name": "order_update"}]})

    # Missing recipient
    resp = await handle_signed(
        app,
        {
            "type": "tool.invoke",
            "tool": WHATSAPP_SEND_TEMPLATE_TOOL,
            "parameters": {"template": "order_update"},
        },
    )
    assert resp["type"] == "result"
    assert resp["output"]["status"] == "failed"
    assert resp["output"]["error"]["code"] == "missing_parameter"

    # Missing template
    resp2 = await handle_signed(
        app,
        {
            "type": "tool.invoke",
            "tool": WHATSAPP_SEND_TEMPLATE_TOOL,
            "parameters": {"to": "+15551234567"},
        },
    )
    assert resp2["output"]["status"] == "failed"
    assert resp2["output"]["error"]["code"] == "missing_parameter"


@pytest.mark.asyncio
async def test_send_template_missing_credentials_in_workspace_settings():
    app = Qefro("test-secret")
    app.whatsapp({"templates": [{"name": "order_update"}]})

    resp = await handle_signed(
        app,
        {
            "type": "tool.invoke",
            "tool": WHATSAPP_SEND_TEMPLATE_TOOL,
            "parameters": {"to": "+15551234567", "template": "order_update"},
        },
    )
    assert resp["type"] == "result"
    assert resp["output"]["status"] == "failed"
    assert resp["output"]["error"]["code"] == "missing_credentials"


def test_send_template_success_normalized_accepted():
    reg = validate_whatsapp_definition(
        {
            "templates": [
                {
                    "name": "order_receipt",
                    "components": [{"type": "BODY", "variables": ["order_id", "total"]}],
                }
            ]
        }
    )

    captured = {}

    def mock_http_post(url, payload, token):
        captured["url"] = url
        captured["payload"] = payload
        captured["token"] = token
        return 200, json.dumps({
            "messaging_product": "whatsapp",
            "contacts": [{"input": "15551234567", "wa_id": "15551234567"}],
            "messages": [{"id": "wamid.HBgLMTE3OTkwMjExODU0ODY5NQ=="}],
        })

    ctx = MockContext(
        parameters={"to": "+15551234567", "template": "order_receipt", "parameters": {"order_id": "ORD-1", "total": "$25"}},
        settings={"whatsapp": {"phone_number_id": "phone_123", "access_token": "token_abc"}},
    )

    res = execute_send_template(ctx, reg, mock_http_post)
    assert res["status"] == "accepted"
    assert res["message_id"] == "wamid.HBgLMTE3OTkwMjExODU0ODY5NQ=="
    assert res["error"] is None
    assert captured["token"] == "token_abc"
    assert captured["payload"]["template"]["components"][0]["parameters"][0]["text"] == "ORD-1"


def test_send_template_provider_rejection():
    reg = validate_whatsapp_definition({"templates": [{"name": "order_receipt"}]})

    def mock_http_post(url, payload, token):
        return 400, json.dumps({
            "error": {
                "message": "(#100) Param components[0] is invalid",
                "type": "OAuthException",
                "code": 100,
                "error_subcode": 2494010,
            }
        })

    ctx = MockContext(
        parameters={"to": "+15551234567", "template": "order_receipt"},
        settings={"whatsapp": {"phone_number_id": "phone_123", "access_token": "token_abc"}},
    )

    res = execute_send_template(ctx, reg, mock_http_post)
    assert res["status"] == "rejected"
    assert res["error"]["code"] == "100"
    assert res["error"]["retryable"] is False


def test_send_template_rate_limit_retryable():
    reg = validate_whatsapp_definition({"templates": [{"name": "order_receipt"}]})

    def mock_http_post(url, payload, token):
        return 429, json.dumps({
            "error": {
                "message": "(#4) Application request limit reached",
                "type": "OAuthException",
                "code": 4,
            }
        })

    ctx = MockContext(
        parameters={"to": "+15551234567", "template": "order_receipt"},
        settings={"whatsapp": {"phone_number_id": "phone_123", "access_token": "token_abc"}},
    )

    res = execute_send_template(ctx, reg, mock_http_post)
    assert res["status"] == "failed"
    assert res["error"]["code"] == "4"
    assert res["error"]["retryable"] is True


def test_launch_flow_success_normalized_accepted():
    reg = validate_whatsapp_definition(
        {
            "flows": [
                {
                    "id": "book_appointment",
                    "provider_flow_id": "meta_flow_777",
                    "cta": "Book Now",
                    "screens": [{"id": "CHOOSE_DATE", "terminal": False}],
                }
            ]
        }
    )

    captured = {}

    def mock_http_post(url, payload, token):
        captured["payload"] = payload
        return 200, json.dumps({
            "messaging_product": "whatsapp",
            "messages": [{"id": "wamid.flow_msg_999"}],
        })

    ctx = MockContext(
        parameters={"to": "+15551234567", "flow_id": "book_appointment"},
        settings={"whatsapp": {"phone_number_id": "phone_123", "access_token": "token_abc"}},
    )

    res = execute_launch_flow(ctx, reg, mock_http_post)
    assert res["status"] == "accepted"
    assert res["message_id"] == "wamid.flow_msg_999"
    assert res["flow_id"] == "book_appointment"
    assert res["provider_flow_id"] == "meta_flow_777"
    assert captured["payload"]["type"] == "interactive"
    assert captured["payload"]["interactive"]["action"]["name"] == "flow"


def test_launch_flow_blocked_status_rejected():
    reg = validate_whatsapp_definition(
        {
            "flows": [
                {
                    "id": "deprecated_flow",
                    "status": "DEPRECATED",
                }
            ]
        }
    )

    ctx = MockContext(
        parameters={"to": "+15551234567", "flow_id": "deprecated_flow"},
        settings={"whatsapp": {"phone_number_id": "phone_123", "access_token": "token_abc"}},
    )

    res = execute_launch_flow(ctx, reg)
    assert res["status"] == "rejected"
    assert res["error"]["code"] == "flow_not_usable"


# ============================================================================
# 4. Tenant & Workspace Credential Isolation
# ============================================================================


def test_workspace_settings_isolation():
    # Workspace channel settings in ctx.settings["whatsapp"] must be used
    creds = resolve_whatsapp_credentials(
        settings={
            "whatsapp": {
                "phone_number_id": "tenant_phone_999",
                "access_token": "tenant_token_999",
            }
        },
        env={"WHATSAPP_ACCESS_TOKEN": "global_env_token"},
    )
    assert creds.phone_number_id == "tenant_phone_999"
    assert creds.access_token == "tenant_token_999"
