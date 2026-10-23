"""Local YOLO inference; importing the planning API never imports torch."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import List

from pydantic import Field

from .config import CompatBaseModel
from .models import ImageBoundingBox, WasteObservation

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


class VisionUnavailableError(RuntimeError):
    pass


class InvalidImageError(ValueError):
    pass


class DetectionResponse(CompatBaseModel):
    model: str
    image_width: int
    image_height: int
    inference_ms: float
    detections: List[WasteObservation] = Field(default_factory=list)


class WasteDetector:
    def __init__(self, model_path: Path | None = None):
        self.model_path = model_path or Path(os.environ.get(
            "CLEANROUTEAI_YOLO_MODEL_PATH", str(Path(__file__).parent / "vision_assets" / "best.pt")
        )).expanduser().resolve()
        self.device = os.environ.get("CLEANROUTEAI_YOLO_DEVICE", "cpu")
        self._model = None
        self._lock = Lock()

    def _load(self):
        # Caller holds the lock: model initialization and prediction are serialized.
        if self._model is None:
            if not self.model_path.is_file():
                raise VisionUnavailableError("YOLO weights missing. Configure CLEANROUTEAI_YOLO_MODEL_PATH or restore backend/vision_assets/best.pt.")
            try:
                from ultralytics import YOLO
                self._model = YOLO(str(self.model_path), task="detect")
            except ImportError as exc:
                raise VisionUnavailableError("Install backend/requirements.txt to enable YOLO detection.") from exc
            except Exception as exc:
                logger.exception("Unable to load YOLO checkpoint")
                raise VisionUnavailableError("Unable to load YOLO weights; check the backend log and model compatibility.") from exc
        return self._model

    def status(self) -> dict:
        with self._lock:
            try:
                model = self._load()
                return {"ready": True, "model": self.model_path.name,
                        "device": self.device, "classes": list(model.names.values())}
            except VisionUnavailableError as exc:
                return {"ready": False, "model": self.model_path.name, "detail": str(exc), "classes": []}

    def detect(self, image_bytes: bytes, confidence: float = 0.25) -> DetectionResponse:
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise InvalidImageError("Upload a nonempty image no larger than 10 MiB.")
        if not 0.05 <= confidence <= 1.0:
            raise InvalidImageError("Confidence must be between 0.05 and 1.0.")
        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
        except ImportError as exc:
            raise VisionUnavailableError("Install backend/requirements.txt to enable image decoding.") from exc
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(image_bytes)) as original:
                    if original.format not in {"JPEG", "PNG", "WEBP"}:
                        raise InvalidImageError("Supported images: JPEG, PNG, and WebP.")
                    if original.width * original.height > MAX_IMAGE_PIXELS:
                        raise InvalidImageError("Image exceeds the 20 megapixel limit.")
                    image = ImageOps.exif_transpose(original).convert("RGB")
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise InvalidImageError("Invalid or oversized image. Upload JPEG, PNG, or WebP.") from exc

        with self._lock:
            model = self._load()
            started = time.perf_counter()
            try:
                result = model.predict(source=image, conf=confidence, imgsz=896,
                                       device=self.device, max_det=100, verbose=False, save=False)[0]
            except Exception as exc:
                logger.exception("YOLO inference failed")
                raise VisionUnavailableError("YOLO inference failed; check the backend log and CLEANROUTEAI_YOLO_DEVICE setting.") from exc
            elapsed = (time.perf_counter() - started) * 1000
            observations = []
            digest = hashlib.sha256(image_bytes).hexdigest()[:24]
            observed_at = datetime.now(timezone.utc)
            if result.boxes is not None:
                for coords, score, cls in zip(
                    result.boxes.xyxyn.cpu().tolist(), result.boxes.conf.cpu().tolist(),
                    result.boxes.cls.cpu().tolist(),
                ):
                    x1, y1, x2, y2 = [min(1.0, max(0.0, float(v))) for v in coords]
                    if x2 <= x1 or y2 <= y1:
                        continue
                    box_key = hashlib.sha256(
                        f"{int(cls)}:{x1:.6f}:{y1:.6f}:{x2:.6f}:{y2:.6f}".encode()
                    ).hexdigest()[:12]
                    observations.append(WasteObservation(
                        detection_id=f"yolo-{digest}-{box_key}", waste_class=result.names[int(cls)],
                        confidence=score, observed_at=observed_at, source=f"YOLO:{self.model_path.name}",
                        image_bbox=ImageBoundingBox(x=x1, y=y1, width=x2-x1, height=y2-y1),
                    ))
            return DetectionResponse(model=self.model_path.name, image_width=image.width,
                                     image_height=image.height, inference_ms=round(elapsed, 1),
                                     detections=observations)


waste_detector = WasteDetector()
