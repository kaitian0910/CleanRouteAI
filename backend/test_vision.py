"""Detector conversion, API failures, and the detector-to-planner contract."""
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.config import settings
from backend.main import app
from backend.models import Coordinate, PlanRequest, RubbishItemSpec, TaskItem, TaskType, WasteObservation
from backend.orchestrator import AgenticWorkflowOrchestrator
from backend.vision import InvalidImageError, MAX_IMAGE_BYTES, VisionUnavailableError, WasteDetector


def image_bytes(size=(120, 80), format="JPEG", **kwargs):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format=format, **kwargs)
    return output.getvalue()


class Values:
    def __init__(self, values):
        self.values = values

    def cpu(self):
        return self

    def tolist(self):
        return self.values


class FakeModel:
    names = {0: "Bottle", 1: "Can"}

    def predict(self, **kwargs):
        self.arguments = kwargs
        return [SimpleNamespace(names=self.names, boxes=SimpleNamespace(
            xyxyn=Values([[0.1, 0.2, 0.6, 0.7], [-0.1, 0.8, 1.1, 1.1], [0.5, 0.5, 0.5, 0.6]]),
            conf=Values([0.91, 0.82, 0.99]), cls=Values([0, 1, 0]),
        ))]


@pytest.fixture
def detector():
    detector = WasteDetector()
    detector._model = FakeModel()
    return detector


def test_normalizes_boxes_preserves_metadata_and_requires_separate_map_location(detector):
    payload = image_bytes()
    result = detector.detect(payload, 0.4)
    assert (result.image_width, result.image_height) == (120, 80)
    assert len(result.detections) == 2
    first, clipped = result.detections
    assert first.waste_class == "Bottle" and first.confidence == 0.91
    assert first.source == "YOLO:best.pt" and first.observed_at.tzinfo is not None
    assert first.image_bbox.width == pytest.approx(0.5)
    assert first.world_location is None and first.rubbish_spec is None
    assert clipped.image_bbox.x == 0 and clipped.image_bbox.width == 1
    assert clipped.image_bbox.y + clipped.image_bbox.height == 1
    assert detector._model.arguments["conf"] == 0.4
    assert detector._model.arguments["save"] is False
    assert detector.detect(payload).detections[0].detection_id == first.detection_id
    # Output ordering must not change observation identity.
    model = detector._model
    original_predict = model.predict
    def reordered(**kwargs):
        results = original_predict(**kwargs)
        for field in ("xyxyn", "conf", "cls"):
            getattr(results[0].boxes, field).values.reverse()
        return results
    model.predict = reordered
    assert {item.detection_id for item in detector.detect(payload).detections} == {item.detection_id for item in result.detections}


@pytest.mark.parametrize("payload", [b"", b"not an image", b"x" * (MAX_IMAGE_BYTES + 1)], ids=["empty", "invalid", "oversized"])
def test_rejects_invalid_uploads_before_loading_model(payload):
    with pytest.raises(InvalidImageError):
        WasteDetector(Path("missing.pt")).detect(payload)


def test_rejects_unsupported_formats_and_pixel_limit(detector, monkeypatch):
    with pytest.raises(InvalidImageError, match="Supported"):
        detector.detect(image_bytes(format="BMP"))
    monkeypatch.setattr("backend.vision.MAX_IMAGE_PIXELS", 100)
    with pytest.raises(InvalidImageError, match="megapixel"):
        detector.detect(image_bytes())


def test_exif_rotation_matches_returned_dimensions(detector):
    exif = Image.Exif()
    exif[274] = 6
    result = detector.detect(image_bytes(exif=exif))
    assert (result.image_width, result.image_height) == (80, 120)
    assert detector._model.arguments["source"].size == (80, 120)


def test_missing_weights_status_is_actionable():
    detector = WasteDetector(Path(__file__).with_name("missing-vision-test-weights.pt"))
    assert detector.status()["ready"] is False
    with pytest.raises(VisionUnavailableError, match="weights missing"):
        detector.detect(image_bytes())


def test_api_detect_validation_roles_and_unavailable_model(detector, monkeypatch):
    monkeypatch.setattr("backend.main.waste_detector", detector)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.operator_token}"}
    status = client.get("/api/vision/status", headers=headers)
    assert status.status_code == 200 and status.json()["classes"] == ["Bottle", "Can"]
    response = client.post("/api/vision/detect?confidence=0.4", files={"image": ("test.jpg", image_bytes(), "image/jpeg")}, headers=headers)
    assert response.status_code == 200
    assert response.json()["detections"][0]["world_location"] is None
    for confidence in ("nan", "0", "1.1"):
        assert client.post(f"/api/vision/detect?confidence={confidence}", files={"image": ("bad.jpg", image_bytes())}, headers=headers).status_code == 422
    assert client.post("/api/vision/detect", files={"image": ("bad.jpg", b"garbage")}, headers=headers).status_code == 422
    assert client.post("/api/vision/detect", files={"image": ("huge.jpg", b"x" * (MAX_IMAGE_BYTES + 1))}, headers=headers).status_code == 413
    assert client.post("/api/vision/detect", files={"image": ("test.jpg", image_bytes())}, headers={"Authorization": f"Bearer {settings.internal_service_token}"}).status_code == 403
    monkeypatch.setattr(settings, "environment", "production")
    assert client.get("/api/vision/status").status_code == 401
    def unavailable(*args):
        raise VisionUnavailableError("Model unavailable")
    monkeypatch.setattr(detector, "detect", unavailable)
    assert client.post("/api/vision/detect", files={"image": ("test.jpg", image_bytes())}, headers=headers).status_code == 503


def test_detection_becomes_a_collection_task_with_observation_retained(detector):
    observation = detector.detect(image_bytes()).detections[0]
    location = Coordinate(lat=3.1391, lon=101.687)
    spec = RubbishItemSpec(description="Operator estimated bottle", estimated_weight_kg=0.05,
                           estimated_volume_m3=0.0005, max_dimension_m=0.2,
                           weight_estimation_method="OPERATOR_ESTIMATE")
    observation = WasteObservation.model_validate({**observation.model_dump(), "world_location": location, "rubbish_spec": spec})
    task = TaskItem(task_id=f"task-{observation.detection_id}", idempotency_key=observation.detection_id,
                    task_type=TaskType.RUBBISH_COLLECTION, location=location,
                    waste_observation=observation, rubbish_spec=spec)
    workflow = AgenticWorkflowOrchestrator()
    workflow.routing_engine._force_offline_for_test = True
    route = workflow.plan_and_optimize(PlanRequest(
        job_id="vision-integration", tasks=[task], start_location=Coordinate(lat=3.139, lon=101.6869),
        disposal_location=Coordinate(lat=3.139, lon=101.6869),
    ))
    assert route.kpi_evaluation.passed
    stored = route.tasks[0].waste_observation
    assert stored.detection_id == observation.detection_id
    assert stored.world_location == location and stored.image_bbox == observation.image_bbox
    assert stored.rubbish_spec.weight_estimation_method == "OPERATOR_ESTIMATE"


def test_supplied_checkpoint_loads_and_runs_actual_cpu_inference():
    detector = WasteDetector()
    status = detector.status()
    assert status["ready"], status
    assert status["classes"] == ["Plastic Bag & Wrapper", "Cigarette", "Bottle", "Bottle Cap & Lid",
                                 "Can", "Cup", "Paper & Cardboard", "Other Litter"]
    result = detector.detect(image_bytes((640, 480)))
    assert result.image_width == 640 and result.image_height == 480
    assert result.inference_ms > 0
    assert result.detections == []
