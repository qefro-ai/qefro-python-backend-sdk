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
from .model import (
    AuthBuilder,
    AuthenticationContext,
    AuthOutcome,
    BusinessFlowMetadata,
    ChallengePayload,
    ChallengeSignal,
    FlowBuilder,
    FlowError,
    ToolDefinition,
    ToolLookup,
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
    "ServerHandle",
    "ToolDefinition",
    "ToolLookup",
    "BusinessFlowMetadata",
    "FlowBuilder",
    "FlowError",
    "ChallengePayload",
    "AuthenticationContext",
    "AuthOutcome",
    "AuthBuilder",
    "ChallengeSignal",
    "normalize_lookup",
    "SDK_NAME",
    "SDK_VERSION",
    "__version__",
]
