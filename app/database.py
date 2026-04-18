from __future__ import annotations
from supabase import create_client, Client
from app.config import get_settings
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Optional
import logging

logger = logging.getLogger("db")


@lru_cache
def get_db() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_key)


# ─── Calls ────────────────────────────────────────────────────────────────────

async def create_call(
    call_id: str,
    to_number: str,
    from_number: str,
    payload: dict,
    instructions: str,
    livekit_room: str,
) -> dict:
    db = get_db()
    data = {
        "call_id": call_id,
        "to_number": to_number,
        "from_number": from_number,
        "status": "initiated",
        "payload": payload,
        "instructions": instructions,
        "livekit_room": livekit_room,
        "created_at": _now(),
    }
    res = db.table("calls").insert(data).execute()
    return res.data[0]


async def update_call(call_id: str, updates: dict) -> None:
    db = get_db()
    db.table("calls").update(updates).eq("call_id", call_id).execute()


async def get_call(call_id: str) -> Optional[dict]:
    db = get_db()
    res = db.table("calls").select("*").eq("call_id", call_id).single().execute()
    return res.data


async def list_calls(limit: int = 50, offset: int = 0) -> list[dict]:
    db = get_db()
    res = (
        db.table("calls")
        .select("*")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return res.data


# ─── Transcripts ──────────────────────────────────────────────────────────────

async def insert_transcript(call_id: str, role: str, text: str) -> dict:
    db = get_db()
    res = db.table("transcripts").insert({
        "call_id": call_id,
        "role": role,
        "text": text,
        "created_at": _now(),
    }).execute()
    return res.data[0]


async def get_transcripts(call_id: str) -> list[dict]:
    db = get_db()
    res = (
        db.table("transcripts")
        .select("*")
        .eq("call_id", call_id)
        .order("created_at")
        .execute()
    )
    return res.data


# ─── Tool Calls ───────────────────────────────────────────────────────────────

async def insert_tool_call(call_id: str, tool_name: str, args: dict) -> dict:
    db = get_db()
    res = db.table("tool_calls").insert({
        "call_id": call_id,
        "tool_name": tool_name,
        "args": args,
        "status": "pending",
        "created_at": _now(),
    }).execute()
    return res.data[0]


async def resolve_tool_call(record_id: str, result: Any, error: Optional[str] = None) -> None:
    db = get_db()
    db.table("tool_calls").update({
        "result": result,
        "status": "error" if error else "success",
        "resolved_at": _now(),
    }).eq("id", record_id).execute()


async def get_tool_calls(call_id: str) -> list[dict]:
    db = get_db()
    res = (
        db.table("tool_calls")
        .select("*")
        .eq("call_id", call_id)
        .order("created_at")
        .execute()
    )
    return res.data


# ─── Events ───────────────────────────────────────────────────────────────────

async def insert_event(call_id: str, event_type: str, data: dict = {}) -> dict:
    db = get_db()
    res = db.table("call_events").insert({
        "call_id": call_id,
        "event_type": event_type,
        "data": data,
        "created_at": _now(),
    }).execute()
    return res.data[0]


async def get_events(call_id: str) -> list[dict]:
    db = get_db()
    res = (
        db.table("call_events")
        .select("*")
        .eq("call_id", call_id)
        .order("created_at")
        .execute()
    )
    return res.data


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
