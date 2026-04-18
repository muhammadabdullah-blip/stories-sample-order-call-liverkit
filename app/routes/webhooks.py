from __future__ import annotations
"""
Webhook endpoints called by the LiveKit agent during a call.

These are INTERNAL endpoints (agent → backend).
In production, protect them with a shared secret header.

Flow per tool call:
  1. LLM decides to call a tool (e.g. "look_up_crm")
  2. agent.py POSTs to POST /webhook/tool with call_id, tool_name, args, payload
  3. This endpoint executes the actual tool logic (CRM lookup, Calendly, etc.)
  4. Returns JSON { result: ... } to the agent
  5. Agent injects result into LLM conversation context
"""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Header
from app.models import (
    TranscriptWebhook,
    CallEventWebhook,
    ToolCallWebhook,
    ToolCallResponse,
    RecordingWebhook,
)
from app.config import get_settings
import app.database as db

router = APIRouter(prefix="/webhook", tags=["Webhooks (internal)"])
logger = logging.getLogger("webhooks")

# ─── Optional: shared-secret auth header ──────────────────────────────────────
# Set WEBHOOK_SECRET in .env.local and send it as X-Webhook-Secret header from agent

def _check_secret(x_webhook_secret: str | None):
    s = get_settings()
    secret = getattr(s, "webhook_secret", "")
    if secret and x_webhook_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")


# ─── POST /webhook/transcript — agent pushes each utterance ──────────────────

@router.post("/transcript", status_code=204)
async def receive_transcript(
    body: TranscriptWebhook,
    x_webhook_secret: str | None = Header(default=None),
):
    """
    Called by the agent after every committed speech event.
    role = "user"  → what the caller said
    role = "agent" → what the AI said
    """
    _check_secret(x_webhook_secret)
    await db.insert_transcript(body.call_id, body.role, body.text)
    logger.info(f"[{body.call_id}] {body.role}: {body.text[:60]}...")


# ─── POST /webhook/call-event — agent pushes lifecycle events ────────────────

@router.post("/call-event", status_code=204)
async def receive_call_event(
    body: CallEventWebhook,
    x_webhook_secret: str | None = Header(default=None),
):
    """
    Agent pushes call lifecycle events: dialing, answered, ended, etc.
    Status machine:  initiated → dialing → answered → completed
                                         ↘ rejected / no_answer / voicemail
    """
    _check_secret(x_webhook_secret)
    await db.insert_event(body.call_id, body.event_type, body.data)

    # Keep calls.status in sync with events
    status_map = {
        "dialing":    "dialing",
        "answered":   "answered",
        "ended":      "completed",
        "rejected":   "rejected",
        "no_answer":  "no_answer",
        "voicemail":  "voicemail",
        "forwarded":  "forwarded",
        "error":      "failed",
    }
    if body.event_type in status_map:
        updates = {"status": status_map[body.event_type]}
        if body.event_type == "answered":
            updates["answered_at"] = datetime.now(timezone.utc).isoformat()
        elif body.event_type in ("ended", "rejected", "no_answer", "voicemail", "error"):
            updates["ended_at"] = datetime.now(timezone.utc).isoformat()
            if "duration_secs" in body.data:
                updates["duration_secs"] = body.data["duration_secs"]
        await db.update_call(body.call_id, updates)

    logger.info(f"[{body.call_id}] event: {body.event_type} {body.data}")


# ─── POST /webhook/tool — agent executes HTTP tool calls ─────────────────────

@router.post("/tool", response_model=ToolCallResponse)
async def execute_tool(
    body: ToolCallWebhook,
    x_webhook_secret: str | None = Header(default=None),
):
    """
    The LLM inside the agent decided to call a tool.
    This endpoint IS the tool — it has access to your full backend,
    databases, external APIs, etc.

    body.args     = arguments the LLM extracted from the conversation
    body.payload  = original call variables (customer_name, order_id, etc.)

    Return { result: <any JSON-serializable value> } back to the agent.
    The agent injects this result into the LLM's context.

    ─── HOW TO ADD NEW TOOLS ────────────────────────────────────────────────
    1. Add an @llm.ai_callable() stub in agent.py with the tool name + params
    2. Add a handler in TOOL_HANDLERS dict below
    3. The LLM will automatically discover the tool from the stub's docstring
    ─────────────────────────────────────────────────────────────────────────
    """
    _check_secret(x_webhook_secret)

    # Record the tool call
    record = await db.insert_tool_call(body.call_id, body.tool_name, body.args)

    try:
        handler = TOOL_HANDLERS.get(body.tool_name)
        if not handler:
            raise ValueError(f"Unknown tool: {body.tool_name}")

        result = await handler(body.args, body.payload, body.call_id)
        await db.resolve_tool_call(record["id"], result)

        logger.info(f"[{body.call_id}] tool {body.tool_name} → {str(result)[:80]}")
        return ToolCallResponse(result=result)

    except Exception as e:
        await db.resolve_tool_call(record["id"], None, error=str(e))
        logger.error(f"[{body.call_id}] tool {body.tool_name} error: {e}")
        return ToolCallResponse(result=None, error=str(e))


# ─── POST /webhook/recording — agent pushes recording URL after egress ───────

@router.post("/recording", status_code=204)
async def receive_recording(
    body: RecordingWebhook,
    x_webhook_secret: str | None = Header(default=None),
):
    """Agent pushes the recording URL once LiveKit Egress uploads to Supabase Storage."""
    _check_secret(x_webhook_secret)

    # LiveKit uploaded directly to Supabase S3 — just save the URL to the DB
    await db.update_call(body.call_id, {
        "recording_url": body.recording_url,
        "egress_id": body.egress_id,
        "duration_secs": body.duration_secs,
    })
    await db.insert_event(body.call_id, "recording_ready", {
        "recording_url": body.recording_url,
        "egress_id": body.egress_id,
    })
    logger.info(f"[{body.call_id}] recording ready: {body.recording_url}")


# ══════════════════════════════════════════════════════════════════════════════
# TOOL HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
# Each handler is an async function:
#   async def my_tool(args: dict, payload: dict, call_id: str) -> Any
#
# args    = LLM-extracted params (declared in agent.py stub)
# payload = original call payload (all your custom variables)
# call_id = for DB lookups if needed

async def _tool_end_call(args: dict, payload: dict, call_id: str):
    """Triggered when LLM decides the call should end. No-op here — agent handles hangup."""
    return {"acknowledged": True}


async def _tool_look_up_availability(args: dict, payload: dict, call_id: str):
    """
    Real implementation: query your calendar / Calendly / Google Calendar.
    args = { "date": "2024-12-10" }
    """
    date = args.get("date", "")
    # TODO: Replace with real calendar API call
    # e.g. calendly_api.get_available_slots(date=date, event_type=payload.get("event_type"))
    return {
        "available_times": ["10:00 AM", "2:00 PM", "4:00 PM"],
        "date": date,
    }


async def _tool_confirm_appointment(args: dict, payload: dict, call_id: str):
    """
    args = { "date": "...", "time": "..." }
    payload may contain customer_id, order_id etc.
    """
    # TODO: Write to your CRM / booking system
    return {
        "confirmed": True,
        "date": args.get("date"),
        "time": args.get("time"),
        "confirmation_number": f"CNF-{call_id[:8].upper()}",
    }


async def _tool_detected_answering_machine(args: dict, payload: dict, call_id: str):
    """Agent detected voicemail. Update status — agent will hang up."""
    await db.update_call(call_id, {"status": "voicemail"})
    await db.insert_event(call_id, "voicemail", {})
    return {"acknowledged": True}


async def _tool_forward_call(args: dict, payload: dict, call_id: str):
    """
    LLM triggered a call forward during conversation.
    args = { "transfer_to": "+12125551234", "reason": "escalation" }
    """
    from app.services.livekit import forward_call
    call = await db.get_call(call_id)
    if not call:
        return {"error": "call not found"}

    transfer_to = args.get("transfer_to") or payload.get("escalation_number", "")
    if not transfer_to:
        return {"error": "no transfer_to number provided"}

    await forward_call(
        room_name=call["livekit_room"],
        participant_identity="phone_user",
        transfer_to=transfer_to,
    )
    await db.insert_event(call_id, "forwarding", {
        "transfer_to": transfer_to,
        "reason": args.get("reason", ""),
    })
    return {"forwarding": True, "transfer_to": transfer_to}


async def _tool_get_order_status(args: dict, payload: dict, call_id: str):
    """
    Example business tool: look up an order by ID.
    args = { "order_id": "ORD-991" }
    payload might have order_id pre-loaded from the call initiation.
    """
    order_id = args.get("order_id") or payload.get("order_id")
    # TODO: query your database / Shopify / CRM
    return {
        "order_id": order_id,
        "status": "shipped",
        "tracking": "1Z999AA10123456784",
        "estimated_delivery": "Tomorrow by 8pm",
    }


# ─── Registry: tool_name → handler ───────────────────────────────────────────
TOOL_HANDLERS: dict[str, callable] = {
    "end_call":                   _tool_end_call,
    "look_up_availability":       _tool_look_up_availability,
    "confirm_appointment":        _tool_confirm_appointment,
    "detected_answering_machine": _tool_detected_answering_machine,
    "forward_call":               _tool_forward_call,
    "get_order_status":           _tool_get_order_status,
    # Add more tools here...
}
