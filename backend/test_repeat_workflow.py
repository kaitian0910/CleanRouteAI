"""Repeat a completed job through the HTTP API without replaying old progress."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.config import settings
from backend.models import (
    Coordinate, EnvironmentMapData, ExclusionZone, GeoPolygon, PlanRequest,
    RubbishItemSpec, TaskItem, TaskType, WasteObservation,
)
from backend.orchestrator import AgenticWorkflowOrchestrator


ORIGIN = Coordinate(lat=3.139, lon=101.6869)


def point(x, y):
    return Coordinate.from_local_meters(x, y, ORIGIN)


def polygon(points):
    return GeoPolygon(vertices=[point(x, y) for x, y in points])


@pytest.fixture
def api(monkeypatch):
    instance = AgenticWorkflowOrchestrator()
    instance.routing_engine._force_offline_for_test = True
    monkeypatch.setattr(main, "orchestrator", instance)
    with TestClient(main.app) as client:
        client.headers["Authorization"] = f"Bearer {settings.operator_token}"
        yield client, instance


def request(job_id="repeat-round", extra_tasks=(), machine_id="sweeper-alpha-01"):
    boundary = polygon([(0, 0), (30, 0), (30, 20), (0, 20)])
    return PlanRequest(job_id=job_id, use_rl=False, enable_depot_cycles=True, machine_id=machine_id,
        start_location=point(3, 3), disposal_location=point(27, 3), operating_boundary=boundary,
        exclusion_zones=[ExclusionZone(zone_id="fixed-obstacle", name="Fixed obstacle",
            polygon=polygon([(18, 9), (21, 9), (21, 12), (18, 12)]))],
        environment_map=EnvironmentMapData(map_id="operator-provided-repeat-site", require_verified_terrain=True,
            verified_traversable_polygons=[boundary]),
        kpi_policy_override={"max_estimated_duration_minutes": 60},
        tasks=[TaskItem(task_id="ordinary-bin", task_type=TaskType.RUBBISH_COLLECTION,
            location=point(8, 6), service_duration_sec=30, time_window_start_min=5,
            time_window_end_min=95, rubbish_spec=RubbishItemSpec(description="Static site bin")), *extra_tasks])


def complete(client, payload):
    planned = client.post("/api/routes/plan", json=payload.model_dump(mode="json"))
    assert planned.status_code == 200, planned.text
    route = planned.json()
    assert route["kpi_evaluation"]["passed"], (route["kpi_evaluation"]["failed_constraints"], route["route_safety_issues"])
    assert client.post(f"/api/routes/{route['route_id']}/approve").status_code == 200
    finished = client.post(f"/api/routes/{route['route_id']}/execute", json={})
    assert finished.status_code == 200, finished.text
    assert finished.json()["execution_state"] == "COMPLETED"
    return client.get(f"/api/routes/{route['route_id']}").json()


def test_repeat_completed_job_creates_one_fresh_round_preserves_site_and_resource_state(api):
    client, instance = api
    source = complete(client, request())
    old_snapshot = deepcopy(source)
    old_frames = deepcopy(instance.simulator.telemetry_store[source["route_id"]])
    assert all(s["completed"] for s in source["segments"])
    assert all(t["completed"] for t in source["tasks"])
    machine = instance.machines[source["machine_id"]]
    # Use the actual current resource state, not the original job's starting defaults.
    machine.current_battery_percent = 5
    machine.current_payload_kg = 7
    response = client.post(f"/api/routes/{source['route_id']}/repeat", json={"idempotency_key": "repeat-click-1"})
    assert response.status_code == 200, response.text
    repeated = response.json()
    assert repeated["kpi_evaluation"]["passed"], repeated["kpi_evaluation"]["failed_constraints"]
    assert repeated["repeat_of_route_id"] == source["route_id"]
    assert repeated["job_id"] != source["job_id"]
    assert repeated["route_id"] != source["route_id"]
    assert repeated["mission_elapsed_min"] == 0
    assert repeated["approved_by"] is None and repeated["status"] == "PROPOSED"
    assert repeated["origin"] == old_frames[-1].position.model_dump(mode="json")
    assert repeated["operating_boundary"] == source["operating_boundary"]
    assert repeated["exclusion_zones"] == source["exclusion_zones"]
    assert repeated["disposal_depot"] == source["disposal_depot"]
    assert repeated["planning_environment_map"] == source["planning_environment_map"]
    assert repeated["kpi_policy_snapshot"] == source["kpi_policy_snapshot"]
    assert repeated["initial_battery_percent"] == 5
    assert machine.current_battery_percent == 5 and machine.current_payload_kg == 7
    assert repeated["segments"][0]["segment_type"] == "DEPOT_RECHARGE"
    assert not any(s["completed"] for s in repeated["segments"])
    assert not any(t["completed"] for t in repeated["tasks"])
    assert repeated["tasks"][0]["task_id"] != source["tasks"][0]["task_id"]
    assert repeated["tasks"][0]["time_window_start_min"] == 5
    assert repeated["tasks"][0]["time_window_end_min"] == 95
    duplicate = client.post(f"/api/routes/{source['route_id']}/repeat", json={"idempotency_key": "repeat-click-1"})
    assert duplicate.status_code == 200 and duplicate.json()["route_id"] == repeated["route_id"]
    assert len(instance.job_route_history) == 2
    assert client.get(f"/api/routes/{source['route_id']}").json() == old_snapshot
    assert instance.simulator.telemetry_store[source["route_id"]] == old_frames

    assert client.post(f"/api/routes/{repeated['route_id']}/approve").status_code == 200
    finished = client.post(f"/api/routes/{repeated['route_id']}/execute", json={})
    assert finished.status_code == 200, finished.text
    assert finished.json()["execution_state"] == "COMPLETED"
    result = client.get(f"/api/routes/{repeated['route_id']}").json()
    assert all(s["completed"] for s in result["segments"])
    assert all(t["completed"] for t in result["tasks"])
    histories = client.get("/api/feedback/history").json()["records"]
    assert len(histories) == 2
    assert {record["job_id"] for record in histories} == {source["job_id"], repeated["job_id"]}
    assert all(record["status"] == "COMPLETED_SIMULATION" and record["actual_completion_minutes"] > 0 for record in histories)
    assert client.get(f"/api/routes/{source['route_id']}").json() == old_snapshot


def test_repeat_rejects_unfinished_job_and_missing_source(api):
    client, _ = api
    planned = client.post("/api/routes/plan", json=request("unfinished-round").model_dump(mode="json")).json()
    assert client.post(f"/api/routes/{planned['route_id']}/repeat", json={"idempotency_key": "repeat-unfinished"}).status_code == 409
    assert client.post("/api/routes/no-such-route/repeat", json={"idempotency_key": "repeat-missing"}).status_code == 404


def test_repeat_does_not_resurrect_completed_detected_litter_or_bulky_items(api):
    client, _ = api
    extras = [
        TaskItem(task_id="observed-bottle", task_type=TaskType.RUBBISH_COLLECTION, location=point(11, 6),
            waste_observation=WasteObservation(detection_id="original-observation", waste_class="Bottle",
                confidence=0.9, source="uploaded-image", world_location=point(11, 6)),
            rubbish_spec=RubbishItemSpec(description="Observed bottle", estimated_weight_kg=0.05)),
        TaskItem(task_id="one-off-bulky", task_type=TaskType.OVERSIZED_RUBBISH, location=point(14, 6),
            rubbish_spec=RubbishItemSpec(description="One-off bulky pickup", is_oversized=True,
                estimated_weight_kg=20, estimated_volume_m3=0.1, max_dimension_m=1, requires_heavy_lifter=True)),
    ]
    source = complete(client, request("observed-pickups", extras, "heavy-utility-02"))
    assert len(source["tasks"]) == 3 and all(t["completed"] for t in source["tasks"])
    response = client.post(f"/api/routes/{source['route_id']}/repeat", json={"idempotency_key": "repeat-ordinary-bin-only"})
    assert response.status_code == 200, response.text
    repeated = response.json()
    assert repeated["kpi_evaluation"]["passed"], repeated["kpi_evaluation"]["failed_constraints"]
    assert len(repeated["tasks"]) == 1
    assert repeated["tasks"][0]["waste_observation"] is None
    assert repeated["tasks"][0]["rubbish_spec"]["description"] == "Static site bin"
    assert repeated["tasks"][0]["task_type"] == "RUBBISH_COLLECTION"
