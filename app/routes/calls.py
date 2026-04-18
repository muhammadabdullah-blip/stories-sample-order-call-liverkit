from __future__ import annotations
import uuid
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from app.models import (
    InitiateCallRequest,
    InitiateCallResponse,
    ForwardCallRequest,
    CallDetail,
    TranscriptLine,
    ToolCallRecord,
    CallEventRecord,
)
from app.services.livekit import dispatch_outbound_call, forward_call
from app.config import get_settings
import app.database as db

router = APIRouter(prefix="/calls", tags=["Calls"])
logger = logging.getLogger("calls-router")


def _resolve_instructions(payload: dict) -> str:
    """
    Pull 'instructions' from payload and format it with the rest of the keys.
    Falls back to a generic prompt if not provided.
    """
    template = payload.get(
        "instructions",
        "You are a helpful AI voice assistant making an outbound call. Be polite and professional.",
    )
    try:
        # Substitute {variable} placeholders with actual payload values
        return template.format(**payload)
    except KeyError as e:
        logger.warning(f"Missing template variable {e} — using template as-is")
        return template


# ─── POST /calls — Initiate a call ───────────────────────────────────────────

@router.post("", response_model=InitiateCallResponse, status_code=201)
async def initiate_call(body: InitiateCallRequest):
    """
    Initiate an outbound call.

    **Body:**
    ```json
    {
      "to": "+12125551234",
      "payload": {
        "instructions": "You are calling {customer_name} about order {order_id}.",
        "customer_name": "John",
        "order_id": "ORD-991"
      }
    }
    ```
    All keys in `payload` are available as `{variable}` placeholders in `instructions`.
    They are also forwarded to every webhook tool call so your tools can access them.
    """
    s = get_settings()
    call_id = str(uuid.uuid4())
    instructions = _resolve_instructions(body.payload)
    room_name = f"call-{call_id}"

    # Persist to Supabase FIRST — the agent fires webhooks immediately after
    # dispatch, and those webhooks reference call_id via foreign key.
    await db.create_call(
        call_id=call_id,
        to_number=body.to,
        from_number=s.sip_from_number,
        payload=body.payload,
        instructions=instructions,
        livekit_room=room_name,
    )

    await db.insert_event(call_id, "initiated", {"to": body.to, "room": room_name})

    # Dispatch the LiveKit agent AFTER the DB record exists
    await dispatch_outbound_call(
        call_id=call_id,
        to_number=body.to,
        payload=body.payload,
    )

    return InitiateCallResponse(
        call_id=call_id,
        status="initiated",
        livekit_room=room_name,
        created_at=datetime.now(timezone.utc),
    )


# ─── GET /calls — List calls ──────────────────────────────────────────────────

@router.get("", response_model=list[dict])
async def list_calls(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List all calls, newest first."""
    return await db.list_calls(limit=limit, offset=offset)


# ─── GET /calls/{call_id} — Full call detail ─────────────────────────────────

@router.get("/{call_id}", response_model=CallDetail)
async def get_call(call_id: str):
    """
    Get full call detail including transcript, tool calls, and events.
    """
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    transcripts = await db.get_transcripts(call_id)
    tool_calls  = await db.get_tool_calls(call_id)
    events      = await db.get_events(call_id)

    return CallDetail(
        **call,
        transcripts=[TranscriptLine(**t) for t in transcripts],
        tool_calls=[ToolCallRecord(**tc) for tc in tool_calls],
        events=[CallEventRecord(**e) for e in events],
    )


# ─── GET /calls/{call_id}/transcript ─────────────────────────────────────────

@router.get("/{call_id}/transcript", response_model=list[TranscriptLine])
async def get_transcript(call_id: str):
    """Return the full ordered transcript for a call."""
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    rows = await db.get_transcripts(call_id)
    return [TranscriptLine(**r) for r in rows]


# ─── POST /calls/{call_id}/forward ───────────────────────────────────────────

@router.post("/{call_id}/forward", status_code=200)
async def forward_active_call(call_id: str, body: ForwardCallRequest):
    """
    Forward (SIP transfer) an active call to another number or SIP URI.
    Can also be called from inside the agent via the forward_call tool.
    """
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    if call["status"] not in ("answered", "active"):
        raise HTTPException(
            status_code=400,
            detail=f"Call is not active (status: {call['status']})",
        )

    await forward_call(
        room_name=call["livekit_room"],
        participant_identity="phone_user",
        transfer_to=body.transfer_to,
        play_dialtone=body.play_dialtone,
    )

    await db.update_call(call_id, {"status": "forwarding"})
    await db.insert_event(call_id, "forwarding", {"transfer_to": body.transfer_to})

    return {"message": "Call forward initiated", "transfer_to": body.transfer_to}
