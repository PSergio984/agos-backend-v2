import json
import base64
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from app.core.ws_manager import ws_manager
from app.services.websocket_service import websocket_service
from app.services.ml_service import ml_service
from app.services.camera_device_service import camera_device_service
from app.services.camera_status_service import camera_status_service
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()


async def _resolve_responder_location_id(websocket: WebSocket, token: str) -> int | None:
    """Decode a responder JWT and return their effective site id, or close the
    socket (1008) and return None on any failure."""
    from jose import jwt, JWTError
    from app.core.config import settings
    from app.crud import responder_crud
    from app.models.responder_related.responders import ResponderStatus

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        responder_id = payload.get("sub")
        if not responder_id or payload.get("type") != "responder":
            raise JWTError("not a responder token")
    except JWTError:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token"
        )
        return None

    async with AsyncSessionLocal() as db:
        responder = await responder_crud.get(db=db, id=responder_id)
    if responder is None or responder.status != ResponderStatus.ACTIVE:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Responder not active"
        )
        return None

    return responder.active_location_id or responder.location_id


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):

    location_id = websocket.query_params.get("location_id")
    token = websocket.query_params.get("token")

    await websocket.accept()

    # Presence-derived channel: a responder token pins the subscription to
    # their effective site (active ?? home), overriding whatever the client
    # asked for. No token → legacy client-chosen channel (PWA/admin viewers).
    if token:
        location_id = await _resolve_responder_location_id(websocket=websocket, token=token)
        if location_id is None:
            return  # socket already closed

    if not location_id:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Missing location_id"
        )
        return

    # Convert location_id to int
    try:
        location_id = int(location_id)
    except ValueError:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid location_id"
        )
        return

    # Reject unknown/inactive sites up front — send_initial_data would
    # otherwise die silently inside the generic except after accept.
    from app.crud import location_crud
    async with AsyncSessionLocal() as db:
        site = await location_crud.get(db=db, id=location_id)
    if site is None or not site.is_active:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Site is not active"
        )
        return

    # Add the WebSocket connection to the manager
    await ws_manager.connect(websocket=websocket, location_id=location_id)

    try:
        # Scope DB session tightly to the initial-data call so we don't hold a
        # connection for the entire WS lifetime (causes ConnectionDoesNotExist
        # when idle poolers/networks drop the underlying TCP connection).
        async with AsyncSessionLocal() as db:
            await websocket_service.send_initial_data(
                websocket=websocket, db=db, location_id=location_id
            )

        while True:
            await websocket.receive_text()  # Keep the connection alive
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(
            "Unexpected error in frontend WebSocket loop — location_id=%s", location_id
        )
    finally:
        await ws_manager.disconnect(websocket=websocket, location_id=location_id)


@router.websocket("/ws/rpi")
async def rpi_websocket_endpoint(websocket: WebSocket):
    camera_device_id_raw = websocket.query_params.get("camera_device_id")
    location_id_raw = websocket.query_params.get("location_id")

    await websocket.accept()

    if not location_id_raw:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Missing location_id",
        )
        return

    try:
        # camera_device_id stays optional: legacy firmware identifies by
        # location_id only and the service resolves the site's default camera.
        camera_device_id = int(camera_device_id_raw) if camera_device_id_raw else None
        location_id = int(location_id_raw)
    except ValueError:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Invalid camera_device_id or location_id",
        )
        return

    # Camera↔site binding: the device's registered site must match the
    # claimed location_id (unknown/inactive device → same rejection).
    async with AsyncSessionLocal() as db:
        bound_device = await camera_device_service.get_for_ingest(
            db=db, camera_device_id=camera_device_id, location_id=location_id
        )
    if bound_device is None:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Camera device is not registered for this location",
        )
        return

    camera_device_id = bound_device.id
    await websocket.send_json(
        {
            "type": "connected",
            "camera_device_id": camera_device_id,
            "location_id": location_id,
        }
    )
    print(f"📷 RPi connected — cam={camera_device_id}, loc={location_id}")

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            if message["type"] != "websocket.receive":
                continue

            if message.get("bytes"):
                image_bytes: bytes = message["bytes"]

                encoded_image = base64.b64encode(image_bytes).decode("utf-8")
                camera_status_service.record_frame(
                    location_id, encoded_image=encoded_image
                )

                # Broadcast the raw frame immediately to all frontend clients
                # so they see a live image feed (rapidly updating picture).
                # This happens before ML inference so the UI stays responsive.
                try:
                    await ws_manager.broadcast_to_location(
                        {
                            "type": "camera_update",
                            "data": {
                                "image": encoded_image,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        },
                        location_id=location_id,
                    )
                except Exception as e:
                    logger.error(
                        "broadcast_to_location failed for location_id=%s: %s",
                        location_id,
                        e,
                        exc_info=True,
                    )

                # Run ML inference and send result back to the RPi
                try:
                    result = await ml_service.process_frame_bytes(
                        image_bytes=image_bytes,
                        camera_device_id=camera_device_id,
                        location_id=location_id,
                    )
                    await websocket.send_json(
                        {"type": "frame_processed", "data": result}
                    )
                except WebSocketDisconnect:
                    break
                except Exception:
                    logger.exception(
                        "ML inference failed for camera_device_id=%s, location_id=%s",
                        camera_device_id,
                        location_id,
                    )
                    try:
                        await websocket.send_json(
                            {"type": "error", "message": "Internal server error"}
                        )
                    except WebSocketDisconnect:
                        break

            # ── Text frame: control messages (ping, etc.) ────────────────────
            elif message.get("text"):
                try:
                    data = json.loads(message["text"])
                    if data.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                except (json.JSONDecodeError, Exception):
                    pass

    except WebSocketDisconnect:
        print(f"📷 RPi disconnected — cam={camera_device_id}, loc={location_id}")
    except Exception:
        logger.exception(
            "Unexpected error in WebSocket loop — camera_device_id=%s, location_id=%s",
            camera_device_id,
            location_id,
        )
        try:
            await websocket.send_json(
                {"type": "error", "message": "Internal server error"}
            )
            await websocket.close()
        except Exception:
            pass
