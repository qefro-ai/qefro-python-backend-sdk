"""End-to-end runtime Business Flows example: a cancellation flow the Qefro
Runtime executes, combining `ask`, `condition` branching, human `approval`,
and an OTP-authenticated `tool` step. Mirrors the JS and Rust SDK examples.

    track-order:   ask -> tool -> condition -> complete
    cancel-order:  ask -> tool -> condition -> approval (human) -> tool (auth: OTP) -> complete

The SDK never executes steps: flows are advertised via `capabilities.list`, and
the Runtime calls back into the tools below over the same signed webhook when a
`tool` step runs.

Run:
    export QEFRO_SIGNING_SECRET=dev-secret
    python examples/order-approval/server.py
"""

import os
import re

from qefro_backend import CustomerProvider, Qefro

# Demo OTP for exercising the challenge step end-to-end (never sent anywhere).
DUMMY_OTP = os.environ.get("DEMO_OTP", "123456")

ORDERS = {
    "ORD-1001": {"id": "ORD-1001", "status": "processing", "items": ["Wireless Mouse", "USB-C Hub"], "total": 79.98, "currency": "USD", "eta": "2026-08-05"},
    "ORD-1002": {"id": "ORD-1002", "status": "shipped", "items": ["Standing Desk Mat"], "total": 49.0, "currency": "USD", "eta": "2026-08-01"},
    "ORD-1003": {"id": "ORD-1003", "status": "delivered", "items": ["Laptop Sleeve"], "total": 24.5, "currency": "USD", "eta": None},
}


def normalize_order_id(raw) -> str:
    return re.sub(r"\s+", "", str(raw or "").strip().upper())


class DemoCustomer(CustomerProvider):
    """The flow's authenticated cancel step triggers this OTP challenge
    automatically — the Runtime pauses the run, relays the prompt to the
    customer, and resumes the tool call with their answer.
    """

    async def lookup(self, ctx):
        identity = ctx.identity or {}
        cid = str(identity.get("customer_id") or identity.get("user_id") or identity.get("phone") or "cust-demo")
        return {"id": cid, "name": f"Customer {cid}"}

    async def authorize(self, ctx):
        if not ctx.response:
            return ctx.auth.sms_otp(
                "+00 0000 000000",
                f"To verify it's you, please enter the one-time code we sent to your phone. (Demo: the code is {DUMMY_OTP}.)",
            )
        if ctx.response.strip() != DUMMY_OTP:
            return ctx.auth.sms_otp(
                "+00 0000 000000",
                f"That code is incorrect. Please try again. (Demo: the code is {DUMMY_OTP}.)",
            )
        return ctx.auth.success(
            ctx.customer,
            {
                "type": "bearer_token",
                "access_token": f"demo-{ctx.customer['id']}",
                "expires_in": 900,
                "customer_id": str(ctx.customer["id"]),
            },
        )


def main():
    signing_secret = os.environ.get("QEFRO_SIGNING_SECRET", "dev-secret")
    port = int(os.environ.get("PORT", "8093"))

    app = Qefro(signing_secret, endpoint_path="/qefro")
    app.customer(DemoCustomer())

    @app.tool(
        "order_status_check",
        auth="none",
        description="Look up the status of an order by order ID.",
        input_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "Order ID such as ORD-1001"}},
            "required": ["order_id"],
        },
    )
    async def order_status_check(ctx):
        order_id = normalize_order_id(ctx.parameters.get("order_id"))
        order = ORDERS.get(order_id)
        if not order:
            return {"found": False, "order_id": order_id, "message": f"No order found for {order_id}.", "sample_ids": list(ORDERS)}
        return {
            "found": True,
            "order_id": order["id"],
            "status": order["status"],
            "items": order["items"],
            "total": order["total"],
            "currency": order["currency"],
            "eta": order["eta"],
            "message": f"Order {order['id']} is {order['status']}.",
        }

    @app.tool(
        "order_cancel",
        auth="required",
        description="Cancel an order by order ID. Requires customer verification.",
        input_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "Order ID to cancel"}},
            "required": ["order_id"],
        },
    )
    async def order_cancel(ctx):
        ctx.customer.require()
        order_id = normalize_order_id(ctx.parameters.get("order_id"))
        order = ORDERS.get(order_id)
        if not order:
            return {"cancelled": False, "order_id": order_id, "message": f"No order found for {order_id}."}
        if order["status"] in ("delivered", "cancelled"):
            return {"cancelled": False, "order_id": order["id"], "message": f"Order {order['id']} is {order['status']} and can no longer be cancelled."}
        order["status"] = "cancelled"
        order["eta"] = None
        return {"cancelled": True, "order_id": order["id"], "message": f"Order {order['id']} has been cancelled. Any payment will be refunded in 3-5 business days."}

    # -----------------------------------------------------------------------
    # Business Flows. Still metadata only from the SDK's point of view.
    # -----------------------------------------------------------------------

    (
        app.flow(
            {
                "id": "track-order",
                "name": "Track an order",
                "description": "Collect an order ID, look it up, and report its status.",
                "category": "orders",
                "tags": ["orders", "tracking"],
                "intent": ["track my order", "where is my order", "what is the status of my order"],
                "outputs": ["order_status_check"],
            }
        )
        .ask("collect_order_id", field="order_id", prompt="Which order would you like to track? Please share your order ID (for example ORD-1001).")
        .tool("lookup_order", tool_ref="order_status_check")
        .condition("check_found", when="order_status_check.found == true", then="report_status", else_="report_missing")
        .complete("report_missing", message="I couldn't find an order with ID {{order_id}}. Please double-check the ID and try again.")
        .complete("report_status", message="Order {{order_id}} is currently {{order_status_check.status}}. {{order_status_check.message}}")
    )

    # cancel-order exercises the full runtime feature set:
    #   ask -> tool -> condition -> approval (human) -> tool with auth (OTP) -> complete
    (
        app.flow(
            {
                "id": "cancel-order",
                "name": "Cancel an order",
                "description": "Collect an order ID, verify it exists, get supervisor approval, verify the customer, then cancel the order.",
                "category": "orders",
                "tags": ["orders", "cancellation"],
                "intent": ["cancel my order", "i want to cancel an order", "stop my order from shipping"],
                "outputs": ["order_cancel"],
            }
        )
        .ask("collect_order_id", field="order_id", prompt="Which order do you want to cancel? Please share your order ID (for example ORD-1001).")
        .tool("lookup_order", tool_ref="order_status_check")
        .condition("check_found", when="order_status_check.found == true", then="await_approval", else_="report_missing")
        .complete("report_missing", message="I couldn't find an order with ID {{order_id}}, so there is nothing to cancel.")
        .approval("await_approval", prompt="Your cancellation request for order {{order_id}} has been sent to a supervisor for approval. I will confirm as soon as it is approved.")
        .tool("do_cancel", tool_ref="order_cancel")
        .complete("confirm_cancelled", message="{{order_cancel.message}}")
    )

    handle = app.listen(port)
    print("order-approval example listening")
    print(f"  Webhook URL   : {handle.url}")
    print(f"  Signing secret: {signing_secret}")
    print(f"  Demo OTP      : {DUMMY_OTP}")
    print("")
    print("Flows advertised via capabilities.list:")
    print("  track-order   ask -> tool -> condition -> complete")
    print("  cancel-order  ask -> tool -> condition -> approval -> tool(auth) -> complete")
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        handle.close()


if __name__ == "__main__":
    main()
