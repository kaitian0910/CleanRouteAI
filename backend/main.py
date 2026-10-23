from __future__ import annotations

import secrets
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from .config import CompatBaseModel as BaseModel, settings
from .execution import ALLOWED_TRANSITIONS, InvalidStateTransitionError, SafetyInterlockError
from .planning_profiles import PlanningProfileStore, validate_parameters
from .site_planning import import_site_geojson
from .models import (
    EnvironmentMapData,
    ExecutionState,
    OperationalEventRequest,
    OperatorRole,
    PlanRequest,
    RouteCandidate,
    RouteLifecycleStatus,
    RLPolicyStatus,
    TaskItem,
    TelemetryFrame,
)
from .orchestrator import AgenticWorkflowOrchestrator, StaleRouteVersionError
from .planners import DefaultCampusEnvironmentProvider, RoutingServiceUnavailableError
from .validators import LocationValidationError
from .vision import DetectionResponse, InvalidImageError, MAX_IMAGE_BYTES, VisionUnavailableError, waste_detector

app = FastAPI(
    title=settings.app_name,
    version="1.3.0",
    description="Local autonomous route planning API with road-aware coverage and automatic failure recovery. Hardware control is disabled.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

orchestrator = AgenticWorkflowOrchestrator(history_path=Path(__file__).parent / "runtime" / "mission_history.json")
planning_profiles = PlanningProfileStore(Path(__file__).parent / "runtime" / "machine_profiles.json")
planning_profiles.restore(orchestrator.machines)
bearer_scheme = HTTPBearer(auto_error=False)


def get_operator_role(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> OperatorRole:
    if credentials is None or not credentials.credentials:
        if settings.environment == "development":
            return OperatorRole.OPERATOR
        raise HTTPException(status_code=401, detail="Bearer token required")
    tokens = {
        OperatorRole.OPERATOR: settings.operator_token,
        OperatorRole.SUPERVISOR: settings.supervisor_token,
        OperatorRole.ADMIN: settings.admin_token,
        OperatorRole.INTERNAL_SERVICE: settings.internal_service_token,
    }
    for role, token in tokens.items():
        if token and secrets.compare_digest(credentials.credentials, token):
            return role
    if settings.environment == "development" and credentials.credentials in (
        "dev-operator-token",
        "test-operator-token",
    ):
        return OperatorRole.OPERATOR
    if not any(tokens.values()):
        raise HTTPException(
            status_code=503,
            detail="Configure CLEANROUTEAI_OPERATOR_TOKEN before using operational APIs",
        )
    raise HTTPException(status_code=403, detail="Invalid bearer token")


def require_human_role(role: OperatorRole) -> None:
    if role not in (
        OperatorRole.OPERATOR,
        OperatorRole.SUPERVISOR,
        OperatorRole.ADMIN,
    ):
        raise HTTPException(
            status_code=403, detail="A human operator role is required"
        )


class ExecuteRequest(BaseModel):
    stop_after_segments: Optional[int] = Field(default=None, ge=1)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=200)
    expected_completed_segments: Optional[int] = Field(default=None, ge=0)


class RLDemoRequest(BaseModel):
    use_rl: bool = True
    seed: int = Field(default=42, ge=0, le=100000)


class RepeatJobRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)


@app.get("/api/rl/status", response_model=RLPolicyStatus)
def rl_status(role: OperatorRole = Depends(get_operator_role)) -> RLPolicyStatus:
    require_human_role(role)
    return RLPolicyStatus.model_validate(orchestrator.rl_policy.status())


@app.post("/api/rl/demo", response_model=RouteCandidate)
def rl_demo(body: RLDemoRequest, role: OperatorRole = Depends(get_operator_role)) -> RouteCandidate:
    require_human_role(role)
    from .rl_scenarios import six_bin_scenario
    # The same operational planner/evaluator is used. This creates a proposal
    # only: there is no approval, execution, or model update in this endpoint.
    return plan_route(six_bin_scenario(body.seed, body.use_rl), role)


@app.get("/api/vision/status")
def vision_status(role: OperatorRole = Depends(get_operator_role)) -> Dict[str, Any]:
    require_human_role(role)
    return waste_detector.status()


@app.post("/api/vision/detect", response_model=DetectionResponse)
def detect_waste(
    image: UploadFile = File(...),
    confidence: float = Query(default=0.25, ge=0.05, le=1.0),
    role: OperatorRole = Depends(get_operator_role),
) -> DetectionResponse:
    require_human_role(role)
    payload = image.file.read(MAX_IMAGE_BYTES + 1)
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds the 10 MiB limit.")
    try:
        return waste_detector.detect(payload, confidence)
    except InvalidImageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VisionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/environment/features", response_model=EnvironmentMapData)
def get_environment_features() -> EnvironmentMapData:
    """Return bundled OSM water exclusions and illustrative non-water reference layers."""
    return DefaultCampusEnvironmentProvider.get_default_environment_map()


@app.post("/api/tasks", response_model=TaskItem)
def create_task(
    task: TaskItem, role: OperatorRole = Depends(get_operator_role)
) -> TaskItem:
    require_human_role(role)
    cached = orchestrator.audit.check_idempotency(task.idempotency_key)
    if cached:
        return cached
    orchestrator.audit.save_idempotency(task.idempotency_key, task)
    orchestrator.audit.record(
        "global-tasks", role.value, "CREATE_TASK", f"Created task {task.task_id}"
    )
    return task


@app.post("/api/routes/plan", response_model=RouteCandidate)
def plan_route(
    req: PlanRequest, role: OperatorRole = Depends(get_operator_role)
) -> RouteCandidate:
    require_human_role(role)
    try:
        return orchestrator.plan_and_optimize(req, actor=role.value)
    except LocationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RoutingServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SafetyInterlockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def find_route(route_id: str) -> RouteCandidate:
    route = orchestrator.routes_by_id.get(route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return route


@app.get("/api/routes/{route_id}", response_model=RouteCandidate)
def get_route(
    route_id: str, _role: OperatorRole = Depends(get_operator_role)
) -> RouteCandidate:
    return orchestrator.refresh_route_evaluation(find_route(route_id))


@app.get("/api/routes/{route_id}/metrics")
def get_route_metrics(
    route_id: str, _role: OperatorRole = Depends(get_operator_role)
) -> Dict[str, Any]:
    route = find_route(route_id)
    frames = orchestrator.simulator.telemetry_store.get(route_id, [])
    orchestrator.refresh_route_evaluation(route)
    audit = [entry for entry in orchestrator.audit._audit_log if entry.job_id == route.job_id]

    def summary(item):
        evaluation = item.kpi_evaluation
        coverage = evaluation.metrics.get("coverage_percent") if evaluation else None
        return {
            "route_id": item.route_id, "version": item.route_version,
            "planning_run_id": item.planning_run_id, "candidate_index": item.candidate_index,
            "is_selected_candidate": item.is_selected_candidate,
            "status": item.status.value, "passed": bool(evaluation and evaluation.passed),
            "score": evaluation.score if evaluation else 0.0,
            "coverage_percent": coverage.actual if coverage else None,
            "total_distance_m": round(item.total_distance_m, 1),
            "estimated_duration_minutes": round(item.estimated_duration_minutes + item.mission_elapsed_min, 1),
            "sweep_angle_deg": item.sweep_angle_deg, "overlap_ratio": item.overlap_ratio,
            "failed_constraints": evaluation.failed_constraints if evaluation else [],
            "reason": item.replan_reason or ("Repeated completed area" if item.repeat_of_route_id else "Initial mission plan"),
            "options_checked": len(item.rl_feedback), "created_at": item.created_at,
            "approved_by": item.approved_by,
            "executed": bool(orchestrator.simulator.telemetry_store.get(item.route_id)),
        }

    options = orchestrator.job_route_history.get(route.job_id, [])
    return {
        "route_id": route.route_id,
        "job_id": route.job_id,
        "route_version": route.route_version,
        "status": route.status.value,
        "execution_state": orchestrator.state_machine.get_state(
            route.job_id
        ).value,
        "planning_diagnostics": route.planning_diagnostics,
        "kpi_evaluation": route.kpi_evaluation,
        "telemetry_count": len(frames),
        "telemetry_frames": frames[-20:],
        "audit_log": [entry for entry in audit if not entry.action.startswith("STATE:")
                      and entry.action not in {"CANDIDATE_EVALUATED", "RL_CANDIDATE"}][-50:],
        "technical_audit_log": audit[-200:],
        "history_versions": [summary(item) for item in options if item.is_selected_candidate],
        "candidate_options": [summary(item) for item in options],
    }


@app.get("/api/routes/{route_id}/feedback")
def get_route_feedback(route_id: str, role: OperatorRole = Depends(get_operator_role)) -> Dict[str, Any]:
    require_human_role(role)
    route = find_route(route_id)
    orchestrator.refresh_route_evaluation(route)
    frames = orchestrator.simulator.telemetry_store.get(route_id, [])
    execution_feedback = []
    if frames and route.kpi_evaluation:
        from .rl_feedback import evaluate_feedback
        record = evaluate_feedback(route, source="SIMULATED_EXECUTION")
        execution_feedback.append({**record.model_dump(mode="json"),
            "decision": orchestrator.state_machine.get_state(route.job_id).value,
            "reason": "Recorded simulated execution; reward is diagnostic and no weights were updated.",
            "mission_elapsed_min": frames[-1].mission_elapsed_min,
            "battery_percent": frames[-1].battery_percent,
            "payload_kg": frames[-1].payload_kg,
            "completed_tasks": sum(task.completed for task in route.tasks),
            "completed_segments": sum(segment.completed for segment in route.segments),
        })
    return {"policy": route.rl_policy_status,
            "candidates": route.rl_feedback, "execution_feedback": execution_feedback,
            "trigger": route.replan_reason or "INITIAL_PLAN",
            "state": orchestrator.state_machine.get_state(route.job_id).value}


@app.get("/api/feedback/history")
def get_mission_history(machine_id: Optional[str] = None, role: OperatorRole = Depends(get_operator_role)) -> Dict[str, Any]:
    require_human_role(role)
    history = orchestrator.mission_history
    return {"records": history.records(machine_id), "detail": history.last_error or
            "Locally saved feasible plans and simulated completion times. Compare matching workload fingerprints only. These records do not update PPO weights."}


@app.post("/api/routes/{route_id}/cancel", response_model=RouteCandidate)
def cancel_route(route_id: str, role: OperatorRole = Depends(get_operator_role)) -> RouteCandidate:
    require_human_role(role)
    try:
        return orchestrator.cancel_route(route_id, actor=role.value, role=role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (InvalidStateTransitionError, SafetyInterlockError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/routes/{route_id}/approve", response_model=RouteCandidate)
def approve_route(
    route_id: str, role: OperatorRole = Depends(get_operator_role)
) -> RouteCandidate:
    require_human_role(role)
    try:
        return orchestrator.approve_route(route_id, actor=role.value, role=role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (StaleRouteVersionError, SafetyInterlockError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _advance_route_request(route_id: str, body: Optional[ExecuteRequest], role: OperatorRole, action: str):
    simulator = orchestrator.simulator
    with simulator.execution_request_lock:
        route = find_route(route_id)
        machine = orchestrator.machines[route.machine_id]
        limit = body.stop_after_segments if body else None
        expected = body.expected_completed_segments if body else None
        key = (route_id, body.idempotency_key) if body and body.idempotency_key else None
        signature = (action, limit, expected)
        if key is not None and key in simulator.execution_request_cache:
            saved_signature, saved_response = simulator.execution_request_cache[key]
            if signature != saved_signature:
                raise HTTPException(status_code=409, detail="Execution idempotency key was reused with different request parameters.")
            return deepcopy(saved_response)
        completed = sum(segment.completed for segment in route.segments)
        if expected is not None and expected != completed:
            raise HTTPException(status_code=409, detail=f"Execution progress changed: expected {expected} completed segments, found {completed}. Refresh the route before continuing.")
        try:
            if action == "resume":
                if route.status.value != "EXECUTING" or orchestrator.state_machine.get_state(route.job_id) not in {ExecutionState.PAUSED, ExecutionState.EXECUTING}:
                    raise SafetyInterlockError("Resume requires a paused or already executing route; start an approved route with execute.")
                orchestrator.state_machine.transition(route.job_id, ExecutionState.EXECUTING, role.value,
                    "Operator resumed paused operation", role=role,
                    mandatory_constraints_passed=bool(route.kpi_evaluation and route.kpi_evaluation.passed))
            frames = simulator.execute_simulation(route, machine, stop_after_segments=limit)
            orchestrator.refresh_route_evaluation(route)
        except (InvalidStateTransitionError, SafetyInterlockError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        state = orchestrator.state_machine.get_state(route.job_id).value
        response = ({"route_id": route_id, "state": state,
                     "total_telemetry_frames": len(frames), "provenance": "SIMULATED"}
                    if action == "resume" else
                    {"route_id": route_id, "status": route.status.value, "execution_state": state,
                     "telemetry_frames_generated": len(frames),
                     "remaining_battery_percent": machine.current_battery_percent, "provenance": "SIMULATED"})
        if key is not None:
            simulator.execution_request_cache[key] = (signature, deepcopy(response))
            # Retain recent retries without unbounded per-route/global growth.
            route_keys = [saved_key for saved_key in simulator.execution_request_cache if saved_key[0] == route_id]
            for expired in route_keys[:-128]:
                simulator.execution_request_cache.pop(expired, None)
            while len(simulator.execution_request_cache) > 1024:
                simulator.execution_request_cache.popitem(last=False)
        return response


@app.post("/api/routes/{route_id}/execute")
def execute_route(route_id: str, body: Optional[ExecuteRequest] = None,
                  role: OperatorRole = Depends(get_operator_role)) -> Dict[str, Any]:
    require_human_role(role)
    return _advance_route_request(route_id, body, role, "execute")


@app.post("/api/routes/{route_id}/pause")
def pause_route(
    route_id: str, role: OperatorRole = Depends(get_operator_role)
) -> Dict[str, str]:
    require_human_role(role)
    route = find_route(route_id)
    if route.status.value != "EXECUTING":
        raise HTTPException(
            status_code=409,
            detail="Pause is only available for a route in execution",
        )
    try:
        state = orchestrator.state_machine.transition(
            route.job_id,
            ExecutionState.PAUSED,
            role.value,
            "Operator requested safe pause",
            role=role,
        )
        orchestrator.audit.record(route.job_id, role.value, "PAUSE_JOB",
            "Operator paused the simulation at a completed segment boundary.",
            route_id=route.route_id, route_version=route.route_version)
        return {"route_id": route_id, "state": state.value}
    except (InvalidStateTransitionError, SafetyInterlockError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/routes/{route_id}/repeat", response_model=RouteCandidate)
def repeat_route(route_id: str, body: RepeatJobRequest,
                 role: OperatorRole = Depends(get_operator_role)) -> RouteCandidate:
    require_human_role(role)
    try:
        return orchestrator.repeat_completed_route(route_id, body.idempotency_key, actor=role.value)
    except KeyError:
        raise HTTPException(status_code=404, detail="Route not found") from None
    except LocationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RoutingServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SafetyInterlockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/routes/{route_id}/resume")
def resume_route(
    route_id: str,
    body: Optional[ExecuteRequest] = None,
    role: OperatorRole = Depends(get_operator_role),
) -> Dict[str, Any]:
    require_human_role(role)
    return _advance_route_request(route_id, body, role, "resume")


@app.post("/api/routes/{route_id}/replan")
def replan_route(
    route_id: str,
    event: OperationalEventRequest,
    role: OperatorRole = Depends(get_operator_role),
) -> Dict[str, Any]:
    require_human_role(role)
    event.route_id = route_id
    try:
        return orchestrator.handle_operational_event(
            event, actor=role.value, role=role
        )
    except RoutingServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (
        SafetyInterlockError,
        LocationValidationError,
        StaleRouteVersionError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/events")
def submit_event(
    event: OperationalEventRequest,
    role: OperatorRole = Depends(get_operator_role),
) -> Dict[str, Any]:
    require_human_role(role)
    try:
        return orchestrator.handle_operational_event(
            event, actor=role.value, role=role
        )
    except RoutingServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (
        SafetyInterlockError,
        LocationValidationError,
        StaleRouteVersionError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/safety/reset-estop")
def reset_emergency_stop(
    _role: OperatorRole = Depends(get_operator_role),
) -> None:
    raise HTTPException(
        status_code=501,
        detail="E-stop reset requires a configured trusted machine adapter",
    )


@app.post("/api/telemetry")
def ingest_telemetry(
    frame: TelemetryFrame, role: OperatorRole = Depends(get_operator_role)
) -> Dict[str, Any]:
    if role != OperatorRole.INTERNAL_SERVICE:
        raise HTTPException(
            status_code=403,
            detail="Telemetry ingestion requires an internal service token",
        )
    if frame.route_id not in orchestrator.routes_by_id:
        raise HTTPException(
            status_code=404, detail="Telemetry route not found"
        )
    route = orchestrator.routes_by_id[frame.route_id]
    if orchestrator._latest_route_by_job.get(route.job_id) != frame.route_id:
        raise HTTPException(
            status_code=409,
            detail="Telemetry references a stale route version",
        )
    if frame.machine_id != route.machine_id:
        raise HTTPException(
            status_code=409,
            detail="Telemetry machine does not match the route machine",
        )
    frames = orchestrator.simulator.telemetry_store.setdefault(
        frame.route_id, []
    )
    if any(existing.telemetry_id == frame.telemetry_id for existing in frames):
        return {
            "ingested": frame.telemetry_id,
            "duplicate": True,
            "event_result": None,
        }
    if frames and frame.mission_elapsed_min < frames[-1].mission_elapsed_min:
        if frame.mission_elapsed_min == 0.0:
            frame.mission_elapsed_min = frames[-1].mission_elapsed_min
        else:
            raise HTTPException(
                status_code=409,
                detail="Telemetry mission time moved backwards",
            )
    frames.append(frame)
    machine = orchestrator.machines[route.machine_id]
    machine.current_battery_percent = frame.battery_percent
    machine.current_payload_kg = frame.payload_kg
    machine.current_payload_volume_m3 = frame.payload_volume_m3
    event_result = None
    state = orchestrator.state_machine.get_state(route.job_id)
    should_replan_deviation = (
        frame.route_deviation_m > settings.kpi_policy.max_route_deviation_m
        and state
        in (
            ExecutionState.AWAITING_APPROVAL,
            ExecutionState.EXECUTING,
            ExecutionState.PAUSED,
            ExecutionState.READY,
        )
    )
    should_replan_low_battery = (
        frame.battery_percent < settings.kpi_policy.min_energy_reserve_percent
        and state
        in (
            ExecutionState.AWAITING_APPROVAL,
            ExecutionState.EXECUTING,
            ExecutionState.PAUSED,
            ExecutionState.READY,
        )
    )
    if (
        frame.emergency_stop_engaged
        or frame.sensor_fault
        or frame.obstacle_blocked
        or should_replan_deviation
        or should_replan_low_battery
    ):
        kind = (
            "EMERGENCY_STOP"
            if frame.emergency_stop_engaged
            else "SENSOR_FAULT"
            if frame.sensor_fault
            else "OBSTACLE_DETECTED"
            if frame.obstacle_blocked
            else "HIGH_ROUTE_DEVIATION"
            if should_replan_deviation
            else "LOW_BATTERY"
        )
        event_result = orchestrator.handle_operational_event(
            OperationalEventRequest(
                job_id=route.job_id,
                route_id=route.route_id,
                event_type=kind,
                location=frame.position if should_replan_deviation else None,
                battery_percent=(
                    frame.battery_percent if should_replan_low_battery else None
                ),
                idempotency_key=f"telemetry:{frame.telemetry_id}",
            ),
            actor=role.value,
            role=role,
        )
    else:
        orchestrator.refresh_route_evaluation(route)
    return {"ingested": frame.telemetry_id, "event_result": event_result}


@app.get("/api/health")
def health_check() -> Dict[str, Any]:
    return {
        "status": "healthy",
        "valhalla_configured_url": settings.valhalla_url,
        "labeled_demo_routing_enabled": settings.allow_labeled_demo_routing,
        "hardware_ros2_actuation_enabled": settings.enable_hardware_ros2_adapter,
        "simulation_only": True,
        "geocoding_configured": False,
        "kpi_policy_version": settings.kpi_policy.policy_version,
        "planning_profiles_error": planning_profiles.last_error,
    }


@app.websocket("/ws/telemetry/{route_id}")
async def telemetry_websocket(websocket: WebSocket, route_id: str) -> None:
    token = websocket.query_params.get("token", "")
    if not settings.operator_token or not secrets.compare_digest(
        token, settings.operator_token
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        for frame in orchestrator.simulator.telemetry_store.get(route_id, []):
            await websocket.send_json(frame.model_dump(mode="json"))
    except WebSocketDisconnect:
        pass
    finally:
        await websocket.close()


class SiteImportRequest(BaseModel):
    geojson: Dict[str, Any]


class PlanningProfileRequest(BaseModel):
    parameters: Dict[str, Any]


@app.get("/api/machines")
def get_machines(role: OperatorRole = Depends(get_operator_role)):
    require_human_role(role)
    return [machine.model_dump(mode="json") for machine in orchestrator.machines.values()]


@app.post("/api/planning/site/import")
def import_planning_site(body: SiteImportRequest, role: OperatorRole = Depends(get_operator_role)):
    require_human_role(role)
    try:
        encoded = json.dumps(body.geojson, allow_nan=False).encode("utf-8")
        if len(encoded) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Site GeoJSON exceeds the 2 MiB limit.")
        return import_site_geojson(body.geojson).model_dump(mode="json")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/machines/{machine_id}/planning-profile")
def update_planning_profile(machine_id: str, body: PlanningProfileRequest,
                            role: OperatorRole = Depends(get_operator_role)):
    require_human_role(role)
    with orchestrator.simulator.execution_request_lock:
        machine = orchestrator.machines.get(machine_id)
        if machine is None:
            raise HTTPException(status_code=404, detail="Machine not found.")
        machine_routes = [route for route in orchestrator.routes_by_id.values() if route.machine_id == machine_id]
        if any(orchestrator.state_machine.get_state(route.job_id) == ExecutionState.EXECUTING for route in machine_routes):
            raise HTTPException(status_code=409, detail="Pause all jobs using this machine before changing planning parameters.")
        try:
            updated = validate_parameters(machine, body.parameters)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if updated.model_dump() == machine.model_dump():
            return {"machine": machine.model_dump(mode="json"), "invalidated_route_ids": [],
                    "detail": "Planning parameters are unchanged."}
        updated.planning_revision = machine.planning_revision + 1
        try:
            planning_profiles.save(updated)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        orchestrator.machines[machine_id] = updated
        invalidated = []
        jobs = set()
        for route in machine_routes:
            if route.status in {RouteLifecycleStatus.COMPLETED, RouteLifecycleStatus.ABORTED, RouteLifecycleStatus.SUPERSEDED}:
                continue
            route.status = RouteLifecycleStatus.SUPERSEDED
            route.approved_by = None
            invalidated.append(route.route_id)
            jobs.add(route.job_id)
        for job_id in jobs:
            state = orchestrator.state_machine.get_state(job_id)
            if ExecutionState.NEEDS_ASSISTANCE in ALLOWED_TRANSITIONS.get(state, set()):
                orchestrator.state_machine.transition(job_id, ExecutionState.NEEDS_ASSISTANCE, role.value,
                    "Machine planning parameters changed; explicitly replan and approve before continuing", role=role)
        orchestrator.audit.record(job_id=f"machine:{machine_id}", actor=role.value,
            action="MACHINE_PLANNING_PROFILE_UPDATED", reason="Local simulation planning parameters changed; affected plans require replanning.",
            details={"planning_revision": updated.planning_revision, "parameters": body.parameters,
                     "invalidated_route_ids": invalidated})
        return {"machine": updated.model_dump(mode="json"), "invalidated_route_ids": invalidated,
                "detail": "Planning parameters saved locally. Replan and approve affected jobs before continuing; physical machine settings were not changed."}
