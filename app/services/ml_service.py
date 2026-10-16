import asyncio
import logging
import random
from collections import deque
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone

from app.core.cloudinary import upload_image
from app.crud import model_readings_crud, location_crud
from app.schemas import ModelReadingCreate, ModelWebSocketResponse, BlockageStatus, ObstructionConfidence
from app.core.database import AsyncSessionLocal
from app.services.websocket_service import websocket_service
from app.models.data_sources.model_readings import ModelReadings
from app.core.state import fusion_state_manager
from app.core.config import settings
from app.utils.obstruction_utils import calculate_obstruction_confidence, obstruction_status_from_tier


logger = logging.getLogger(__name__)

WEIGHTS_PATH = Path(__file__).parent.parent / "ml" / "weights" / "best.onnx"

# Coverage % -> per-frame status
CLEAR_MAX = 20.0      # < 20%      -> clear
PARTIAL_MAX = 60.0    # 20% - 60%  -> partial
                      # >= 60%     -> blocked

# F1 — adaptive-sampling hysteresis: stay on the elevated (fast) capture interval
# for this long after fusion risk relaxes, so the cadence doesn't oscillate.
ELEVATED_RELAX_HYSTERESIS_SECONDS = 5 * 60
ELEVATED_ALERT_NAMES = ("Warning", "Critical")

# YOLO thresholds
CONF_THRESHOLD = 0.35
IOU_THRESHOLD = 0.5

# Fallback input size if ONNX model declares dynamic spatial dims
DEFAULT_INPUT_SIZE = 640


class MLService:
    """
    YOLOv8 ONNX inference on JPEG frames from the camera.
    Throttled to FRAME_CAPTURE_INTERVAL_SECONDS per device.
    Per-frame status is smoothed (2-of-3) before broadcast + fusion so a single
    frame with debris just passing through doesn't flip the status.
    Bounding boxes are drawn on the frame before upload to Cloudinary.
    Falls back to random placeholder values when weights are absent.
    """

    def __init__(self):
        self._last_processed: dict[int, datetime] = {}
        # F1 — rolling window of recent raw statuses per camera (confidence engine).
        self._windows: dict[int, deque] = {}
        # F1 — adaptive sampling: monotonic deadline until which the fast interval holds.
        self._elevated_until: dict[int, float] = {}
        self._session = self._load_session()
        self._input_name, self._input_size = self._inspect_input()

    def _capture_interval_seconds(self, camera_device_id: int, location_id: int) -> int:
        """F1 adaptive sampling: drop to the elevated interval while fusion risk is
        elevated (Warning/Critical), with hysteresis so it doesn't flap back."""
        import time

        alert_name = fusion_state_manager.get_alert_name(location_id)
        now_ts = time.monotonic()

        if alert_name in ELEVATED_ALERT_NAMES:
            self._elevated_until[camera_device_id] = now_ts + ELEVATED_RELAX_HYSTERESIS_SECONDS
            return settings.FRAME_CAPTURE_INTERVAL_ELEVATED_SECONDS

        if now_ts < self._elevated_until.get(camera_device_id, 0.0):
            return settings.FRAME_CAPTURE_INTERVAL_ELEVATED_SECONDS

        return settings.FRAME_CAPTURE_INTERVAL_SECONDS

    def _load_session(self):
        if not WEIGHTS_PATH.exists():
            logger.warning(
                "ONNX weights not found at %s; using placeholder predictions",
                WEIGHTS_PATH,
            )
            return None
        try:
            import onnxruntime as ort
            session = ort.InferenceSession(
                str(WEIGHTS_PATH), providers=["CPUExecutionProvider"]
            )
            logger.info("ONNX model loaded from %s", WEIGHTS_PATH)
            return session
        except Exception as e:
            logger.warning(
                "Failed to load ONNX model (%s); using placeholder",
                type(e).__name__,
            )
            return None

    def _inspect_input(self):
        if self._session is None:
            return None, (DEFAULT_INPUT_SIZE, DEFAULT_INPUT_SIZE)
        inp = self._session.get_inputs()[0]
        shape = inp.shape  # [batch, 3, H, W]
        h = shape[2] if isinstance(shape[2], int) else DEFAULT_INPUT_SIZE
        w = shape[3] if isinstance(shape[3], int) else DEFAULT_INPUT_SIZE
        return inp.name, (w, h)

    async def process_frame_bytes(
        self,
        image_bytes: bytes,
        camera_device_id: int,
        location_id: int,
    ) -> dict:
        now = datetime.now(timezone.utc)
        capture_interval = self._capture_interval_seconds(camera_device_id, location_id)
        last = self._last_processed.get(camera_device_id)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < capture_interval - 5:
                return {}

        self._last_processed[camera_device_id] = now

        raw_percentage, raw_status, upload_bytes = await self._infer_and_annotate(image_bytes)
        confidence = self._compute_confidence(camera_device_id, raw_status, raw_percentage)
        # Sustained-evidence status drives fusion; a lone flagged frame stays "possible".
        smoothed_status = self._status_from_tier(confidence.tier)

        public_id = f"rpi_{camera_device_id}_{int(now.timestamp())}"
        upload_result = await upload_image(BytesIO(upload_bytes), filename=public_id)
        image_url = upload_result["secure_url"] if upload_result else f"rpi_frame_{public_id}.jpg"

        async with AsyncSessionLocal() as db:
            # The gated write lives in this session, not the request's — the
            # check must run here under FOR SHARE so a frame can't land on a
            # site deactivated while inference+upload were in flight.
            site = await location_crud.get_for_share(db=db, id=location_id)
            if site is None or not site.is_active:
                return {}
            obj_in = ModelReadingCreate(
                camera_device_id=camera_device_id,
                image_path=image_url,
                timestamp=now,
                blockage_percentage=raw_percentage,
                blockage_status=raw_status,
            )
            db_obj: ModelReadings = await model_readings_crud.create_and_return(
                db=db, obj_in=obj_in
            )

        blockage_reading = ModelWebSocketResponse(
            status="success",
            message="Retrieved successfully",
            blockage_status=smoothed_status,
            confidence=confidence,
        )

        await websocket_service.broadcast_update(
            update_type="blockage_detection_update",
            data=blockage_reading.model_dump(mode="json"),
            location_id=location_id,
        )

        await fusion_state_manager.recalculate_visual_status_score(
            blockage_status=BlockageStatus(
                status=smoothed_status,
                timestamp=db_obj.timestamp,
                confidence=confidence,
            ),
            location_id=location_id,
        )

        return blockage_reading.model_dump(mode="json")

    async def _infer_and_annotate(self, image_bytes: bytes) -> tuple[float, str, bytes]:
        """Return (raw_percentage, raw_status, bytes_to_upload)."""
        if self._session is None:
            pct, status = self._placeholder_inference()
            return pct, status, image_bytes

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run_inference, image_bytes)

    def _run_inference(self, image_bytes: bytes) -> tuple[float, str, bytes]:
        try:
            import numpy as np
            from PIL import Image

            img = Image.open(BytesIO(image_bytes)).convert("RGB")
            img_np = np.array(img)
            orig_h, orig_w = img_np.shape[:2]

            x, scale, pad = self._preprocess(img_np)
            outputs = self._session.run(None, {self._input_name: x})
            detections = self._postprocess(outputs[0], scale, pad, (orig_h, orig_w))

            pct = self._compute_coverage_pct(detections, (orig_h, orig_w))
            annotated = self._draw_boxes(img, detections)

            buf = BytesIO()
            annotated.save(buf, format="JPEG", quality=85)
            return pct, self._status_from_pct(pct), buf.getvalue()
        except Exception as e:
            logger.warning("Inference failed (%s); treating as clear", type(e).__name__)
            return 0.0, "clear", image_bytes

    def _preprocess(self, img_np):
        """Letterbox resize to model input, normalize to [0,1], HWC->CHW, add batch."""
        import numpy as np
        from PIL import Image

        w_in, h_in = self._input_size
        h, w = img_np.shape[:2]
        scale = min(w_in / w, h_in / h)
        new_w, new_h = int(w * scale), int(h * scale)

        resized = np.array(Image.fromarray(img_np).resize((new_w, new_h), Image.BILINEAR))

        pad_w = w_in - new_w
        pad_h = h_in - new_h
        left, top = pad_w // 2, pad_h // 2
        padded = np.full((h_in, w_in, 3), 114, dtype=np.uint8)
        padded[top:top + new_h, left:left + new_w] = resized

        x = padded.astype(np.float32) / 255.0
        x = x.transpose(2, 0, 1)[np.newaxis, ...]
        return x, scale, (left, top)

    def _postprocess(self, output, scale, pad, orig_shape):
        """YOLOv8 single-class output (1, 5, N) -> list of (x1,y1,x2,y2,conf)."""
        import numpy as np

        pred = output[0].T  # (N, 5): cx, cy, w, h, conf
        mask = pred[:, 4] >= CONF_THRESHOLD
        pred = pred[mask]
        if len(pred) == 0:
            return []

        boxes = np.empty((len(pred), 4), dtype=np.float32)
        boxes[:, 0] = pred[:, 0] - pred[:, 2] / 2
        boxes[:, 1] = pred[:, 1] - pred[:, 3] / 2
        boxes[:, 2] = pred[:, 0] + pred[:, 2] / 2
        boxes[:, 3] = pred[:, 1] + pred[:, 3] / 2
        scores = pred[:, 4].copy()

        pad_x, pad_y = pad
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad_x) / scale
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad_y) / scale

        h, w = orig_shape
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, w)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, h)

        keep = self._nms(boxes, scores, IOU_THRESHOLD)
        return [
            (float(boxes[i, 0]), float(boxes[i, 1]),
             float(boxes[i, 2]), float(boxes[i, 3]),
             float(scores[i]))
            for i in keep
        ]

    @staticmethod
    def _nms(boxes, scores, iou_thresh):
        import numpy as np

        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]

        keep = []
        while len(order) > 0:
            i = int(order[0])
            keep.append(i)
            if len(order) == 1:
                break
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0.0, xx2 - xx1)
            h = np.maximum(0.0, yy2 - yy1)
            inter = w * h
            iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
            order = order[1:][iou <= iou_thresh]
        return keep

    @staticmethod
    def _compute_coverage_pct(detections, orig_shape) -> float:
        """Union of detection-box widths over the full frame width, as a percentage."""
        _, w = orig_shape
        if not detections:
            return 0.0

        spans: list[tuple[int, int]] = []
        for x1, y1, x2, y2, _ in detections:
            x1i, x2i = max(0, int(x1)), min(w, int(x2))
            if x2i > x1i and y2 > y1:
                spans.append((x1i, x2i))

        if not spans:
            return 0.0

        spans.sort()
        merged: list[list[int]] = []
        for start, end in spans:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)

        blocked_width = sum(end - start for start, end in merged)
        return round(100.0 * float(blocked_width) / float(w), 2)

    @staticmethod
    def _status_from_pct(pct: float) -> str:
        if pct < CLEAR_MAX:
            return "clear"
        if pct < PARTIAL_MAX:
            return "partial"
        return "blocked"

    @staticmethod
    def _draw_boxes(img, detections):
        from PIL import ImageDraw

        out = img.copy()
        draw = ImageDraw.Draw(out)
        for x1, y1, x2, y2, conf in detections:
            draw.rectangle([x1, y1, x2, y2], outline="red", width=3)
            draw.text((x1 + 2, y1 + 2), f"{conf:.2f}", fill="red")
        return out

    def _compute_confidence(
        self, camera_device_id: int, raw_status: str, raw_percentage: float
    ) -> ObstructionConfidence:
        """F1 confidence engine (generalized from the old 2-of-3 smoother).

        Pushes the raw status into a rolling window of the last K readings and
        derives a graded tier + score. `blocked` counts fully; `partial` counts
        as `OBSTRUCTION_PARTIAL_WEIGHT`. The window must be full before escalating
        past `possible` (cold-start guard)."""
        k = max(1, settings.OBSTRUCTION_WINDOW_K)
        buf = self._windows.get(camera_device_id)
        if buf is None or buf.maxlen != k:
            buf = deque(buf or [], maxlen=k)
            self._windows[camera_device_id] = buf
        buf.append(raw_status)

        return calculate_obstruction_confidence(list(buf), raw_percentage)

    @staticmethod
    def _status_from_tier(tier: str) -> str:
        return obstruction_status_from_tier(tier)

    def _placeholder_inference(self) -> tuple[float, str]:
        status = random.choices(
            ["clear", "partial", "blocked"], weights=[0.7, 0.2, 0.1], k=1
        )[0]
        if status == "clear":
            pct = round(random.uniform(0, CLEAR_MAX - 0.01), 2)
        elif status == "partial":
            pct = round(random.uniform(CLEAR_MAX, PARTIAL_MAX - 0.01), 2)
        else:
            pct = round(random.uniform(PARTIAL_MAX, 100), 2)
        return pct, status


ml_service = MLService()
