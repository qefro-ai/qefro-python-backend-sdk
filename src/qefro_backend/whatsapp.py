"""WhatsApp capability registration and operations.

Developers register message templates and interactive Flows in code.
Provider credentials (access token and phone number ID) are owned by the Qefro
platform and workspace channel settings (injected at runtime via ctx.settings["whatsapp"]).
Developers do NOT configure WhatsApp provider credentials in the SDK.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Set, Union

logger = logging.getLogger("qefro.whatsapp")

WHATSAPP_SEND_TEMPLATE_TOOL = "whatsapp_send_template"
WHATSAPP_LAUNCH_FLOW_TOOL = "whatsapp_launch_flow"
WHATSAPP_GET_TEMPLATE_STATUS_TOOL = "whatsapp_get_template_status"
WHATSAPP_GET_FLOW_STATUS_TOOL = "whatsapp_get_flow_status"

WHATSAPP_PERMISSIONS = ["whatsapp.send", "whatsapp.read"]

SEND_TEMPLATE_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Recipient phone number in E.164 format (e.g. +15551234567 or 15551234567)",
        },
        "template": {
            "type": "string",
            "description": "Stable template reference identifier (e.g. order_update)",
        },
        "language": {
            "type": "string",
            "description": "Language code (e.g. en_US; defaults to template definition or en_US)",
        },
        "parameters": {
            "type": "object",
            "description": "Key-value map of named parameters to populate template variables",
        },
        "components": {
            "type": "array",
            "description": "Optional direct Meta Graph API component parameters override",
        },
        "correlation_id": {
            "type": "string",
            "description": "Optional caller-provided tracking/correlation identifier",
        },
    },
    "required": ["to", "template"],
}

LAUNCH_FLOW_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Recipient phone number in E.164 format",
        },
        "flow_id": {
            "type": "string",
            "description": "Stable flow identifier or provider Meta Flow ID",
        },
        "flow_token": {
            "type": "string",
            "description": "Unique session token for the interactive flow session",
        },
        "cta": {
            "type": "string",
            "description": "Call-to-action button text (max 20 characters)",
        },
        "body": {
            "type": "string",
            "description": "Message body text describing the action (max 1024 characters)",
        },
        "header": {
            "type": "string",
            "description": "Optional header text for the interactive message",
        },
        "footer": {
            "type": "string",
            "description": "Optional footer text for the interactive message",
        },
        "mode": {
            "type": "string",
            "enum": ["published", "draft"],
            "description": "Flow mode (default: published)",
        },
        "flow_action": {
            "type": "string",
            "enum": ["navigate", "data_exchange"],
            "description": "Flow action mode (default: navigate)",
        },
        "flow_action_payload": {
            "type": "object",
            "description": "Payload for flow navigation: target screen and optional initial data",
            "properties": {
                "screen": {"type": "string"},
                "data": {"type": "object"},
            },
        },
        "correlation_id": {
            "type": "string",
            "description": "Optional caller-provided tracking/correlation identifier",
        },
    },
    "required": ["to", "flow_id"],
}

GET_TEMPLATE_STATUS_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "template": {
            "type": "string",
            "description": "Template name or provider ID to query",
        },
        "waba_id": {
            "type": "string",
            "description": "Optional WhatsApp Business Account ID override",
        },
    },
    "required": ["template"],
}

GET_FLOW_STATUS_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "flow_id": {
            "type": "string",
            "description": "Flow identifier or Meta Flow ID to query",
        },
    },
    "required": ["flow_id"],
}

VALID_TEMPLATE_STATUSES = frozenset(
    {"APPROVED", "PENDING", "REJECTED", "PAUSED", "DISABLED", "UNKNOWN"}
)
VALID_FLOW_STATUSES = frozenset(
    {"PUBLISHED", "DRAFT", "DEPRECATED", "BLOCKED", "THROTTLED", "UNKNOWN"}
)


def mask_phone_number(phone: str) -> str:
    s = str(phone).strip()
    if len(s) <= 4:
        return "***"
    head = s[:3]
    tail = s[-2:]
    return f"{head}{'*' * max(1, len(s) - 5)}{tail}"


def clean_recipient_phone(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    trimmed = raw.strip()
    if not trimmed:
        return ""
    has_plus = trimmed.startswith("+")
    digits = re.sub(r"\D", "", trimmed)
    if not digits:
        return ""
    return f"+{digits}" if has_plus else digits


@dataclass
class WhatsAppTemplateComponent:
    type: str
    format: Optional[str] = None
    text: Optional[str] = None
    variables: Optional[List[str]] = None
    buttons: Optional[List[Dict[str, Any]]] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type}
        if self.format is not None:
            out["format"] = self.format
        if self.text is not None:
            out["text"] = self.text
        if self.variables is not None:
            out["variables"] = self.variables
        if self.buttons is not None:
            out["buttons"] = self.buttons
        return out


@dataclass
class WhatsAppTemplateDefinition:
    name: str
    language: str = "en_US"
    category: Optional[str] = None
    provider_id: Optional[str] = None
    status: str = "APPROVED"
    components: Optional[List[Union[WhatsAppTemplateComponent, Dict[str, Any]]]] = None
    description: Optional[str] = None

    def validate(self) -> "WhatsAppTemplateDefinition":
        name = self.name.strip() if isinstance(self.name, str) else ""
        if not name:
            raise ValueError("whatsapp template requires a non-empty name")
        language = self.language.strip() if isinstance(self.language, str) and self.language.strip() else "en_US"
        status = self.status if self.status in VALID_TEMPLATE_STATUSES else "APPROVED"
        parsed_components: List[Union[WhatsAppTemplateComponent, Dict[str, Any]]] = []
        for c in self.components or []:
            if isinstance(c, WhatsAppTemplateComponent):
                parsed_components.append(c)
            elif isinstance(c, dict) and "type" in c:
                parsed_components.append(
                    WhatsAppTemplateComponent(
                        type=c["type"],
                        format=c.get("format"),
                        text=c.get("text"),
                        variables=c.get("variables"),
                        buttons=c.get("buttons"),
                    )
                )
            else:
                parsed_components.append(c)
        return WhatsAppTemplateDefinition(
            name=name,
            language=language,
            category=self.category,
            provider_id=self.provider_id,
            status=status,
            components=parsed_components,
            description=self.description,
        )

    def to_wire(self) -> Dict[str, Any]:
        comps: List[Dict[str, Any]] = []
        for c in self.components or []:
            if isinstance(c, WhatsAppTemplateComponent):
                comps.append(c.to_wire())
            elif isinstance(c, dict):
                comps.append(c)
        out: Dict[str, Any] = {
            "name": self.name,
            "language": self.language,
            "status": self.status,
        }
        if self.category is not None:
            out["category"] = self.category
        if self.provider_id is not None:
            out["provider_id"] = self.provider_id
        if comps:
            out["components"] = comps
        if self.description is not None:
            out["description"] = self.description
        return out


@dataclass
class WhatsAppFlowScreen:
    id: str
    title: Optional[str] = None
    terminal: bool = False
    data: Optional[Dict[str, Any]] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "terminal": self.terminal}
        if self.title is not None:
            out["title"] = self.title
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass
class WhatsAppFlowDefinition:
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    provider_flow_id: Optional[str] = None
    categories: Optional[List[str]] = None
    status: str = "PUBLISHED"
    mode: str = "published"
    cta: str = "Continue"
    screens: Optional[List[Union[WhatsAppFlowScreen, Dict[str, Any]]]] = None
    endpoint_url: Optional[str] = None

    def validate(self) -> "WhatsAppFlowDefinition":
        flow_id = self.id.strip() if isinstance(self.id, str) else ""
        if not flow_id:
            raise ValueError("whatsapp flow requires a non-empty id")
        status = self.status if self.status in VALID_FLOW_STATUSES else "PUBLISHED"
        mode = self.mode if self.mode in ("published", "draft") else ("draft" if status == "DRAFT" else "published")
        cta = self.cta.strip() if isinstance(self.cta, str) and self.cta.strip() else "Continue"
        parsed_screens: List[Union[WhatsAppFlowScreen, Dict[str, Any]]] = []
        for s in self.screens or []:
            if isinstance(s, WhatsAppFlowScreen):
                parsed_screens.append(s)
            elif isinstance(s, dict) and "id" in s:
                parsed_screens.append(
                    WhatsAppFlowScreen(
                        id=s["id"],
                        title=s.get("title"),
                        terminal=bool(s.get("terminal", False)),
                        data=s.get("data"),
                    )
                )
            else:
                parsed_screens.append(s)
        return WhatsAppFlowDefinition(
            id=flow_id,
            name=self.name,
            description=self.description,
            provider_flow_id=self.provider_flow_id,
            categories=list(self.categories or []),
            status=status,
            mode=mode,
            cta=cta,
            screens=parsed_screens,
            endpoint_url=self.endpoint_url,
        )

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "mode": self.mode,
            "cta": self.cta,
            "screens_count": len(self.screens or []),
        }
        if self.name is not None:
            out["name"] = self.name
        if self.description is not None:
            out["description"] = self.description
        if self.provider_flow_id is not None:
            out["provider_flow_id"] = self.provider_flow_id
        if self.categories:
            out["categories"] = self.categories
        return out


@dataclass
class WhatsAppRegistration:
    version: int = 1
    provider: str = "meta"
    templates: List[WhatsAppTemplateDefinition] = field(default_factory=list)
    flows: List[WhatsAppFlowDefinition] = field(default_factory=list)

    def to_capability(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "provider": self.provider,
            "templates": [t.to_wire() for t in self.templates],
            "flows": [f.to_wire() for f in self.flows],
        }


def validate_whatsapp_definition(
    definition: Union[WhatsAppRegistration, Dict[str, Any]]
) -> WhatsAppRegistration:
    if isinstance(definition, WhatsAppRegistration):
        return definition
    if not isinstance(definition, dict):
        raise ValueError("whatsapp definition must be a dict or WhatsAppRegistration")

    version = int(definition.get("version", 1))
    provider = str(definition.get("provider", "meta"))
    raw_templates = definition.get("templates") or []
    raw_flows = definition.get("flows") or []

    templates: List[WhatsAppTemplateDefinition] = []
    seen_templates: Set[str] = set()
    for t in raw_templates:
        if isinstance(t, WhatsAppTemplateDefinition):
            v = t.validate()
        elif isinstance(t, dict):
            v = WhatsAppTemplateDefinition(
                name=t.get("name", ""),
                language=t.get("language", "en_US"),
                category=t.get("category"),
                provider_id=t.get("provider_id") or t.get("providerId"),
                status=t.get("status", "APPROVED"),
                components=t.get("components"),
                description=t.get("description"),
            ).validate()
        else:
            raise ValueError("invalid whatsapp template format")
        if v.name in seen_templates:
            raise ValueError(f'duplicate whatsapp template name "{v.name}"')
        seen_templates.add(v.name)
        templates.push(v) if hasattr(templates, "push") else templates.append(v)

    flows: List[WhatsAppFlowDefinition] = []
    seen_flows: Set[str] = set()
    for f in raw_flows:
        if isinstance(f, WhatsAppFlowDefinition):
            v = f.validate()
        elif isinstance(f, dict):
            v = WhatsAppFlowDefinition(
                id=f.get("id", ""),
                name=f.get("name"),
                description=f.get("description"),
                provider_flow_id=f.get("provider_flow_id") or f.get("providerFlowId"),
                categories=f.get("categories"),
                status=f.get("status", "PUBLISHED"),
                mode=f.get("mode", "published"),
                cta=f.get("cta", "Continue"),
                screens=f.get("screens"),
                endpoint_url=f.get("endpoint_url") or f.get("endpointUrl"),
            ).validate()
        else:
            raise ValueError("invalid whatsapp flow format")
        if v.id in seen_flows:
            raise ValueError(f'duplicate whatsapp flow id "{v.id}"')
        seen_flows.add(v.id)
        flows.append(v)

    return WhatsAppRegistration(
        version=version,
        provider=provider,
        templates=templates,
        flows=flows,
    )


@dataclass
class ResolvedWhatsAppCredentials:
    access_token: str
    phone_number_id: str
    waba_id: Optional[str] = None
    api_version: str = "v22.0"
    base_url: str = "https://graph.facebook.com"
    provider: str = "meta"


def resolve_whatsapp_credentials(
    settings: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> ResolvedWhatsAppCredentials:
    import os

    e = env if env is not None else os.environ
    s = settings or {}
    nested = s.get("whatsapp") if isinstance(s.get("whatsapp"), dict) else {}

    access_token = (
        nested.get("access_token")
        or nested.get("accessToken")
        or s.get("whatsapp_access_token")
        or e.get("WHATSAPP_ACCESS_TOKEN", "")
    )
    if isinstance(access_token, str):
        access_token = access_token.strip()
    else:
        access_token = ""

    phone_number_id = (
        nested.get("phone_number_id")
        or nested.get("phoneNumberId")
        or s.get("whatsapp_phone_number_id")
        or s.get("phone_number_id")
        or e.get("WHATSAPP_PHONE_NUMBER_ID", "")
    )
    if isinstance(phone_number_id, str):
        phone_number_id = phone_number_id.strip()
    else:
        phone_number_id = ""

    waba_id = (
        nested.get("waba_id")
        or nested.get("wabaId")
        or s.get("whatsapp_waba_id")
        or s.get("waba_id")
        or e.get("WHATSAPP_WABA_ID")
        or e.get("WHATSAPP_BUSINESS_ACCOUNT_ID")
    )
    if isinstance(waba_id, str):
        waba_id = waba_id.strip() or None
    else:
        waba_id = None

    api_version = e.get("WHATSAPP_API_VERSION", "v22.0").strip()
    base_url = e.get("WHATSAPP_GRAPH_BASE_URL", "https://graph.facebook.com").strip().rstrip("/")
    provider = str(nested.get("provider") or s.get("provider") or "meta")

    return ResolvedWhatsAppCredentials(
        access_token=access_token,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        api_version=api_version,
        base_url=base_url,
        provider=provider,
    )


def build_meta_template_components(
    template_def: Optional[WhatsAppTemplateDefinition],
    parameters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not template_def or not template_def.components:
        if parameters:
            params = [{"type": "text", "text": str(v)} for v in parameters.values()]
            return [{"type": "body", "parameters": params}]
        return []

    meta_components: List[Dict[str, Any]] = []
    for c in template_def.components:
        c_type = c.type.lower() if isinstance(c, WhatsAppTemplateComponent) else str(c.get("type", "")).lower()
        if c_type in ("header", "body"):
            variables = (
                c.variables if isinstance(c, WhatsAppTemplateComponent) else c.get("variables")
            ) or []
            if variables:
                params: List[Dict[str, Any]] = []
                for v in variables:
                    val = parameters.get(v, "")
                    params.append({"type": "text", "text": str(val)})
                meta_components.append({"type": c_type, "parameters": params})
    return meta_components


def execute_send_template(
    ctx: Any,
    registration: WhatsAppRegistration,
    http_post: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    params = ctx.parameters or {}
    raw_to = (
        params.get("to")
        or (ctx.identity.get("phone") if ctx.identity else None)
        or (ctx.identity.get("whatsapp_number") if ctx.identity else None)
    )
    recipient = clean_recipient_phone(raw_to)
    template_ref = str(params.get("template") or "").strip()
    correlation_id = str(params.get("correlation_id") or "").strip() or str(uuid.uuid4())

    if not recipient:
        return {
            "status": "failed",
            "message_id": None,
            "recipient": "",
            "template": template_ref,
            "correlation_id": correlation_id,
            "provider": "meta",
            "timestamp": now,
            "error": {
                "code": "missing_parameter",
                "message": 'Parameter "to" is required and must contain digits',
                "retryable": False,
            },
        }

    if not template_ref:
        return {
            "status": "failed",
            "message_id": None,
            "recipient": recipient,
            "template": "",
            "correlation_id": correlation_id,
            "provider": "meta",
            "timestamp": now,
            "error": {
                "code": "missing_parameter",
                "message": 'Parameter "template" is required',
                "retryable": False,
            },
        }

    creds = resolve_whatsapp_credentials(ctx.settings)
    if not creds.access_token or not creds.phone_number_id:
        return {
            "status": "failed",
            "message_id": None,
            "recipient": recipient,
            "template": template_ref,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": "missing_credentials",
                "message": "WhatsApp provider credentials (access token and phone number ID) not configured",
                "retryable": False,
            },
        }

    registered: Optional[WhatsAppTemplateDefinition] = None
    for t in registration.templates:
        if t.name == template_ref:
            registered = t
            break

    language = str(params.get("language") or "").strip() or (registered.language if registered else "en_US")
    provider_id = (registered.provider_id if registered else None) or template_ref

    components_override = params.get("components")
    if isinstance(components_override, list):
        components = components_override
    else:
        var_params = params.get("parameters") if isinstance(params.get("parameters"), dict) else {}
        components = build_meta_template_components(registered, var_params)

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient.lstrip("+"),
        "type": "template",
        "template": {
            "name": provider_id,
            "language": {"code": language},
            "components": components,
        },
    }

    url = f"{creds.base_url}/{creds.api_version}/{creds.phone_number_id}/messages"
    logger.info(
        '[WhatsApp] Sending template "%s" to %s (correlation: %s)',
        template_ref,
        mask_phone_number(recipient),
        correlation_id,
    )

    try:
        if http_post:
            status_code, text = http_post(url, payload, creds.access_token)
        else:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {creds.access_token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status_code = resp.status
                text = resp.read().decode("utf-8")

        body: Dict[str, Any] = {}
        try:
            body = json.loads(text)
        except Exception:
            pass

        if 200 <= status_code < 300:
            messages = body.get("messages") or []
            msg_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
            return {
                "status": "accepted",
                "message_id": msg_id,
                "recipient": recipient,
                "template": template_ref,
                "language": language,
                "provider_id": provider_id,
                "correlation_id": correlation_id,
                "provider": creds.provider,
                "timestamp": now,
                "error": None,
            }

        meta_err = body.get("error") if isinstance(body.get("error"), dict) else {}
        err_code = str(meta_err.get("code") or status_code)
        is_retryable = status_code == 429 or err_code in ("4", "80007")
        return {
            "status": "failed" if is_retryable else "rejected",
            "message_id": None,
            "recipient": recipient,
            "template": template_ref,
            "language": language,
            "provider_id": provider_id,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": err_code,
                "message": meta_err.get("message") or text or "WhatsApp API request rejected",
                "subcode": meta_err.get("error_subcode"),
                "retryable": is_retryable,
            },
        }
    except urllib.error.HTTPError as e:
        status_code = e.code
        err_text = e.read().decode("utf-8") if e.fp else ""
        body = {}
        try:
            body = json.loads(err_text)
        except Exception:
            pass
        meta_err = body.get("error") if isinstance(body.get("error"), dict) else {}
        err_code = str(meta_err.get("code") or status_code)
        is_retryable = status_code == 429 or err_code in ("4", "80007")
        return {
            "status": "failed" if is_retryable else "rejected",
            "message_id": None,
            "recipient": recipient,
            "template": template_ref,
            "language": language,
            "provider_id": provider_id,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": err_code,
                "message": meta_err.get("message") or err_text or "WhatsApp API request rejected",
                "subcode": meta_err.get("error_subcode"),
                "retryable": is_retryable,
            },
        }
    except Exception as err:
        logger.error(
            "[WhatsApp] Network error sending template to %s: %s",
            mask_phone_number(recipient),
            err,
        )
        return {
            "status": "unknown",
            "message_id": None,
            "recipient": recipient,
            "template": template_ref,
            "language": language,
            "provider_id": provider_id,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": "timeout_or_network_error",
                "message": f"Request failed; outcome ambiguous: {err}",
                "retryable": True,
            },
        }


def execute_launch_flow(
    ctx: Any,
    registration: WhatsAppRegistration,
    http_post: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    params = ctx.parameters or {}
    raw_to = (
        params.get("to")
        or (ctx.identity.get("phone") if ctx.identity else None)
        or (ctx.identity.get("whatsapp_number") if ctx.identity else None)
    )
    recipient = clean_recipient_phone(raw_to)
    flow_ref = str(params.get("flow_id") or "").strip()
    correlation_id = str(params.get("correlation_id") or "").strip() or str(uuid.uuid4())

    if not recipient:
        return {
            "status": "failed",
            "message_id": None,
            "recipient": "",
            "flow_id": flow_ref,
            "correlation_id": correlation_id,
            "provider": "meta",
            "timestamp": now,
            "error": {
                "code": "missing_parameter",
                "message": 'Parameter "to" is required and must contain digits',
                "retryable": False,
            },
        }

    if not flow_ref:
        return {
            "status": "failed",
            "message_id": None,
            "recipient": recipient,
            "flow_id": "",
            "correlation_id": correlation_id,
            "provider": "meta",
            "timestamp": now,
            "error": {
                "code": "missing_parameter",
                "message": 'Parameter "flow_id" is required',
                "retryable": False,
            },
        }

    creds = resolve_whatsapp_credentials(ctx.settings)
    if not creds.access_token or not creds.phone_number_id:
        return {
            "status": "failed",
            "message_id": None,
            "recipient": recipient,
            "flow_id": flow_ref,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": "missing_credentials",
                "message": "WhatsApp provider credentials (access token and phone number ID) not configured",
                "retryable": False,
            },
        }

    registered: Optional[WhatsAppFlowDefinition] = None
    for f in registration.flows:
        if f.id == flow_ref or f.provider_flow_id == flow_ref:
            registered = f
            break

    if registered and registered.status in ("BLOCKED", "DEPRECATED"):
        return {
            "status": "rejected",
            "message_id": None,
            "recipient": recipient,
            "flow_id": flow_ref,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": "flow_not_usable",
                "message": f'WhatsApp Flow "{flow_ref}" is in unusable status: {registered.status}',
                "retryable": False,
            },
        }

    provider_flow_id = (registered.provider_flow_id if registered else None) or flow_ref
    flow_token = str(params.get("flow_token") or "").strip() or str(uuid.uuid4())
    cta = str(params.get("cta") or "").strip() or (registered.cta if registered else "Continue")
    mode = str(params.get("mode") or "").strip() or (registered.mode if registered else "published")
    flow_action = str(params.get("flow_action") or "").strip() or "navigate"
    raw_desc = (registered.description if registered else None) or f"Start {cta}"
    body_text = str(params.get("body") or "").strip() or raw_desc
    header_text = str(params.get("header") or "").strip() if params.get("header") else None
    footer_text = str(params.get("footer") or "").strip() if params.get("footer") else None

    action_payload: Dict[str, Any] = {
        "flow_token": flow_token,
        "flow_id": provider_flow_id,
        "flow_cta": cta[:20],
        "flow_action": flow_action,
    }
    if mode in ("draft", "published"):
        action_payload["mode"] = mode

    if isinstance(params.get("flow_action_payload"), dict):
        action_payload["flow_action_payload"] = params["flow_action_payload"]
    elif registered and registered.screens:
        first_screen = registered.screens[0]
        screen_id = first_screen.id if isinstance(first_screen, WhatsAppFlowScreen) else first_screen.get("id")
        action_payload["flow_action_payload"] = {
            "screen": screen_id,
            "data": {},
        }

    interactive: Dict[str, Any] = {
        "type": "flow",
        "body": {"text": body_text[:1024]},
        "action": {
            "name": "flow",
            "parameters": action_payload,
        },
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text}
    if footer_text:
        interactive["footer"] = {"text": footer_text}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient.lstrip("+"),
        "type": "interactive",
        "interactive": interactive,
    }

    url = f"{creds.base_url}/{creds.api_version}/{creds.phone_number_id}/messages"
    logger.info(
        '[WhatsApp] Launching Flow "%s" (%s) to %s (correlation: %s)',
        flow_ref,
        provider_flow_id,
        mask_phone_number(recipient),
        correlation_id,
    )

    try:
        if http_post:
            status_code, text = http_post(url, payload, creds.access_token)
        else:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {creds.access_token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status_code = resp.status
                text = resp.read().decode("utf-8")

        body = {}
        try:
            body = json.loads(text)
        except Exception:
            pass

        if 200 <= status_code < 300:
            messages = body.get("messages") or []
            msg_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
            return {
                "status": "accepted",
                "message_id": msg_id,
                "recipient": recipient,
                "flow_id": flow_ref,
                "provider_flow_id": provider_flow_id,
                "flow_token": flow_token,
                "correlation_id": correlation_id,
                "provider": creds.provider,
                "timestamp": now,
                "error": None,
            }

        meta_err = body.get("error") if isinstance(body.get("error"), dict) else {}
        err_code = str(meta_err.get("code") or status_code)
        is_retryable = status_code == 429 or err_code in ("4", "80007")
        return {
            "status": "failed" if is_retryable else "rejected",
            "message_id": None,
            "recipient": recipient,
            "flow_id": flow_ref,
            "provider_flow_id": provider_flow_id,
            "flow_token": flow_token,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": err_code,
                "message": meta_err.get("message") or text or "WhatsApp Flow launch rejected",
                "subcode": meta_err.get("error_subcode"),
                "retryable": is_retryable,
            },
        }
    except urllib.error.HTTPError as e:
        status_code = e.code
        err_text = e.read().decode("utf-8") if e.fp else ""
        body = {}
        try:
            body = json.loads(err_text)
        except Exception:
            pass
        meta_err = body.get("error") if isinstance(body.get("error"), dict) else {}
        err_code = str(meta_err.get("code") or status_code)
        is_retryable = status_code == 429 or err_code in ("4", "80007")
        return {
            "status": "failed" if is_retryable else "rejected",
            "message_id": None,
            "recipient": recipient,
            "flow_id": flow_ref,
            "provider_flow_id": provider_flow_id,
            "flow_token": flow_token,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": err_code,
                "message": meta_err.get("message") or err_text or "WhatsApp Flow launch rejected",
                "subcode": meta_err.get("error_subcode"),
                "retryable": is_retryable,
            },
        }
    except Exception as err:
        logger.error(
            "[WhatsApp] Network error launching Flow to %s: %s",
            mask_phone_number(recipient),
            err,
        )
        return {
            "status": "unknown",
            "message_id": None,
            "recipient": recipient,
            "flow_id": flow_ref,
            "provider_flow_id": provider_flow_id,
            "flow_token": flow_token,
            "correlation_id": correlation_id,
            "provider": creds.provider,
            "timestamp": now,
            "error": {
                "code": "timeout_or_network_error",
                "message": f"Request failed; outcome ambiguous: {err}",
                "retryable": True,
            },
        }


def execute_get_template_status(
    ctx: Any,
    registration: WhatsAppRegistration,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    params = ctx.parameters or {}
    template_ref = str(params.get("template") or "").strip()

    if not template_ref:
        return {
            "status": "UNKNOWN",
            "template": "",
            "provider": "meta",
            "timestamp": now,
            "error": {"code": "missing_parameter", "message": 'Parameter "template" is required'},
        }

    creds = resolve_whatsapp_credentials(ctx.settings)
    registered: Optional[WhatsAppTemplateDefinition] = None
    for t in registration.templates:
        if t.name == template_ref:
            registered = t
            break

    # If no network credentials configured, return registered template status if present
    if not creds.access_token or (not creds.waba_id and not (registered and registered.provider_id)):
        if registered:
            return {
                "status": registered.status,
                "template": registered.name,
                "language": registered.language,
                "category": registered.category,
                "provider_id": registered.provider_id,
                "provider": creds.provider,
                "timestamp": now,
                "error": None,
            }
        return {
            "status": "UNKNOWN",
            "template": template_ref,
            "provider": creds.provider,
            "timestamp": now,
            "error": {"code": "missing_credentials", "message": "WhatsApp access token and WABA ID not configured"},
        }

    target_id = (registered.provider_id if registered else None) or template_ref
    url = f"{creds.base_url}/{creds.api_version}/{target_id}?fields=name,status,language,category,id"

    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {creds.access_token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "status" in data:
                return {
                    "status": data["status"],
                    "template": data.get("name") or template_ref,
                    "provider_id": data.get("id") or target_id,
                    "language": data.get("language") or (registered.language if registered else "en_US"),
                    "category": data.get("category") or (registered.category if registered else None),
                    "provider": creds.provider,
                    "timestamp": now,
                    "error": None,
                }
        return {
            "status": registered.status if registered else "UNKNOWN",
            "template": template_ref,
            "provider_id": registered.provider_id if registered else None,
            "provider": creds.provider,
            "timestamp": now,
            "error": None,
        }
    except Exception as err:
        return {
            "status": registered.status if registered else "UNKNOWN",
            "template": template_ref,
            "provider_id": registered.provider_id if registered else None,
            "provider": creds.provider,
            "timestamp": now,
            "error": {"code": "network_error", "message": str(err)},
        }


def execute_get_flow_status(
    ctx: Any,
    registration: WhatsAppRegistration,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    params = ctx.parameters or {}
    flow_ref = str(params.get("flow_id") or "").strip()

    if not flow_ref:
        return {
            "status": "UNKNOWN",
            "flow_id": "",
            "provider": "meta",
            "timestamp": now,
            "error": {"code": "missing_parameter", "message": 'Parameter "flow_id" is required'},
        }

    creds = resolve_whatsapp_credentials(ctx.settings)
    registered: Optional[WhatsAppFlowDefinition] = None
    for f in registration.flows:
        if f.id == flow_ref or f.provider_flow_id == flow_ref:
            registered = f
            break

    if not creds.access_token:
        if registered:
            return {
                "status": registered.status,
                "flow_id": registered.id,
                "provider_flow_id": registered.provider_flow_id,
                "name": registered.name,
                "categories": registered.categories or [],
                "validation_errors": [],
                "provider": creds.provider,
                "timestamp": now,
                "error": None,
            }
        return {
            "status": "UNKNOWN",
            "flow_id": flow_ref,
            "provider": creds.provider,
            "timestamp": now,
            "error": {"code": "missing_credentials", "message": "WhatsApp access token not configured"},
        }

    target_id = (registered.provider_flow_id if registered else None) or flow_ref
    url = f"{creds.base_url}/{creds.api_version}/{target_id}?fields=id,name,status,categories,validation_errors"

    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {creds.access_token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "status" in data:
                return {
                    "status": data["status"],
                    "flow_id": flow_ref,
                    "provider_flow_id": data.get("id") or target_id,
                    "name": data.get("name") or (registered.name if registered else None),
                    "categories": data.get("categories") or (registered.categories if registered else []),
                    "validation_errors": data.get("validation_errors") or [],
                    "provider": creds.provider,
                    "timestamp": now,
                    "error": None,
                }
        return {
            "status": registered.status if registered else "UNKNOWN",
            "flow_id": flow_ref,
            "provider_flow_id": registered.provider_flow_id if registered else None,
            "categories": registered.categories if registered else [],
            "validation_errors": [],
            "provider": creds.provider,
            "timestamp": now,
            "error": None,
        }
    except Exception as err:
        return {
            "status": registered.status if registered else "UNKNOWN",
            "flow_id": flow_ref,
            "provider_flow_id": registered.provider_flow_id if registered else None,
            "provider": creds.provider,
            "timestamp": now,
            "error": {"code": "network_error", "message": str(err)},
        }
