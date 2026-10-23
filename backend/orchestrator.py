from __future__ import annotations

from copy import deepcopy
from threading import RLock
from time import monotonic
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .config import KPIPolicyConfig, settings
from .evaluator import KPIRouteQualityEvaluator
from .rl_feedback import evaluate_feedback
from .rl_policy import G3Policy
from .mission_history import MissionHistory, layout_fingerprint, sweep_context_fingerprint
from .repeat_planning import compare_repeat_plan
from .execution import (
    DeterministicRouteSimulator,
    ExecutionStateMachine,
    InvalidStateTransitionError,
    SafetyInterlockError,
)
from .models import (
    AuditAndEventManager,
    Coordinate,
    DataProvenance,
    EnvironmentMapData,
    ExecutionState,
    MachineProfile,
    OperationalEventRequest,
    OperatorRole,
    PlanningDiagnostics,
    PlanRequest,
    RecoveryAttemptRecord,
    RLPolicyStatus,
    ReplanComparison,
    RouteCandidate,
    RouteLifecycleStatus,
    RouteSegment,
    TaskItem,
    TaskType,
    TerrainFeatureType,
    UnreachableSection,
)
from .planners import (
    CoveragePlanner,
    DefaultCampusEnvironmentProvider,
    RouteOptimizationEngine,
    RoutingServiceUnavailableError,
    TraversabilityGraph,
    ValhallaRoutingService,
)
from .validators import (
    LocationValidationError,
    MachineCapabilityValidator,
    RequestInterpreter,
    TaskAndLocationValidator,
)


class StaleRouteVersionError(Exception):
    pass


class AgenticWorkflowOrchestrator:
    """Coordinates deterministic road/terrain-aware planning, bounded automatic failure recovery,
    independent KPI/safety evaluation, and execution approval.
    """

    def __init__(self, history_path=None) -> None:
        self.mission_history = MissionHistory(history_path)
        self.audit = AuditAndEventManager()
        self.state_machine = ExecutionStateMachine(self.audit)
        self.simulator = DeterministicRouteSimulator(self.state_machine, self.audit)
        self.interpreter = RequestInterpreter()
        self.request_validator = TaskAndLocationValidator()
        self.capability_validator = MachineCapabilityValidator()
        self.coverage_planner = CoveragePlanner()
        self.routing_engine = ValhallaRoutingService()
        self.route_optimizer = RouteOptimizationEngine()
        self.evaluator = KPIRouteQualityEvaluator()
        self.rl_policy = G3Policy()
        self.machines: Dict[str, MachineProfile] = {
            "sweeper-alpha-01": MachineProfile(
                machine_id="sweeper-alpha-01",
                name="Compact Autonomous Sweeper Alpha-01",
                max_payload_kg=150.0,
                max_item_length_m=1.2,
                allowed_surface_classes=[
                    "PEDESTRIAN_PLAZA",
                    "PEDESTRIAN_WALKWAY",
                    "SERVICE_ROAD",
                ],
                allow_public_road_sweeping=False,
                allow_public_road_transit=False,
                allow_authorized_road_crossing=True,
                requires_heavy_rated_crossing=False,
                road_clearance_buffer_m=1.0,
            ),
            "heavy-utility-02": MachineProfile(
                machine_id="heavy-utility-02",
                name="Heavy Municipal Collector 02",
                max_payload_kg=450.0,
                max_payload_volume_m3=4.0,
                max_item_length_m=2.5,
                has_heavy_lifter=True,
                has_bulky_compactor=True,
                allowed_surface_classes=[
                    "PEDESTRIAN_PLAZA",
                    "SERVICE_ROAD",
                ],
                allow_public_road_sweeping=False,
                allow_public_road_transit=False,
                allow_authorized_road_crossing=True,
                requires_heavy_rated_crossing=True,
                road_clearance_buffer_m=1.5,
            ),
        }
        self.routes_by_id: Dict[str, RouteCandidate] = {}
        self.job_route_history: Dict[str, List[RouteCandidate]] = {}
        self._latest_route_by_job: Dict[str, str] = {}
        self._completed_evaluation_ids: Set[str] = set()
        self._planning_guard = RLock()
        self._planning_jobs: Set[str] = set()
        self.simulator.latest_route_by_job = self._latest_route_by_job
        self.simulator.on_complete = self._record_completed_simulation

    def _record_completed_simulation(self, route: RouteCandidate, elapsed: float) -> None:
        # Capture the final result while the machine still has this run's
        # finishing resources. A later job must not rewrite historical results.
        self.refresh_route_evaluation(route)
        self.mission_history.record_completion(route, elapsed)
        self.audit.record(route.job_id, "simulator", "COMPLETE_JOB",
            "Cleaning run finished in simulation; completed work and timing were recorded.",
            route_id=route.route_id, route_version=route.route_version,
            details={"simulated_completion_minutes": elapsed})

    def repeat_completed_route(self, route_id: str, idempotency_key: str, actor: str = "operator") -> RouteCandidate:
        """Create another reviewed job without resurrecting collected one-off waste."""
        if not idempotency_key or len(idempotency_key) > 200:
            raise LocationValidationError("Provide a repeat request key of 1 to 200 characters.")
        with self.simulator.execution_request_lock:
            key = f"repeat:{route_id}:{idempotency_key}"
            cached = self.audit.check_idempotency(key)
            if cached is not None:
                return deepcopy(cached)
            source = self.routes_by_id.get(route_id)
            if source is None:
                raise KeyError(route_id)
            if source.status != RouteLifecycleStatus.COMPLETED or self.state_machine.get_state(source.job_id) != ExecutionState.COMPLETED:
                raise SafetyInterlockError("Only a completed job can be repeated. Finish or safely resolve the current job first.")
            if source.machine_id not in self.machines:
                raise LocationValidationError("The source machine is no longer available.")
            if any(route.machine_id == source.machine_id and self.state_machine.get_state(route.job_id)
                   in {ExecutionState.EXECUTING, ExecutionState.EMERGENCY_STOP} for route in self.routes_by_id.values()):
                raise SafetyInterlockError("This machine has an executing or emergency-stopped job. Resolve it before repeating work.")
            tasks = []
            excluded_one_off = 0
            for previous in source.tasks:
                routine_bin = (previous.task_type == TaskType.RUBBISH_COLLECTION
                    and previous.waste_observation is None
                    and not (previous.rubbish_spec and previous.rubbish_spec.is_oversized))
                if not routine_bin and previous.completed:
                    excluded_one_off += 1
                    continue
                fresh = previous.model_copy(deep=True)
                fresh.task_id = f"task-{uuid4().hex[:8]}"
                fresh.idempotency_key = f"repeat-task-{uuid4().hex}"
                fresh.completed = False
                # Availability windows remain explicit operator constraints;
                # the new mission clock restarts without weakening them.
                tasks.append(fresh)
            if source.operating_boundary is None and not tasks:
                raise SafetyInterlockError("No recurring work remains. Collected litter and bulky items need a new observation before another collection job.")
            frames = [frames[-1] for frames in self.simulator.telemetry_store.values()
                      if frames and frames[-1].machine_id == source.machine_id]
            position = max(frames, key=lambda frame: frame.timestamp).position if frames else source.disposal_depot
            policy = KPIPolicyConfig.model_validate(source.kpi_policy_snapshot or settings.kpi_policy.model_dump())
            policy_values = policy.model_dump()
            policy_values.pop("policy_version", None)
            request = PlanRequest(
                machine_id=source.machine_id, use_rl=source.use_rl,
                enable_depot_cycles=source.depot_cycles_enabled,
                start_location=position.model_copy(deep=True),
                disposal_location=source.disposal_depot.model_copy(deep=True),
                operating_boundary=source.operating_boundary.model_copy(deep=True) if source.operating_boundary else None,
                exclusion_zones=[zone.model_copy(deep=True) for zone in source.exclusion_zones],
                environment_map=(source.planning_environment_map.model_copy(deep=True)
                    if source.planning_environment_map else EnvironmentMapData(
                        map_id="repeat-preserved-environment",
                        features=[feature.model_copy(deep=True) for feature in source.environment_features_applied])),
                tasks=tasks, kpi_policy_override=policy_values,
                natural_language_command="Patrol the selected area" if source.task_mode == TaskType.PATROL else None,
                mission_elapsed_min=0.0, preserve_completed=False,
            )
            repeated = self.plan_and_optimize(request, actor=actor)
            repeated.repeat_of_route_id = source.route_id
            source_frames = self.simulator.telemetry_store.get(source.route_id, [])
            repeated.repeat_comparison = compare_repeat_plan(source, repeated,
                source_frames[-1].mission_elapsed_min if source_frames else None)
            repeated.candidate_rationale.append(
                f"Repeated completed job {source.route_id} as fresh work using current machine resources. "
                f"Excluded {excluded_one_off} completed one-off litter/bulky task(s); approval is required again."
            )
            self.audit.save_idempotency(key, deepcopy(repeated))
            self.audit.record(request.job_id, actor, "REPEAT_COMPLETED_JOB",
                "Created a new reviewed job; prior completed work and telemetry were retained.",
                route_id=repeated.route_id, route_version=repeated.route_version,
                details={"repeat_of_route_id": source.route_id, "excluded_completed_one_off_tasks": excluded_one_off})
            return repeated

    def _resolve_environment_map(self, req: PlanRequest) -> Optional[EnvironmentMapData]:
        if req.environment_map is not None:
            if settings.enable_default_campus_terrain_layer:
                from .water_map import merge_mapped_water
                return merge_mapped_water(req.environment_map)
            return req.environment_map
        if settings.enable_default_campus_terrain_layer:
            return DefaultCampusEnvironmentProvider.get_default_environment_map()
        return None

    def _build_nav_graph_for_request(
        self,
        req: PlanRequest,
        machine: MachineProfile,
        env_map: Optional[EnvironmentMapData],
        use_authorized_crossings: bool = True,
    ) -> TraversabilityGraph:
        origin = (
            req.operating_boundary.vertices[0]
            if req.operating_boundary
            else (req.start_location or Coordinate(lat=3.13900, lon=101.68690))
        )
        obs_polys = [z.polygon.to_shapely_local(origin) for z in req.exclusion_zones]
        water_polys: List[Polygon] = []
        building_polys: List[Polygon] = []
        restricted_polys: List[Polygon] = []
        road_polys: List[Polygon] = []
        prohibited_surf_polys: List[Polygon] = []

        if env_map:
            for feat in env_map.features:
                fp = feat.polygon.to_shapely_local(origin)
                if feat.feature_type == TerrainFeatureType.WATER_BODY:
                    water_polys.append(fp.buffer(max(feat.clearance_buffer_m, 0.3)))
                elif feat.feature_type == TerrainFeatureType.BUILDING:
                    building_polys.append(fp.buffer(max(feat.clearance_buffer_m, 0.3)))
                elif feat.feature_type == TerrainFeatureType.RESTRICTED_ZONE:
                    restricted_polys.append(fp.buffer(max(feat.clearance_buffer_m, 0.2)))
                elif feat.feature_type == TerrainFeatureType.PUBLIC_ROAD:
                    if not machine.allow_public_road_transit:
                        road_polys.append(fp)
                elif feat.feature_type != TerrainFeatureType.UNVERIFIED_TERRAIN:
                    surf_ok = feat.feature_type.value in machine.allowed_surface_classes
                    mach_ok = (
                        feat.allowed_machine_ids is None
                        or machine.machine_id in feat.allowed_machine_ids
                    )
                    width_ok = machine.clearance_width_m <= feat.min_clearance_width_m
                    if not (surf_ok and mach_ok and width_ok):
                        prohibited_surf_polys.append(fp)

        hazard_union = (
            unary_union(obs_polys + water_polys + building_polys + restricted_polys)
            if (obs_polys or water_polys or building_polys or restricted_polys)
            else Polygon()
        )
        road_union = unary_union(road_polys) if road_polys else Polygon()
        surf_union = (
            unary_union(prohibited_surf_polys) if prohibited_surf_polys else Polygon()
        )

        if req.operating_boundary is not None:
            outer = req.operating_boundary.to_shapely_local(origin)
            free_nav = outer.difference(hazard_union).difference(road_union).difference(surf_union)
        else:
            free_nav = Polygon()

        return TraversabilityGraph(
            origin=origin,
            machine=machine,
            free_navigation_space=free_nav,
            impassable_hazard_union=hazard_union,
            prohibited_road_union=road_union,
            prohibited_surface_union=surf_union,
            environment_map=env_map,
            use_authorized_crossings=use_authorized_crossings,
            requested_boundary_poly=req.operating_boundary.to_shapely_local(origin) if req.operating_boundary else None,
        )

    def plan_and_optimize(
        self, req: PlanRequest, actor: str = "operator",
        completed_segments: Optional[List[RouteSegment]] = None,
        replan_reason: Optional[str] = None,
    ) -> RouteCandidate:
        # Only competing planners for the same job are rejected. The long
        # search never holds the execution lock, so E-Stop can interrupt it.
        with self._planning_guard:
            if req.job_id in self._planning_jobs:
                raise SafetyInterlockError("This job already has a planning request in progress. Wait for it to finish before replanning.")
            self._planning_jobs.add(req.job_id)
        try:
            return self._plan_and_optimize(req, actor, completed_segments, replan_reason)
        except InvalidStateTransitionError as exc:
            if self.state_machine.get_state(req.job_id) in (ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP):
                raise SafetyInterlockError("Planning was stopped or cancelled. No new route was published.") from exc
            raise
        finally:
            with self._planning_guard:
                self._planning_jobs.discard(req.job_id)

    def _plan_and_optimize(
        self,
        req: PlanRequest,
        actor: str = "operator",
        completed_segments: Optional[List[RouteSegment]] = None,
        replan_reason: Optional[str] = None,
    ) -> RouteCandidate:
        plan_key = (
            f"plan:{req.job_id}:{req.idempotency_key}"
            if req.idempotency_key
            else None
        )
        cached_plan = self.audit.check_idempotency(plan_key)
        if cached_plan is not None:
            return deepcopy(cached_plan)

        if req.preserve_completed:
            previous_id = self._latest_route_by_job.get(req.job_id)
            previous = self.routes_by_id.get(previous_id) if previous_id else None
            if previous is None:
                raise LocationValidationError(
                    "Cannot preserve progress: this job has no previous route."
                )
            if previous.machine_id != req.machine_id:
                raise LocationValidationError(
                    "Progress can only be preserved for the same machine. Start a new job to change machines."
                )
            # Trust recorded execution, not the browser's possibly stale robot
            # position or task flags. Keep the submitted request unmodified.
            req = req.model_copy(deep=True)
            if req.environment_map is None:
                req.environment_map = previous.planning_environment_map
            frames = self.simulator.telemetry_store.get(previous.route_id, [])
            req.start_location = frames[-1].position if frames else previous.origin
            req.mission_elapsed_min = (
                frames[-1].mission_elapsed_min if frames else previous.mission_elapsed_min
            )
            if req.disposal_location is None:
                req.disposal_location = previous.disposal_depot
            completed_tasks = {
                task.task_id: task for task in previous.tasks if task.completed
            }
            req.tasks = [
                deepcopy(completed_tasks.get(task.task_id, task)) for task in req.tasks
            ]
            for task in req.tasks:
                task.completed = task.task_id in completed_tasks
            requested_ids = {task.task_id for task in req.tasks}
            req.tasks.extend(
                deepcopy(task) for task_id, task in completed_tasks.items()
                if task_id not in requested_ids
            )
            if req.operating_boundary == previous.operating_boundary:
                completed_segments = previous.segments
            replan_reason = replan_reason or "operator_revision_preserving_progress"

        state = self.state_machine.get_state(req.job_id)
        if state == ExecutionState.EMERGENCY_STOP:
            raise SafetyInterlockError(
                "EMERGENCY_STOP is active. A supervisor must verify a hardware reset first."
            )
        if state in (
            ExecutionState.AWAITING_APPROVAL,
            ExecutionState.READY,
            ExecutionState.PAUSED,
            ExecutionState.EXECUTING,
            ExecutionState.REPLANNING,
        ):
            if state != ExecutionState.REPLANNING:
                self.state_machine.transition(
                    req.job_id,
                    ExecutionState.REPLANNING,
                    actor,
                    "Operator or auto-pilot initiated replanning",
                )
            state = self.state_machine.transition(
                req.job_id,
                ExecutionState.VALIDATING,
                actor,
                "Validate revised planning request",
            )
        if state not in (
            ExecutionState.IDLE,
            ExecutionState.COMPLETED,
            ExecutionState.ABORTED,
            ExecutionState.NEEDS_ASSISTANCE,
            ExecutionState.VALIDATING,
        ):
            raise SafetyInterlockError(
                f"Cannot start planning while job state is {state.value}."
            )

        if state != ExecutionState.VALIDATING:
            self.state_machine.transition(
                req.job_id,
                ExecutionState.VALIDATING,
                actor,
                "Validate planning request",
            )
        if req.machine_id not in self.machines:
            self.state_machine.transition(
                req.job_id,
                ExecutionState.NEEDS_ASSISTANCE,
                actor,
                "Unknown machine profile",
            )
            raise LocationValidationError(
                f"Unknown machine profile: {req.machine_id}"
            )
        intent = self.interpreter.parse(req.natural_language_command)
        if intent.emergency_override_attempted:
            self.state_machine.transition(
                req.job_id,
                ExecutionState.NEEDS_ASSISTANCE,
                actor,
                "Unsafe override language rejected",
            )
            raise SafetyInterlockError(
                "Natural-language instructions cannot override safety interlocks."
            )
        if intent.detected_oversized_item:
            for task in req.tasks:
                if (
                    task.task_type == TaskType.OVERSIZED_RUBBISH
                    and task.rubbish_spec is None
                ):
                    task.rubbish_spec = intent.detected_oversized_item
        env_map = self._resolve_environment_map(req)
        try:
            mode = self.request_validator.validate_plan_request(req, intent, environment_map=env_map)
        except LocationValidationError:
            self.state_machine.transition(
                req.job_id,
                ExecutionState.NEEDS_ASSISTANCE,
                actor,
                "Planning request validation failed",
            )
            raise
        machine = self.machines[req.machine_id]
        self.state_machine.register_job_machine(req.job_id, req.machine_id)
        self.state_machine.transition(
            req.job_id, ExecutionState.PLANNING, actor, "Generate route candidate"
        )

        policy = settings.kpi_policy
        if req.kpi_policy_override:
            try:
                policy = type(policy).model_validate({**policy.model_dump(), **req.kpi_policy_override})
            except Exception as exc:
                self.state_machine.transition(
                    req.job_id,
                    ExecutionState.NEEDS_ASSISTANCE,
                    actor,
                    "Invalid KPI policy override",
                )
                raise LocationValidationError(
                    f"Invalid KPI policy override: {exc}"
                ) from exc

        started = monotonic()
        history = self.job_route_history.setdefault(req.job_id, [])
        revision = max((item.route_version for item in history), default=0) + 1
        planning_run_id = f"plan-{uuid4().hex[:12]}"
        previous_latest_id = self._latest_route_by_job.get(req.job_id)
        previous_route = (
            self.routes_by_id.get(previous_latest_id)
            if previous_latest_id
            else None
        )
        best: Optional[RouteCandidate] = None
        final: Optional[RouteCandidate] = None
        angles = list(settings.optimization.candidate_sweep_angles_deg)
        fingerprint = layout_fingerprint(req, machine, policy, env_map, completed_segments=completed_segments)
        context_fingerprint = sweep_context_fingerprint(req, machine, policy, env_map, mode=mode)
        historical_angle = self.mission_history.preferred_angle(fingerprint, context_fingerprint) if req.enable_depot_cycles and mode in (
            TaskType.AREA_SWEEP, TaskType.HYBRID, TaskType.PATROL,
        ) else None
        if historical_angle in angles:
            angles.remove(historical_angle)
            angles.insert(0, historical_angle)
        optimize_completion = req.enable_depot_cycles and mode in (
            TaskType.AREA_SWEEP, TaskType.HYBRID, TaskType.PATROL,
        )
        evaluated_settings = set()

        # Bounded Plan-Diagnose-Replan-Evaluate Agentic Recovery Context
        recovery_ctx: Dict[str, Any] = {
            "route_version": revision,
            "planning_run_id": planning_run_id,
            "prune_unreachable_cells": False,
            "use_authorized_crossings": True,
            "snap_depot_to_portal": False,
            "reorder_cells": True,
            "prune_unreachable_tasks": False,
            "angle_idx": 0,
            "overlap_steps": 0,
        }
        recovery_history: List[RecoveryAttemptRecord] = []
        recovery_actions_applied: List[str] = []
        seen_failed_signatures: Set[Tuple[Any, ...]] = set()
        termination_override: Optional[str] = None

        try:
            recommendation = self.rl_policy.recommend(req, machine, completed_segments=completed_segments)
        except Exception as exc:
            recommendation = {"status": {"mode": "DETERMINISTIC_FALLBACK", "detail": f"Policy error: {type(exc).__name__}"}, "reason": "Policy inference failed; deterministic planning continues."}
        policy_status = RLPolicyStatus.model_validate(recommendation.get("status", {}))
        proposal = recommendation.get("proposed_action")
        # Validate again at the orchestration boundary; even a faulty adapter is advisory.
        if proposal and (proposal.get("preferred_task_id") not in {t.task_id for t in req.tasks if not t.completed}
                         or proposal.get("route_mode") != "DIRECT"
                         or type(proposal.get("action_index")) is not int
                         or proposal["action_index"] not in range(0, 12, 2)
                         or proposal["action_index"] // 2 >= len(req.tasks)
                         or req.tasks[proposal["action_index"] // 2].task_id != proposal.get("preferred_task_id")):
            proposal = None
            policy_status.mode = "DETERMINISTIC_FALLBACK"
            policy_status.detail = "Rejected invalid policy action; deterministic planning continues."
        if proposal:
            self.audit.record(req.job_id, actor, "RL_RECOMMENDATION", recommendation.get("reason", "Constrained PPO dispatch proposal"), details=proposal)
        traces = []
        baseline_route = None
        baseline_feedback = None

        def attach_feedback(selected):
            for record in traces:
                if record.route_id == selected.route_id:
                    record.decision = "SELECTED_AWAITING_APPROVAL" if record.feasible else "REJECTED_HARD_CONSTRAINT"
                elif record.feasible and record.decision == "ELIGIBLE":
                    record.decision = "NOT_SELECTED"
            selected.rl_policy_status = policy_status
            selected.rl_feedback = [record.model_copy(deep=True) for record in traces]
            selected.history_hint_applied = historical_angle in angles
            # Alternatives are recorded for inspection, but only the chosen
            # result becomes the job's current route after the search ends.
            self._publish_selected(selected)
            selected.candidate_rationale.append(policy_status.detail)
            if selected.depot_cycles_enabled:
                if selected.history_hint_applied:
                    selected.candidate_rationale.append("A sweep angle suggested by previous timing feedback was reevaluated as a warm start with current resources. This search hint does not reuse old feasibility results or update model weights.")
                self.mission_history.record_plan(selected, fingerprint, context_fingerprint)
            if plan_key:
                self.audit.save_idempotency(plan_key, deepcopy(selected))
            self.audit.record(req.job_id, actor, "PLAN_SELECTED", f"Selected option {selected.candidate_index} for route revision {selected.route_version}.", route_id=selected.route_id, route_version=selected.route_version,
                              details={"policy_mode": policy_status.mode, "candidate_count": len(traces)})
            return selected

        recovery_ctx["cycle_policy"] = policy
        for iteration in range(settings.optimization.max_iterations):
            if monotonic() - started > settings.optimization.timeout_seconds:
                break
            if self.state_machine.get_state(req.job_id) != ExecutionState.PLANNING:
                self.state_machine.transition(
                    req.job_id,
                    ExecutionState.PLANNING,
                    actor,
                    "Continue bounded plan-diagnose-replan-evaluate loop",
                )
            try:
                candidate = self._make_candidate(
                    req,
                    mode,
                    machine,
                    history,
                    iteration,
                    angles,
                    completed_segments=completed_segments,
                    replan_reason=replan_reason,
                    env_map=env_map,
                    recovery_ctx=recovery_ctx,
                )
            except RoutingServiceUnavailableError as exc:
                if optimize_completion and best is not None and best.kpi_evaluation.passed:
                    termination_override = "optional_comparison_routing_unavailable"
                    self.audit.record(req.job_id, actor, "COMPARISON_STOPPED",
                                      f"Retained independently evaluated feasible plan after {type(exc).__name__} in an optional comparison.")
                    break
                self.state_machine.transition(
                    req.job_id,
                    ExecutionState.NEEDS_ASSISTANCE,
                    actor,
                    "Required routing service unavailable",
                )
                raise
            except Exception as exc:
                if optimize_completion and best is not None and best.kpi_evaluation.passed:
                    termination_override = "optional_comparison_generation_failed"
                    self.audit.record(req.job_id, actor, "COMPARISON_STOPPED",
                                      f"Retained independently evaluated feasible plan after {type(exc).__name__} in an optional comparison.")
                    break
                self.state_machine.transition(
                    req.job_id,
                    ExecutionState.NEEDS_ASSISTANCE,
                    actor,
                    "Candidate generation failed",
                )
                raise

            self.state_machine.transition(
                req.job_id,
                ExecutionState.EVALUATING,
                actor,
                "Independently evaluate route geometry and KPIs",
            )
            try:
                candidate.kpi_policy_snapshot = policy.model_dump()
                candidate.kpi_evaluation = self.evaluator.evaluate(
                    candidate, machine, policy
                )
            except Exception as exc:
                if optimize_completion and best is not None and best.kpi_evaluation.passed:
                    termination_override = "optional_comparison_evaluation_failed"
                    self.audit.record(req.job_id, actor, "COMPARISON_STOPPED",
                                      f"Retained independently evaluated feasible plan after {type(exc).__name__} during optional evaluation.")
                    break
                self.state_machine.transition(
                    req.job_id,
                    ExecutionState.NEEDS_ASSISTANCE,
                    actor,
                    "KPI evaluation failed",
                )
                raise

            candidate.status = (
                RouteLifecycleStatus.PROPOSED
                if candidate.kpi_evaluation.passed
                else RouteLifecycleStatus.DRAFT_FAILED_KPI
            )
            candidate.candidate_rationale.append(
                f"KPI score {candidate.kpi_evaluation.score:.2f}; "
                + (
                    "all configured constraints passed."
                    if candidate.kpi_evaluation.passed
                    else "failed constraints: "
                    + ", ".join(candidate.kpi_evaluation.failed_constraints)
                    + "."
                )
            )
            candidate.optimization_termination_reason = (
                "acceptance_criteria_passed"
                if candidate.kpi_evaluation.passed
                else None
            )
            if previous_route is not None and replan_reason is not None:
                candidate.replan_comparison = self._compare_replan(
                    previous_route, candidate, machine.current_battery_percent
                )

            candidate.planning_diagnostics = self._build_planning_diagnostics(
                candidate=candidate,
                recovery_history=recovery_history,
                recovery_actions_applied=recovery_actions_applied,
            )

            source = "TRAINED_PPO" if recovery_ctx.get("preferred_task_id") else ("DETERMINISTIC_BASELINE" if iteration == 0 else ("COMPLETION_TIME_COMPARISON" if optimize_completion and best and best.kpi_evaluation.passed else "DETERMINISTIC_RECOVERY"))
            record = evaluate_feedback(candidate, source=source,
                                       recommendation=recommendation if source == "TRAINED_PPO" else None,
                                       baseline=baseline_feedback)
            traces.append(record)
            if iteration == 0:
                baseline_route, baseline_feedback = candidate, record
            candidate.rl_policy_status = policy_status
            candidate.rl_feedback = [item.model_copy(deep=True) for item in traces]
            self.audit.record(req.job_id, actor, "CANDIDATE_EVALUATED", record.reason,
                              route_id=candidate.route_id, route_version=candidate.route_version,
                              details={"source": source, "reward": record.reward, "feasible": record.feasible, "failed_constraints": record.failed_constraints})
            self._store_candidate(candidate)
            history.append(candidate)
            final = candidate
            evaluated_settings.add((recovery_ctx["angle_idx"] % max(1, len(angles)), recovery_ctx["overlap_steps"]))
            if optimize_completion:
                def rank(item):
                    feasible = item.kpi_evaluation.passed and not item.route_safety_issues and not item.unreachable_sections
                    return (feasible, -item.estimated_completion_minutes if feasible else item.kpi_evaluation.score)
                better = best is None or rank(candidate) > rank(best)
            else:
                better = best is None or candidate.kpi_evaluation.score > best.kpi_evaluation.score
            if better:
                best = candidate

            if iteration == 0 and proposal and iteration + 1 < settings.optimization.max_iterations and monotonic() - started < settings.optimization.timeout_seconds:
                recovery_ctx["preferred_task_id"] = proposal["preferred_task_id"]
                continue

            if source == "TRAINED_PPO" and baseline_feedback and baseline_feedback.feasible:
                if not record.feasible or record.reward < baseline_feedback.reward + settings.optimization.min_improvement_epsilon:
                    record.decision = "REJECTED_HARD_CONSTRAINT" if not record.feasible else "REJECTED_NO_IMPROVEMENT"
                    record.reason = record.reason if not record.feasible else "Feasible proposal did not improve the baseline reward by the configured epsilon."
                    candidate.status = RouteLifecycleStatus.SUPERSEDED
                    baseline_route.status = RouteLifecycleStatus.PROPOSED
                    self.state_machine.transition(req.job_id, ExecutionState.AWAITING_APPROVAL, actor, "Deterministic baseline retained after independent PPO candidate evaluation")
                    return attach_feedback(baseline_route)
                record.reason = f"PPO candidate passed hard constraints and improved reward by {record.reward-baseline_feedback.reward:.6f}."
            recovery_ctx.pop("preferred_task_id", None)

            if candidate.kpi_evaluation.passed:
                if recovery_history:
                    recovery_history[-1].succeeded = True
                candidate.planning_diagnostics = self._build_planning_diagnostics(
                    candidate=candidate,
                    recovery_history=recovery_history,
                    recovery_actions_applied=recovery_actions_applied,
                )
                if optimize_completion:
                    # Independently compare bounded alternatives even after the
                    # first feasible plan. Service and return travel count toward
                    # the whole-job completion objective; safety gates stay hard.
                    choices = [(i, 0) for i in range(max(1, len(angles)))]
                    choices += [(i, 1) for i in range(max(1, len(angles)))]
                    next_settings = next((s for s in choices if s not in evaluated_settings), None)
                    if next_settings is not None and iteration + 1 < settings.optimization.max_iterations:
                        recovery_ctx["angle_idx"], recovery_ctx["overlap_steps"] = next_settings
                        continue
                    termination_override = "completion_time_comparison_finished"
                    break
                self.state_machine.transition(
                    req.job_id,
                    ExecutionState.AWAITING_APPROVAL,
                    actor,
                    "Candidate passed KPI and safety evaluation",
                )
                if plan_key:
                    self.audit.save_idempotency(plan_key, deepcopy(candidate))
                return attach_feedback(candidate)

            # Plan failed evaluation -> Diagnose failure & select deterministic recovery action
            sig = (
                round(candidate.covered_area_m2, 1),
                round(candidate.total_distance_m, 1),
                len(candidate.segments),
                tuple(sorted(candidate.kpi_evaluation.failed_constraints)),
                tuple(sorted(candidate.route_safety_issues)),
                recovery_ctx["prune_unreachable_cells"],
                recovery_ctx["snap_depot_to_portal"],
                recovery_ctx["prune_unreachable_tasks"],
            )

            failure_class, failure_cause, recovery_action, changed_params = (
                self._diagnose_and_select_recovery(
                    candidate, req, recovery_ctx, iteration, len(angles)
                )
            )

            if sig in seen_failed_signatures and not changed_params:
                termination_override = "equivalent_failed_plan_detected"
                break
            seen_failed_signatures.add(sig)

            if recovery_action == "TERMINATE_UNRECOVERABLE":
                termination_override = f"unrecoverable_{failure_class.lower()}"
                break

            if iteration + 1 < settings.optimization.max_iterations:
                for k, v in changed_params.items():
                    recovery_ctx[k] = v
                if recovery_action not in recovery_actions_applied:
                    recovery_actions_applied.append(recovery_action)
                recovery_history.append(
                    RecoveryAttemptRecord(
                        attempt_number=len(recovery_history) + 1,
                        route_version=candidate.route_version,
                        failure_classification=failure_class,
                        failure_cause=failure_cause,
                        recovery_action=recovery_action,
                        parameters_changed=changed_params,
                        succeeded=False,
                    )
                )
                self.audit.record(
                    req.job_id,
                    actor,
                    f"RECOVERY:{recovery_action}",
                    f"Attempt {len(recovery_history)}: {failure_class} ({failure_cause})",
                    candidate.route_id,
                    candidate.route_version,
                )
                self.state_machine.transition(
                    req.job_id,
                    ExecutionState.PLANNING,
                    actor,
                    f"Apply recovery action {recovery_action} after {failure_class}",
                )

        result = best or final
        if result is None:
            self.state_machine.transition(
                req.job_id,
                ExecutionState.NEEDS_ASSISTANCE,
                actor,
                "No candidate could be produced",
            )
            raise LocationValidationError("No route candidate could be produced.")

        result.optimization_termination_reason = termination_override or (
            "optimization_timeout"
            if monotonic() - started > settings.optimization.timeout_seconds
            else "iteration_limit_reached"
        )
        result.planning_diagnostics = self._build_planning_diagnostics(
            candidate=result,
            recovery_history=recovery_history,
            recovery_actions_applied=recovery_actions_applied,
        )
        if final is not None and result.route_id != final.route_id:
            final.status = RouteLifecycleStatus.SUPERSEDED
            result.status = RouteLifecycleStatus.PROPOSED if result.kpi_evaluation.passed else RouteLifecycleStatus.DRAFT_FAILED_KPI
        if optimize_completion and result.kpi_evaluation.passed:
            for item in history:
                if item.route_id != result.route_id and item.status == RouteLifecycleStatus.PROPOSED:
                    item.status = RouteLifecycleStatus.SUPERSEDED
            result.candidate_rationale.append(
                f"Selected the shortest estimated completion among {sum(r.feasible for r in traces)} feasible candidates "
                f"from {len(traces)} evaluated alternatives, including depot service. This is a bounded comparison, not a global optimum."
            )
        if result.status == RouteLifecycleStatus.PROPOSED:
            if self.state_machine.get_state(req.job_id) == ExecutionState.PLANNING:
                self.state_machine.transition(req.job_id, ExecutionState.EVALUATING, actor,
                                              "Publish independently evaluated best feasible candidate")
            self.state_machine.transition(
                req.job_id,
                ExecutionState.AWAITING_APPROVAL,
                actor,
                result.optimization_termination_reason,
            )
        else:
            self.state_machine.transition(
                req.job_id,
                ExecutionState.NEEDS_ASSISTANCE,
                actor,
                result.optimization_termination_reason,
            )
        if plan_key:
            self.audit.save_idempotency(plan_key, deepcopy(result))
        return attach_feedback(result)

    def _diagnose_and_select_recovery(
        self,
        candidate: RouteCandidate,
        req: PlanRequest,
        recovery_ctx: Dict[str, Any],
        iteration: int,
        num_angles: int,
    ) -> Tuple[str, str, str, Dict[str, Any]]:
        """Deterministic failure classifier and recovery-action selector."""
        issues = candidate.route_safety_issues
        failed_kpis = (
            candidate.kpi_evaluation.failed_constraints
            if candidate.kpi_evaluation
            else []
        )

        # 1. Check if entire area is water/road/building/unverified with 0 eligible area
        if any("no_eligible_traversable_area" in iss for iss in issues):
            return (
                "NO_ELIGIBLE_TRAVERSABLE_AREA",
                "Requested polygon consists entirely of impassable water, roads, buildings, or unverified terrain.",
                "TERMINATE_UNRECOVERABLE",
                {},
            )

        # 2. Check depot / start access failure
        if any(
            "depot_access_unreachable" in iss or "start_access_unreachable" in iss
            for iss in issues
        ):
            if not recovery_ctx["snap_depot_to_portal"]:
                return (
                    "DEPOT_ACCESS_BLOCKED",
                    "Start or disposal depot point has no direct obstacle-free connector to the traversability graph.",
                    "FIND_ALTERNATIVE_DEPOT_ACCESS_POINT",
                    {"snap_depot_to_portal": True},
                )
            return (
                "DEPOT_ACCESS_BLOCKED",
                "Depot remains completely isolated from traversable graph even within portal snap tolerance.",
                "TERMINATE_UNRECOVERABLE",
                {},
            )

        # 3. Check disconnected coverage cell / impassable barrier between swaths
        if (
            any("sweep_connector_unreachable" in iss for iss in issues)
            or len(candidate.unreachable_sections) > 0
        ):
            if req.allow_partial_coverage_recovery and not recovery_ctx["prune_unreachable_cells"]:
                return (
                    "DISCONNECTED_COVERAGE_CELL",
                    "Impassable barrier, road, or water body divides the polygon with no permitted machine crossing.",
                    "SPLIT_AND_PRUNE_UNREACHABLE_CELLS",
                    {
                        "prune_unreachable_cells": True,
                        "reorder_cells": True,
                    },
                )

        # 4. Check unreachable collection tasks
        has_unreachable_task = any(
            "task_unreachable_on_traversability_graph" in item.get("reasons", [])
            for item in candidate.skipped_tasks
        )
        if has_unreachable_task and req.allow_partial_coverage_recovery and not recovery_ctx["prune_unreachable_tasks"]:
            return (
                "UNREACHABLE_COLLECTION_TASK",
                "One or more collection tasks lie across a prohibited road or barrier without a permitted crossing.",
                "EXCLUDE_UNREACHABLE_TASKS_AND_RECOMPUTE",
                {"prune_unreachable_tasks": True},
            )

        # 5. Check coverage below target
        if (
            "coverage_below_target" in failed_kpis
            or "missed_coverage_exceeds_limit" in failed_kpis
        ):
            next_overlap = (iteration + 1) % 2 if recovery_ctx["overlap_steps"] == 0 else recovery_ctx["overlap_steps"] + 1
            next_angle = ((iteration + 1) // 2) % max(1, num_angles)
            return (
                "KPI_COVERAGE_BELOW_TARGET",
                f"Covered area ({candidate.covered_area_m2:.1f} m^2) is below policy coverage threshold on target ({candidate.target_area_m2:.1f} m^2).",
                "ADJUST_SWEEP_ANGLE_AND_INCREASE_OVERLAP",
                {
                    "overlap_steps": next_overlap,
                    "angle_idx": next_angle,
                    "prune_unreachable_cells": True if req.allow_partial_coverage_recovery else recovery_ctx["prune_unreachable_cells"],
                },
            )

        # 6. Check repeated coverage exceeded
        if "repeated_coverage_exceeds_limit" in failed_kpis:
            next_overlap = max(0, recovery_ctx["overlap_steps"] - 1)
            next_angle = (recovery_ctx["angle_idx"] + 1) % max(1, num_angles)
            return (
                "KPI_REPEATED_COVERAGE_EXCEEDED",
                "Swath overlap produced excessive repeated coverage.",
                "REDUCE_OVERLAP_AND_REORDER_CELLS",
                {"overlap_steps": next_overlap, "angle_idx": next_angle},
            )

        # 7. Default parameter exploration for remaining iterations
        if iteration + 1 < settings.optimization.max_iterations:
            next_angle = (recovery_ctx["angle_idx"] + 1) % max(1, num_angles)
            next_overlap = (iteration + 1) % 2
            if (
                next_angle == recovery_ctx["angle_idx"]
                and next_overlap == recovery_ctx["overlap_steps"]
            ):
                return (
                    "UNRESOLVABLE_KPI_OR_CAPABILITY_CONSTRAINT",
                    "Constraint violation cannot be resolved by geometric replanning.",
                    "TERMINATE_UNRECOVERABLE",
                    {},
                )
            return (
                "KPI_OPTIMIZATION_SEARCH",
                f"Searching alternative sweep orientation for constraints: {', '.join(failed_kpis or issues)}",
                "ROTATE_SWEEP_ORIENTATION",
                {"angle_idx": next_angle, "overlap_steps": next_overlap},
            )

        return (
            "ITERATION_LIMIT",
            "Maximum recovery attempts reached.",
            "TERMINATE_UNRECOVERABLE",
            {},
        )

    def _build_planning_diagnostics(
        self,
        candidate: RouteCandidate,
        recovery_history: List[RecoveryAttemptRecord],
        recovery_actions_applied: List[str],
    ) -> PlanningDiagnostics:
        req_area = max(candidate.requested_area_m2, candidate.target_area_m2)
        elig_area = candidate.effective_eligible_area_m2
        cov_area = candidate.covered_area_m2
        inacc_area = candidate.inaccessible_area_m2
        unv_area = candidate.unverified_area_m2

        reach_cov_pct: Optional[float] = (
            round(min(100.0, (cov_area / elig_area) * 100.0), 2)
            if elig_area > 0.0
            else (100.0 if candidate.task_mode in (TaskType.RUBBISH_COLLECTION, TaskType.OVERSIZED_RUBBISH) else 0.0)
        )
        req_cov_pct: Optional[float] = (
            round(min(100.0, (cov_area / req_area) * 100.0), 2)
            if req_area > 0.0
            else None
        )
        inacc_pct: Optional[float] = (
            round(min(100.0, (inacc_area / req_area) * 100.0), 2)
            if req_area > 0.0
            else 0.0
        )
        unv_pct: Optional[float] = (
            round(min(100.0, (unv_area / req_area) * 100.0), 2)
            if req_area > 0.0
            else 0.0
        )

        cells = candidate.coverage_cells
        tot_cells = len(cells)
        reach_cells = sum(1 for c in cells if c.reachable)
        planned_cells = sum(1 for c in cells if c.swept and c.swath_count > 0)
        excl_cells = sum(1 for c in cells if not c.reachable)
        completed_cells = 0
        if planned_cells > 0 and candidate.segments:
            all_done = all(
                s.completed
                for s in candidate.segments
                if s.segment_type == "SWEEP_SWATH"
            )
            if all_done:
                completed_cells = planned_cells

        passed = bool(
            candidate.kpi_evaluation
            and candidate.kpi_evaluation.passed
            and not candidate.route_safety_issues
            and not candidate.unreachable_sections
        )

        has_partial_exclusions = (
            excl_cells > 0 or inacc_area > 1.0 or unv_area > 1.0
        )
        if passed:
            status_str = "PARTIAL_SUCCESS" if has_partial_exclusions else "SUCCESS"
        elif not candidate.segments or any(
            "unreachable" in iss or "no_eligible" in iss
            for iss in candidate.route_safety_issues
        ):
            status_str = "BLOCKED"
        else:
            status_str = "FAILED_KPI"

        road_conflicts = [
            r
            for r in candidate.candidate_rationale
            if "Road-access conflict" in r or "Machine access restriction" in r or "Crossing" in r
        ]
        all_excluded_sections = list(candidate.unreachable_sections)
        if candidate.planning_diagnostics and candidate.planning_diagnostics.excluded_sections:
            existing_ids = {s.section_id for s in all_excluded_sections}
            for sec in candidate.planning_diagnostics.excluded_sections:
                if sec.section_id not in existing_ids:
                    all_excluded_sections.append(sec)

        if status_str == "SUCCESS":
            summary = (
                f"Validated route {candidate.route_id} (v{candidate.route_version}) covers "
                f"{reach_cov_pct:.1f}% of eligible area ({elig_area:.0f} m^2) across {planned_cells} cell(s)."
            )
        elif status_str == "PARTIAL_SUCCESS":
            summary = (
                f"Partial feasible plan {candidate.route_id} (v{candidate.route_version}): safely sweeps "
                f"{reach_cov_pct:.1f}% of reachable eligible area ({elig_area:.0f} m^2) while excluding "
                f"{inacc_area + unv_area:.0f} m^2 of impassable roads/water/buildings/isolated cells."
            )
        else:
            reasons = ", ".join(
                candidate.route_safety_issues
                or (
                    candidate.kpi_evaluation.failed_constraints
                    if candidate.kpi_evaluation
                    else ["validation_failed"]
                )
            )
            summary = (
                f"Planning {status_str} after {len(recovery_history)} recovery attempt(s): {reasons}"
            )

        return PlanningDiagnostics(
            planning_status=status_str,
            route_id=candidate.route_id,
            route_version=candidate.route_version,
            machine_id=candidate.machine_id,
            task_mode=candidate.task_mode,
            requested_area_m2=round(req_area, 2),
            effective_eligible_area_m2=round(elig_area, 2),
            covered_area_m2=round(cov_area, 2),
            inaccessible_area_m2=round(inacc_area, 2),
            unverified_area_m2=round(unv_area, 2),
            reachable_coverage_percent=reach_cov_pct,
            requested_coverage_percent=req_cov_pct,
            inaccessible_area_percent=inacc_pct,
            unverified_area_percent=unv_pct,
            total_distance_m=round(candidate.total_distance_m, 2),
            estimated_duration_minutes=round(candidate.estimated_duration_minutes, 2),
            total_cells_detected=tot_cells,
            reachable_cells_count=reach_cells,
            planned_cells_count=planned_cells,
            completed_cells_count=completed_cells,
            excluded_cells_count=excl_cells,
            coverage_cells=cells,
            road_access_conflicts=road_conflicts,
            obstacle_and_connectivity_issues=list(candidate.route_safety_issues),
            replanning_attempts_count=len(recovery_history),
            recovery_actions_applied=list(recovery_actions_applied),
            recovery_history=list(recovery_history),
            excluded_sections=all_excluded_sections,
            passed_final_validation=passed,
            summary_explanation=summary,
        )

    def _make_candidate(
        self,
        req: PlanRequest,
        mode: TaskType,
        machine: MachineProfile,
        history: List[RouteCandidate],
        iteration: int,
        angles: List[float],
        completed_segments: Optional[List[RouteSegment]] = None,
        replan_reason: Optional[str] = None,
        env_map: Optional[EnvironmentMapData] = None,
        recovery_ctx: Optional[Dict[str, Any]] = None,
    ) -> RouteCandidate:
        assert req.start_location is not None and req.disposal_location is not None
        # Keep geometry/energy calculations tied to one profile revision even
        # when another request updates the machine while planning is running.
        machine = machine.model_copy(deep=True)
        r_ctx = recovery_ctx or {
            "prune_unreachable_cells": False,
            "use_authorized_crossings": True,
            "snap_depot_to_portal": False,
            "reorder_cells": True,
            "prune_unreachable_tasks": False,
            "angle_idx": iteration // 2,
            "overlap_steps": iteration % 2,
        }

        tasks = deepcopy(req.tasks)
        feasible_tasks, rejected, trips = (
            self.capability_validator.validate_task_batch(machine, tasks)
        )
        skipped = [
            {
                "task_id": task.task_id,
                "reasons": check.rejection_reasons,
                "recommended_actions": check.recommended_actions,
            }
            for task, check in rejected
        ]
        version = r_ctx.get("route_version", (history[-1].route_version + 1) if history else 1)
        segments: List[RouteSegment] = deepcopy(
            [
                segment
                for segment in (completed_segments or [])
                if segment.completed
            ]
        )
        routing_provenance = DataProvenance.PLANNED
        requested_area = effective_eligible_area = inaccessible_area = unverified_area = 0.0
        target_area = covered_area = repeated_area = missed_area = sweep_distance = transit_distance = 0.0
        task_schedule = []
        route_safety_issues: List[str] = []
        scheduled_task_order: List[TaskItem] = []
        coverage_cells = []
        excluded_sections: List[UnreachableSection] = []
        unreachable_sections: List[UnreachableSection] = []
        applied_features = []
        crossings_used: List[str] = []

        angle_idx = r_ctx.get("angle_idx", iteration // 2)
        overlap_steps = r_ctx.get("overlap_steps", iteration % 2)
        angle = angles[angle_idx % len(angles)] if angles else 0.0
        overlap = min(
            0.65,
            machine.default_overlap_ratio
            + overlap_steps * settings.optimization.overlap_step_increment,
        )
        candidate_rationale = [
            f"Bounded deterministic candidate {iteration + 1}: sweep angle {angle:.1f} degrees, "
            f"overlap {overlap:.2f}."
        ]
        if req.deduplicated_task_ids:
            candidate_rationale.append(
                f"Duplicate task reports were removed by idempotency key: {', '.join(req.deduplicated_task_ids)}."
            )

        # Build road/terrain-aware TraversabilityGraph and validate start & depot access
        nav_graph = self._build_nav_graph_for_request(
            req,
            machine,
            env_map,
            use_authorized_crossings=r_ctx.get("use_authorized_crossings", True),
        )
        eff_start, start_err, start_snapped = nav_graph.validate_portal_access(
            req.start_location,
            "start",
            allow_snap=r_ctx.get("snap_depot_to_portal", False),
            max_snap_m=settings.optimization.max_depot_snap_distance_m,
        )
        eff_depot, depot_err, depot_snapped = nav_graph.validate_portal_access(
            req.disposal_location,
            "depot",
            allow_snap=r_ctx.get("snap_depot_to_portal", False),
            max_snap_m=settings.optimization.max_depot_snap_distance_m,
        )
        if start_err:
            route_safety_issues.append(start_err)
        if depot_err:
            route_safety_issues.append(depot_err)
        if start_snapped and eff_start:
            candidate_rationale.append(
                f"Recovery snapped start portal to verified graph access point ({eff_start.lat:.5f}, {eff_start.lon:.5f})."
            )
        if depot_snapped and eff_depot:
            candidate_rationale.append(
                f"Recovery snapped disposal depot to verified graph access point ({eff_depot.lat:.5f}, {eff_depot.lon:.5f})."
            )

        active_start = eff_start or req.start_location
        active_depot = eff_depot or req.disposal_location
        collections_first = mode == TaskType.HYBRID and any(not task.completed for task in feasible_tasks)
        # Collection deadlines share the mission clock with sweeping and depot
        # service. Finish the time-constrained bin round before the long sweep.
        # Its final disposal stop is also the starting portal for coverage.
        coverage_start = active_depot if collections_first else active_start

        if mode in (TaskType.AREA_SWEEP, TaskType.PATROL, TaskType.HYBRID):
            assert req.operating_boundary is not None
            coverage = self.coverage_planner.plan_coverage(
                req.operating_boundary,
                req.exclusion_zones,
                machine,
                sweep_angle_deg=angle,
                overlap_ratio=overlap,
                completed_segments=completed_segments,
                environment_map=env_map,
                origin_coord=coverage_start,
                prune_unreachable_cells=r_ctx.get("prune_unreachable_cells", False),
                use_authorized_crossings=r_ctx.get("use_authorized_crossings", True),
                reorder_cells=r_ctx.get("reorder_cells", True),
            )
            existing_segment_ids = {segment.segment_id for segment in segments}
            if not start_err and not depot_err:
                segments.extend(
                    segment
                    for segment in coverage.segments
                    if segment.segment_id not in existing_segment_ids
                )
            requested_area = coverage.requested_area_m2
            effective_eligible_area = coverage.effective_eligible_area_m2
            inaccessible_area = coverage.inaccessible_area_m2
            unverified_area = coverage.unverified_area_m2
            target_area, covered_area = (
                coverage.target_area_m2,
                coverage.covered_area_m2 if not (start_err or depot_err) else 0.0,
            )
            repeated_area, missed_area = (
                coverage.repeated_area_m2,
                coverage.missed_area_m2,
            )
            coverage_cells = coverage.coverage_cells
            excluded_sections = coverage.excluded_sections
            unreachable_sections = coverage.unreachable_sections
            applied_features = coverage.environment_features_applied
            crossings_used = list(coverage.authorized_crossings_used)
            for rc in coverage.road_access_conflicts:
                candidate_rationale.append(rc)
            route_safety_issues.extend(coverage.route_safety_issues)

            remaining_sweep_segments = [
                segment
                for segment in coverage.segments
                if segment.segment_type == "SWEEP_SWATH"
                and not segment.completed
                and segment.waypoints
            ]
            if remaining_sweep_segments and not start_err and not depot_err:
                first_point = remaining_sweep_segments[0].waypoints[0]
                last_point = remaining_sweep_segments[-1].waypoints[-1]

                # Validate ingress path over nav_graph
                u_start = coverage_start.to_local_meters(nav_graph.origin)
                v_first = first_point.to_local_meters(nav_graph.origin)
                ing_pts, ing_dist, ing_xings = ([], 0.0, []) if collections_first else nav_graph.find_shortest_permitted_path(
                    u_start, v_first, require_within_boundary=False
                )
                if ing_pts is None and not collections_first:
                    route_safety_issues.append(
                        "start_access_unreachable: no permitted path from start point to sweeping area"
                    )
                elif ing_pts is not None and ing_dist > 0.5 and not collections_first:
                    for xid in ing_xings:
                        if xid not in crossings_used:
                            crossings_used.append(xid)
                    segments.insert(
                        len([segment for segment in segments if segment.completed]),
                        RouteSegment(
                            segment_type="TRANSIT",
                            waypoints=[
                                Coordinate.from_local_meters(px, py, nav_graph.origin)
                                for px, py in ing_pts
                            ],
                            distance_m=round(ing_dist, 2),
                            estimated_duration_sec=round(
                                ing_dist / max(machine.cruise_speed_mps, 0.2), 2
                            ),
                            crossing_id=ing_xings[0] if ing_xings else None,
                            provenance=DataProvenance.PLANNED,
                        ),
                    )
                    transit_distance += ing_dist

                u_last = last_point.to_local_meters(nav_graph.origin)
                v_depot = active_depot.to_local_meters(nav_graph.origin)
                egr_pts, egr_dist, egr_xings = nav_graph.find_shortest_permitted_path(
                    u_last, v_depot, require_within_boundary=False, prefer_perimeter=True
                )
                if egr_pts is None:
                    route_safety_issues.append(
                        "depot_access_unreachable: no permitted return path from sweeping area to disposal depot"
                    )
                elif egr_dist > 0.5:
                    for xid in egr_xings:
                        if xid not in crossings_used:
                            crossings_used.append(xid)
                    segments.append(
                        RouteSegment(
                            segment_type="RETURN_TO_BASE",
                            waypoints=[
                                Coordinate.from_local_meters(px, py, nav_graph.origin)
                                for px, py in egr_pts
                            ],
                            distance_m=round(egr_dist, 2),
                            estimated_duration_sec=round(
                                egr_dist / max(machine.cruise_speed_mps, 0.2), 2
                            ),
                            crossing_id=egr_xings[0] if egr_xings else None,
                            provenance=DataProvenance.PLANNED,
                        )
                    )
                    transit_distance += egr_dist

        deferred_sweep = []
        if collections_first:
            deferred_sweep = [segment for segment in segments if not segment.completed]
            segments = [segment for segment in segments if segment.completed]

        if mode in (
            TaskType.RUBBISH_COLLECTION,
            TaskType.OVERSIZED_RUBBISH,
            TaskType.HYBRID,
        ):
            tasks_to_route = [t for t in feasible_tasks if not t.completed]
            if tasks_to_route and not start_err and not depot_err:
                collection_start = active_start
                locations = (
                    [collection_start]
                    + [t.location for t in tasks_to_route]
                    + [active_depot]
                )
                use_graph = env_map is not None or len(req.exclusion_zones) > 0
                # Sweep alternatives share the same collection locations and
                # terrain during this planning request. Reuse their checked
                # graph paths; task scheduling and final validation still run
                # independently for every candidate. Never reuse across jobs.
                collection_cache = r_ctx.setdefault("collection_path_cache", {})
                collection_key = (tuple((point.lat, point.lon) for point in locations),
                    machine.cruise_speed_mps, bool(r_ctx.get("use_authorized_crossings", True)), use_graph)
                if collection_key not in collection_cache:
                    collection_cache[collection_key] = self.routing_engine.compute_matrix_and_paths(
                        locations, machine.cruise_speed_mps,
                        nav_graph=nav_graph if use_graph else None,
                    )
                matrix, paths = deepcopy(collection_cache[collection_key])
                routing_provenance = matrix.provenance
                plan = self.route_optimizer.optimize_collection_route(
                    collection_start,
                    active_depot,
                    tasks_to_route,
                    machine,
                    matrix.distance_matrix_m,
                    matrix.duration_matrix_sec,
                    paths,
                    matrix.provenance,
                    preferred_task_id=(recovery_ctx or {}).get("preferred_task_id"),
                    start_elapsed_min=req.mission_elapsed_min
                    + sum(
                        s.estimated_duration_sec
                        for s in segments
                        if not s.completed
                    )
                    / 60.0,
                )
                segments.extend(plan.segments)
                transit_distance += plan.total_transit_distance_m
                trips = plan.disposal_trips
                task_schedule = plan.task_schedule
                scheduled_task_order = plan.ordered_tasks
                for task in plan.unreachable_tasks:
                    skipped.append(
                        {
                            "task_id": task.task_id,
                            "reasons": ["task_unreachable_on_traversability_graph"],
                            "recommended_actions": [
                                "Verify authorized crossing or relocate collection point to accessible pathway."
                            ],
                        }
                    )
                    if not r_ctx.get("prune_unreachable_tasks", False):
                        route_safety_issues.append(
                            f"task_unreachable_on_traversability_graph: {task.task_id}"
                        )
                for task in plan.skipped_optional_tasks:
                    skipped.append(
                        {
                            "task_id": task.task_id,
                            "reasons": ["task_time_window_infeasible"],
                            "recommended_actions": [
                                "Adjust its elapsed mission-minute window or revise the mission schedule."
                            ],
                        }
                    )
                if plan.skipped_optional_tasks:
                    candidate_rationale.append(
                        "The time-window scheduler left infeasible tasks unassigned and reported them for operator review."
                    )
                candidate_rationale.append(
                    f"Collection order honors task priority, travel time, service duration, time windows, and depot trips ({plan.disposal_trips})."
                )

        if deferred_sweep:
            sweep_entry = next((segment.waypoints[0] for segment in deferred_sweep if segment.waypoints), None)
            last_pending = next((segment for segment in reversed(segments) if not segment.completed and segment.waypoints), None)
            after_collection = last_pending.waypoints[-1] if last_pending else active_start
            if sweep_entry is not None:
                points, distance, bridge_crossings = nav_graph.find_shortest_permitted_path(
                    after_collection.to_local_meters(nav_graph.origin),
                    sweep_entry.to_local_meters(nav_graph.origin), require_within_boundary=False,
                )
                if points is None:
                    route_safety_issues.append("start_access_unreachable: no permitted path from collection round to remaining sweep")
                elif distance > 0.5:
                    for crossing in bridge_crossings:
                        if crossing not in crossings_used:
                            crossings_used.append(crossing)
                    waypoints = [Coordinate.from_local_meters(x, y, nav_graph.origin) for x, y in points]
                    waypoints[0], waypoints[-1] = after_collection, sweep_entry
                    segments.append(RouteSegment(segment_type="TRANSIT", waypoints=waypoints,
                        distance_m=distance, estimated_duration_sec=distance / machine.cruise_speed_mps,
                        crossing_id=bridge_crossings[0] if bridge_crossings else None))
            segments.extend(deferred_sweep)
            candidate_rationale.append("Hybrid mission: collect scheduled bins and unload before sweeping; depot service and the full sweep share the same elapsed mission clock.")

        sweep_distance = sum(
            segment.distance_m
            for segment in segments
            if not segment.completed
            and segment.segment_type in {"SWEEP_SWATH", "TURN_CONNECTOR"}
        )
        transit_distance = sum(
            segment.distance_m
            for segment in segments
            if not segment.completed
            and segment.segment_type in {"TRANSIT", "RETURN_TO_BASE"}
        )
        total_distance = sweep_distance + transit_distance
        total_seconds = sum(
            seg.estimated_duration_sec for seg in segments if not seg.completed
        )
        energy = (
            sweep_distance / 1000.0 * machine.energy_consumption_kwh_per_km_sweep
            + transit_distance
            / 1000.0
            * machine.energy_consumption_kwh_per_km_travel
        )
        battery_spent_pct = (
            energy / max(machine.battery_capacity_kwh, 0.01) * 100.0
        )
        payload_kg = machine.current_payload_kg + sum(
            t.rubbish_spec.estimated_weight_kg or 0.0
            for t in feasible_tasks
            if t.rubbish_spec and not t.completed
        )
        payload_volume = machine.current_payload_volume_m3 + sum(
            t.rubbish_spec.estimated_volume_m3 or 0.0
            for t in feasible_tasks
            if t.rubbish_spec and not t.completed
        )
        if (
            mode
            in (
                TaskType.RUBBISH_COLLECTION,
                TaskType.OVERSIZED_RUBBISH,
                TaskType.HYBRID,
            )
            and tasks_to_route
            and not start_err
            and not depot_err
        ):
            payload_kg = plan.peak_payload_kg
            payload_volume = plan.peak_payload_volume_m3
        payload_pct = max(
            payload_kg / max(machine.max_payload_kg, 0.01) * 100.0,
            payload_volume / max(machine.max_payload_volume_m3, 0.01) * 100.0,
        )
        scheduled_ids = {t.task_id for t in scheduled_task_order}
        if r_ctx.get("prune_unreachable_tasks", False):
            unreachable_task_ids = {
                item["task_id"]
                for item in skipped
                if "task_unreachable_on_traversability_graph" in item.get("reasons", [])
            }
            skipped = [
                item
                for item in skipped
                if item["task_id"] not in unreachable_task_ids
            ]
            feasible_tasks = [
                t for t in feasible_tasks if t.task_id not in unreachable_task_ids
            ]
        candidate_tasks = (
            list(scheduled_task_order)
            + [t for t in feasible_tasks if t.task_id not in scheduled_ids]
            + [t for t, _ in rejected]
        )
        candidate = RouteCandidate(
            planning_run_id=r_ctx.get("planning_run_id"),
            candidate_index=iteration + 1,
            use_rl=req.use_rl,
            job_id=req.job_id,
            route_version=version,
            plan_epoch=version,
            machine_id=machine.machine_id,
            machine_profile_revision=getattr(machine, "planning_revision", 0),
            task_mode=mode,
            routing_provenance=routing_provenance,
            mission_elapsed_min=req.mission_elapsed_min,
            origin=active_start,
            disposal_depot=active_depot,
            operating_boundary=req.operating_boundary,
            exclusion_zones=req.exclusion_zones,
            tasks=candidate_tasks,
            task_schedule=task_schedule,
            skipped_tasks=skipped,
            segments=segments,
            route_safety_issues=route_safety_issues,
            candidate_rationale=candidate_rationale,
            deduplicated_task_ids=req.deduplicated_task_ids,
            replan_reason=replan_reason,
            unreachable_sections=unreachable_sections,
            requested_area_m2=requested_area,
            effective_eligible_area_m2=effective_eligible_area,
            inaccessible_area_m2=inaccessible_area,
            unverified_area_m2=unverified_area,
            target_area_m2=target_area,
            covered_area_m2=covered_area,
            repeated_area_m2=repeated_area,
            missed_area_m2=missed_area,
            sweep_angle_deg=angle,
            overlap_ratio=overlap,
            total_distance_m=total_distance,
            sweep_distance_m=sweep_distance,
            transit_distance_m=transit_distance,
            estimated_duration_minutes=total_seconds / 60.0,
            estimated_energy_kwh=energy,
            estimated_remaining_battery_percent=max(
                0.0, machine.current_battery_percent - battery_spent_pct
            ),
            payload_utilization_percent=payload_pct,
            disposal_trips=trips,
            coverage_cells=coverage_cells,
            environment_features_applied=applied_features,
            authorized_crossings_used=crossings_used,
        )
        candidate.initial_battery_percent = machine.current_battery_percent
        candidate.planning_environment_map = env_map
        if req.enable_depot_cycles:
            from .depot_cycles import plan_depot_cycles
            cycle_policy = r_ctx.get("cycle_policy") or KPIPolicyConfig.model_validate(
                {**settings.kpi_policy.model_dump(), **(req.kpi_policy_override or {})}
            )
            def depot_connector(start, end):
                points, _, used_crossings = nav_graph.find_shortest_permitted_path(
                    start.to_local_meters(nav_graph.origin), end.to_local_meters(nav_graph.origin),
                    require_within_boundary=False,
                )
                if points is None:
                    return None
                for crossing in used_crossings:
                    if crossing not in candidate.authorized_crossings_used:
                        candidate.authorized_crossings_used.append(crossing)
                result = [Coordinate.from_local_meters(x, y, nav_graph.origin) for x, y in points]
                if len(result) >= 2:
                    result[0], result[-1] = start, end
                return result or [start]
            candidate = plan_depot_cycles(candidate, machine, cycle_policy, depot_connector)
        else:
            candidate.estimated_completion_minutes = candidate.mission_elapsed_min + candidate.estimated_duration_minutes
        geometry_issues = self.request_validator.validate_route_geometry(
            candidate, machine, environment_map=env_map
        )
        candidate.route_safety_issues = list(
            dict.fromkeys(candidate.route_safety_issues + geometry_issues)
        )
        if candidate.route_safety_issues:
            candidate.candidate_rationale.append(
                "One or more route sections failed geometry checks and cannot be approved."
            )
        # Preserve excluded sections in diagnostics even when unreachable_sections was pruned
        candidate.planning_diagnostics = PlanningDiagnostics(
            planning_status="PLANNING",
            route_id=candidate.route_id,
            route_version=candidate.route_version,
            machine_id=candidate.machine_id,
            task_mode=candidate.task_mode,
            excluded_sections=excluded_sections,
        )
        return candidate

    def _store_candidate(self, candidate: RouteCandidate) -> None:
        self.routes_by_id[candidate.route_id] = candidate

    def _publish_selected(self, candidate: RouteCandidate) -> None:
        with self.state_machine.control_lock:
            self._publish_selected_locked(candidate)

    def _publish_selected_locked(self, candidate: RouteCandidate) -> None:
        if self.state_machine.get_state(candidate.job_id) in (ExecutionState.EMERGENCY_STOP, ExecutionState.ABORTED):
            raise SafetyInterlockError("Planning was stopped or cancelled. No new route was published.")
        previous_id = self._latest_route_by_job.get(candidate.job_id)
        if previous_id and previous_id in self.routes_by_id:
            previous = self.routes_by_id[previous_id]
            if previous.status not in (
                RouteLifecycleStatus.COMPLETED,
                RouteLifecycleStatus.ABORTED,
            ):
                previous.status = RouteLifecycleStatus.SUPERSEDED
        for option in self.job_route_history.get(candidate.job_id, []):
            if option.planning_run_id == candidate.planning_run_id and option.route_id != candidate.route_id and option.status == RouteLifecycleStatus.PROPOSED:
                option.status = RouteLifecycleStatus.SUPERSEDED
        candidate.is_selected_candidate = True
        self.routes_by_id[candidate.route_id] = candidate
        self._latest_route_by_job[candidate.job_id] = candidate.route_id

    def refresh_route_evaluation(self, route: RouteCandidate) -> RouteCandidate:
        """Recompute the evaluator view using this route's original policy and telemetry."""
        if route.status == RouteLifecycleStatus.COMPLETED and route.route_id in self._completed_evaluation_ids:
            return route
        machine = self.machines[route.machine_id]
        policy = KPIPolicyConfig.model_validate(
            route.kpi_policy_snapshot or settings.kpi_policy.model_dump()
        )
        frames = self.simulator.telemetry_store.get(route.route_id, [])
        route.kpi_evaluation = self.evaluator.evaluate(
            route, machine, policy, frames
        )
        if route.status == RouteLifecycleStatus.COMPLETED:
            self._completed_evaluation_ids.add(route.route_id)
        return route

    @staticmethod
    def _coverage_percent(route: RouteCandidate) -> float:
        if route.target_area_m2 <= 0.0:
            return 100.0
        return round(
            min(100.0, route.covered_area_m2 / route.target_area_m2 * 100.0), 2
        )

    def _compare_replan(
        self,
        previous: RouteCandidate,
        revised: RouteCandidate,
        previous_battery_percent: float,
    ) -> ReplanComparison:
        previous_remaining = [
            segment for segment in previous.segments if not segment.completed
        ]
        previous_distance = sum(
            segment.distance_m for segment in previous_remaining
        )
        previous_duration = (
            sum(segment.estimated_duration_sec for segment in previous_remaining)
            / 60.0
        )
        revised_open_tasks = sum(
            not task.completed
            and task.task_type
            in (TaskType.RUBBISH_COLLECTION, TaskType.OVERSIZED_RUBBISH)
            for task in revised.tasks
        )
        previous_open_tasks = sum(
            not task.completed
            and task.task_type
            in (TaskType.RUBBISH_COLLECTION, TaskType.OVERSIZED_RUBBISH)
            for task in previous.tasks
        )
        previous_coverage = self._coverage_percent(previous)
        revised_coverage = self._coverage_percent(revised)
        return ReplanComparison(
            previous_route_id=previous.route_id,
            previous_route_version=previous.route_version,
            previous_remaining_distance_m=round(previous_distance, 2),
            revised_remaining_distance_m=revised.total_distance_m,
            distance_delta_m=round(
                revised.total_distance_m - previous_distance, 2
            ),
            previous_remaining_duration_min=round(previous_duration, 2),
            revised_remaining_duration_min=revised.estimated_duration_minutes,
            duration_delta_min=round(
                revised.estimated_duration_minutes - previous_duration, 2
            ),
            previous_coverage_percent=previous_coverage,
            revised_coverage_percent=revised_coverage,
            coverage_delta_percent=round(
                revised_coverage - previous_coverage, 2
            ),
            previous_missed_area_m2=previous.missed_area_m2,
            revised_missed_area_m2=revised.missed_area_m2,
            missed_area_delta_m2=round(
                revised.missed_area_m2 - previous.missed_area_m2, 2
            ),
            previous_battery_percent=round(previous_battery_percent, 2),
            revised_remaining_battery_percent=revised.estimated_remaining_battery_percent,
            open_collection_tasks_delta=revised_open_tasks - previous_open_tasks,
        )

    def cancel_route(self, route_id: str, actor: str, role: OperatorRole) -> RouteCandidate:
        """Cancel the job's current route without clearing machine safety state."""
        if role not in (OperatorRole.OPERATOR, OperatorRole.SUPERVISOR, OperatorRole.ADMIN):
            raise PermissionError("An operator role is required to cancel jobs.")
        with self.simulator.execution_request_lock, self.state_machine.control_lock:
            source = self.routes_by_id.get(route_id)
            if source is None:
                raise KeyError("Route not found")
            latest_id = self._latest_route_by_job.get(source.job_id)
            route = self.routes_by_id.get(latest_id) if latest_id else source
            if route is None:
                raise KeyError("Current job route not found")
            state = self.state_machine.get_state(route.job_id)
            if state == ExecutionState.EMERGENCY_STOP:
                raise SafetyInterlockError("Emergency stop is active. Cancelling a job cannot clear it; a trusted hardware reset is required.")
            self.state_machine.assert_machine_not_stopped(route.machine_id)
            if state in (ExecutionState.COMPLETED, ExecutionState.ABORTED):
                return route
            self.state_machine.transition(route.job_id, ExecutionState.ABORTED, actor,
                "Operator cancelled the job; completed work and recorded telemetry were retained.", role=role)
            route.status = RouteLifecycleStatus.ABORTED
            self.audit.record(route.job_id, actor, "CANCEL_JOB",
                "Cancelled remaining work and released the machine reservation.",
                route_id=route.route_id, route_version=route.route_version,
                details={"requested_route_id": route_id,
                         "completed_segments": sum(segment.completed for segment in route.segments)})
            return route

    def approve_route(
        self, route_id: str, actor: str, role: OperatorRole
    ) -> RouteCandidate:
        with self.simulator.execution_request_lock, self.state_machine.control_lock:
            return self._approve_route(route_id, actor, role)

    def _approve_route(
        self, route_id: str, actor: str, role: OperatorRole
    ) -> RouteCandidate:
        if role not in (
            OperatorRole.OPERATOR,
            OperatorRole.SUPERVISOR,
            OperatorRole.ADMIN,
        ):
            raise PermissionError(
                "An operator role is required to approve routes."
            )
        route = self.routes_by_id.get(route_id)
        if route is None:
            raise KeyError("Route not found")
        current_machine = self.machines.get(route.machine_id)
        if current_machine is None or getattr(route, "machine_profile_revision", 0) != getattr(current_machine, "planning_revision", 0):
            raise SafetyInterlockError("Machine parameters changed after this plan was calculated. Replan before approval.")
        if (
            self._latest_route_by_job.get(route.job_id) != route_id
            or route.status != RouteLifecycleStatus.PROPOSED
        ):
            raise StaleRouteVersionError(
                "Only the latest candidate that passed KPI evaluation can be approved."
            )
        if not route.kpi_evaluation or not route.kpi_evaluation.passed:
            raise SafetyInterlockError(
                "Candidate did not pass mandatory KPI criteria."
            )
        if route.route_safety_issues or route.unreachable_sections:
            raise SafetyInterlockError(
                "Candidate contains unresolved route geometry."
            )
        if self.state_machine.get_state(route.job_id) != ExecutionState.AWAITING_APPROVAL:
            raise SafetyInterlockError("This job is not waiting for route approval. Review its current state.")
        # Recheck the actual geometry against current mandatory water data.
        # A saved KPI pass or an imported map must not bypass a known shoreline.
        geometry_issues = self.request_validator.validate_route_geometry(route, current_machine)
        if geometry_issues:
            route.route_safety_issues = geometry_issues
            self.state_machine.transition(route.job_id, ExecutionState.NEEDS_ASSISTANCE,
                actor, "Route failed its current pre-approval geometry check", role=role)
            raise SafetyInterlockError("Route failed the current water/access safety check. Replan before approval.")
        self.state_machine.register_job_machine(route.job_id, route.machine_id)
        self.state_machine.assert_machine_available(route.machine_id, route.job_id)
        route.status = RouteLifecycleStatus.APPROVED
        route.approved_by = actor
        self.state_machine.transition(
            route.job_id,
            ExecutionState.READY,
            actor,
            "Operator approved route",
            role=role,
            mandatory_constraints_passed=True,
        )
        self.audit.record(
            route.job_id,
            actor,
            "APPROVE_ROUTE",
            "Approved KPI-passing route",
            route_id,
            route.route_version,
        )
        return route

    def handle_operational_event(
        self,
        event: OperationalEventRequest,
        actor: str = "operator",
        role: OperatorRole = OperatorRole.OPERATOR,
    ) -> Dict[str, object]:
        kind = event.event_type.upper()
        observation_id = (
            event.waste_observation.detection_id
            if event.waste_observation
            else None
        )
        dedupe_value = (
            event.idempotency_key or event.detection_id or observation_id
        )
        event_key = (
            f"event:{event.job_id}:{dedupe_value}" if dedupe_value else None
        )
        cached_event = self.audit.check_idempotency(event_key)
        if cached_event is not None:
            return deepcopy(cached_event)

        def finish(result: Dict[str, object]) -> Dict[str, object]:
            if event_key:
                self.audit.save_idempotency(event_key, deepcopy(result))
            return result

        route = (
            self.routes_by_id.get(event.route_id) if event.route_id else None
        )
        if event.route_id and (route is None or route.job_id != event.job_id):
            raise LocationValidationError("The event route must belong to the supplied job.")
        if route is None:
            rid = self._latest_route_by_job.get(event.job_id)
            route = self.routes_by_id.get(rid) if rid else None
        if kind == "EMERGENCY_STOP":
            current = self.state_machine.get_state(event.job_id)
            if current != ExecutionState.EMERGENCY_STOP:
                self.state_machine.transition(
                    event.job_id,
                    ExecutionState.EMERGENCY_STOP,
                    actor,
                    "Emergency stop event",
                    role=role,
                )
                self.audit.record(event.job_id, actor, "EMERGENCY_STOP",
                    "Emergency stop recorded and latched for the machine.",
                    route_id=route.route_id if route else None,
                    route_version=route.route_version if route else None)
            return finish({"state": ExecutionState.EMERGENCY_STOP.value})
        if (
            event.route_id
            and self._latest_route_by_job.get(event.job_id) != event.route_id
        ):
            raise StaleRouteVersionError(
                "Operational event references a stale route version."
            )
        if (
            kind in {"SENSOR_FAULT", "FULL_PAYLOAD"}
            or (
                kind == "OBSTACLE_DETECTED" and event.new_exclusion_zone is None
            )
            or (kind == "LOW_BATTERY" and route is None)
        ):
            current = self.state_machine.get_state(event.job_id)
            if current not in (
                ExecutionState.EMERGENCY_STOP,
                ExecutionState.NEEDS_ASSISTANCE,
            ):
                self.state_machine.transition(
                    event.job_id,
                    ExecutionState.NEEDS_ASSISTANCE,
                    actor,
                    f"Operational event: {kind}",
                    role=role,
                )
            result = {
                "state": self.state_machine.get_state(event.job_id).value,
                "event_type": kind,
            }
            if kind == "OBSTACLE_DETECTED":
                result["replan_blocked_reason"] = (
                    "A validated obstacle exclusion polygon is required before replanning."
                )
            return finish(result)
        if (
            not route
            and kind == "LOCATION_CHANGE"
            and event.new_operating_boundary is not None
        ):
            req_initial = PlanRequest(
                job_id=event.job_id,
                machine_id="sweeper-alpha-01",
                operating_boundary=event.new_operating_boundary,
                exclusion_zones=(
                    [event.new_exclusion_zone]
                    if event.new_exclusion_zone
                    else []
                ),
            )
            candidate_init = self.plan_and_optimize(
                req_initial,
                actor=actor,
                replan_reason="operating_boundary_changed",
            )
            return finish(
                {
                    "state": self.state_machine.get_state(event.job_id).value,
                    "candidate": candidate_init,
                }
            )
        if not route:
            raise LocationValidationError(
                "No existing route was found for this operational event."
            )
        if kind == "LOCATION_CHANGE" and event.new_operating_boundary is None:
            raise LocationValidationError(
                "Location change requires a new operating boundary."
            )
        if kind == "OBSTACLE_DETECTED" and event.new_exclusion_zone is None:
            raise LocationValidationError(
                "Obstacle replanning requires a validated exclusion polygon."
            )
        if kind not in {
            "LOCATION_CHANGE",
            "OVERSIZED_RUBBISH",
            "MISSED_COLLECTION",
            "TIME_WINDOW_CHANGED",
            "ROUTE_DEVIATION",
            "HIGH_ROUTE_DEVIATION",
            "OBSTACLE_DETECTED",
            "LOW_BATTERY",
        }:
            raise LocationValidationError(
                f"Unsupported operational event type: {kind}."
            )

        if kind == "LOW_BATTERY":
            machine = self.machines[route.machine_id]
            if event.battery_percent is not None:
                machine.current_battery_percent = event.battery_percent
            elif not self.simulator.telemetry_store.get(route.route_id):
                current = self.state_machine.get_state(event.job_id)
                if current not in (
                    ExecutionState.EMERGENCY_STOP,
                    ExecutionState.NEEDS_ASSISTANCE,
                ):
                    self.state_machine.transition(
                        event.job_id,
                        ExecutionState.NEEDS_ASSISTANCE,
                        actor,
                        "Low-battery report had no current measured battery value",
                        role=role,
                    )
                return finish(
                    {
                        "state": self.state_machine.get_state(
                            event.job_id
                        ).value,
                        "event_type": kind,
                        "replan_blocked_reason": "A measured battery percentage is required to evaluate the remaining mission.",
                    }
                )

        tasks = [
            deepcopy(t) for t in route.tasks if event.retain_existing_tasks
        ]
        if kind == "OVERSIZED_RUBBISH":
            observation = event.waste_observation
            if (
                event.location is not None
                and observation is not None
                and observation.world_location is not None
                and event.location != observation.world_location
            ):
                raise LocationValidationError(
                    "Conflicting event and observation world locations were supplied."
                )
            location = event.location or (
                observation.world_location if observation else None
            )
            if location is None:
                raise LocationValidationError(
                    "An oversized collection report requires a location."
                )
            spec = event.rubbish_spec or (
                observation.rubbish_spec if observation else None
            )
            if spec is None and event.natural_language_report:
                spec = self.interpreter.parse(
                    event.natural_language_report
                ).detected_oversized_item
            if spec is None:
                from .models import RubbishItemSpec

                spec = RubbishItemSpec(
                    is_oversized=True,
                    dimensions_uncertain=True,
                    estimated_weight_kg=None,
                    estimated_volume_m3=None,
                )
            stored_observation = deepcopy(observation) if observation else None
            if stored_observation and stored_observation.world_location is None:
                stored_observation.world_location = location
            tasks.append(
                TaskItem(
                    task_type=TaskType.OVERSIZED_RUBBISH,
                    location=location,
                    priority=10,
                    rubbish_spec=spec,
                    waste_observation=stored_observation,
                    idempotency_key=dedupe_value,
                )
            )
        elif kind == "MISSED_COLLECTION":
            if not event.task_id:
                raise LocationValidationError(
                    "A missed-collection event requires task_id."
                )
            task = next(
                (deepcopy(t) for t in route.tasks if t.task_id == event.task_id),
                None,
            )
            if task is None:
                raise LocationValidationError(
                    f"Unknown collection task: {event.task_id}."
                )
            task.completed = False
            if all(t.task_id != task.task_id for t in tasks):
                tasks.append(task)
            else:
                tasks = [
                    task if t.task_id == task.task_id else t for t in tasks
                ]
        elif kind == "TIME_WINDOW_CHANGED":
            if (
                not event.task_id
                or event.task_time_window_start_min is None
                or event.task_time_window_end_min is None
            ):
                raise LocationValidationError(
                    "Time-window changes require task_id and both elapsed-minute bounds."
                )
            task = next(
                (deepcopy(t) for t in route.tasks if t.task_id == event.task_id),
                None,
            )
            if task is None:
                raise LocationValidationError(
                    f"Unknown collection task: {event.task_id}."
                )
            task.time_window_start_min = event.task_time_window_start_min
            task.time_window_end_min = event.task_time_window_end_min
            task = TaskItem.model_validate(task.model_dump())
            if all(t.task_id != task.task_id for t in tasks):
                tasks.append(task)
            else:
                tasks = [
                    task if t.task_id == task.task_id else t for t in tasks
                ]
        elif (
            kind in {"ROUTE_DEVIATION", "HIGH_ROUTE_DEVIATION"}
            and event.location is None
        ):
            raise LocationValidationError(
                "A route-deviation event requires the measured machine location."
            )

        current = self.state_machine.get_state(event.job_id)
        if current in (
            ExecutionState.READY,
            ExecutionState.EXECUTING,
            ExecutionState.PAUSED,
            ExecutionState.AWAITING_APPROVAL,
        ):
            self.state_machine.transition(
                event.job_id,
                ExecutionState.REPLANNING,
                actor,
                f"Replan after {kind}",
                role=role,
            )
            self.state_machine.transition(
                event.job_id,
                ExecutionState.VALIDATING,
                actor,
                "Validate event-driven plan",
                role=role,
            )
        elif current == ExecutionState.NEEDS_ASSISTANCE:
            self.state_machine.transition(
                event.job_id,
                ExecutionState.VALIDATING,
                actor,
                "Validate event-driven plan",
                role=role,
            )
        elif current == ExecutionState.IDLE:
            self.state_machine.transition(
                event.job_id,
                ExecutionState.VALIDATING,
                actor,
                "Validate event-driven plan",
                role=role,
            )
        new_boundary = event.new_operating_boundary or route.operating_boundary
        boundary_unchanged = (
            event.new_operating_boundary is None
            or event.new_operating_boundary == route.operating_boundary
        )
        previous_frames = self.simulator.telemetry_store.get(
            route.route_id, []
        )
        current_position = (
            event.location
            if kind in {"ROUTE_DEVIATION", "HIGH_ROUTE_DEVIATION"}
            and event.location
            else previous_frames[-1].position
            if previous_frames
            else route.origin
        )
        mission_elapsed_min = (
            previous_frames[-1].mission_elapsed_min
            if previous_frames
            else route.mission_elapsed_min
            + sum(
                segment.estimated_duration_sec
                for segment in route.segments
                if segment.completed
            )
            / 60.0
        )
        req = PlanRequest(
            job_id=event.job_id,
            enable_depot_cycles=route.depot_cycles_enabled,
            machine_id=route.machine_id,
            use_rl=route.use_rl,
            kpi_policy_override={key: value for key, value in route.kpi_policy_snapshot.items() if key != "policy_version"},
            start_location=current_position,
            mission_elapsed_min=mission_elapsed_min,
            disposal_location=route.disposal_depot,
            operating_boundary=new_boundary,
            environment_map=route.planning_environment_map,
            exclusion_zones=route.exclusion_zones
            + ([event.new_exclusion_zone] if event.new_exclusion_zone else []),
            tasks=tasks,
        )
        candidate = self.plan_and_optimize(
            req,
            actor=actor,
            completed_segments=route.segments if boundary_unchanged else None,
            replan_reason=(
                "obstacle_report_with_validated_exclusion"
                if kind == "OBSTACLE_DETECTED"
                else "new_validated_waste_location"
                if kind == "OVERSIZED_RUBBISH"
                else "missed_collection"
                if kind == "MISSED_COLLECTION"
                else "task_time_window_changed"
                if kind == "TIME_WINDOW_CHANGED"
                else "high_route_deviation"
                if kind in {"ROUTE_DEVIATION", "HIGH_ROUTE_DEVIATION"}
                else "low_battery_remaining_mission"
                if kind == "LOW_BATTERY"
                else "operating_boundary_changed"
            ),
        )
        previous_battery = (
            event.battery_percent
            if event.battery_percent is not None
            else previous_frames[-1].battery_percent
            if previous_frames
            else route.estimated_remaining_battery_percent
        )
        candidate.replan_comparison = self._compare_replan(
            route, candidate, previous_battery
        )
        candidate.candidate_rationale.append(
            f"Compared with route v{route.route_version}, remaining distance changed by "
            f"{candidate.replan_comparison.distance_delta_m:+.1f} m and remaining time by "
            f"{candidate.replan_comparison.duration_delta_min:+.1f} min."
        )
        self.audit.record(
            event.job_id,
            actor,
            f"EVENT:{kind}",
            "Operational event caused route revision",
            candidate.route_id,
            candidate.route_version,
            details={
                "detection_id": observation_id,
                "observation_source": (
                    event.waste_observation.source
                    if event.waste_observation
                    else None
                ),
            },
        )
        return finish(
            {
                "state": self.state_machine.get_state(event.job_id).value,
                "candidate": candidate,
            }
        )
