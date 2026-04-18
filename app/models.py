from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Any, Optional
from datetime import datetime


# ─── Call Initiation ──────────────────────────────────────────────────────────

class InitiateCallRequest(BaseModel):
    to: str = Field(..., description="E.164 phone number to dial, e.g. +12125551234")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arbitrary variables injected into the agent. "
            "Must include 'instructions' key (the system prompt template). "
            "All other keys are available as {variable} placeholders in the template."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "to": "+12125551234",
                "payload": {
                    "instructions": "You are calling {customer_name} to confirm their appointment on {date}.",
                    "customer_name": "John",
                    "date": "Tuesday at 3pm",
                    "order_id": "ORD-9912",
                },
            }
        }
    }


class InitiateCallResponse(BaseModel):
    call_id: str
    status: str
    livekit_room: str
    created_at: datetime


# ─── Call Forward ─────────────────────────────────────────────────────────────

class ForwardCallRequest(BaseModel):
    transfer_to: str = Field(..., description="E.164 number or SIP URI to transfer the call to")
    play_dialtone: bool = True


# ─── Webhook Payloads (agent → FastAPI) ───────────────────────────────────────

class TranscriptWebhook(BaseModel):
    call_id: str
    role: str       # "user" | "agent"
    text: str


class CallEventWebhook(BaseModel):
    call_id: str
    event_type: str
    data: dict[str, Any] = {}


class ToolCallWebhook(BaseModel):
    """
    Agent sends this when the LLM triggers an HTTP tool call.
    FastAPI executes the tool logic and returns the result.
    The agent injects the result back into the LLM conversation.
    """
    call_id: str
    tool_name: str
    args: dict[str, Any] = {}
    payload: dict[str, Any] = {}   # original call payload (all variables)


class ToolCallResponse(BaseModel):
    result: Any
    error: Optional[str] = None


class RecordingWebhook(BaseModel):
    call_id: str
    egress_id: str
    recording_url: str
    duration_secs: Optional[int] = None


# ─── Read Models ──────────────────────────────────────────────────────────────

class TranscriptLine(BaseModel):
    id: str
    role: str
    text: str
    created_at: datetime


class ToolCallRecord(BaseModel):
    id: str
    tool_name: str
    args: dict[str, Any]
    result: Optional[Any]
    status: str
    created_at: datetime


class CallEventRecord(BaseModel):
    id: str
    event_type: str
    data: dict[str, Any]
    created_at: datetime


class CallDetail(BaseModel):
    call_id: str
    to_number: str
    from_number: Optional[str]
    status: str
    payload: dict[str, Any]
    livekit_room: Optional[str]
    recording_url: Optional[str]
    duration_secs: Optional[int]
    created_at: datetime
    answered_at: Optional[datetime]
    ended_at: Optional[datetime]
    transcripts: list[TranscriptLine] = []
    tool_calls: list[ToolCallRecord] = []
    events: list[CallEventRecord] = []
