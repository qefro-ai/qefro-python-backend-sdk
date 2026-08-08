"""Marketing capability registration (ADR-004 Phase 1).

Apps register metadata only — audiences, variables, actions, landing pages,
and channels. The platform owns campaigns, delivery, and analytics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

AudienceSource = Literal["customer_hub", "app_query", "static_filter"]
VariableType = Literal["string", "number", "datetime", "boolean", "url", "currency"]
VariableSource = Literal["customer_hub", "app_context", "campaign", "literal"]
ActionKind = Literal["url", "deep_link", "quick_reply", "whatsapp_cta", "postback"]
LandingHost = Literal["app", "platform"]

_AUDIENCE_SOURCES = frozenset({"customer_hub", "app_query", "static_filter"})
_VARIABLE_TYPES = frozenset({"string", "number", "datetime", "boolean", "url", "currency"})
_VARIABLE_SOURCES = frozenset({"customer_hub", "app_context", "campaign", "literal"})
_ACTION_KINDS = frozenset({"url", "deep_link", "quick_reply", "whatsapp_cta", "postback"})
_LANDING_HOSTS = frozenset({"app", "platform"})


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"marketing: {path} must be a non-empty string")
    return value.strip()


def _optional_str(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"marketing: {path} must be a string")
    trimmed = value.strip()
    return trimmed or None


def _optional_bool(value: Any, path: str) -> Optional[bool]:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"marketing: {path} must be a boolean")
    return value


def _optional_dict(value: Any, path: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"marketing: {path} must be an object")
    return value


def _assert_unique(ids: List[str], kind: str) -> None:
    seen = set()
    for i in ids:
        if i in seen:
            raise ValueError(f'marketing: duplicate {kind} id "{i}"')
        seen.add(i)


@dataclass
class MarketingAudience:
    id: str
    label: str
    source: str
    description: Optional[str] = None
    customerHub: Optional[Dict[str, Any]] = None
    appQuery: Optional[Dict[str, Any]] = None
    staticFilter: Optional[Dict[str, Any]] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "label": self.label, "source": self.source}
        if self.description is not None:
            out["description"] = self.description
        if self.customerHub is not None:
            out["customerHub"] = self.customerHub
        if self.appQuery is not None:
            out["appQuery"] = self.appQuery
        if self.staticFilter is not None:
            out["staticFilter"] = self.staticFilter
        return out


@dataclass
class MarketingVariable:
    id: str
    label: str
    type: str
    source: str
    path: Optional[str] = None
    required: Optional[bool] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "type": self.type,
            "source": self.source,
        }
        if self.path is not None:
            out["path"] = self.path
        if self.required is not None:
            out["required"] = self.required
        return out


@dataclass
class MarketingAction:
    id: str
    label: str
    kind: str
    landingPageId: Optional[str] = None
    urlTemplate: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "label": self.label, "kind": self.kind}
        if self.landingPageId is not None:
            out["landingPageId"] = self.landingPageId
        if self.urlTemplate is not None:
            out["urlTemplate"] = self.urlTemplate
        if self.payload is not None:
            out["payload"] = self.payload
        return out


@dataclass
class MarketingLandingPage:
    id: str
    label: str
    path: str
    host: str

    def to_wire(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "path": self.path, "host": self.host}


@dataclass
class MarketingChannel:
    id: str
    provider: Optional[str] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id}
        if self.provider is not None:
            out["provider"] = self.provider
        if self.label is not None:
            out["label"] = self.label
        if self.enabled is not None:
            out["enabled"] = self.enabled
        return out


@dataclass
class MarketingDefinition:
    version: Optional[int] = None
    audiences: List[Any] = field(default_factory=list)
    variables: List[Any] = field(default_factory=list)
    actions: List[Any] = field(default_factory=list)
    landingPages: List[Any] = field(default_factory=list)
    channels: List[Any] = field(default_factory=list)


@dataclass
class MarketingRegistration:
    version: int
    audiences: List[MarketingAudience]
    variables: List[MarketingVariable]
    actions: List[MarketingAction]
    landingPages: List[MarketingLandingPage]
    channels: List[MarketingChannel]

    def to_capability(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "metadata": {
                "audiences": [a.to_wire() for a in self.audiences],
                "variables": [v.to_wire() for v in self.variables],
                "actions": [a.to_wire() for a in self.actions],
                "landingPages": [p.to_wire() for p in self.landingPages],
                "channels": [c.to_wire() for c in self.channels],
            },
        }


def _as_dict(item: Any, index: int, kind: str) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "__dict__"):
        return {k: v for k, v in item.__dict__.items() if not k.startswith("_")}
    raise ValueError(f"marketing: {kind}[{index}] must be an object")


def _validate_audience(raw: Any, index: int) -> MarketingAudience:
    a = _as_dict(raw, index, "audiences")
    source = _require_str(a.get("source"), f"audiences[{index}].source")
    if source not in _AUDIENCE_SOURCES:
        raise ValueError(
            f"marketing: audiences[{index}].source must be one of customer_hub|app_query|static_filter"
        )
    customer_hub = a.get("customerHub")
    if customer_hub is not None and not isinstance(customer_hub, dict):
        raise ValueError(f"marketing: audiences[{index}].customerHub must be an object")
    app_query = a.get("appQuery")
    if app_query is not None:
        if not isinstance(app_query, dict):
            raise ValueError(f"marketing: audiences[{index}].appQuery must be an object")
        tool = _require_str(app_query.get("tool"), f"audiences[{index}].appQuery.tool")
        app_query = {"tool": tool, **({} if app_query.get("input") is None else {"input": _optional_dict(app_query.get("input"), f"audiences[{index}].appQuery.input")})}
        if "input" in app_query and app_query["input"] is None:
            del app_query["input"]
    return MarketingAudience(
        id=_require_str(a.get("id"), f"audiences[{index}].id"),
        label=_require_str(a.get("label"), f"audiences[{index}].label"),
        source=source,
        description=_optional_str(a.get("description"), f"audiences[{index}].description"),
        customerHub=customer_hub,
        appQuery=app_query,
        staticFilter=_optional_dict(a.get("staticFilter"), f"audiences[{index}].staticFilter"),
    )


def _validate_variable(raw: Any, index: int) -> MarketingVariable:
    v = _as_dict(raw, index, "variables")
    var_type = _require_str(v.get("type"), f"variables[{index}].type")
    if var_type not in _VARIABLE_TYPES:
        raise ValueError(
            f"marketing: variables[{index}].type must be one of string|number|datetime|boolean|url|currency"
        )
    source = _require_str(v.get("source"), f"variables[{index}].source")
    if source not in _VARIABLE_SOURCES:
        raise ValueError(
            f"marketing: variables[{index}].source must be one of customer_hub|app_context|campaign|literal"
        )
    return MarketingVariable(
        id=_require_str(v.get("id"), f"variables[{index}].id"),
        label=_require_str(v.get("label"), f"variables[{index}].label"),
        type=var_type,
        source=source,
        path=_optional_str(v.get("path"), f"variables[{index}].path"),
        required=_optional_bool(v.get("required"), f"variables[{index}].required"),
    )


def _validate_action(raw: Any, index: int) -> MarketingAction:
    a = _as_dict(raw, index, "actions")
    kind = _require_str(a.get("kind"), f"actions[{index}].kind")
    if kind not in _ACTION_KINDS:
        raise ValueError(
            f"marketing: actions[{index}].kind must be one of url|deep_link|quick_reply|whatsapp_cta|postback"
        )
    return MarketingAction(
        id=_require_str(a.get("id"), f"actions[{index}].id"),
        label=_require_str(a.get("label"), f"actions[{index}].label"),
        kind=kind,
        landingPageId=_optional_str(a.get("landingPageId"), f"actions[{index}].landingPageId"),
        urlTemplate=_optional_str(a.get("urlTemplate"), f"actions[{index}].urlTemplate"),
        payload=_optional_dict(a.get("payload"), f"actions[{index}].payload"),
    )


def _validate_landing_page(raw: Any, index: int) -> MarketingLandingPage:
    p = _as_dict(raw, index, "landingPages")
    host = _require_str(p.get("host"), f"landingPages[{index}].host")
    if host not in _LANDING_HOSTS:
        raise ValueError(f"marketing: landingPages[{index}].host must be one of app|platform")
    return MarketingLandingPage(
        id=_require_str(p.get("id"), f"landingPages[{index}].id"),
        label=_require_str(p.get("label"), f"landingPages[{index}].label"),
        path=_require_str(p.get("path"), f"landingPages[{index}].path"),
        host=host,
    )


def _validate_channel(raw: Any, index: int) -> MarketingChannel:
    c = _as_dict(raw, index, "channels")
    return MarketingChannel(
        id=_require_str(c.get("id"), f"channels[{index}].id"),
        provider=_optional_str(c.get("provider"), f"channels[{index}].provider"),
        label=_optional_str(c.get("label"), f"channels[{index}].label"),
        enabled=_optional_bool(c.get("enabled"), f"channels[{index}].enabled"),
    )


def validate_marketing_definition(
    defn: Union[MarketingDefinition, Dict[str, Any]],
) -> MarketingRegistration:
    """Validate and normalize a marketing definition."""
    if isinstance(defn, MarketingDefinition):
        raw = {
            "version": defn.version,
            "audiences": defn.audiences,
            "variables": defn.variables,
            "actions": defn.actions,
            "landingPages": defn.landingPages,
            "channels": defn.channels,
        }
    elif isinstance(defn, dict):
        raw = defn
    else:
        raise ValueError("marketing: definition must be an object")

    version = raw.get("version")
    if version is None:
        version = 1
    elif not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("marketing: version must be a positive integer")

    audiences_raw = raw.get("audiences") or []
    variables_raw = raw.get("variables") or []
    actions_raw = raw.get("actions") or []
    landing_raw = raw.get("landingPages") or []
    channels_raw = raw.get("channels") or []

    for name, arr in (
        ("audiences", audiences_raw),
        ("variables", variables_raw),
        ("actions", actions_raw),
        ("landingPages", landing_raw),
        ("channels", channels_raw),
    ):
        if not isinstance(arr, list):
            raise ValueError(f"marketing: {name} must be an array")

    audiences = [_validate_audience(a, i) for i, a in enumerate(audiences_raw)]
    variables = [_validate_variable(v, i) for i, v in enumerate(variables_raw)]
    actions = [_validate_action(a, i) for i, a in enumerate(actions_raw)]
    landing_pages = [_validate_landing_page(p, i) for i, p in enumerate(landing_raw)]
    channels = [_validate_channel(c, i) for i, c in enumerate(channels_raw)]

    _assert_unique([a.id for a in audiences], "audience")
    _assert_unique([v.id for v in variables], "variable")
    _assert_unique([a.id for a in actions], "action")
    _assert_unique([p.id for p in landing_pages], "landingPage")
    _assert_unique([c.id for c in channels], "channel")

    page_ids = {p.id for p in landing_pages}
    for i, action in enumerate(actions):
        if action.landingPageId and action.landingPageId not in page_ids:
            raise ValueError(
                f'marketing: actions[{i}].landingPageId "{action.landingPageId}" does not match any landingPages[].id'
            )

    return MarketingRegistration(
        version=version,
        audiences=audiences,
        variables=variables,
        actions=actions,
        landingPages=landing_pages,
        channels=channels,
    )


__all__ = [
    "MarketingAudience",
    "MarketingVariable",
    "MarketingAction",
    "MarketingLandingPage",
    "MarketingChannel",
    "MarketingDefinition",
    "MarketingRegistration",
    "validate_marketing_definition",
]
