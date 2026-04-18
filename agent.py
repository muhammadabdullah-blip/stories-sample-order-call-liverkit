"""
LiveKit Outbound Caller Agent
─────────────────────────────
This worker is started separately from FastAPI:
    python agent.py dev

It receives dispatch jobs from FastAPI → LiveKit, dials the phone number,
runs the VoicePipeline, and reports everything back to FastAPI via webhooks.

Metadata shape (JSON string from dispatch):
    {
        "call_id":  "uuid",
        "to":       "+12125551234",
        "payload":  { "instructions": "...", "customer_name": "...", ... }
    }
"""

import asyncio
import json
import logging
import os
from time import perf_counter
from typing import Annotated

import httpx
from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, silero

load_dotenv(dotenv_path=".env.local")

logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

# Suppress harmless Deepgram "connection closed unexpectedly" on call end
logging.getLogger("livekit.plugins.deepgram").setLevel(logging.CRITICAL)


class _SuppressSTTCleanup(logging.Filter):
    """Filter out the 'failed to recognize speech' retry warning that fires
    every time a call ends. It's a cosmetic SDK cleanup issue, not a real error."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "failed to recognize speech" not in record.getMessage()


logging.getLogger("livekit.agents").addFilter(_SuppressSTTCleanup())

OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DIAL_TIMEOUT = int(os.getenv("DIAL_TIMEOUT_SECS", "60"))
MAX_CALL_DURATION = int(os.getenv("MAX_CALL_DURATION_SECS", "1800"))
ENABLE_RECORDING = os.getenv("ENABLE_RECORDING", "false").lower() == "true"

# ══════════════════════════════════════════════════════════════════════════════
# HTTP helpers — agent → FastAPI backend
# ══════════════════════════════════════════════════════════════════════════════


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        h["X-Webhook-Secret"] = WEBHOOK_SECRET
    return h


async def _post(endpoint: str, data: dict, *, wait_response: bool = False):
    """
    wait_response=False → fire-and-forget (transcript lines, events)
    wait_response=True  → blocking call (tool calls need the response)
    """
    url = f"{BACKEND_URL}{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=data, headers=_headers())
            if wait_response:
                return r.json()
    except Exception as e:
        logger.error(f"Webhook {endpoint} failed: {e}")
        if wait_response:
            return {"error": str(e)}


async def notify(endpoint: str, data: dict):
    """Fire-and-forget event notification."""
    asyncio.create_task(_post(endpoint, data, wait_response=False))


async def call_tool_webhook(data: dict) -> dict:
    """Blocking tool call — waits for FastAPI to return the tool result."""
    result = await _post("/webhook/tool", data, wait_response=True)
    return result or {}


# ══════════════════════════════════════════════════════════════════════════════
# Recording (LiveKit Egress → Supabase S3-compatible Storage)
# ══════════════════════════════════════════════════════════════════════════════

# Supabase S3 credentials (generate from Project Settings > Storage > S3 Access Keys)
SUPABASE_S3_ACCESS_KEY = os.getenv("SUPABASE_S3_ACCESS_KEY", "")
SUPABASE_S3_SECRET_KEY = os.getenv("SUPABASE_S3_SECRET_KEY", "")
SUPABASE_S3_REGION = os.getenv("SUPABASE_S3_REGION", "us-east-1")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "call-recordings")


async def start_recording(ctx: JobContext, call_id: str) -> str | None:
    """Start an audio-only room composite egress that uploads directly to Supabase Storage."""
    if not ENABLE_RECORDING:
        logger.info(
            f"[{call_id}] Recording disabled (set ENABLE_RECORDING=true to enable)"
        )
        return None

    if not SUPABASE_S3_ACCESS_KEY or not SUPABASE_S3_SECRET_KEY:
        logger.warning(
            f"[{call_id}] Recording enabled but SUPABASE_S3_ACCESS_KEY / SUPABASE_S3_SECRET_KEY not set"
        )
        return None

    # Build S3 endpoint from Supabase project URL
    # e.g. https://rifdfsrjhwqdeqgvbsnl.supabase.co → https://rifdfsrjhwqdeqgvbsnl.supabase.co/storage/v1/s3
    s3_endpoint = f"{SUPABASE_URL}/storage/v1/s3"

    try:
        req = api.RoomCompositeEgressRequest(
            room_name=ctx.room.name,
            audio_only=True,
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=api.EncodedFileType.OGG,
                    filepath=f"recordings/{call_id}.ogg",
                    s3=api.S3Upload(
                        access_key=SUPABASE_S3_ACCESS_KEY,
                        secret=SUPABASE_S3_SECRET_KEY,
                        region=SUPABASE_S3_REGION,
                        bucket=SUPABASE_STORAGE_BUCKET,
                        endpoint=s3_endpoint,
                        force_path_style=True,
                    ),
                )
            ],
        )
        resp = await ctx.api.egress.start_room_composite_egress(req)
        egress_id = resp.egress_id
        logger.info(f"[{call_id}] Egress started: {egress_id}")

        # Track egress completion in background
        asyncio.create_task(_monitor_egress(ctx, call_id, egress_id))
        return egress_id

    except Exception as e:
        logger.error(f"[{call_id}] Failed to start egress: {e}")
        return None


async def _monitor_egress(ctx: JobContext, call_id: str, egress_id: str):
    """Poll egress status until it completes, then notify backend with the Supabase URL."""
    for _ in range(120):  # poll for up to 10 minutes
        await asyncio.sleep(5)
        try:
            resp = await ctx.api.egress.list_egress(
                api.ListEgressRequest(egress_id=egress_id)
            )
            if not resp.items:
                break
            item = resp.items[0]
            if item.status in (
                api.EgressStatus.EGRESS_COMPLETE,
                api.EgressStatus.EGRESS_FAILED,
            ):
                if item.status == api.EgressStatus.EGRESS_COMPLETE and item.file_results:
                    # Construct the public Supabase Storage URL
                    filename = item.file_results[0].filename or f"recordings/{call_id}.ogg"
                    recording_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_STORAGE_BUCKET}/{filename}"
                    duration = int(item.file_results[0].duration / 1_000_000_000)

                    await notify(
                        "/webhook/recording",
                        {
                            "call_id": call_id,
                            "egress_id": egress_id,
                            "recording_url": recording_url,
                            "duration_secs": duration,
                        },
                    )
                    logger.info(f"[{call_id}] Recording uploaded to Supabase: {recording_url}")
                elif item.status == api.EgressStatus.EGRESS_FAILED:
                    logger.error(f"[{call_id}] Egress failed")
                break
        except Exception as e:
            logger.warning(f"Egress poll error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CallActions — LLM-callable tools
# ══════════════════════════════════════════════════════════════════════════════


class CallActions(llm.FunctionContext):
    """
    All tools defined here are stubs. The real logic lives in FastAPI
    /webhook/tool handlers (webhooks.py → TOOL_HANDLERS).

    The LLM learns what tools exist from the docstrings + type annotations.
    Each stub:
      1. Notifies the backend (logs the tool call)
      2. Gets the result from the backend
      3. Returns the result to the LLM's context

    To add a new tool:
      → Add an @llm.ai_callable() method here (stub only — no logic)
      → Add the handler in webhooks.py TOOL_HANDLERS
    """

    def __init__(
        self,
        *,
        lk_api: api.LiveKitAPI,
        participant: rtc.RemoteParticipant,
        room: rtc.Room,
        call_id: str,
        payload: dict,
    ):
        super().__init__()
        self.lk_api = lk_api
        self.participant = participant
        self.room = room
        self.call_id = call_id
        self.payload = payload

    async def _webhook(self, tool_name: str, args: dict) -> dict:
        return await call_tool_webhook(
            {
                "call_id": self.call_id,
                "tool_name": tool_name,
                "args": args,
                "payload": self.payload,
            }
        )

    async def _hangup(self):
        try:
            await self.lk_api.room.remove_participant(
                api.RoomParticipantIdentity(
                    room=self.room.name,
                    identity=self.participant.identity,
                )
            )
        except Exception as e:
            logger.info(f"Hangup error (may already be disconnected): {e}")

    # ── Tool: end the call ────────────────────────────────────────────────────
    @llm.ai_callable()
    async def end_call(self):
        """Called when the user wants to end the call or the conversation is complete."""
        logger.info(f"[{self.call_id}] end_call triggered by LLM")
        await self._webhook("end_call", {})
        await self._hangup()

    # ── Tool: check availability ──────────────────────────────────────────────
    @llm.ai_callable()
    async def look_up_availability(
        self,
        date: Annotated[str, "The date to check availability for, e.g. '2024-12-10'"],
    ):
        """Called when the user asks about appointment availability on a specific date."""
        return await self._webhook("look_up_availability", {"date": date})

    # ── Tool: confirm appointment ─────────────────────────────────────────────
    @llm.ai_callable()
    async def confirm_appointment(
        self,
        date: Annotated[str, "Date of the appointment, e.g. '2024-12-10'"],
        time: Annotated[str, "Time of the appointment, e.g. '2:00 PM'"],
    ):
        """Called when the user confirms they want to book an appointment at a specific date and time. Only call when certain."""
        return await self._webhook("confirm_appointment", {"date": date, "time": time})

    # ── Tool: answering machine detected ─────────────────────────────────────
    @llm.ai_callable()
    async def detected_answering_machine(self):
        """Called when the call reaches voicemail or an answering machine. Call this AFTER hearing the voicemail greeting."""
        logger.info(f"[{self.call_id}] voicemail detected")
        await self._webhook("detected_answering_machine", {})
        await self._hangup()

    # ── Tool: forward the call ────────────────────────────────────────────────
    @llm.ai_callable()
    async def forward_call(
        self,
        transfer_to: Annotated[
            str,
            "The E.164 phone number or SIP URI to transfer the call to, e.g. '+12125551234'",
        ],
        reason: Annotated[
            str, "Brief reason for the transfer, e.g. 'customer requested human agent'"
        ] = "",
    ):
        """
        Forwards (transfers) the call to a different phone number or agent.
        Use this when the user explicitly asks to speak to a human, or when
        escalation is needed based on the conversation context.
        """
        logger.info(f"[{self.call_id}] forward_call → {transfer_to}")
        return await self._webhook(
            "forward_call", {"transfer_to": transfer_to, "reason": reason}
        )

    # ── Tool: get order/customer status ──────────────────────────────────────
    @llm.ai_callable()
    async def get_order_status(
        self,
        order_id: Annotated[str, "The order ID to look up"] = "",
    ):
        """Called when the user asks about the status of their order or account."""
        oid = order_id or self.payload.get("order_id", "")
        return await self._webhook("get_order_status", {"order_id": oid})


# ══════════════════════════════════════════════════════════════════════════════
# Voice Pipeline
# ══════════════════════════════════════════════════════════════════════════════


def build_voice_pipeline(
    ctx: JobContext,
    participant: rtc.RemoteParticipant,
    instructions: str,
    call_id: str,
    payload: dict,
) -> VoicePipelineAgent:
    """Build and return the VoicePipelineAgent (does not start it)."""
    # Prepend a conciseness instruction to reduce LLM token generation time
    system_prompt = (
        "IMPORTANT: Keep your responses SHORT and conversational (1-2 sentences max). "
        "You are on a phone call — speak naturally and briefly.\n\n" + instructions
    )
    initial_ctx = llm.ChatContext().append(role="system", text=system_prompt)

    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-2-phonecall"),  # phone-audio-optimized model
        llm=openai.LLM(model="gpt-4o-mini", temperature=0.6),
        tts=deepgram.TTS(
            model="aura-asteria-en"
        ),  # ~200ms latency vs ~2s for OpenAI TTS
        chat_ctx=initial_ctx,
        allow_interruptions=True,  # user can cut in anytime
        min_endpointing_delay=0.2,  # respond almost immediately after user stops
        preemptive_synthesis=True,  # start TTS while LLM is still streaming
        fnc_ctx=CallActions(
            lk_api=ctx.api,
            participant=participant,
            room=ctx.room,
            call_id=call_id,
            payload=payload,
        ),
    )

    # ── Transcript hooks ──────────────────────────────────────────────────────
    # These fire after each committed speech turn and ship to the backend.

    @agent.on("user_speech_committed")
    def on_user_speech(msg: llm.ChatMessage):
        text = msg.content if isinstance(msg.content, str) else str(msg.content)
        asyncio.create_task(
            notify(
                "/webhook/transcript",
                {
                    "call_id": call_id,
                    "role": "user",
                    "text": text,
                },
            )
        )

    @agent.on("agent_speech_committed")
    def on_agent_speech(msg: llm.ChatMessage):
        text = msg.content if isinstance(msg.content, str) else str(msg.content)
        asyncio.create_task(
            notify(
                "/webhook/transcript",
                {
                    "call_id": call_id,
                    "role": "agent",
                    "text": text,
                },
            )
        )

    return agent


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint
# ══════════════════════════════════════════════════════════════════════════════


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # ── Parse metadata from dispatch ─────────────────────────────────────────
    try:
        meta = json.loads(ctx.job.metadata)
        call_id = meta["call_id"]
        to_num = meta["to"]
        payload = meta.get("payload", {})
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Invalid metadata: {ctx.job.metadata!r} — {e}")
        ctx.shutdown()
        return

    # Resolve instructions: format template with payload variables
    template = payload.get(
        "instructions",
        "You are a professional AI voice assistant making an outbound call. Be concise and polite.",
    )
    try:
        instructions = template.format(**payload)
    except KeyError:
        instructions = template

    logger.info(f"[{call_id}] Dialing {to_num}")
    await notify(
        "/webhook/call-event", {"call_id": call_id, "event_type": "dialing", "data": {}}
    )

    # ── Dial via Twilio SIP trunk ─────────────────────────────────────────────
    user_identity = "phone_user"
    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=OUTBOUND_TRUNK_ID,
                sip_call_to=to_num,
                participant_identity=user_identity,
            )
        )
    except Exception as e:
        logger.error(f"[{call_id}] SIP dial failed: {e}")
        await notify(
            "/webhook/call-event",
            {"call_id": call_id, "event_type": "error", "data": {"error": str(e)}},
        )
        ctx.shutdown()
        return

    # ── Wait for participant ──────────────────────────────────────────────────
    participant = await ctx.wait_for_participant(identity=user_identity)

    # ── Start recording ───────────────────────────────────────────────────────
    egress_id = await start_recording(ctx, call_id)
    if egress_id:
        await notify(
            "/webhook/call-event",
            {
                "call_id": call_id,
                "event_type": "recording_started",
                "data": {"egress_id": egress_id},
            },
        )

    # ── Build & start voice pipeline ─────────────────────────────────────────
    agent = build_voice_pipeline(ctx, participant, instructions, call_id, payload)
    agent.start(ctx.room, participant)

    # ── Monitor call status ───────────────────────────────────────────────────
    call_start = perf_counter()
    answered = False

    while True:
        elapsed = perf_counter() - call_start
        status = participant.attributes.get("sip.callStatus")

        # ── Dial timeout (only before the call is answered) ───────────────
        if not answered and elapsed > DIAL_TIMEOUT:
            logger.info(f"[{call_id}] Dial timeout after {DIAL_TIMEOUT}s")
            await notify(
                "/webhook/call-event",
                {
                    "call_id": call_id,
                    "event_type": "no_answer",
                    "data": {"reason": "timeout"},
                },
            )
            break

        # ── Max call duration (hard limit after answered) ─────────────────
        if answered and elapsed > MAX_CALL_DURATION:
            logger.info(f"[{call_id}] Max call duration reached ({MAX_CALL_DURATION}s)")
            await notify(
                "/webhook/call-event",
                {
                    "call_id": call_id,
                    "event_type": "ended",
                    "data": {"duration_secs": int(elapsed), "reason": "max_duration"},
                },
            )
            break

        if status == "active" and not answered:
            answered = True
            logger.info(f"[{call_id}] Call answered")
            await notify(
                "/webhook/call-event",
                {"call_id": call_id, "event_type": "answered", "data": {}},
            )

        elif participant.disconnect_reason == rtc.DisconnectReason.USER_REJECTED:
            logger.info(f"[{call_id}] Call rejected")
            await notify(
                "/webhook/call-event",
                {"call_id": call_id, "event_type": "rejected", "data": {}},
            )
            break

        elif participant.disconnect_reason == rtc.DisconnectReason.USER_UNAVAILABLE:
            logger.info(f"[{call_id}] No answer")
            await notify(
                "/webhook/call-event",
                {"call_id": call_id, "event_type": "no_answer", "data": {}},
            )
            break

        elif status == "automation":
            # DTMF dialing in progress — normal, keep waiting
            pass

        # Participant disconnected during an active call → call ended
        if answered and participant.disconnect_reason is not None:
            duration = int(perf_counter() - call_start)
            logger.info(f"[{call_id}] Call ended after {duration}s")
            await notify(
                "/webhook/call-event",
                {
                    "call_id": call_id,
                    "event_type": "ended",
                    "data": {"duration_secs": duration},
                },
            )
            break

        await asyncio.sleep(0.1)

    ctx.shutdown()


# ══════════════════════════════════════════════════════════════════════════════
# Worker bootstrap
# ══════════════════════════════════════════════════════════════════════════════


def prewarm(proc: JobProcess):
    """Pre-load the Silero VAD model before any calls come in."""
    proc.userdata["vad"] = silero.VAD.load()


if __name__ == "__main__":
    if not OUTBOUND_TRUNK_ID or not OUTBOUND_TRUNK_ID.startswith("ST_"):
        raise ValueError(
            "SIP_OUTBOUND_TRUNK_ID is not set or invalid. "
            "It must start with 'ST_'. Check your .env.local."
        )
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
            prewarm_fnc=prewarm,
        )
    )
