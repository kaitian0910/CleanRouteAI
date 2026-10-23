"""Operator-supplied planning inputs cannot silently reuse old approvals."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .config import settings
from .execution import SafetyInterlockError
from .models import ExecutionState, MachineProfile, RouteLifecycleStatus
from .planning_profiles import PARAMETERS, PlanningProfileStore
from .test_http_execution import initial_plan
from .test_depot_mission import workflow
from .test_mission_history import history_directory


@pytest.fixture
def api(monkeypatch):
    from . import main
    instance = workflow()
    monkeypatch.setattr(main, "orchestrator", instance)
    store = PlanningProfileStore()
    monkeypatch.setattr(main, "planning_profiles", store)
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "operator_token", "profile-token")
    monkeypatch.setattr(settings.optimization, "max_iterations", 1)
    instance.machines["sweeper-alpha-01"].battery_capacity_kwh = 0.6
    return instance, TestClient(main.app), {"Authorization": "Bearer profile-token"}, store


def update(client, headers, parameters):
    return client.post("/api/machines/sweeper-alpha-01/planning-profile",
        json={"parameters": parameters}, headers=headers)


def test_profile_update_invalidates_approval_without_changing_live_resources(api):
    instance, client, headers, _ = api
    _, planned = initial_plan(client, headers)
    route = instance.routes_by_id[planned["route_id"]]
    old = instance.machines[route.machine_id]
    old.current_payload_kg = 15
    result = update(client, headers, {"working_width_m": 1.8, "battery_capacity_kwh": 1.0})
    assert result.status_code == 200, result.text
    machine = instance.machines[route.machine_id]
    assert machine.current_battery_percent == old.current_battery_percent
    assert machine.current_payload_kg == 15
    assert machine.planning_revision == old.planning_revision + 1
    assert route.route_id in result.json()["invalidated_route_ids"]
    assert route.status == RouteLifecycleStatus.SUPERSEDED
    assert route.approved_by is None
    assert instance.state_machine.get_state(route.job_id) == ExecutionState.NEEDS_ASSISTANCE
    assert client.post(f"/api/routes/{route.route_id}/execute", headers=headers).status_code == 409
    listed = client.get("/api/machines", headers=headers).json()
    assert next(row for row in listed if row["machine_id"] == machine.machine_id)["working_width_m"] == 1.8


@pytest.mark.parametrize("parameters", [{"current_battery_percent": 100}, {"working_width_m": 0},
    {"sweep_speed_mps": -1}, {"battery_capacity_kwh": True}, {"cruise_speed_mps": "2"},
    {"recharged_battery_percent": 101}, {}])
def test_profile_api_rejects_unsafe_or_unsupported_parameters(api, parameters):
    instance, client, headers, _ = api
    before = instance.machines["sweeper-alpha-01"].model_dump()
    assert update(client, headers, parameters).status_code == 422
    assert instance.machines["sweeper-alpha-01"].model_dump() == before


def test_profile_update_is_denied_while_machine_has_executing_job(api):
    instance, client, headers, _ = api
    _, planned = initial_plan(client, headers)
    instance.state_machine.transition(planned["job_id"], ExecutionState.EXECUTING, "test", "Execution in progress")
    before = instance.machines["sweeper-alpha-01"].planning_revision
    assert update(client, headers, {"working_width_m": 1.8}).status_code == 409
    assert instance.machines["sweeper-alpha-01"].planning_revision == before
    assert instance.routes_by_id[planned["route_id"]].approved_by is not None


def test_paused_route_requires_replan_after_parameter_update(api):
    instance, client, headers, _ = api
    _, planned = initial_plan(client, headers)
    route_id = planned["route_id"]
    assert client.post(f"/api/routes/{route_id}/execute", headers=headers,
        json={"stop_after_segments": 1}).status_code == 200
    before = sum(segment.completed for segment in instance.routes_by_id[route_id].segments)
    assert update(client, headers, {"sweep_speed_mps": 0.8}).status_code == 200
    assert client.post(f"/api/routes/{route_id}/resume", headers=headers,
        json={"stop_after_segments": 1}).status_code == 409
    assert sum(segment.completed for segment in instance.routes_by_id[route_id].segments) == before
    assert instance.state_machine.get_state(planned["job_id"]) == ExecutionState.NEEDS_ASSISTANCE


def test_revision_guard_blocks_inflight_old_candidate_approval_and_simulation(api):
    instance, client, headers, _ = api
    _, planned = initial_plan(client, headers)
    route = instance.routes_by_id[planned["route_id"]]
    machine = instance.machines[route.machine_id]
    machine.planning_revision += 1  # Candidate finished after a concurrent profile change.
    route.status = RouteLifecycleStatus.PROPOSED
    assert client.post(f"/api/routes/{route.route_id}/approve", headers=headers).status_code == 409
    route.status = RouteLifecycleStatus.APPROVED
    instance.state_machine._states[route.job_id] = ExecutionState.READY
    with pytest.raises(SafetyInterlockError, match="parameters changed"):
        instance.simulator.execute_simulation(route, machine)
    assert not any(segment.completed for segment in route.segments)


def test_profile_persistence_restores_only_planning_parameters(history_directory):
    path = history_directory / "machine_profiles.json"
    machine = MachineProfile(machine_id="one", name="One", current_battery_percent=37,
        current_payload_kg=17, working_width_m=1.9, planning_revision=7)
    store = PlanningProfileStore(path)
    store.save(machine)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["one"]) == PARAMETERS
    restored = {"one": MachineProfile(machine_id="one", name="One")}
    PlanningProfileStore(path).restore(restored)
    assert restored["one"].working_width_m == 1.9
    assert restored["one"].current_battery_percent == 95
    assert restored["one"].current_payload_kg == 0
    assert restored["one"].planning_revision == 1


def test_profile_storage_errors_surface_in_health(api, history_directory):
    from . import main
    instance, client, _, _ = api
    bad = history_directory / "bad.json"
    bad.write_text('{"sweeper-alpha-01":{"current_battery_percent":100}}', encoding="utf-8")
    main.planning_profiles = PlanningProfileStore(bad)
    main.planning_profiles.restore(instance.machines)
    assert client.get("/api/health").json()["planning_profiles_error"] is not None


def test_failed_profile_save_does_not_apply_or_invalidate_routes(api, history_directory):
    from . import main
    instance, client, headers, _ = api
    _, planned = initial_plan(client, headers)
    file_parent = history_directory / "not-a-directory"
    file_parent.write_text("occupied", encoding="utf-8")
    main.planning_profiles = PlanningProfileStore(file_parent / "machine_profiles.json")
    before = instance.machines["sweeper-alpha-01"].model_dump()
    assert update(client, headers, {"working_width_m": 1.9}).status_code == 503
    assert instance.machines["sweeper-alpha-01"].model_dump() == before
    assert instance.routes_by_id[planned["route_id"]].status == RouteLifecycleStatus.APPROVED
    assert client.get("/api/health").json()["planning_profiles_error"] is not None


def test_profile_api_rejects_nonfinite_values(api):
    _, client, headers, _ = api
    response = client.post("/api/machines/sweeper-alpha-01/planning-profile", headers={
        **headers, "Content-Type": "application/json"}, content='{"parameters":{"working_width_m":NaN}}')
    assert response.status_code == 422


def test_site_import_api_preserves_explicit_geometry_and_rejects_invalid_or_oversized_input(api):
    _, client, headers, _ = api
    data = json.loads((Path(__file__).parent / "examples" / "illustrative_site.geojson").read_text())
    result = client.post("/api/planning/site/import", headers=headers, json={"geojson": data})
    assert result.status_code == 200, result.text
    assert result.json()["source"] == "OPERATOR_PROVIDED_GEOJSON"
    assert result.json()["plan_fields"]["environment_map"]["require_verified_terrain"]
    assert result.json()["warnings"]
    assert client.post("/api/planning/site/import", headers=headers, json={"geojson": {}}).status_code == 422
    data["oversized"] = "x" * (2 * 1024 * 1024)
    assert client.post("/api/planning/site/import", headers=headers, json={"geojson": data}).status_code == 413
