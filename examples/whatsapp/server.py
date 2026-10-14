"""WhatsApp Capability example for Qefro Python SDK.

Demonstrates registering WhatsApp message templates and interactive Flows.
Credentials are not configured in code; they are supplied dynamically by Qefro
from workspace channel settings (ctx.settings["whatsapp"]).
"""

import os
from qefro_connector import (
    CustomerProvider,
    Qefro,
    WhatsAppFlowDefinition,
    WhatsAppFlowScreen,
    WhatsAppTemplateComponent,
    WhatsAppTemplateDefinition,
)


class SimpleCustomer(CustomerProvider):
    async def lookup(self, ctx):
        ident = ctx.identity or {}
        return {"id": ident.get("phone") or ident.get("customer_id") or "wa-user-1"}

    async def authorize(self, ctx):
        return ctx.auth.success(
            ctx.customer,
            {"type": "bearer_token", "access_token": f"wa-{ctx.customer.get('id')}", "expires_in": 900},
        )


def main():
    signing_secret = os.environ.get("QEFRO_SIGNING_SECRET", "dev-secret")
    port = int(os.environ.get("PORT", "8089"))

    app = Qefro(signing_secret, endpoint_path="/qefro")
    app.customer(SimpleCustomer())

    # ------------------------------------------------------------------------
    # WhatsApp Capabilities: Message Templates and Interactive Flows
    # (Credentials are dynamically resolved from Qefro workspace channel settings)
    # ------------------------------------------------------------------------
    app.whatsapp(
        {
            "version": 1,
            "templates": [
                {
                    "name": "order_update",
                    "language": "en_US",
                    "category": "UTILITY",
                    "provider_id": "order_update_v1",
                    "description": "Notify customer of an update to their active order",
                    "components": [
                        {
                            "type": "BODY",
                            "text": "Hello {{1}}, your order {{2}} has been updated to {{3}}.",
                            "variables": ["customer_name", "order_id", "status"],
                        }
                    ],
                },
                {
                    "name": "appointment_confirmation",
                    "language": "en_US",
                    "category": "UTILITY",
                    "description": "Confirm an appointment booking date and time",
                    "components": [
                        {
                            "type": "BODY",
                            "text": "Your appointment is confirmed for {{1}} at {{2}}.",
                            "variables": ["appointment_date", "appointment_time"],
                        }
                    ],
                },
            ],
            "flows": [
                {
                    "id": "book_viewing",
                    "name": "Book Property Viewing Flow",
                    "description": "Interactive WhatsApp Flow to select date and time for property viewing",
                    "status": "PUBLISHED",
                    "mode": "published",
                    "cta": "Schedule Viewing",
                    "screens": [
                        {"id": "SELECT_PROPERTY", "title": "Select Property", "terminal": False},
                        {"id": "SELECT_SLOT", "title": "Pick Date & Time", "terminal": False},
                        {"id": "CONFIRM_VIEWING", "title": "Confirm Details", "terminal": True},
                    ],
                }
            ],
        }
    )

    @app.tool("wa_template_preview", auth="none")
    async def preview_template(ctx):
        tpl_name = ctx.parameters.get("template")
        phone = ctx.identity.get("phone") if ctx.identity else None
        return {
            "template": tpl_name,
            "preview": f"Template {tpl_name} for {phone}",
            "approved": True,
        }

    print(f"Starting Qefro Python WhatsApp SDK example on port {port}...")
    app.run(port)


if __name__ == "__main__":
    main()
