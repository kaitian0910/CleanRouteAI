from __future__ import annotations
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
import pydantic
from pydantic import Field
from shapely.geometry import Polygon

from .config import CompatBaseModel, PYDANTIC_V2

if PYDANTIC_V2:
    from pydantic import model_validator
else:
    from pydantic import root_validator

    def model_validator(*, mode: str = "after"):
        def decorator(fn):
            @root_validator(pre=(mode == "before"), skip_on_failure=True, allow_reuse=True)
            def _wrapper(cls, values):
                if mode == "before":
                    return fn(cls, values)
                obj = cls.construct(**values)
                res = fn(obj)
                if isinstance(res, cls):
                    return res.__dict__
                return values
            return _wrapper
        return decorator


POSTGIS_INIT_SQL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS jobs (
    job_id VARCHAR(64) PRIMARY KEY,
    machine_id VARCHAR(64) NOT NULL,
    execution_state VARCHAR(32) NOT NULL DEFAULT 'IDLE',
    active_epoch INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS route_candidates (
    route_id VARCHAR(64) PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL REFERENCES jobs(job_id),
    route_version INTEGER NOT NULL,
    plan_epoch INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    operating_boundary GEOMETRY(Polygon, 4326),
    route_path GEOMETRY(MultiLineString, 4326),
    kpi_passed BOOLEAN NOT NULL DEFAULT FALSE,
    kpi_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionState(str, Enum):
    IDLE = "IDLE"
    VALIDATING = "VALIDATING"
    PLANNING = "PLANNING"
    EVALUATING = "EVALUATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY = "READY"
    EXECUTING = "EXECUTING"
    PAUSED = "PAUSED"
    REPLANNING = "REPLANNING"
    NEEDS_ASSISTANCE = "NEEDS_ASSISTANCE"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class RouteLifecycleStatus(str, Enum):
    PROPOSED = "PROPOSED"
    DRAFT_FAILED_KPI = "DRAFT_FAILED_KPI"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SUPERSEDED = "SUPERSEDED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class TaskType(str, Enum):
    AREA_SWEEP = "AREA_SWEEP"
    RUBBISH_COLLECTION = "RUBBISH_COLLECTION"
    OVERSIZED_RUBBISH = "OVERSIZED_RUBBISH"
    PATROL = "PATROL"
    HYBRID = "HYBRID"


class DataProvenance(str, Enum):
    PLANNED = "PLANNED"
    ESTIMATED = "ESTIMATED"
    SIMULATED = "SIMULATED"
    MEASURED_TELEMETRY = "MEASURED_TELEMETRY"
    SYNTHETIC_FALLBACK_GRAPH = "SYNTHETIC_FALLBACK_GRAPH"


class OperatorRole(str, Enum):
    OPERATOR = "operator"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"
    INTERNAL_SERVICE = "internal_service"


class TerrainFeatureType(str, Enum):
    PUBLIC_ROAD = "PUBLIC_ROAD"
    SERVICE_ROAD = "SERVICE_ROAD"
    PEDESTRIAN_PLAZA = "PEDESTRIAN_PLAZA"
    PEDESTRIAN_WALKWAY = "PEDESTRIAN_WALKWAY"
    WATER_BODY = "WATER_BODY"
    BUILDING = "BUILDING"
    RESTRICTED_ZONE = "RESTRICTED_ZONE"
    UNVERIFIED_TERRAIN = "UNVERIFIED_TERRAIN"


class Coordinate(CompatBaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)

    @model_validator(mode="after")
    def validate_finite_coord(self) -> "Coordinate":
        if not math.isfinite(self.lat) or not math.isfinite(self.lon):
            raise ValueError("Coordinates must be finite numbers.")
        return self

    def to_local_meters(self, origin: Coordinate) -> Tuple[float, float]:
        lat_rad = math.radians(origin.lat)
        x = (self.lon - origin.lon) * 111320.0 * math.cos(lat_rad)
        y = (self.lat - origin.lat) * 110540.0
        return (x, y)

    @staticmethod
    def from_local_meters(x: float, y: float, origin: Coordinate) -> Coordinate:
        lat_rad = math.radians(origin.lat)
        lon = origin.lon + x / (111320.0 * math.cos(lat_rad))
        lat = origin.lat + y / 110540.0
        return Coordinate(lat=round(lat, 7), lon=round(lon, 7))


class GeoPolygon(CompatBaseModel):
    vertices: List[Coordinate] = Field(...)

    @model_validator(mode="after")
    def validate_polygon_geometry(self) -> "GeoPolygon":
        if not self.vertices or len(self.vertices) < 3:
            raise ValueError("GeoPolygon requires at least 3 vertices.")
        origin = self.vertices[0]
        pts = [v.to_local_meters(origin) for v in self.vertices]
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        poly = Polygon(pts)
        if not poly.is_valid or poly.area < 4.0:
            raise ValueError("Invalid operating polygon: self-intersecting or area below 4.0 m^2.")
        return self

    def to_shapely_local(self, origin: Coordinate) -> Polygon:
        pts = [v.to_local_meters(origin) for v in self.vertices]
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        return Polygon(pts)

    @staticmethod
    def from_shapely_local(poly: Polygon, origin: Coordinate) -> Optional["GeoPolygon"]:
        if poly.is_empty or not isinstance(poly, Polygon) or poly.area < 4.0:
            return None
        coords = list(poly.exterior.coords)[:-1]
        if len(coords) < 3:
            return None
        vertices = [Coordinate.from_local_meters(float(x), float(y), origin) for x, y in coords]
        try:
            return GeoPolygon(vertices=vertices)
        except ValueError:
            return None


class ExclusionZone(CompatBaseModel):
    zone_id: str = Field(default_factory=lambda: f"excl-{uuid4().hex[:8]}")
    name: str = "Restricted Zone"
    reason: str = "Obstacle / Prohibited Area"
    polygon: GeoPolygon


class TerrainFeature(CompatBaseModel):
    feature_id: str = Field(default_factory=lambda: f"feat-{uuid4().hex[:8]}")
    name: str
    feature_type: TerrainFeatureType
    polygon: GeoPolygon
    clearance_buffer_m: float = Field(default=0.0, ge=0.0)
    max_slope_deg: float = Field(default=2.0, ge=0.0)
    min_clearance_width_m: float = Field(default=3.0, gt=0.0)
    allowed_machine_ids: Optional[List[str]] = None


class AuthorizedCrossing(CompatBaseModel):
    crossing_id: str = Field(default_factory=lambda: f"xing-{uuid4().hex[:8]}")
    name: str
    crossing_type: str = "PEDESTRIAN_CROSSWALK"
    start_point: Coordinate
    end_point: Coordinate
    corridor_polygon: Optional[GeoPolygon] = None
    width_m: float = Field(default=2.5, gt=0.0)
    max_weight_kg: float = Field(default=300.0, gt=0.0)
    allowed_machine_ids: List[str] = Field(
        default_factory=lambda: ["sweeper-alpha-01"]
    )
    allows_heavy_collector: bool = False
    risk_score: float = Field(default=0.15, ge=0.0, le=1.0)


class TraversabilityNode(CompatBaseModel):
    node_id: str
    node_type: str = "PATH_INTERSECTION"
    location: Coordinate
    cell_id: Optional[str] = None


class TraversabilityEdge(CompatBaseModel):
    edge_id: str = Field(default_factory=lambda: f"edge-{uuid4().hex[:8]}")
    source_id: str
    target_id: str
    waypoints: List[Coordinate] = Field(default_factory=list)
    distance_m: float = Field(default=0.0, ge=0.0)
    travel_time_sec: float = Field(default=0.0, ge=0.0)
    surface_class: str = "PEDESTRIAN_PLAZA"
    risk_score: float = Field(default=0.05, ge=0.0, le=1.0)
    clearance_width_m: float = Field(default=3.0, gt=0.0)
    slope_deg: float = Field(default=1.0, ge=0.0)
    allowed_machine_ids: Optional[List[str]] = None
    permitted: bool = True
    prohibition_reason: Optional[str] = None


class EnvironmentMapData(CompatBaseModel):
    map_id: str = "kl-perdana-planetarium-gis"
    features: List[TerrainFeature] = Field(default_factory=list)
    authorized_crossings: List[AuthorizedCrossing] = Field(default_factory=list)
    verified_traversable_polygons: List[GeoPolygon] = Field(default_factory=list)
    require_verified_terrain: bool = False
    hazard_coverage_boundary: Optional[GeoPolygon] = None
    water_map_version: Optional[str] = None
    nav_nodes: List[TraversabilityNode] = Field(default_factory=list)
    nav_edges: List[TraversabilityEdge] = Field(default_factory=list)


class MachineProfile(CompatBaseModel):
    machine_id: str
    name: str
    planning_revision: int = Field(default=0, ge=0)
    working_width_m: float = Field(default=1.6, gt=0.0)
    default_overlap_ratio: float = Field(default=0.12, ge=0.0, le=0.65)
    min_turning_radius_m: float = Field(default=1.2, gt=0.0)
    clearance_width_m: float = Field(default=1.4, gt=0.0)
    cruise_speed_mps: float = Field(default=1.5, gt=0.0)
    sweep_speed_mps: float = Field(default=1.0, gt=0.0)
    max_payload_kg: float = Field(default=180.0, gt=0.0)
    max_payload_volume_m3: float = Field(default=1.2, gt=0.0)
    max_item_length_m: float = Field(default=1.2, gt=0.0)
    battery_capacity_kwh: float = Field(default=12.0, gt=0.0)
    current_battery_percent: float = Field(default=95.0, ge=0.0, le=100.0)
    depot_service_minutes: float = Field(default=20.0, gt=0.0, le=240.0)
    recharged_battery_percent: float = Field(default=100.0, gt=0.0, le=100.0)
    energy_consumption_kwh_per_km_travel: float = Field(default=0.45, ge=0.0)
    energy_consumption_kwh_per_km_sweep: float = Field(default=0.85, ge=0.0)
    has_heavy_lifter: bool = False
    has_bulky_compactor: bool = False
    current_payload_kg: float = Field(default=0.0, ge=0.0)
    current_payload_volume_m3: float = Field(default=0.0, ge=0.0)
    # Road & Surface Access Permissions
    allowed_surface_classes: List[str] = Field(
        default_factory=lambda: [
            "PEDESTRIAN_PLAZA",
            "PEDESTRIAN_WALKWAY",
            "SERVICE_ROAD",
        ]
    )
    allow_public_road_sweeping: bool = False
    allow_public_road_transit: bool = False
    allow_authorized_road_crossing: bool = True
    requires_heavy_rated_crossing: bool = False
    road_clearance_buffer_m: float = Field(default=1.0, ge=0.0)
    max_slope_deg: float = Field(default=12.0, ge=0.0)


class RubbishItemSpec(CompatBaseModel):
    description: str = "Standard bin collection"
    is_oversized: bool = False
    estimated_weight_kg: Optional[float] = Field(default=15.0, ge=0.0)
    estimated_volume_m3: Optional[float] = Field(default=0.12, ge=0.0)
    max_dimension_m: Optional[float] = Field(default=0.6, ge=0.0)
    dimensions_uncertain: bool = False
    requires_heavy_lifter: bool = False
    weight_estimation_method: Optional[str] = None
    weight_estimation_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    volume_estimation_method: Optional[str] = None
    volume_estimation_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    dimension_estimation_method: Optional[str] = None
    dimension_estimation_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class ImageBoundingBox(CompatBaseModel):
    """Normalized image coordinates; never treated as a world/map position."""
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ImageBoundingBox":
        if self.x + self.width > 1.0 or self.y + self.height > 1.0:
            raise ValueError("Normalized image bounding box must fit within [0, 1] coordinates.")
        return self


class WasteObservation(CompatBaseModel):
    """Optional detector report; actionability still requires a validated world location."""
    detection_id: str
    waste_class: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    observed_at: Optional[datetime] = None
    source: Optional[str] = None
    image_bbox: Optional[ImageBoundingBox] = None
    world_location: Optional[Coordinate] = None
    rubbish_spec: Optional[RubbishItemSpec] = None


class RLBinState(CompatBaseModel):
    """Explicit simulated/measured inputs for the supplied G3 six-bin policy."""
    fill_kg: float = Field(ge=0.0, le=140.0, allow_inf_nan=False)
    traffic_multiplier: float = Field(ge=0.0, le=2.0, allow_inf_nan=False)
    accident: bool = False


class RLPolicyStatus(CompatBaseModel):
    mode: str = "DETERMINISTIC_FALLBACK"
    checkpoint_loaded: bool = False
    checkpoint: str = "ppo_model.zip"
    observation_schema: str = "g3-six-bin-48-v1"
    action_schema: str = "g3-dispatch-14-v1"
    detail: str = "RL policy has not been requested."
    training_enabled: bool = False


class CandidateFeedback(CompatBaseModel):
    route_id: str
    route_version: int
    source: str
    observation: List[float] = Field(default_factory=list)
    proposed_action: Optional[Dict[str, Any]] = None
    reward: float
    reward_version: str = "cleanrouteai-kpi-v1"
    reward_components: Dict[str, float] = Field(default_factory=dict)
    feasible: bool
    failed_constraints: List[str] = Field(default_factory=list)
    decision: str
    reason: str
    metrics: Dict[str, float] = Field(default_factory=dict)
    metric_deltas: Dict[str, float] = Field(default_factory=dict)
    inference_ms: float = 0.0


class TaskItem(CompatBaseModel):
    task_id: str = Field(default_factory=lambda: f"task-{uuid4().hex[:8]}")
    idempotency_key: Optional[str] = None
    task_type: TaskType
    location: Coordinate
    priority: int = Field(default=5, ge=1, le=10)
    service_duration_sec: float = Field(default=60.0, ge=0.0)
    time_window_start_min: float = Field(default=0.0, ge=0.0)
    time_window_end_min: float = Field(default=480.0, ge=0.0)
    rubbish_spec: Optional[RubbishItemSpec] = None
    waste_observation: Optional[WasteObservation] = None
    rl_bin_state: Optional[RLBinState] = None
    completed: bool = False

    @model_validator(mode="after")
    def validate_time_window(self) -> "TaskItem":
        if self.time_window_end_min <= self.time_window_start_min:
            raise ValueError("Task time window end must be after its start (elapsed mission minutes).")
        # Standard bin pins previously had no contents specification, so both
        # capacity planning and simulated collection treated them as zero load.
        # Use the existing editable bin estimates; never infer photo weight or
        # retroactively assign a weight to an old, already completed pickup.
        if (self.task_type == TaskType.RUBBISH_COLLECTION and self.rubbish_spec is None
                and self.waste_observation is None and not self.completed):
            self.rubbish_spec = RubbishItemSpec(
                description="Standard bin contents (editable estimate)",
                weight_estimation_method="DEFAULT_BIN_CONTENTS_ESTIMATE",
                volume_estimation_method="DEFAULT_BIN_CONTENTS_ESTIMATE",
            )
        return self


class TaskScheduleEntry(CompatBaseModel):
    task_id: str
    arrival_min: float
    service_start_min: float
    service_end_min: float
    waiting_min: float
    window_start_min: float
    window_end_min: float
    priority: int
    status: str = "SCHEDULED"
    reason: Optional[str] = None
    disposal_before: bool = False


class RouteSegment(CompatBaseModel):
    outing_index: int = Field(default=1, ge=1)
    segment_id: str = Field(default_factory=lambda: f"seg-{uuid4().hex[:8]}")
    segment_type: str
    waypoints: List[Coordinate]
    distance_m: float
    estimated_duration_sec: float
    swath_width_m: float = 0.0
    target_task_id: Optional[str] = None
    cell_id: Optional[str] = None
    surface_class: str = "PEDESTRIAN_PLAZA"
    crossing_id: Optional[str] = None
    provenance: DataProvenance = DataProvenance.PLANNED
    completed: bool = False


class UnreachableSection(CompatBaseModel):
    section_id: str
    reason: str
    area_m2: float
    centroid: Coordinate


class CoverageCellSummary(CompatBaseModel):
    cell_id: str
    polygon: GeoPolygon
    area_m2: float
    reachable: bool
    swept: bool
    surface_class: str = "PEDESTRIAN_PLAZA"
    exclusion_reason: Optional[str] = None
    swath_count: int = 0


class RecoveryAttemptRecord(CompatBaseModel):
    attempt_number: int
    route_version: int
    failure_classification: str
    failure_cause: str
    recovery_action: str
    parameters_changed: Dict[str, Any] = Field(default_factory=dict)
    succeeded: bool = False


class PlanningDiagnostics(CompatBaseModel):
    planning_status: str = "SUCCESS"  # SUCCESS | PARTIAL_SUCCESS | BLOCKED | FAILED_KPI
    route_id: str
    route_version: int
    machine_id: str
    task_mode: TaskType
    requested_area_m2: float = 0.0
    effective_eligible_area_m2: float = 0.0
    covered_area_m2: float = 0.0
    inaccessible_area_m2: float = 0.0
    unverified_area_m2: float = 0.0
    reachable_coverage_percent: Optional[float] = None
    requested_coverage_percent: Optional[float] = None
    inaccessible_area_percent: Optional[float] = None
    unverified_area_percent: Optional[float] = None
    total_distance_m: Optional[float] = None
    estimated_duration_minutes: Optional[float] = None
    total_cells_detected: int = 0
    reachable_cells_count: int = 0
    planned_cells_count: int = 0
    completed_cells_count: int = 0
    excluded_cells_count: int = 0
    coverage_cells: List[CoverageCellSummary] = Field(default_factory=list)
    road_access_conflicts: List[str] = Field(default_factory=list)
    obstacle_and_connectivity_issues: List[str] = Field(default_factory=list)
    replanning_attempts_count: int = 0
    recovery_actions_applied: List[str] = Field(default_factory=list)
    recovery_history: List[RecoveryAttemptRecord] = Field(default_factory=list)
    excluded_sections: List[UnreachableSection] = Field(default_factory=list)
    passed_final_validation: bool = False
    summary_explanation: str = ""


class MetricEntry(CompatBaseModel):
    actual: float
    target: Optional[float] = None
    target_max: Optional[float] = None
    target_min: Optional[float] = None
    passed: bool
    is_constraint: bool = True
    provenance: DataProvenance = DataProvenance.ESTIMATED
    unit: str = ""


class KPIEvaluationResponse(CompatBaseModel):
    evaluation_id: str = Field(default_factory=lambda: f"eval-{uuid4().hex[:8]}")
    route_version: int
    policy_version: str
    passed: bool
    score: float
    metrics: Dict[str, MetricEntry]
    failed_constraints: List[str]
    recommended_adjustments: List[str]
    requires_human_review: bool
    evaluated_at: datetime = Field(default_factory=utc_now)


class ReplanComparison(CompatBaseModel):
    previous_route_id: str
    previous_route_version: int
    previous_remaining_distance_m: float
    revised_remaining_distance_m: float
    distance_delta_m: float
    previous_remaining_duration_min: float
    revised_remaining_duration_min: float
    duration_delta_min: float
    previous_coverage_percent: float
    revised_coverage_percent: float
    coverage_delta_percent: float
    previous_missed_area_m2: float
    revised_missed_area_m2: float
    missed_area_delta_m2: float
    previous_battery_percent: float
    revised_remaining_battery_percent: float
    open_collection_tasks_delta: int


class RepeatPlanComparison(CompatBaseModel):
    previous_route_id: str
    candidates_checked: int
    feasible_candidates: int
    previous_estimated_completion_minutes: float
    previous_actual_completion_minutes: Optional[float] = None
    new_estimated_completion_minutes: float
    estimated_time_change_minutes: float
    sweep_pattern_changed: bool
    collection_order_changed: bool
    travel_path_changed: bool
    history_applied: bool
    summary: str


class RouteCandidate(CompatBaseModel):
    planning_run_id: Optional[str] = None
    candidate_index: int = Field(default=1, ge=1)
    is_selected_candidate: bool = False
    repeat_of_route_id: Optional[str] = None
    repeat_comparison: Optional[RepeatPlanComparison] = None
    history_hint_applied: bool = False
    use_rl: bool = True
    depot_cycles_enabled: bool = False
    initial_battery_percent: Optional[float] = None
    outing_count: int = 1
    depot_visits: int = 0
    charging_duration_minutes: float = 0.0
    max_outing_duration_minutes: float = 0.0
    max_outing_distance_m: float = 0.0
    minimum_battery_percent: float = 0.0
    estimated_completion_minutes: float = 0.0
    route_id: str = Field(default_factory=lambda: f"route-{uuid4().hex[:8]}")
    job_id: str
    route_version: int
    plan_epoch: int = 1
    cancellation_token: str = Field(default_factory=lambda: f"tok-{uuid4().hex[:10]}")
    machine_id: str
    machine_profile_revision: int = Field(default=0, ge=0)
    status: RouteLifecycleStatus = RouteLifecycleStatus.PROPOSED
    task_mode: TaskType
    routing_provenance: DataProvenance = DataProvenance.PLANNED
    mission_elapsed_min: float = Field(default=0.0, ge=0.0)
    origin: Coordinate
    disposal_depot: Coordinate
    operating_boundary: Optional[GeoPolygon] = None
    exclusion_zones: List[ExclusionZone] = Field(default_factory=list)
    tasks: List[TaskItem] = Field(default_factory=list)
    task_schedule: List[TaskScheduleEntry] = Field(default_factory=list)
    skipped_tasks: List[Dict[str, Any]] = Field(default_factory=list)
    route_safety_issues: List[str] = Field(default_factory=list)
    candidate_rationale: List[str] = Field(default_factory=list)
    rl_policy_status: RLPolicyStatus = Field(default_factory=RLPolicyStatus)
    rl_feedback: List[CandidateFeedback] = Field(default_factory=list)
    deduplicated_task_ids: List[str] = Field(default_factory=list)
    segments: List[RouteSegment] = Field(default_factory=list)
    unreachable_sections: List[UnreachableSection] = Field(default_factory=list)
    requested_area_m2: float = 0.0
    effective_eligible_area_m2: float = 0.0
    inaccessible_area_m2: float = 0.0
    unverified_area_m2: float = 0.0
    target_area_m2: float = 0.0
    covered_area_m2: float = 0.0
    repeated_area_m2: float = 0.0
    missed_area_m2: float = 0.0
    sweep_angle_deg: float = 0.0
    overlap_ratio: float = 0.12
    total_distance_m: float = 0.0
    sweep_distance_m: float = 0.0
    transit_distance_m: float = 0.0
    estimated_duration_minutes: float = 0.0
    estimated_energy_kwh: float = 0.0
    estimated_remaining_battery_percent: float = 0.0
    payload_utilization_percent: float = 0.0
    disposal_trips: int = 1
    coverage_cells: List[CoverageCellSummary] = Field(default_factory=list)
    environment_features_applied: List[TerrainFeature] = Field(default_factory=list)
    planning_environment_map: Optional[EnvironmentMapData] = None
    authorized_crossings_used: List[str] = Field(default_factory=list)
    planning_diagnostics: Optional[PlanningDiagnostics] = None
    kpi_evaluation: Optional[KPIEvaluationResponse] = None
    kpi_policy_snapshot: Dict[str, Any] = Field(default_factory=dict)
    optimization_termination_reason: Optional[str] = None
    replan_reason: Optional[str] = None
    replan_comparison: Optional[ReplanComparison] = None
    approved_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class PlanRequest(CompatBaseModel):
    enable_depot_cycles: bool = False
    job_id: str = Field(default_factory=lambda: f"job-{uuid4().hex[:8]}")
    idempotency_key: Optional[str] = None
    preserve_completed: bool = False
    use_rl: bool = True
    natural_language_command: Optional[str] = None
    machine_id: str = "sweeper-alpha-01"
    start_location: Optional[Coordinate] = None
    disposal_location: Optional[Coordinate] = None
    mission_elapsed_min: float = Field(default=0.0, ge=0.0)
    place_name: Optional[str] = None
    operating_boundary: Optional[GeoPolygon] = None
    exclusion_zones: List[ExclusionZone] = Field(default_factory=list)
    tasks: List[TaskItem] = Field(default_factory=list)
    deduplicated_task_ids: List[str] = Field(default_factory=list)
    kpi_policy_override: Optional[Dict[str, float]] = None
    environment_map: Optional[EnvironmentMapData] = None
    allow_partial_coverage_recovery: bool = True


class OperationalEventRequest(CompatBaseModel):
    job_id: str
    route_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    detection_id: Optional[str] = None
    event_type: str
    natural_language_report: Optional[str] = None
    location: Optional[Coordinate] = None
    battery_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    waste_observation: Optional[WasteObservation] = None
    task_id: Optional[str] = None
    task_time_window_start_min: Optional[float] = Field(default=None, ge=0.0)
    task_time_window_end_min: Optional[float] = Field(default=None, ge=0.0)
    new_operating_boundary: Optional[GeoPolygon] = None
    new_exclusion_zone: Optional[ExclusionZone] = None
    rubbish_spec: Optional[RubbishItemSpec] = None
    retain_existing_tasks: bool = True


class TelemetryFrame(CompatBaseModel):
    telemetry_id: str = Field(default_factory=lambda: f"tel-{uuid4().hex[:8]}")
    machine_id: str
    route_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    mission_elapsed_min: float = Field(default=0.0, ge=0.0)
    position: Coordinate
    speed_mps: float = Field(ge=0.0)
    battery_percent: float = Field(ge=0.0, le=100.0)
    payload_kg: float = Field(ge=0.0)
    payload_volume_m3: float = Field(default=0.0, ge=0.0)
    active_segment_id: Optional[str] = None
    route_deviation_m: float = Field(default=0.0, ge=0.0)
    provenance: DataProvenance = DataProvenance.MEASURED_TELEMETRY
    sensor_fault: bool = False
    obstacle_blocked: bool = False
    emergency_stop_engaged: bool = False


class AuditEntry(CompatBaseModel):
    audit_id: str = Field(default_factory=lambda: f"aud-{uuid4().hex[:10]}")
    timestamp: datetime = Field(default_factory=utc_now)
    job_id: str
    route_id: Optional[str] = None
    route_version: Optional[int] = None
    actor: str
    action: str
    reason: str
    details: Dict[str, Any] = Field(default_factory=dict)


class AuditAndEventManager:
    def __init__(self) -> None:
        self._audit_log: List[AuditEntry] = []
        self._idempotency_cache: Dict[str, Any] = {}

    def record(
        self,
        job_id: str,
        actor: str,
        action: str,
        reason: str,
        route_id: Optional[str] = None,
        route_version: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            job_id=job_id,
            route_id=route_id,
            route_version=route_version,
            actor=actor,
            action=action,
            reason=reason,
            details=details or {},
        )
        self._audit_log.append(entry)
        return entry

    def check_idempotency(self, key: Optional[str]) -> Optional[Any]:
        return self._idempotency_cache.get(key) if key else None

    def save_idempotency(self, key: Optional[str], response_obj: Any) -> None:
        if key:
            self._idempotency_cache[key] = response_obj
