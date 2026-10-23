export interface Coordinate {
  lat: number;
  lon: number;
}

export type DrawMode =
  | "SELECT_ZONE"
  | "BOX_AREA"
  | "BOUNDARY"
  | "EXCLUSION"
  | "INSTANT_OBSTACLE"
  | "OBSTACLE"
  | "TASK"
  | "OVERSIZED"
  | "EVENT_WASTE"
  | "START"
  | "DEPOT"
  | "NONE";

export type ExecutionState =
  | "IDLE"
  | "VALIDATING"
  | "PLANNING"
  | "EVALUATING"
  | "AWAITING_APPROVAL"
  | "READY"
  | "EXECUTING"
  | "PAUSED"
  | "REPLANNING"
  | "NEEDS_ASSISTANCE"
  | "COMPLETED"
  | "ABORTED"
  | "EMERGENCY_STOP";

export type RouteLifecycleStatus =
  | "PROPOSED"
  | "DRAFT_FAILED_KPI"
  | "APPROVED"
  | "EXECUTING"
  | "SUPERSEDED"
  | "COMPLETED"
  | "ABORTED";

export type TaskType =
  | "AREA_SWEEP"
  | "RUBBISH_COLLECTION"
  | "OVERSIZED_RUBBISH"
  | "PATROL"
  | "HYBRID";

export type DataProvenance =
  | "PLANNED"
  | "ESTIMATED"
  | "SIMULATED"
  | "MEASURED_TELEMETRY"
  | "SYNTHETIC_FALLBACK_GRAPH";

export type TerrainFeatureType =
  | "PUBLIC_ROAD"
  | "SERVICE_ROAD"
  | "PEDESTRIAN_PLAZA"
  | "PEDESTRIAN_WALKWAY"
  | "WATER_BODY"
  | "BUILDING"
  | "RESTRICTED_ZONE"
  | "UNVERIFIED_TERRAIN";

export interface GeoPolygon {
  vertices: Coordinate[];
}

export interface ExclusionZone {
  zone_id: string;
  name: string;
  reason: string;
  polygon: GeoPolygon;
}

export interface TerrainFeature {
  feature_id: string;
  name: string;
  feature_type: TerrainFeatureType;
  polygon: GeoPolygon;
  clearance_buffer_m: number;
  max_slope_deg?: number;
  min_clearance_width_m?: number;
  allowed_machine_ids?: string[] | null;
}

export interface AuthorizedCrossing {
  crossing_id: string;
  name: string;
  crossing_type: string;
  start_point: Coordinate;
  end_point: Coordinate;
  corridor_polygon?: GeoPolygon | null;
  width_m: number;
  max_weight_kg: number;
  allowed_machine_ids: string[];
  allows_heavy_collector: boolean;
  risk_score: number;
}

export interface EnvironmentMapData {
  map_id: string;
  features: TerrainFeature[];
  authorized_crossings: AuthorizedCrossing[];
  verified_traversable_polygons?: GeoPolygon[];
  require_verified_terrain?: boolean;
  hazard_coverage_boundary?: GeoPolygon | null;
  water_map_version?: string | null;
}

export interface CoverageCellSummary {
  cell_id: string;
  polygon: GeoPolygon;
  area_m2: number;
  reachable: boolean;
  swept: boolean;
  surface_class: string;
  exclusion_reason?: string | null;
  swath_count: number;
}

export interface RecoveryAttemptRecord {
  attempt_number: number;
  route_version: number;
  failure_classification: string;
  failure_cause: string;
  recovery_action: string;
  parameters_changed: Record<string, any>;
  succeeded: boolean;
}

export interface UnreachableSection {
  section_id: string;
  reason: string;
  area_m2: number;
  centroid: Coordinate;
}

export interface PlanningDiagnostics {
  planning_status: "SUCCESS" | "PARTIAL_SUCCESS" | "BLOCKED" | "FAILED_KPI" | string;
  route_id: string;
  route_version: number;
  machine_id: string;
  task_mode: TaskType;
  requested_area_m2: number;
  effective_eligible_area_m2: number;
  covered_area_m2: number;
  inaccessible_area_m2: number;
  unverified_area_m2: number;
  reachable_coverage_percent?: number | null;
  requested_coverage_percent?: number | null;
  inaccessible_area_percent?: number | null;
  unverified_area_percent?: number | null;
  total_distance_m?: number | null;
  estimated_duration_minutes?: number | null;
  total_cells_detected: number;
  reachable_cells_count: number;
  planned_cells_count: number;
  completed_cells_count: number;
  excluded_cells_count: number;
  coverage_cells: CoverageCellSummary[];
  road_access_conflicts: string[];
  obstacle_and_connectivity_issues: string[];
  replanning_attempts_count: number;
  recovery_actions_applied: string[];
  recovery_history: RecoveryAttemptRecord[];
  excluded_sections: UnreachableSection[];
  passed_final_validation: boolean;
  summary_explanation: string;
}

export interface RubbishItemSpec {
  description: string;
  is_oversized: boolean;
  estimated_weight_kg?: number | null;
  estimated_volume_m3?: number | null;
  max_dimension_m?: number | null;
  dimensions_uncertain: boolean;
  requires_heavy_lifter: boolean;
  weight_estimation_method?: string | null;
  volume_estimation_method?: string | null;
  dimension_estimation_method?: string | null;
}

export interface WasteObservation {
  detection_id: string;
  waste_class?: string | null;
  confidence?: number | null;
  observed_at?: string | null;
  source?: string | null;
  image_bbox?: { x: number; y: number; width: number; height: number } | null;
  world_location?: Coordinate | null;
  rubbish_spec?: RubbishItemSpec | null;
}

export interface TaskItem {
  task_id: string;
  idempotency_key?: string | null;
  task_type: TaskType;
  location: Coordinate;
  priority: number;
  service_duration_sec: number;
  time_window_start_min: number;
  time_window_end_min: number;
  rubbish_spec?: RubbishItemSpec | null;
  waste_observation?: WasteObservation | null;
  completed: boolean;
}

export interface TaskScheduleEntry {
  task_id: string;
  arrival_min: number;
  service_start_min: number;
  service_end_min: number;
  waiting_min: number;
  window_start_min: number;
  window_end_min: number;
  priority: number;
  status: string;
  reason?: string | null;
  disposal_before: boolean;
}

export interface RouteSegment {
  segment_id: string;
  segment_type: string;
  waypoints: Coordinate[];
  distance_m: number;
  estimated_duration_sec: number;
  swath_width_m: number;
  target_task_id?: string | null;
  cell_id?: string | null;
  surface_class?: string;
  crossing_id?: string | null;
  provenance: DataProvenance;
  completed: boolean;
}

export interface MetricEntry {
  actual: number;
  target?: number | null;
  target_max?: number | null;
  target_min?: number | null;
  passed: boolean;
  is_constraint?: boolean;
  provenance: DataProvenance;
  unit: string;
}

export interface KPIEvaluationResponse {
  evaluation_id: string;
  route_version: number;
  policy_version: string;
  passed: boolean;
  score: number;
  metrics: Record<string, MetricEntry>;
  failed_constraints: string[];
  recommended_adjustments: string[];
  requires_human_review: boolean;
  evaluated_at?: string;
}

export interface ReplanComparison {
  previous_route_id: string;
  previous_route_version: number;
  previous_remaining_distance_m: number;
  revised_remaining_distance_m: number;
  distance_delta_m: number;
  previous_remaining_duration_min: number;
  revised_remaining_duration_min: number;
  duration_delta_min: number;
  previous_coverage_percent: number;
  revised_coverage_percent: number;
  coverage_delta_percent: number;
  previous_missed_area_m2: number;
  revised_missed_area_m2: number;
  missed_area_delta_m2: number;
  previous_battery_percent: number;
  revised_remaining_battery_percent: number;
  open_collection_tasks_delta: number;
}

export interface RepeatPlanComparison {
  previous_route_id: string;
  candidates_checked: number;
  feasible_candidates: number;
  previous_estimated_completion_minutes: number;
  previous_actual_completion_minutes: number | null;
  new_estimated_completion_minutes: number;
  estimated_time_change_minutes: number;
  sweep_pattern_changed: boolean;
  collection_order_changed: boolean;
  travel_path_changed: boolean;
  history_applied: boolean;
  summary: string;
}

export interface RouteCandidate {
  planning_run_id?: string | null;
  candidate_index?: number;
  is_selected_candidate?: boolean;
  route_id: string;
  repeat_of_route_id?: string | null;
  repeat_comparison?: RepeatPlanComparison | null;
  history_hint_applied?: boolean;
  job_id: string;
  route_version: number;
  plan_epoch: number;
  cancellation_token?: string;
  machine_id: string;
  status: RouteLifecycleStatus;
  task_mode: TaskType;
  routing_provenance: DataProvenance;
  mission_elapsed_min: number;
  initial_battery_percent?: number | null;
  origin: Coordinate;
  disposal_depot: Coordinate;
  operating_boundary?: GeoPolygon | null;
  exclusion_zones: ExclusionZone[];
  tasks: TaskItem[];
  task_schedule: TaskScheduleEntry[];
  skipped_tasks: Array<{
    task_id: string;
    reasons?: string[];
    recommended_actions?: string[];
  }>;
  route_safety_issues: string[];
  candidate_rationale: string[];
  deduplicated_task_ids: string[];
  segments: RouteSegment[];
  unreachable_sections: UnreachableSection[];
  requested_area_m2?: number;
  effective_eligible_area_m2?: number;
  inaccessible_area_m2?: number;
  unverified_area_m2?: number;
  target_area_m2: number;
  covered_area_m2: number;
  repeated_area_m2: number;
  missed_area_m2: number;
  sweep_angle_deg: number;
  overlap_ratio: number;
  total_distance_m: number;
  sweep_distance_m: number;
  transit_distance_m: number;
  estimated_duration_minutes: number;
  depot_cycles_enabled?: boolean;
  outing_count?: number;
  depot_visits?: number;
  charging_duration_minutes?: number;
  max_outing_duration_minutes?: number;
  max_outing_distance_m?: number;
  minimum_battery_percent?: number;
  estimated_completion_minutes?: number;
  estimated_energy_kwh: number;
  estimated_remaining_battery_percent: number;
  payload_utilization_percent: number;
  disposal_trips: number;
  coverage_cells?: CoverageCellSummary[];
  environment_features_applied?: TerrainFeature[];
  planning_environment_map?: EnvironmentMapData | null;
  authorized_crossings_used?: string[];
  planning_diagnostics?: PlanningDiagnostics | null;
  kpi_evaluation?: KPIEvaluationResponse | null;
  kpi_policy_snapshot?: Record<string, any>;
  optimization_termination_reason?: string | null;
  replan_reason?: string | null;
  replan_comparison?: ReplanComparison | null;
  approved_by?: string | null;
  created_at?: string;
}

export interface RouteHistorySummary {
  planning_run_id?: string | null;
  candidate_index?: number;
  is_selected_candidate?: boolean;
  reason?: string;
  options_checked?: number;
  created_at?: string;
  approved_by?: string | null;
  executed?: boolean;
  route_id: string;
  version: number;
  status: RouteLifecycleStatus;
  passed: boolean;
  score: number;
  coverage_percent?: number | null;
  total_distance_m: number;
  estimated_duration_minutes: number;
  sweep_angle_deg: number;
  overlap_ratio: number;
  failed_constraints: string[];
}

export interface TelemetryFrame {
  telemetry_id: string;
  machine_id: string;
  route_id: string;
  timestamp?: string;
  mission_elapsed_min: number;
  position: Coordinate;
  speed_mps: number;
  battery_percent: number;
  payload_kg: number;
  payload_volume_m3: number;
  active_segment_id?: string | null;
  route_deviation_m: number;
  provenance: DataProvenance;
  sensor_fault?: boolean;
  obstacle_blocked?: boolean;
  emergency_stop_engaged?: boolean;
}

export interface AuditEntry {
  audit_id: string;
  timestamp: string;
  job_id: string;
  route_id?: string | null;
  route_version?: number | null;
  actor: string;
  action: string;
  reason: string;
  details?: Record<string, any>;
}
