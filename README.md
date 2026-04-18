# LiveKit Outbound Call API

REST API for outbound AI voice calls — built on LiveKit + Twilio SIP.
Every call is fully logged in Supabase: transcript, tool calls, events, recording URL.

---

## Architecture

```
Client → POST /calls            FastAPI creates call record, dispatches LiveKit job
              ↓
         LiveKit Worker         Dials via Twilio SIP, runs VoicePipeline (VAD→STT→LLM→TTS)
              ↓ (webhooks)
         POST /webhook/transcript     → per-utterance transcript lines
         POST /webhook/call-event     → lifecycle events (dialing, answered, ended…)
         POST /webhook/tool           → LLM tool calls (blocking — returns result to agent)
         POST /webhook/recording      → recording URL after egress completes
              ↓
         Supabase                calls | transcripts | tool_calls | call_events
```

---

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 agent.py download-files  # downloads VAD model
```

### 2. Set up environment

```bash
cp .env.example .env.local
# Fill in all values in .env.local
```

### 3. Set up Supabase

Run `schema.sql` in your Supabase SQL editor.

### 4. Set up Twilio SIP Trunk

Follow the Twilio + LiveKit SIP setup from the original README, then:

```bash
lk sip outbound create outbound-trunk.json
# Copy the ST_xxx ID into .env.local as SIP_OUTBOUND_TRUNK_ID
```

### 5. Start both processes (two terminals)

```bash
# Terminal 1 — FastAPI backend
uvicorn app.main:app --reload --port 8000

# Terminal 2 — LiveKit agent worker
python3 agent.py dev
```

---

## API Reference

### Initiate a call

```
POST /calls
```

```json
{
  "to": "+12125551234",
  "payload": {
    "instructions": "You are calling {customer_name} about their order {order_id}. Confirm delivery on {date}.",
    "customer_name": "John Smith",
    "order_id": "ORD-9912",
    "date": "Tuesday"
  }
}
```

Response:
```json
{
  "call_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "initiated",
  "livekit_room": "call-550e8400-...",
  "created_at": "2024-12-10T14:30:00Z"
}
```

### Get call detail (includes transcript + events + tool calls)

```
GET /calls/{call_id}
```

### Get transcript only

```
GET /calls/{call_id}/transcript
```

### List all calls

```
GET /calls?limit=50&offset=0
```

### Forward an active call

```
POST /calls/{call_id}/forward
```
```json
{
  "transfer_to": "+12125559999",
  "play_dialtone": true
}
```

---

## Adding Custom Tools

### Step 1 — Add stub in `agent.py`

```python
@llm.ai_callable()
async def check_credit_score(
    self,
    customer_id: Annotated[str, "The customer ID to check"],
):
    """Called when the user asks about their credit score or account standing."""
    return await self._webhook("check_credit_score", {"customer_id": customer_id})
```

### Step 2 — Add handler in `app/routes/webhooks.py`

```python
async def _tool_check_credit_score(args: dict, payload: dict, call_id: str):
    customer_id = args.get("customer_id") or payload.get("customer_id")
    # Call your CRM / API
    score = my_crm.get_credit_score(customer_id)
    return {"score": score, "tier": "gold" if score > 700 else "standard"}

TOOL_HANDLERS["check_credit_score"] = _tool_check_credit_score
```

The LLM will automatically discover the tool from its docstring + type annotations.
The result is injected back into the conversation — the agent can reference it naturally.

---

## Variable Injection

All keys in `payload` are available as `{variable}` placeholders in `instructions`:

```json
{
  "payload": {
    "instructions": "Hi {name}, calling about {product}. Your discount code is {code}.",
    "name": "Sarah",
    "product": "Premium Plan",
    "code": "SAVE20"
  }
}
```

The `payload` is also forwarded to every tool call webhook, so tools can access
`payload["customer_id"]`, `payload["order_id"]`, etc. without the LLM needing
to pass them as args.

---

## Supabase Tables

| Table | Contents |
|---|---|
| `calls` | One row per call. Status, payload, recording URL, duration. |
| `transcripts` | Every utterance (user + agent), ordered by time. |
| `tool_calls` | Every LLM tool invocation: name, args, result, status. |
| `call_events` | Granular lifecycle events: dialing, answered, forwarding, ended… |

---

## Production Notes

- Run FastAPI behind **nginx** or deploy on **Railway / Fly.io / Render**
- Set `BACKEND_URL` to your public URL so the agent can reach it
- Set `WEBHOOK_SECRET` to protect internal endpoints from external access
- Use **pm2** or **systemd** to keep both processes alive
- For high volume, run multiple agent workers (LiveKit handles routing)
- Store recordings in **Cloudflare R2** (set `S3_ENDPOINT`) for cheaper egress
