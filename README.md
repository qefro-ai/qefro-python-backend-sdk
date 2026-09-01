# Qefro Python Backend SDK

`qefro-backend` connects **external business systems** to Qefro
(ERP / POS / CRM) over the signed `/qefro` webhook.

It is **not** how you build a Marketplace App. Marketplace Apps are
metadata: `qefro app init` → validate → package → install.

Wire-compatible with the [JavaScript](https://github.com/qefro-ai/qefro-js-backend-sdk)
and [Rust](https://github.com/qefro-ai/qefro-rust-backend-sdk) SDKs: same signed
protocol, same `capabilities.list` schema, same 8 flow step types.

## Install

```bash
pip install qefro-backend
```

Zero runtime dependencies — it uses only the Python standard library.

## Quick start

```python
from qefro_backend import Qefro

app = Qefro(signing_secret="dev-secret")  # or QEFRO_SIGNING_SECRET from env

@app.tool("get_orders", auth="required", lookup={"required": ["email"]})
async def get_orders(ctx):
    customer = ctx.customer.require()
    return [{"orderId": "ord_1", "customerId": customer["id"]}]

app.run(8088)  # POST http://0.0.0.0:8088/qefro
```

Set the same signing secret in Admin Console → **Business Tools → SDK
Connections**, then **Sync Tools**. Handlers may be sync or `async`.

## Customer authorization

Register a provider to resolve and verify customers. `auth="required"` tools
authorize automatically before the handler runs; a `challenge` outcome pauses
the tool and asks the customer for a code, then resumes.

```python
from qefro_backend import Qefro, CustomerProvider

app = Qefro("dev-secret")

class Customers(CustomerProvider):
    async def lookup(self, ctx):
        return {"id": ctx.identity.get("phone", "demo")}

    async def authorize(self, ctx):
        if not ctx.response:
            return ctx.auth.sms_otp(ctx.customer["id"], "Enter the code we texted you.")
        if ctx.response.strip() != "123456":
            return ctx.auth.sms_otp(ctx.customer["id"], "Wrong code, try again.")
        return ctx.auth.success(ctx.customer, {"type": "bearer_token", "access_token": "demo"})

app.customer(Customers())
```

## Customer Hub (optional)

When `QEFRO_CUSTOMER_HUB_ENABLED=true`, tools can call Hub via
`platform.customer` on `tool.invoke` (or `QEFRO_CUSTOMER_HUB_URL` + service
token). Hub is **optional** — defaults keep existing apps working
(`ENABLED=false`, `OPTIONAL=true`). Soft-skip returns `None` / no-ops when
Hub is off or unreachable; set `QEFRO_CUSTOMER_HUB_OPTIONAL=false` to hard-fail.

```python
@app.tool("create_reservation", auth="none")
async def create_reservation(ctx):
    customer = await ctx.customer.resolve({"whatsapp_number": ctx.identity.get("phone")})
    # Hub properties: ctx.customer.id, .phone_number, .whatsapp_number, .display_name
    await ctx.timeline.append({
        "event_type": "reservation.created",
        "payload": {"code": "R-1001"},
    })
    await ctx.membership.attach({"solution_id": "restaurant-pro"})
    await ctx.consent.grant({"purpose": "marketing"})
    return {"customer_id": customer["id"] if customer else None}
```

`ctx.storage` talks to managed storage (ADR-002) the same way as the JS SDK —
never call Mongo or storage-service URLs yourself.

## Customer Hub Person and Business Events

`ctx.person` queues Customer Hub mutations (`note`, `tag`, `assign`, `activity`,
`merge`) for the Qefro runtime. Declare events with `app.business_event(...)`
and publish them with `ctx.emit(...)` — delivery is outbox → ingest, not CRM
automation.

```python
from qefro_backend import Qefro, on_person_created

app = Qefro("dev-secret")
app.business_event({"event_type": "quotation.created", "version": 1})

@app.tool("create_quotation", auth="none")
async def create_quotation(ctx):
    await ctx.person.tag("vip")
    ctx.emit({"event_type": "quotation.created", "event_id": "Q-1001", "data": {"amount": 125000}})
    return {"id": "Q-1001"}

app.flow({"id": "welcome", "trigger": on_person_created()}).message("hi", "Hi").complete("done")
```

## Business Flows

Flows describe how your Business Tools are orchestrated. They are **metadata
only** — the SDK advertises them through `capabilities.list` and the Qefro
Runtime discovers, validates, versions, and executes them. Nothing runs inside
the SDK.

```python
(
    app.flow({
        "id": "order_lookup",              # immutable identity — renaming `name` never creates a new flow
        "name": "Order Lookup",
        "description": "Look up customer orders",
        "category": "crm",
        "tags": ["customer", "orders"],
        "intent": ["track order", "where is my order"],
        "inputs": ["email"],
        "outputs": ["get_orders"],
    })
    .ask("email", field="email", prompt="Please enter your email.")
    .tool("orders", tool_ref="get_orders")
    .complete("done", message="Here are your recent orders.")
)
```

Every step needs a unique `id`; `tool` steps reference an existing Business Tool
by `tool_ref`. Step builders: `.ask() .tool() .challenge() .upload() .condition()
.delay() .approval() .complete() .message() .tag() .assign() .activity()`. A duplicate/empty flow or step id raises
`FlowError`. See [`examples/order-approval`](examples/order-approval) for a full
`ask → tool → condition → approval → OTP-authenticated tool → complete` flow.

## Protocol

| Message | Purpose |
| --- | --- |
| `ping` | Health / Test Connection |
| `capabilities.list` | Discover tools **and** business flows for Sync Tools |
| `tools.list` | Legacy tool-only discovery (still supported) |
| `tool.invoke` | Run a handler |
| `tool.resume` | Continue after a customer challenge reply |

Requests are HMAC-SHA256 signed (`X-Qefro-Signature` / `X-Qefro-Timestamp`,
payload `v1:<timestamp>:<body>`). Responses include `X-Qefro-Protocol`,
`X-Qefro-SDK`, and `X-Qefro-Version`.

## Examples

```bash
export QEFRO_SIGNING_SECRET=dev-secret
python examples/basic/server.py            # ask -> tool -> complete
python examples/order-approval/server.py   # condition + approval + OTP-authenticated tool
```

## Docs

- [Register SDK Business Tools](https://docs.qefro.com/docs/guides/register-sdk-business-tools)
- [Define Business Flows](https://docs.qefro.com/docs/guides/define-business-flows)
- [Run Business Flows](https://docs.qefro.com/docs/guides/run-business-flows)

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
