"""Qefro backend SDK.

Register Business Tools and Business Flows, serve the signed Qefro webhook, and
orchestrate customer authorization. Flows are metadata only — the SDK advertises
them through ``capabilities.list`` and the Qefro Runtime executes them.

Example
-------
>>> from qefro_backend import Qefro
>>> app = Qefro("dev-secret")
>>> @app.tool("get_orders", auth="required")
... async def get_orders(ctx):
...     return [{"orderId": "ord_1"}]
>>> app.run(8088)  # doctest: +SKIP
"""

from .app import (
    SDK_NAME,
    SDK_VERSION,
    CustomerAuthorizeContext,
    CustomerContext,
    CustomerLookupContext,
    CustomerProvider,
    Qefro,
    QefroConfig,
    ServerHandle,
    ToolContext,
)
from .customer_hub import (
    ConsentContext,
    MembershipContext,
    TimelineContext,
    env_flag_true,
    hub_customer_from_person,
    is_customer_hub_enabled,
    is_customer_hub_optional,
    pick_identity,
    read_identity_phone,
)
from .model import (
    AuthBuilder,
    AuthenticationContext,
    AuthOutcome,
    BusinessFlowMetadata,
    ChallengePayload,
    ChallengeSignal,
    EventHandlerDefinition,
    FlowBuilder,
    FlowError,
    FlowTrigger,
    ToolDefinition,
    ToolLookup,
    normalize_flow_trigger,
    normalize_lookup,
)

__version__ = SDK_VERSION

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
    "ToolDefinition",
    "ToolLookup",
    "BusinessFlowMetadata",
    "FlowTrigger",
    "EventHandlerDefinition",
    "FlowBuilder",
    "FlowError",
    "normalize_flow_trigger",
    "ChallengePayload",
    "AuthenticationContext",
    "AuthOutcome",
    "AuthBuilder",
    "ChallengeSignal",
    "normalize_lookup",
    "env_flag_true",
    "is_customer_hub_enabled",
    "is_customer_hub_optional",
    "hub_customer_from_person",
    "pick_identity",
    "read_identity_phone",
    "SDK_NAME",
    "SDK_VERSION",
    "__version__",
]
