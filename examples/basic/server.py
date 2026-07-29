"""Minimal Qefro Python SDK handler for Sync Tools / Test Connection.

Run:
    export QEFRO_SIGNING_SECRET=dev-secret
    python examples/basic/server.py

Then create an SDK Connection pointing at your public /qefro webhook (or a
tunnel) with the same secret, and Sync Tools.
"""

import os

from qefro_backend import CustomerProvider, Qefro


class DemoCustomer(CustomerProvider):
    async def lookup(self, ctx):
        identity = ctx.identity or {}
        cid = identity.get("customer_id") or identity.get("email") or identity.get("phone") or "demo-customer"
        return {"id": cid}

    async def authorize(self, ctx):
        # First call: challenge by email if we have one; resume succeeds.
        if not ctx.response:
            email = (ctx.identity or {}).get("email")
            if email:
                return ctx.auth.email_otp(email)
        return ctx.auth.success(
            ctx.customer,
            {"type": "bearer_token", "access_token": "dev", "expires_in": 900},
        )


def main():
    signing_secret = os.environ.get("QEFRO_SIGNING_SECRET", "dev-secret")
    port = int(os.environ.get("PORT", "8088"))

    app = Qefro(signing_secret, endpoint_path="/qefro")
    app.customer(DemoCustomer())

    @app.tool(
        "get_orders",
        auth="required",
        description="List orders for the authenticated customer",
        lookup={"required": ["email"]},
    )
    async def get_orders(ctx):
        customer = ctx.customer.require()
        return [{"orderId": "ord_1", "customerId": customer["id"]}]

    # Business Flows are metadata only: advertised through capabilities.list and
    # orchestrated by the Qefro Runtime.
    (
        app.flow(
            {
                "id": "order_lookup",
                "name": "Order Lookup",
                "description": "Lookup customer orders",
                "category": "crm",
                "tags": ["customer", "orders"],
                "intent": ["track order", "where is my order", "find my shipment"],
                "inputs": ["email"],
                "outputs": ["customer", "orders"],
            }
        )
        .ask("email", field="email", prompt="Please enter your email.")
        .tool("orders", tool_ref="get_orders")
        .complete("done", message="Here are your recent orders.")
    )

    handle = app.listen(port)
    print(f"Qefro Python SDK listening at {handle.url}")
    print("Wire this URL into Admin Console -> SDK Connections, then Sync Tools.")
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        handle.close()


if __name__ == "__main__":
    main()
