from __future__ import annotations
import json
import logging
from livekit import api as lkapi
from app.config import get_settings

logger = logging.getLogger("livekit-service")


def _client() -> lkapi.LiveKitAPI:
    s = get_settings()
    return lkapi.LiveKitAPI(
        url=s.livekit_url,
        api_key=s.livekit_api_key,
        api_secret=s.livekit_api_secret,
    )


async def dispatch_outbound_call(
    call_id: str,
    to_number: str,
    payload: dict,
) -> str:
    """
    Creates a LiveKit agent dispatch.
    Returns the LiveKit room name that was auto-created.

    Metadata is a JSON string consumed by agent.py:
        { "call_id": "...", "to": "+1...", "payload": { ...variables } }
    """
    s = get_settings()
    metadata = json.dumps({
        "call_id": call_id,
        "to": to_number,
        "payload": payload,
    })

    async with _client() as lk:
        room_name = f"call-{call_id}"
        # Create the room first
        await lk.room.create_room(
            lkapi.CreateRoomRequest(name=room_name, empty_timeout=300)
        )
        # Dispatch the agent into that room
        await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                room=room_name,
                agent_name="outbound-caller",
                metadata=metadata,
            )
        )

    logger.info(f"Dispatched call {call_id} to room {room_name}")
    return room_name


async def forward_call(
    room_name: str,
    participant_identity: str,
    transfer_to: str,
    play_dialtone: bool = True,
) -> None:
    """
    SIP REFER — transfer the active call to another number or SIP URI.
    transfer_to can be:
      - E.164 phone number:  "+12125551234"  (becomes tel:+12125551234)
      - SIP URI:             "sip:agent@company.com"
    """
    if transfer_to.startswith("+") or transfer_to.lstrip("+").isdigit():
        transfer_uri = f"tel:{transfer_to}"
    elif not transfer_to.startswith("sip:") and not transfer_to.startswith("tel:"):
        transfer_uri = f"tel:{transfer_to}"
    else:
        transfer_uri = transfer_to

    async with _client() as lk:
        await lk.sip.transfer_sip_participant(
            lkapi.TransferSIPParticipantRequest(
                room_name=room_name,
                participant_identity=participant_identity,
                transfer_to=transfer_uri,
                play_dialtone=play_dialtone,
            )
        )
    logger.info(f"Forwarded call in room {room_name} to {transfer_uri}")


async def stop_egress(egress_id: str) -> None:
    """Stop a recording egress (call it when the call ends)."""
    async with _client() as lk:
        try:
            await lk.egress.stop_egress(lkapi.StopEgressRequest(egress_id=egress_id))
        except Exception as e:
            logger.warning(f"Could not stop egress {egress_id}: {e}")
