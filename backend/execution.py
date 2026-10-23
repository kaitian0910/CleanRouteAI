from typing import Dict, List, Optional, Set
from collections import OrderedDict
from threading import RLock
from math import hypot
from .config import KPIPolicyConfig, settings
from .evaluator import replay_depot_cycles
from .models import (
    DataProvenance,
    ExecutionState,
    MachineProfile,
    OperatorRole,
    RouteCandidate,
    RouteLifecycleStatus,
    TelemetryFrame,
    AuditAndEventManager,
)
from .validators import TaskAndLocationValidator


class InvalidStateTransitionError(Exception):
    pass


class SafetyInterlockError(Exception):
    pass


ALLOWED_TRANSITIONS: Dict[ExecutionState, Set[ExecutionState]] = {
    ExecutionState.IDLE: {ExecutionState.VALIDATING, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.EMERGENCY_STOP},
    ExecutionState.VALIDATING: {ExecutionState.PLANNING, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.ABORTED, ExecutionState.IDLE, ExecutionState.EMERGENCY_STOP},
    ExecutionState.PLANNING: {ExecutionState.EVALUATING, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP},
    ExecutionState.EVALUATING: {ExecutionState.PLANNING, ExecutionState.AWAITING_APPROVAL, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP},
    ExecutionState.AWAITING_APPROVAL: {ExecutionState.READY, ExecutionState.REPLANNING, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP},
    ExecutionState.READY: {ExecutionState.EXECUTING, ExecutionState.REPLANNING, ExecutionState.PAUSED, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP},
    ExecutionState.EXECUTING: {ExecutionState.PAUSED, ExecutionState.REPLANNING, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.COMPLETED, ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP},
    ExecutionState.PAUSED: {ExecutionState.EXECUTING, ExecutionState.REPLANNING, ExecutionState.READY, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP},
    ExecutionState.REPLANNING: {ExecutionState.VALIDATING, ExecutionState.PLANNING, ExecutionState.EVALUATING, ExecutionState.PAUSED, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP},
    ExecutionState.NEEDS_ASSISTANCE: {ExecutionState.VALIDATING, ExecutionState.REPLANNING, ExecutionState.PAUSED, ExecutionState.IDLE, ExecutionState.ABORTED, ExecutionState.EMERGENCY_STOP},
    ExecutionState.COMPLETED: {ExecutionState.IDLE, ExecutionState.VALIDATING, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.EMERGENCY_STOP},
    ExecutionState.ABORTED: {ExecutionState.IDLE, ExecutionState.VALIDATING, ExecutionState.NEEDS_ASSISTANCE, ExecutionState.EMERGENCY_STOP},
    ExecutionState.EMERGENCY_STOP: {ExecutionState.IDLE},
}


class ExecutionStateMachine:
    def __init__(self, audit: AuditAndEventManager) -> None:
        self._states: Dict[str, ExecutionState] = {}
        self._audit = audit
        self._lock = RLock()
        self._machine_by_job: Dict[str, str] = {}
        self._machine_owners: Dict[str, str] = {}
        self._machine_estops: Set[str] = set()

    @property
    def control_lock(self):
        """Short control updates share this lock; long planning never holds it."""
        return self._lock

    def register_job_machine(self, job_id: str, machine_id: str) -> None:
        """Bind job safety state to the physical resource it represents.

        Planning proposals can coexist. Approval reserves the machine until the
        job finishes, is aborted, or requires assistance. An emergency stop is
        latched separately so starting another job cannot clear it.
        """
        with self._lock:
            previous = self._machine_by_job.get(job_id)
            if previous is not None and previous != machine_id:
                raise SafetyInterlockError("A job cannot change its bound machine. Start a new job to change machines.")
            self._machine_by_job[job_id] = machine_id
            state = self.get_state(job_id)
            if state == ExecutionState.EMERGENCY_STOP:
                self._machine_estops.add(machine_id)

    def assert_machine_available(self, machine_id: str, job_id: str) -> None:
        with self._lock:
            self.assert_machine_not_stopped(machine_id)
            owner = self._machine_owners.get(machine_id)
            if owner is not None and owner != job_id:
                raise SafetyInterlockError("This machine is reserved by another unfinished job. Finish or safely resolve that job before starting another.")

    def assert_machine_not_stopped(self, machine_id: str) -> None:
        with self._lock:
            if machine_id in self._machine_estops:
                raise SafetyInterlockError("This machine has an active emergency stop. A new job or cancellation cannot clear it; a trusted hardware reset is required.")

    def get_state(self, job_id: str) -> ExecutionState:
        return self._states.get(job_id, ExecutionState.IDLE)

    def transition(
        self,
        job_id: str,
        target: ExecutionState,
        actor: str,
        reason: str,
        role: OperatorRole = OperatorRole.INTERNAL_SERVICE,
        mandatory_constraints_passed: bool = True,
        hardware_estop_cleared: bool = False,
    ) -> ExecutionState:
        with self._lock:
            return self._transition_locked(job_id, target, actor, reason, role,
                mandatory_constraints_passed, hardware_estop_cleared)

    def _transition_locked(
        self, job_id: str, target: ExecutionState, actor: str, reason: str,
        role: OperatorRole, mandatory_constraints_passed: bool,
        hardware_estop_cleared: bool,
    ) -> ExecutionState:
        current = self.get_state(job_id)
        if current == ExecutionState.EMERGENCY_STOP and target != ExecutionState.EMERGENCY_STOP:
            if target != ExecutionState.IDLE or not hardware_estop_cleared:
                raise SafetyInterlockError("EMERGENCY_STOP active. Hardware reset required.")
            if role not in (OperatorRole.SUPERVISOR, OperatorRole.ADMIN):
                raise SafetyInterlockError("Supervisor or admin role required to clear EMERGENCY_STOP.")

        if target in (ExecutionState.READY, ExecutionState.EXECUTING) and not mandatory_constraints_passed:
            raise SafetyInterlockError(f"Cannot enter {target.value}: mandatory KPI constraints failed.")

        machine_id = self._machine_by_job.get(job_id)
        if machine_id is not None and target in (ExecutionState.READY, ExecutionState.EXECUTING):
            self.assert_machine_available(machine_id, job_id)

        if current == target:
            return current
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidStateTransitionError(f"Illegal transition {current.value} -> {target.value}")

        self._states[job_id] = target
        if machine_id is not None:
            if target == ExecutionState.EMERGENCY_STOP:
                self._machine_estops.add(machine_id)
                self._machine_owners.pop(machine_id, None)
            elif current == ExecutionState.EMERGENCY_STOP and target == ExecutionState.IDLE:
                # Reaching this branch already required the existing trusted
                # hardware confirmation and supervisor/admin role above.
                self._machine_estops.discard(machine_id)
            elif target in (ExecutionState.READY, ExecutionState.EXECUTING):
                self._machine_owners[machine_id] = job_id
            if target in (ExecutionState.COMPLETED, ExecutionState.ABORTED,
                          ExecutionState.IDLE, ExecutionState.NEEDS_ASSISTANCE):
                if self._machine_owners.get(machine_id) == job_id:
                    self._machine_owners.pop(machine_id, None)
        self._audit.record(job_id=job_id, actor=actor, action=f"STATE:{current.value}->{target.value}", reason=reason)
        return target


class DeterministicRouteSimulator:
    def __init__(self, state_machine: ExecutionStateMachine, audit: AuditAndEventManager) -> None:
        self.state_machine = state_machine
        self.audit = audit
        self.telemetry_store: Dict[str, List[TelemetryFrame]] = {}
        self.route_validator = TaskAndLocationValidator()
        self.latest_route_by_job: Optional[Dict[str, str]] = None
        self.on_complete = None
        # HTTP execute/resume share one lock and bounded response cache so a
        # retried one-step request cannot move the simulator twice.
        self.execution_request_lock = RLock()
        self.execution_request_cache = OrderedDict()

    def execute_simulation(
        self, route: RouteCandidate, machine: MachineProfile, stop_after_segments: Optional[int] = None
    ) -> List[TelemetryFrame]:
        self.state_machine.register_job_machine(route.job_id, route.machine_id)
        self.state_machine.assert_machine_available(route.machine_id, route.job_id)
        st = self.state_machine.get_state(route.job_id)
        initial_run = st == ExecutionState.READY and route.status == RouteLifecycleStatus.APPROVED
        resumed_run = st == ExecutionState.EXECUTING and route.status == RouteLifecycleStatus.EXECUTING
        if st == ExecutionState.EMERGENCY_STOP or not (initial_run or resumed_run):
            raise SafetyInterlockError("Route must be approved and in READY state, or already executing after a safe resume.")
        if not route.kpi_evaluation or not route.kpi_evaluation.passed:
            self.state_machine.transition(
                route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                "Route no longer passes the current KPI evaluation",
            )
            raise SafetyInterlockError("Simulation requires a route that passed the current KPI evaluation.")
        if route.route_safety_issues or route.unreachable_sections:
            self.state_machine.transition(
                route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                "Route has unresolved geometry issues",
            )
            raise SafetyInterlockError("Simulation is blocked by unresolved route geometry.")
        if not route.approved_by:
            self.state_machine.transition(
                route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                "Simulation requires recorded human approval",
            )
            raise SafetyInterlockError("Simulation requires an explicit recorded human approval.")
        if machine.machine_id != route.machine_id:
            self.state_machine.transition(
                route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                "Simulation machine does not match the approved route",
            )
            raise SafetyInterlockError("Simulation machine does not match the approved route.")
        if machine.planning_revision != route.machine_profile_revision:
            raise SafetyInterlockError("Machine parameters changed after planning. Replan and approve before execution.")

        if initial_run:
            self.state_machine.transition(route.job_id, ExecutionState.EXECUTING, "simulator", "Executing simulation")
        route.status = RouteLifecycleStatus.EXECUTING
        policy = KPIPolicyConfig.model_validate(route.kpi_policy_snapshot) if route.kpi_policy_snapshot else settings.kpi_policy
        energy_until_depot: Dict[str, float] = {}
        if route.depot_cycles_enabled:
            previous_frames = self.telemetry_store.get(route.route_id, [])
            replay = replay_depot_cycles(route, machine, policy,
                max(route.mission_elapsed_min, previous_frames[-1].mission_elapsed_min if previous_frames else 0.0))
            if (replay["issues"]
                or replay["max_outing_duration_minutes"] > policy.max_estimated_duration_minutes + 1e-6
                or replay["max_outing_distance_m"] > policy.max_travel_distance_m + 1e-6):
                self.state_machine.transition(route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Depot-cycle mission failed independent battery, service or outing validation")
                raise SafetyInterlockError("Simulation stopped because depot-cycle validation failed.")
            # Linear look-ahead table: before every leg retain enough energy for
            # all remaining approved movement up to the next depot service.
            remaining_energy = 0.0
            for segment in reversed(route.segments):
                if segment.completed:
                    continue
                if segment.segment_type == "DEPOT_RECHARGE":
                    remaining_energy = 0.0
                    continue
                leg_m = max(segment.distance_m, sum(hypot(*b.to_local_meters(a))
                    for a, b in zip(segment.waypoints, segment.waypoints[1:])), 0.0)
                rate = machine.energy_consumption_kwh_per_km_sweep if segment.segment_type == "SWEEP_SWATH" else machine.energy_consumption_kwh_per_km_travel
                remaining_energy += leg_m / 1000.0 * rate / machine.battery_capacity_kwh * 100.0
                energy_until_depot[segment.segment_id] = remaining_energy
        frames = self.telemetry_store.setdefault(route.route_id, [])
        battery = machine.current_battery_percent
        payload = machine.current_payload_kg
        payload_volume = machine.current_payload_volume_m3
        elapsed_min = max(
            route.mission_elapsed_min,
            frames[-1].mission_elapsed_min if frames else route.mission_elapsed_min,
        )
        schedule_by_task = {entry.task_id: entry for entry in route.task_schedule}
        count = 0

        for seg in route.segments:
            if seg.completed:
                continue
            self.state_machine.assert_machine_available(machine.machine_id, route.job_id)
            if machine.planning_revision != route.machine_profile_revision:
                self.state_machine.transition(route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Machine planning parameters changed during execution")
                raise SafetyInterlockError("Machine parameters changed during execution; replan before continuing.")
            # Recheck state and hard constraints at every simulated segment boundary.
            if self.state_machine.get_state(route.job_id) != ExecutionState.EXECUTING:
                raise SafetyInterlockError("Simulation stopped because the execution state changed.")
            if route.status != RouteLifecycleStatus.EXECUTING:
                if self.state_machine.get_state(route.job_id) == ExecutionState.EXECUTING:
                    self.state_machine.transition(
                        route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                        "Route is no longer active",
                    )
                raise SafetyInterlockError("Simulation stopped because this route is no longer active.")
            if not route.approved_by:
                self.state_machine.transition(
                    route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Simulation approval was removed during execution",
                )
                raise SafetyInterlockError("Simulation stopped because human approval is missing.")
            if (
                self.latest_route_by_job is not None
                and self.latest_route_by_job.get(route.job_id) != route.route_id
            ):
                self.state_machine.transition(
                    route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Active route version became stale during execution",
                )
                raise SafetyInterlockError("Simulation stopped because this route version is stale.")
            if not route.kpi_evaluation or not route.kpi_evaluation.passed:
                self.state_machine.transition(
                    route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Route KPI evaluation failed during execution",
                )
                raise SafetyInterlockError("Simulation stopped because the route no longer passes KPI policy.")
            geometry_issues = self.route_validator.validate_route_geometry(
                route, machine, segments_to_check=[seg],
            )
            if geometry_issues:
                self.state_machine.transition(
                    route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Segment geometry failed its pre-execution recheck",
                )
                raise SafetyInterlockError("Simulation stopped before a segment that failed geometry validation.")
            if payload > machine.max_payload_kg or payload_volume > machine.max_payload_volume_m3:
                self.state_machine.transition(
                    route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Current payload exceeds machine capacity",
                )
                raise SafetyInterlockError("Simulation stopped because the current payload exceeds machine capacity.")
            valid_recharge = (
                route.depot_cycles_enabled and seg.segment_type == "DEPOT_RECHARGE"
                and bool(seg.waypoints)
                and all(hypot(*p.to_local_meters(route.disposal_depot)) <= 1.0 for p in seg.waypoints)
                and seg.distance_m <= 0.1
                and seg.estimated_duration_sec / 60.0 + 1e-6 >= machine.depot_service_minutes
            )
            if seg.segment_type == "DEPOT_RECHARGE" and not valid_recharge:
                self.state_machine.transition(route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Recharge is not stationary at the depot or service duration is insufficient")
                raise SafetyInterlockError("Simulation stopped before invalid depot recharge.")
            if battery + 1e-6 < policy.min_energy_reserve_percent and not valid_recharge:
                self.state_machine.transition(
                    route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Current battery is below the configured reserve",
                )
                raise SafetyInterlockError("Simulation stopped because current battery is below the configured reserve.")
            if (route.depot_cycles_enabled and not valid_recharge
                and battery - energy_until_depot.get(seg.segment_id, 0.0) + 1e-6 < policy.min_energy_reserve_percent):
                self.state_machine.transition(route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                    "Remaining outing no longer retains the battery reserve for its depot return")
                raise SafetyInterlockError("Simulation stopped before starting work without a safe depot return reserve.")
            if stop_after_segments is not None and count >= stop_after_segments:
                self.state_machine.transition(route.job_id, ExecutionState.PAUSED, "simulator", "Safe pause at segment boundary")
                return frames

            next_elapsed_min = elapsed_min + max(seg.estimated_duration_sec, 0.0) / 60.0
            if seg.segment_type == "COLLECTION_STOP" and seg.target_task_id:
                schedule = schedule_by_task.get(seg.target_task_id)
                if schedule is None:
                    self.state_machine.transition(
                        route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                        "Collection stop has no feasible schedule entry",
                    )
                    raise SafetyInterlockError("Simulation stopped because a collection stop has no valid schedule.")
                if (
                    elapsed_min + 0.02 < schedule.window_start_min
                    or next_elapsed_min > schedule.window_end_min + 0.02
                ):
                    self.state_machine.transition(
                        route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                        "Collection service would miss its elapsed mission time window",
                    )
                    raise SafetyInterlockError("Simulation stopped because collection service would miss its time window.")

            distance_km = max(seg.distance_m, 0.0) / 1000.0
            energy_kwh = distance_km * (
                machine.energy_consumption_kwh_per_km_sweep
                if seg.segment_type == "SWEEP_SWATH"
                else machine.energy_consumption_kwh_per_km_travel
            )
            if machine.battery_capacity_kwh > 0:
                projected_battery = battery - energy_kwh / machine.battery_capacity_kwh * 100.0
                if projected_battery + 1e-6 < policy.min_energy_reserve_percent and not valid_recharge:
                    self.state_machine.transition(
                        route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                        "Projected segment would cross the configured battery reserve",
                    )
                    raise SafetyInterlockError("Simulation stopped before a segment that would breach the battery reserve.")

            if seg.segment_type == "COLLECTION_STOP" and seg.target_task_id:
                task = next((t for t in route.tasks if t.task_id == seg.target_task_id), None)
                if task and task.rubbish_spec:
                    next_payload_kg = payload + (task.rubbish_spec.estimated_weight_kg or 0.0)
                    next_payload_volume = payload_volume + (task.rubbish_spec.estimated_volume_m3 or 0.0)
                    if next_payload_kg > machine.max_payload_kg or next_payload_volume > machine.max_payload_volume_m3:
                        self.state_machine.transition(
                            route.job_id, ExecutionState.NEEDS_ASSISTANCE, "simulator",
                            "Collection stop would exceed the machine weight or volume limit",
                        )
                        raise SafetyInterlockError("Simulation stopped before exceeding the machine payload weight or volume limit.")
                    payload = next_payload_kg
                    payload_volume = next_payload_volume
            elif seg.segment_type in {"RETURN_TO_BASE", "DEPOT_RECHARGE"}:
                payload = 0.0
                payload_volume = 0.0

            seg.completed = True
            seg.provenance = DataProvenance.SIMULATED
            if seg.segment_type == "COLLECTION_STOP" and seg.target_task_id:
                for task in route.tasks:
                    if task.task_id == seg.target_task_id:
                        task.completed = True
                        break
            if machine.battery_capacity_kwh > 0:
                battery = max(0.0, battery - energy_kwh / machine.battery_capacity_kwh * 100.0)
            if valid_recharge:
                battery = machine.recharged_battery_percent
                self.audit.record(job_id=route.job_id, actor="simulator", action="DEPOT_RECHARGE",
                    reason=f"Completed {seg.estimated_duration_sec / 60.0:.1f} min depot service; battery {battery:.1f}%, payload unloaded")
            machine.current_battery_percent = battery
            machine.current_payload_kg = payload
            machine.current_payload_volume_m3 = payload_volume
            elapsed_min = next_elapsed_min
            count += 1
            frames.append(
                TelemetryFrame(
                    machine_id=machine.machine_id,
                    route_id=route.route_id,
                    mission_elapsed_min=round(elapsed_min, 3),
                    position=seg.waypoints[-1] if seg.waypoints else route.origin,
                    speed_mps=(
                        0.0 if seg.segment_type in {"WAIT_FOR_WINDOW", "COLLECTION_STOP", "DEPOT_RECHARGE"}
                        else machine.sweep_speed_mps if seg.segment_type in {"SWEEP_SWATH", "TURN_CONNECTOR"}
                        else machine.cruise_speed_mps
                    ),
                    battery_percent=round(battery, 2),
                    payload_kg=round(payload, 2),
                    payload_volume_m3=round(payload_volume, 3),
                    active_segment_id=seg.segment_id,
                    provenance=DataProvenance.SIMULATED,
                )
            )

        if self.state_machine.get_state(route.job_id) != ExecutionState.EXECUTING:
            raise SafetyInterlockError("Simulation stopped because the execution state changed before completion.")
        route.status = RouteLifecycleStatus.COMPLETED
        self.state_machine.transition(route.job_id, ExecutionState.COMPLETED, "simulator", "Simulation complete")
        if self.on_complete and frames:
            self.on_complete(route, frames[-1].mission_elapsed_min)
        return frames
