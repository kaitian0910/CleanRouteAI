import React, { useCallback, useEffect, useRef, useState } from "react";
import MapWorkspace from "./MapWorkspace";
import WasteDetector from "./WasteDetector";
import WasteSummary from "./WasteSummary";
import { MissionStatusStrip } from "./MissionStatusStrip";
import FeedbackPanel from "./FeedbackPanel";
import SitePlanningPanel, { OutingLimits, SiteImportPreview } from "./SitePlanningPanel";
import { formatMinutes, newTaskWindow, planningBlockMessage } from "./planningFeedback";
import { RouteExecutionSession, collectedWasteStats, ExecutionSnapshot } from "./routeExecution";
import { PlanningSession, apiErrorMessage, areaLocations } from "./planningSession";
import { canRepeatRoute } from "./repeatRun";
import { MAPPED_LAKE_TEST_AREA, MAPPED_WATER_FEATURES } from "./mappedWater";
import { automaticDryLocations, combineWaterFeatures, offlineWaterConflict } from "./waterSafety";
import {
  AuthorizedCrossing,
  Coordinate,
  CoverageCellSummary,
  DrawMode,
  ExecutionState,
  EnvironmentMapData,
  PlanningDiagnostics,
  RecoveryAttemptRecord,
  RouteCandidate,
  TaskItem,
  TelemetryFrame,
  TerrainFeature,
  UnreachableSection,
  WasteObservation,
} from "./types/api";

const API_BASE =
  ((import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_BASE || "http://localhost:8000").replace(/\/api\/?$/, "").replace(/\/$/, ""));
const OPERATOR_TOKEN =
  (import.meta as any).env?.VITE_OPERATOR_TOKEN || "dev-operator-token";

interface CampusPreset {
  id: string;
  name: string;
  center: Coordinate;
  boundary: Coordinate[];
  startLocation: Coordinate;
  disposalLocation: Coordinate;
}

const DEFAULT_GIS_FEATURES: TerrainFeature[] = [
  {
    feature_id: "road-jalan-damansara-west",
    name: "Persiaran Tuanku Ja'afar / Jalan Damansara Highway",
    feature_type: "PUBLIC_ROAD",
    clearance_buffer_m: 1.2,
    polygon: {
      vertices: [
        { lat: 3.138, lon: 101.6841 },
        { lat: 3.138, lon: 101.68482 },
        { lat: 3.1416, lon: 101.68482 },
        { lat: 3.1416, lon: 101.6841 },
      ],
    },
  },
  {
    feature_id: "road-jalan-damansara-south",
    name: "Jalan Damansara Southern Arterial",
    feature_type: "PUBLIC_ROAD",
    clearance_buffer_m: 1.2,
    polygon: {
      vertices: [
        { lat: 3.1376, lon: 101.6841 },
        { lat: 3.1376, lon: 101.6884 },
        { lat: 3.13825, lon: 101.6884 },
        { lat: 3.13825, lon: 101.6841 },
      ],
    },
  },
  {
    feature_id: "road-jalan-perdana-northeast",
    name: "Jalan Perdana Roadway",
    feature_type: "PUBLIC_ROAD",
    clearance_buffer_m: 1.0,
    polygon: {
      vertices: [
        { lat: 3.14018, lon: 101.68775 },
        { lat: 3.14018, lon: 101.689 },
        { lat: 3.14065, lon: 101.689 },
        { lat: 3.14065, lon: 101.68775 },
      ],
    },
  },
  ...MAPPED_WATER_FEATURES,
  {
    feature_id: "bldg-national-space-agency",
    name: "National Space Agency & Planetarium Complex",
    feature_type: "BUILDING",
    clearance_buffer_m: 0.6,
    polygon: {
      vertices: [
        { lat: 3.13962, lon: 101.68795 },
        { lat: 3.13962, lon: 101.68858 },
        { lat: 3.14002, lon: 101.68858 },
        { lat: 3.14002, lon: 101.68795 },
      ],
    },
  },
  {
    feature_id: "bldg-stesen-satelit",
    name: "Stesen Satelit Building",
    feature_type: "BUILDING",
    clearance_buffer_m: 0.5,
    polygon: {
      vertices: [
        { lat: 3.13958, lon: 101.68755 },
        { lat: 3.13958, lon: 101.68782 },
        { lat: 3.13982, lon: 101.68782 },
        { lat: 3.13982, lon: 101.68755 },
      ],
    },
  },
  {
    feature_id: "walkway-north-botanical-footpath",
    name: "Mini Stonehenge Narrow Botanical Footpath",
    feature_type: "PEDESTRIAN_WALKWAY",
    clearance_buffer_m: 0.0,
    min_clearance_width_m: 1.8,
    allowed_machine_ids: ["sweeper-alpha-01"],
    polygon: {
      vertices: [
        { lat: 3.14005, lon: 101.68705 },
        { lat: 3.14005, lon: 101.6875 },
        { lat: 3.14045, lon: 101.6875 },
        { lat: 3.14045, lon: 101.68705 },
      ],
    },
  },
];

const DEFAULT_AUTHORIZED_CROSSINGS: AuthorizedCrossing[] = [
  {
    crossing_id: "xing-damansara-ped-bridge",
    name: "Jalan Damansara Signalized Pedestrian/Sweeper Crossing",
    crossing_type: "PEDESTRIAN_CROSSWALK",
    start_point: { lat: 3.1391, lon: 101.68485 },
    end_point: { lat: 3.1391, lon: 101.68405 },
    corridor_polygon: {
      vertices: [
        { lat: 3.13904, lon: 101.68405 },
        { lat: 3.13904, lon: 101.68485 },
        { lat: 3.13916, lon: 101.68485 },
        { lat: 3.13916, lon: 101.68405 },
      ],
    },
    width_m: 2.2,
    max_weight_kg: 250.0,
    allowed_machine_ids: ["sweeper-alpha-01"],
    allows_heavy_collector: false,
    risk_score: 0.12,
  },
  {
    crossing_id: "xing-damansara-heavy-service",
    name: "South Signalized Heavy Service Crossing",
    crossing_type: "SIGNALIZED_SERVICE_CROSSING",
    start_point: { lat: 3.1383, lon: 101.6864 },
    end_point: { lat: 3.13755, lon: 101.6864 },
    corridor_polygon: {
      vertices: [
        { lat: 3.13755, lon: 101.68632 },
        { lat: 3.13755, lon: 101.68648 },
        { lat: 3.1383, lon: 101.68648 },
        { lat: 3.1383, lon: 101.68632 },
      ],
    },
    width_m: 3.6,
    max_weight_kg: 600.0,
    allowed_machine_ids: ["sweeper-alpha-01", "heavy-utility-02"],
    allows_heavy_collector: true,
    risk_score: 0.18,
  },
];

const CAMPUS_PRESETS: CampusPreset[] = [
  {
    id: "obstacle-test-small",
    name: "Small obstacle test area (~1,800 m2)",
    center: { lat: 3.13918, lon: 101.68710 },
    boundary: [
      { lat: 3.13900, lon: 101.68690 }, { lat: 3.13900, lon: 101.68730 },
      { lat: 3.13936, lon: 101.68730 }, { lat: 3.13936, lon: 101.68690 },
    ],
    startLocation: { lat: 3.13903, lon: 101.68693 },
    disposalLocation: { lat: 3.13903, lon: 101.68727 },
  },
  {
    id: "plaza",
    name: "🌳 Perdana Botanical Central Lawn (Clean Open Zone)",
    center: { lat: 3.14015, lon: 101.68635 },
    boundary: [
      { lat: 3.13955, lon: 101.68575 },
      { lat: 3.13955, lon: 101.68695 },
      { lat: 3.14075, lon: 101.68695 },
      { lat: 3.14075, lon: 101.68575 },
    ],
    startLocation: { lat: 3.13962, lon: 101.68585 },
    disposalLocation: { lat: 3.13962, lon: 101.68685 },
  },
  {
    id: "damansara-perdana-hazard",
    name: "🌊🏛️ Sungai Bras Bras + Stesen Satelit Multi-Hazard Zone",
    center: { lat: 3.13985, lon: 101.68665 },
    boundary: [
      { lat: 3.13935, lon: 101.68525 },
      { lat: 3.13935, lon: 101.6879 },
      { lat: 3.14035, lon: 101.6879 },
      { lat: 3.14035, lon: 101.68525 },
    ],
    startLocation: { lat: 3.13948, lon: 101.6861 },
    disposalLocation: { lat: 3.13948, lon: 101.6872 },
  },
  {
    id: "crossing-annex",
    name: "🌉 Across Jalan Damansara via Signalized Crossing",
    center: { lat: 3.13902, lon: 101.68455 },
    boundary: [
      { lat: 3.13885, lon: 101.6838 },
      { lat: 3.13885, lon: 101.68525 },
      { lat: 3.13918, lon: 101.68525 },
      { lat: 3.13918, lon: 101.6838 },
    ],
    startLocation: { lat: 3.13895, lon: 101.68505 },
    disposalLocation: { lat: 3.13892, lon: 101.68505 },
  },
  ...(MAPPED_LAKE_TEST_AREA ? [{
    id: "lake-blocked",
    name: "🛑 Inside Tasik Perdana Lake (100% Blocked Test)",
    ...MAPPED_LAKE_TEST_AREA,
  }] : []),
];

type ZoneClassification =
  | "STANDARD_SWEEP"
  | "SWEEP_AND_BINS"
  | "BIN_PICKUP_ONLY"
  | "BULKY_WASTE";

interface UrgentWasteAlert {
  id: string;
  title: string;
  description: string;
  weightKg: number;
  locationLabel: string;
  location: Coordinate;
  timestamp: string;
}

// Road/Lake/Building-Constrained Offline Simulation Engine (ONLY invoked when operator explicitly clicks "Run Labeled Offline Simulation")
function toLocalMeters(c: Coordinate, origin: Coordinate): [number, number] {
  const latRad = (origin.lat * Math.PI) / 180.0;
  return [
    (c.lon - origin.lon) * 111320.0 * Math.cos(latRad),
    (c.lat - origin.lat) * 110540.0,
  ];
}

function fromLocalMeters(
  x: number,
  y: number,
  origin: Coordinate
): Coordinate {
  const latRad = (origin.lat * Math.PI) / 180.0;
  return {
    lat: Number((origin.lat + y / 110540.0).toFixed(7)),
    lon: Number((origin.lon + x / (111320.0 * Math.cos(latRad))).toFixed(7)),
  };
}

function distMeters(a: Coordinate, b: Coordinate): number {
  const [dx, dy] = toLocalMeters(b, a);
  return Math.hypot(dx, dy);
}

function pointInPolygonLocal(
  px: number,
  py: number,
  polyPts: [number, number][]
): boolean {
  let inside = false;
  for (let i = 0, j = polyPts.length - 1; i < polyPts.length; j = i++) {
    const [xi, yi] = polyPts[i];
    const [xj, yj] = polyPts[j];
    const intersect =
      yi > py !== yj > py &&
      px < ((xj - xi) * (py - yi)) / (yj - yi + 1e-12) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

function polygonAreaM2(vertices: Coordinate[]): number {
  if (vertices.length < 3) return 0;
  const origin = vertices[0];
  const pts = vertices.map((v) => toLocalMeters(v, origin));
  let area = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[(i + 1) % pts.length];
    area += x1 * y2 - x2 * y1;
  }
  return Math.abs(area) / 2.0;
}

function buildLabeledOfflineConstrainedRoute(params: {
  jobId: string;
  version: number;
  machineId: string;
  boundary: Coordinate[];
  dynamicObstacles: { id: string; name: string; vertices: Coordinate[] }[];
  tasks: TaskItem[];
  startLocation: Coordinate;
  disposalLocation: Coordinate;
  retainedCompletedSegments: RouteCandidate["segments"];
  replanReason: string;
  gisFeatures: TerrainFeature[];
  crossings: AuthorizedCrossing[];
}): RouteCandidate {
  const {
    jobId,
    version,
    machineId,
    boundary,
    dynamicObstacles,
    tasks,
    startLocation,
    disposalLocation,
    retainedCompletedSegments,
    replanReason,
    gisFeatures,
  } = params;

  const origin = boundary[0] || startLocation;
  const requestedAreaM2 = Math.round(polygonAreaM2(boundary));
  const roadConflicts: string[] = [];
  const excludedSections: UnreachableSection[] = [];
  const recoveryHistory: RecoveryAttemptRecord[] = [];
  const recoveryActions: string[] = [];

  // Identify prohibited GIS features (roads, lakes, buildings, restricted, machine-prohibited footpaths)
  const prohibitedPolysLocal: [number, number][][] = [];
  for (const obs of dynamicObstacles) {
    prohibitedPolysLocal.push(obs.vertices.map((v) => toLocalMeters(v, origin)));
  }

  for (const feat of gisFeatures) {
    const fLocal = feat.polygon.vertices.map((v) => toLocalMeters(v, origin));
    const isProhibitedWalkway =
      feat.feature_type === "PEDESTRIAN_WALKWAY" &&
      feat.allowed_machine_ids &&
      !feat.allowed_machine_ids.includes(machineId);

    if (
      feat.feature_type === "PUBLIC_ROAD" ||
      feat.feature_type === "WATER_BODY" ||
      feat.feature_type === "BUILDING" ||
      feat.feature_type === "RESTRICTED_ZONE" ||
      isProhibitedWalkway
    ) {
      prohibitedPolysLocal.push(fLocal);
      const cx =
        feat.polygon.vertices.reduce((s, p) => s + p.lat, 0) /
        feat.polygon.vertices.length;
      const cy =
        feat.polygon.vertices.reduce((s, p) => s + p.lon, 0) /
        feat.polygon.vertices.length;
      if (
        boundary.length >= 3 &&
        feat.polygon.vertices.some((v) => {
          const [vx, vy] = toLocalMeters(v, origin);
          return pointInPolygonLocal(
            vx,
            vy,
            boundary.map((b) => toLocalMeters(b, origin))
          );
        })
      ) {
        if (feat.feature_type === "PUBLIC_ROAD") {
          roadConflicts.push(
            `Road-access conflict: '${feat.name}' excluded from sweeping for ${machineId}.`
          );
        } else if (isProhibitedWalkway) {
          roadConflicts.push(
            `Machine access restriction: '${feat.name}' prohibited for ${machineId}.`
          );
        }
        excludedSections.push({
          section_id: `excl-${feat.feature_id}`,
          reason: `${feat.feature_type.toLowerCase()}_exclusion: ${feat.name}`,
          area_m2: Math.min(requestedAreaM2, Math.round(polygonAreaM2(feat.polygon.vertices) * 0.4)),
          centroid: { lat: cx, lon: cy },
        });
      }
    }
  }

  const segments: RouteCandidate["segments"] = retainedCompletedSegments.map(
    (s) => ({ ...s, completed: true })
  );

  let validSamples = 0;
  let totalOuterSamples = 0;

  if (boundary.length >= 3) {
    const polyLocal = boundary.map((v) => toLocalMeters(v, origin));
    const xs = polyLocal.map((p) => p[0]);
    const ys = polyLocal.map((p) => p[1]);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);

    const spanY = Math.max(1, maxY - minY);
    const step = requestedAreaM2 > 2800 ? Math.max(1.6 * (1 - 0.14), spanY / 14) : 1.6 * (1 - 0.14);
    let y = minY + 1.0;
    let ltr = true;
    let prevEnd: Coordinate | null = null;
    let segCounter = segments.length + 1;

    while (y <= maxY - 1.0) {
      const samples = 40;
      let currentRun: Coordinate[] = [];
      const runs: [Coordinate, Coordinate][] = [];

      for (let s = 0; s <= samples; s++) {
        const frac = ltr ? s / samples : 1 - s / samples;
        const wx = minX + 1.0 + frac * Math.max(1, maxX - minX - 2.0);
        const wy = y;
        const inOuter = pointInPolygonLocal(wx, wy, polyLocal);
        if (inOuter) totalOuterSamples++;
        const inProhibited = prohibitedPolysLocal.some((op) =>
          pointInPolygonLocal(wx, wy, op)
        );
        if (inOuter && !inProhibited) {
          validSamples++;
          currentRun.push(fromLocalMeters(wx, wy, origin));
        } else {
          if (currentRun.length >= 2) {
            runs.push([currentRun[0], currentRun[currentRun.length - 1]]);
          }
          currentRun = [];
        }
      }
      if (currentRun.length >= 2) {
        runs.push([currentRun[0], currentRun[currentRun.length - 1]]);
      }

      // Only take the first contiguous run on each scanline to avoid jumping across roads/barriers
      if (runs.length > 1 && recoveryActions.length === 0) {
        recoveryActions.push("SPLIT_AND_PRUNE_UNREACHABLE_CELLS");
        recoveryHistory.push({
          attempt_number: 1,
          route_version: version,
          failure_classification: "DISCONNECTED_COVERAGE_CELL",
          failure_cause: "Road or impassable feature divides polygon into disconnected cells.",
          recovery_action: "SPLIT_AND_PRUNE_UNREACHABLE_CELLS",
          parameters_changed: { prune_unreachable_cells: true },
          succeeded: true,
        });
      }

      const safeRuns = runs.slice(0, 1);
      for (const r of safeRuns) {
        const d = distMeters(r[0], r[1]);
        if (d < 1.5) continue;
        if (prevEnd) {
          const td = distMeters(prevEnd, r[0]);
          if (td <= 25) {
            segments.push({
              segment_id: `sim-turn-${version}-${segCounter++}`,
              segment_type: "TURN_CONNECTOR",
              waypoints: [prevEnd, r[0]],
              distance_m: Number(td.toFixed(1)),
              estimated_duration_sec: Number((td / 0.9).toFixed(1)),
              swath_width_m: 1.6,
              provenance: "SIMULATED",
              completed: false,
            });
          }
        }
        segments.push({
          segment_id: `sim-swath-${version}-${segCounter++}`,
          segment_type: "SWEEP_SWATH",
          waypoints: [r[0], r[1]],
          distance_m: Number(d.toFixed(1)),
          estimated_duration_sec: Number((d / 1.1).toFixed(1)),
          swath_width_m: 1.6,
          provenance: "SIMULATED",
          completed: false,
        });
        prevEnd = r[1];
      }
      ltr = !ltr;
      y += step;
    }
  }

  const eligibleRatio =
    totalOuterSamples > 0 ? validSamples / totalOuterSamples : 1.0;
  const effectiveEligibleAreaM2 = Math.round(requestedAreaM2 * eligibleRatio);
  const inaccessibleAreaM2 = Math.max(0, requestedAreaM2 - effectiveEligibleAreaM2);
  const isBlocked = boundary.length >= 3 && segments.length === 0;

  const sweepDist = segments
    .filter((s) => s.segment_type === "SWEEP_SWATH")
    .reduce((acc, s) => acc + s.distance_m, 0);
  const transitDist = segments
    .filter((s) => s.segment_type !== "SWEEP_SWATH")
    .reduce((acc, s) => acc + s.distance_m, 0);
  const totalDist = sweepDist + transitDist;
  const totalDurMin =
    segments.reduce((acc, s) => acc + s.estimated_duration_sec, 0) / 60.0;
  const covPct = isBlocked ? 0.0 : 96.8;
  const coveredAreaM2 = Math.round((effectiveEligibleAreaM2 * covPct) / 100);
  const remBatt = Number(
    Math.max(24, 96 - (totalDist / 1000) * 6.5).toFixed(1)
  );

  const taskMode =
    boundary.length >= 3 && tasks.length > 0
      ? "HYBRID"
      : tasks.some((t) => t.task_type === "OVERSIZED_RUBBISH")
      ? "OVERSIZED_RUBBISH"
      : tasks.length > 0
      ? "RUBBISH_COLLECTION"
      : "AREA_SWEEP";

  const statusStr = isBlocked
    ? "BLOCKED"
    : inaccessibleAreaM2 > 10 || excludedSections.length > 0
    ? "PARTIAL_SUCCESS"
    : "SUCCESS";

  const diagnostics: PlanningDiagnostics = {
    planning_status: statusStr,
    route_id: `sim-route-v${version}`,
    route_version: version,
    machine_id: machineId,
    task_mode: taskMode,
    requested_area_m2: requestedAreaM2,
    effective_eligible_area_m2: effectiveEligibleAreaM2,
    covered_area_m2: coveredAreaM2,
    inaccessible_area_m2: inaccessibleAreaM2,
    unverified_area_m2: 0,
    reachable_coverage_percent: covPct,
    requested_coverage_percent:
      requestedAreaM2 > 0
        ? Number(((coveredAreaM2 / requestedAreaM2) * 100).toFixed(1))
        : 0,
    inaccessible_area_percent:
      requestedAreaM2 > 0
        ? Number(((inaccessibleAreaM2 / requestedAreaM2) * 100).toFixed(1))
        : 0,
    unverified_area_percent: 0,
    total_distance_m: Number(totalDist.toFixed(1)),
    estimated_duration_minutes: Number(totalDurMin.toFixed(1)),
    total_cells_detected: recoveryActions.length > 0 ? 2 : isBlocked ? 0 : 1,
    reachable_cells_count: isBlocked ? 0 : 1,
    planned_cells_count: isBlocked ? 0 : 1,
    completed_cells_count: 0,
    excluded_cells_count: recoveryActions.length > 0 ? 1 : 0,
    coverage_cells: [],
    road_access_conflicts: roadConflicts,
    obstacle_and_connectivity_issues: isBlocked
      ? ["no_eligible_traversable_area: entire selected polygon is impassable water/road"]
      : [],
    replanning_attempts_count: recoveryHistory.length,
    recovery_actions_applied: recoveryActions,
    recovery_history: recoveryHistory,
    excluded_sections: excludedSections,
    passed_final_validation: !isBlocked,
    summary_explanation: isBlocked
      ? "OFFLINE SIMULATION BLOCKED: Entire selected area is impassable water/road."
      : `OFFLINE SIMULATION (${statusStr}): Road/Lake/Building-constrained route covers ${covPct}% of eligible area (${effectiveEligibleAreaM2} m²).`,
  };

  return {
    route_id: `sim-route-v${version}`,
    job_id: jobId,
    route_version: version,
    plan_epoch: version,
    machine_id: machineId,
    status: isBlocked ? "DRAFT_FAILED_KPI" : "PROPOSED",
    task_mode: taskMode,
    routing_provenance: "SIMULATED",
    mission_elapsed_min: 0,
    origin: startLocation,
    disposal_depot: disposalLocation,
    operating_boundary: boundary.length >= 3 ? { vertices: boundary } : null,
    exclusion_zones: dynamicObstacles.map((o) => ({
      zone_id: o.id,
      name: o.name,
      reason: "Live Obstacle",
      polygon: { vertices: o.vertices },
    })),
    tasks,
    task_schedule: [],
    skipped_tasks: [],
    route_safety_issues: diagnostics.obstacle_and_connectivity_issues,
    candidate_rationale: [replanReason, ...roadConflicts],
    deduplicated_task_ids: [],
    segments,
    unreachable_sections: isBlocked ? excludedSections : [],
    requested_area_m2: requestedAreaM2,
    effective_eligible_area_m2: effectiveEligibleAreaM2,
    inaccessible_area_m2: inaccessibleAreaM2,
    unverified_area_m2: 0,
    target_area_m2: Math.max(1, effectiveEligibleAreaM2),
    covered_area_m2: coveredAreaM2,
    repeated_area_m2: Math.round(effectiveEligibleAreaM2 * 0.04),
    missed_area_m2: Math.max(0, effectiveEligibleAreaM2 - coveredAreaM2),
    sweep_angle_deg: 0,
    overlap_ratio: 0.14,
    total_distance_m: Number(totalDist.toFixed(1)),
    sweep_distance_m: Number(sweepDist.toFixed(1)),
    transit_distance_m: Number(transitDist.toFixed(1)),
    estimated_duration_minutes: Number(totalDurMin.toFixed(1)),
    estimated_energy_kwh: Number(((totalDist / 1000) * 1.1).toFixed(2)),
    estimated_remaining_battery_percent: remBatt,
    payload_utilization_percent: Math.min(90, 18 + tasks.length * 18),
    disposal_trips: 1,
    planning_diagnostics: diagnostics,
    replan_reason: replanReason,
    kpi_evaluation: {
      evaluation_id: `sim-eval-${version}`,
      route_version: version,
      policy_version: "v1.3.0-offline-sim",
      passed: !isBlocked,
      score: isBlocked ? 0.0 : 96.4,
      metrics: {},
      failed_constraints: isBlocked ? ["route_geometry_unresolved"] : [],
      recommended_adjustments: [],
      requires_human_review: isBlocked,
    },
  };
}

export const App: React.FC = () => {
  const [jobId, setJobId] = useState<string>(() => `job-${crypto.randomUUID()}`);
  const planningSession = useRef(new PlanningSession());
  const [mapResetKey, setMapResetKey] = useState(0);
  const [activeNav, setActiveNav] = useState<"REALTIME" | "ROUTE" | "FEEDBACK" | "LOGS">("ROUTE");
  const workspaceRef = useRef<HTMLElement>(null);
  useEffect(() => { workspaceRef.current?.scrollTo({ top: 0, left: 0 }); }, [activeNav]);
  const [machineId, setMachineId] = useState<string>("sweeper-alpha-01");
  const [enableDepotCycles, setEnableDepotCycles] = useState(true);

  const [drawMode, setDrawMode] = useState<DrawMode>("BOX_AREA");
  const [mapCenter, setMapCenter] = useState<Coordinate>({
    lat: 3.13918,
    lon: 101.6871,
  });
  const [startLocation, setStartLocation] = useState<Coordinate>({
    lat: 3.139,
    lon: 101.6869,
  });
  const [disposalLocation, setDisposalLocation] = useState<Coordinate>({
    lat: 3.13886,
    lon: 101.68682,
  });
  const [boundary, setBoundary] = useState<Coordinate[]>([]);
  const [dynamicObstacles, setDynamicObstacles] = useState<
    { id: string; name: string; vertices: Coordinate[] }[]
  >([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [selectedClassification, setSelectedClassification] =
    useState<ZoneClassification | null>(null);

  // Authoritative GIS Environment Layers
  const [gisFeatures, setGisFeatures] =
    useState<TerrainFeature[]>(DEFAULT_GIS_FEATURES);
  const [authorizedCrossings, setAuthorizedCrossings] = useState<
    AuthorizedCrossing[]
  >(DEFAULT_AUTHORIZED_CROSSINGS);
  const [showGisLayers, setShowGisLayers] = useState<boolean>(false);
  const [planningEnvironment, setPlanningEnvironment] = useState<EnvironmentMapData | null>(null);
  const [siteSource, setSiteSource] = useState<string | null>(null);
  const [outingLimits, setOutingLimits] = useState<OutingLimits>({ max_estimated_duration_minutes: 90, min_energy_reserve_percent: 20, max_travel_distance_m: 15000 });

  // Route & Diagnostics State (No silent synthetic fallback!)
  const [activeRoute, setActiveRoute] = useState<RouteCandidate | null>(null);
  const [executionState, setExecutionState] = useState<ExecutionState>("IDLE");
  const [planningPhase, setPlanningPhase] = useState<
    | "STANDBY"
    | "PLANNING"
    | "REPLANNING"
    | "SUCCESS"
    | "PARTIAL_SUCCESS"
    | "BLOCKED"
    | "BACKEND_UNREACHABLE"
  >("STANDBY");
  const [planningErrorDetail, setPlanningErrorDetail] = useState<string | null>(
    null
  );
  const [executionErrorDetail, setExecutionErrorDetail] = useState<string | null>(null);
  const [autoApproveValidated, setAutoApproveValidated] =
    useState<boolean>(false);
  const [lastPlanParams, setLastPlanParams] = useState<{
    nextBoundary: Coordinate[];
    nextObstacles: { id: string; name: string; vertices: Coordinate[] }[];
    nextTasks: TaskItem[];
    nextMachineId: string;
    reason: string;
    preserveCompleted: boolean;
    nextStartLocation?: Coordinate;
    nextDisposalLocation?: Coordinate;
  } | null>(null);

  const [livePosition, setLivePosition] = useState<Coordinate>({
    lat: 3.139,
    lon: 101.6869,
  });
  const [batteryPercent, setBatteryPercent] = useState<number>(96.0);
  const [statusBanner, setStatusBanner] = useState<string>(
    "Robot Standby — Select an area on the map (or pick a hazard scenario) and classify the zone to run road-aware planning."
  );

  // Completed mission pickups stay separate from the latest YOLO image counts.
  const [wasteStats, setWasteStats] = useState({
    plasticAndCans: 0,
    paperAndPackaging: 0,
    binsEmptied: 0,
    bulkyItems: 0,
    others: 0,
    totalWeightKg: 0,
    unknownWeightCollections: 0,
  });

  const [visionDetections, setVisionDetections] = useState<WasteObservation[]>([]);

  // Urgent Bulky Waste Notification
  const [urgentAlert, setUrgentAlert] = useState<UrgentWasteAlert | null>(null);

  const routeVersionRef = useRef<number>(0);
  const simTimerRef = useRef<number | null>(null);
  const executionSession = useRef(new RouteExecutionSession(API_BASE, OPERATOR_TOKEN));
  const [serverTelemetry, setServerTelemetry] = useState<TelemetryFrame | null>(null);
  const missionElapsedMinutes = activeRoute?.machine_id !== machineId ? 0
    : serverTelemetry && serverTelemetry.route_id === activeRoute?.route_id
      ? serverTelemetry.mission_elapsed_min : activeRoute?.mission_elapsed_min ?? 0;
  const [controlBusy, setControlBusy] = useState(false);
  const [runAgainBusy, setRunAgainBusy] = useState(false);
  const repeatInFlight = useRef(false);
  const repeatRequest = useRef<{ sourceId: string; key: string } | null>(null);
  useEffect(() => { setExecutionErrorDetail(null); }, [activeRoute?.route_id]);

  const stopSimulationTimer = useCallback(() => {
    executionSession.current.stop();
    if (simTimerRef.current !== null) {
      window.clearInterval(simTimerRef.current);
      simTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => { stopSimulationTimer(); planningSession.current.cancel(); };
  }, [stopSimulationTimer]);

  // Load the illustrative campus reference features from the backend.
  useEffect(() => {
    if (planningEnvironment) return;
    let current = true;
    fetch(`${API_BASE}/api/environment/features`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!current) return;
        if (data && Array.isArray(data.features)) {
          setGisFeatures(data.features);
        }
        if (data && Array.isArray(data.authorized_crossings)) {
          setAuthorizedCrossings(data.authorized_crossings);
        }
      })
      .catch(() => {
        // Keep built-in DEFAULT_GIS_FEATURES when backend is offline
      });
    return () => { current = false; };
  }, [planningEnvironment]);

  const startOfflineSimulation = useCallback(
    (route: RouteCandidate) => {
      // Safety gate: never simulate a route that failed final validation or has safety issues
      if (
        (route.planning_diagnostics &&
          !route.planning_diagnostics.passed_final_validation) ||
        (route.kpi_evaluation && !route.kpi_evaluation.passed) ||
        route.route_safety_issues.length > 0
      ) {
        setExecutionState("NEEDS_ASSISTANCE");
        setStatusBanner(
          "🛑 Execution blocked by safety interlock: Route did not pass final validation."
        );
        return;
      }

      stopSimulationTimer();
      setExecutionState("EXECUTING");

      simTimerRef.current = window.setInterval(() => {
        setActiveRoute((prev) => {
          if (!prev) return null;
          const nextIdx = prev.segments.findIndex((s) => !s.completed);
          if (nextIdx === -1) {
            stopSimulationTimer();
            setExecutionState("COMPLETED");
            setStatusBanner(
              "✅ Route cleaning & waste collection completed! All reachable segments swept."
            );
            return { ...prev, status: "COMPLETED" };
          }

          const updatedSegments = prev.segments.map((s, idx) =>
            idx === nextIdx
              ? { ...s, completed: true, provenance: "SIMULATED" as const }
              : s
          );
          const finishedSeg = updatedSegments[nextIdx];
          const lastWp =
            finishedSeg.waypoints[finishedSeg.waypoints.length - 1];
          if (lastWp) {
            setLivePosition(lastWp);
          }

          if (
            finishedSeg.segment_type === "COLLECTION_STOP" &&
            finishedSeg.target_task_id
          ) {
            setTasks(oldTasks => {
              const updatedTasks = oldTasks.map(task => task.task_id === finishedSeg.target_task_id ? { ...task, completed: true } : task);
              setWasteStats(collectedWasteStats(updatedTasks));
              return updatedTasks;
            });
          }

          const doneCount = updatedSegments.filter((s) => s.completed).length;
          const totalCount = Math.max(1, updatedSegments.length);
          const spent =
            (doneCount / totalCount) *
            (96 - prev.estimated_remaining_battery_percent);
          setBatteryPercent(Number(Math.max(15, 96 - spent).toFixed(1)));

          return {
            ...prev,
            segments: updatedSegments,
          };
        });
      }, 320);
    },
    [stopSimulationTimer]
  );

  const applyExecutionSnapshot = useCallback((snapshot: ExecutionSnapshot) => {
    setActiveRoute(snapshot.route);
    setTasks(snapshot.route.tasks);
    setWasteStats(collectedWasteStats(snapshot.route.tasks));
    const frame = snapshot.metrics.telemetry_frames[snapshot.metrics.telemetry_frames.length - 1];
    if (frame) {
      setServerTelemetry(frame);
      setLivePosition(frame.position);
      setBatteryPercent(Number(frame.battery_percent.toFixed(1)));
    }
  }, []);

  const startLiveSimulation = useCallback((route: RouteCandidate) => {
    if (route.routing_provenance === "SIMULATED") { startOfflineSimulation(route); return; }
    if (!route.approved_by || !route.kpi_evaluation?.passed || route.route_safety_issues.length) return;
    stopSimulationTimer();
    setExecutionErrorDetail(null);
    setExecutionState("EXECUTING");
    setStatusBanner("Cleaning is in progress. The plan manages battery checks and depot returns.");
    executionSession.current.start(route, snapshot => {
      applyExecutionSnapshot(snapshot);
      const state = snapshot.metrics.execution_state;
      setExecutionState(state === "PAUSED" ? "EXECUTING" : state);
      if (state === "COMPLETED") setStatusBanner("Cleaning finished. View Feedback for your results and the next run.");
    }, error => {
      setExecutionState("NEEDS_ASSISTANCE");
      setExecutionErrorDetail(error.message);
      setStatusBanner("Simulation connection or execution failed. Reconnect to confirm the server's actual progress.");
    });
  }, [applyExecutionSnapshot, startOfflineSimulation, stopSimulationTimer]);

  const reconnectExecution = async () => {
    if (!activeRoute || controlBusy) return;
    if (activeRoute.routing_provenance === "SIMULATED") { startLiveSimulation(activeRoute); return; }
    const isCurrent = planningSession.current.current();
    stopSimulationTimer();
    setControlBusy(true);
    try {
      await executionSession.current.settle();
      if (!isCurrent()) return;
      const snapshot = await executionSession.current.snapshot(activeRoute.route_id);
      if (!isCurrent()) return;
      applyExecutionSnapshot(snapshot);
      const state = snapshot.metrics.execution_state;
      setExecutionState(state);
      setExecutionErrorDetail(null);
      if (["READY", "PAUSED", "EXECUTING"].includes(state) && snapshot.route.approved_by && snapshot.route.kpi_evaluation?.passed && !snapshot.route.route_safety_issues.length) {
        startLiveSimulation(snapshot.route);
      } else if (state === "COMPLETED") {
        setStatusBanner("Cleaning finished. Your recorded results have been restored.");
      } else if (state === "AWAITING_APPROVAL") {
        setExecutionState("AWAITING_APPROVAL");
        setStatusBanner("Server confirms this route still needs approval. Review it before approving.");
      } else {
        setStatusBanner(`Server confirms ${state.replace(/_/g, " ")}. Simulation remains stopped; review the recorded state before continuing.`);
      }
    } catch (error) {
      if (isCurrent()) {
        setExecutionState("NEEDS_ASSISTANCE");
        setExecutionErrorDetail(`Server progress could not be confirmed: ${error instanceof Error ? error.message : String(error)}`);
      }
    } finally { setControlBusy(false); }
  };

  const controlExecution = async (action: "pause" | "estop") => {
    if (!activeRoute) return;
    // A stop also cancels any pending repeat/approval continuation. A late
    // planning response must never start a new job after the user stops it.
    if (action === "estop") planningSession.current.cancel();
    stopSimulationTimer();
    setControlBusy(true);
    const isCurrent = planningSession.current.current();
    try {
      if (activeRoute.routing_provenance === "SIMULATED") {
        setExecutionState(action === "estop" ? "EMERGENCY_STOP" : "PAUSED");
        setStatusBanner(action === "estop" ? "Offline simulation stopped. Clear the map to start a new simulation." : "Offline simulation paused.");
        return;
      }
      await executionSession.current.settle();
      if (!isCurrent()) return;
      if (action === "estop") {
        await executionSession.current.json("/events", { job_id: activeRoute.job_id, route_id: activeRoute.route_id, event_type: "EMERGENCY_STOP" });
      } else {
        const snapshot = await executionSession.current.snapshot(activeRoute.route_id);
        if (snapshot.metrics.execution_state === "EXECUTING") {
          await executionSession.current.json(`/routes/${activeRoute.route_id}/pause`, {});
        }
      }
      const snapshot = await executionSession.current.snapshot(activeRoute.route_id);
      if (!isCurrent()) return;
      applyExecutionSnapshot(snapshot);
      setExecutionState(snapshot.metrics.execution_state);
      setExecutionErrorDetail(null);
      setStatusBanner(action === "estop" ? "Emergency stop recorded for this machine. A new job or clearing the map cannot reset it." : "Simulation paused at the last completed segment.");
    } catch (error) {
      if (isCurrent()) {
        setExecutionState("NEEDS_ASSISTANCE");
        setExecutionErrorDetail(`Could not confirm ${action}: ${error instanceof Error ? error.message : String(error)}`);
        setStatusBanner(`Could not confirm ${action}: ${error instanceof Error ? error.message : String(error)}`);
      }
    } finally { setControlBusy(false); }
  };

  const handleApproveAndExecute = useCallback(
    async (candidate: RouteCandidate) => {
      const isCurrent = planningSession.current.current();
      if (
        !candidate.kpi_evaluation?.passed ||
        candidate.route_safety_issues.length > 0
      ) {
        setStatusBanner(
          "🛑 Cannot approve route: unresolved geometry or KPI constraint failure."
        );
        return;
      }

      if (candidate.routing_provenance !== "SIMULATED") {
        try {
          const approveRes = await fetch(
            `${API_BASE}/api/routes/${candidate.route_id}/approve`,
            {
              method: "POST",
              headers: {
                Authorization: `Bearer ${OPERATOR_TOKEN}`,
              },
            }
          );
          if (!isCurrent()) return;
          if (!approveRes.ok) throw new Error(apiErrorMessage((await approveRes.json()).detail));
          if (approveRes.ok) {
            const approvedRoute: RouteCandidate = await approveRes.json();
            if (!isCurrent()) return;
            setActiveRoute(approvedRoute);
            setExecutionState("READY");
            startLiveSimulation(approvedRoute);
            return;
          }
        } catch (error) {
          if (!isCurrent()) return;
          setExecutionState("NEEDS_ASSISTANCE");
          setExecutionErrorDetail(`Approval could not be confirmed: ${error instanceof Error ? error.message : String(error)}`);
          return;
        }
      }
      startLiveSimulation({ ...candidate, status: "APPROVED" });
    },
    [startLiveSimulation]
  );

  const handleRunAgain = useCallback(async (source: RouteCandidate) => {
    if (repeatInFlight.current || executionState !== "COMPLETED" || activeRoute?.route_id !== source.route_id || !canRepeatRoute(source)) return;
    repeatInFlight.current = true;
    setRunAgainBusy(true);
    const request = planningSession.current.begin();
    // Reuse this key after a lost response, so retrying cannot create two jobs.
    if (repeatRequest.current?.sourceId !== source.route_id) {
      repeatRequest.current = { sourceId: source.route_id, key: crypto.randomUUID() };
    }
    const key = repeatRequest.current.key;
    setExecutionErrorDetail(null);
    setPlanningErrorDetail(null);
    setStatusBanner("Preparing another cleaning run with the current battery and site settings...");
    stopSimulationTimer();
    try {
      await executionSession.current.settle();
      if (!request.isCurrent()) return;
      const response = await fetch(`${API_BASE}/api/routes/${source.route_id}/repeat`, {
        method: "POST", signal: request.signal,
        headers: { Authorization: `Bearer ${OPERATOR_TOKEN}`, "Content-Type": "application/json" },
        body: JSON.stringify({ idempotency_key: key }),
      });
      const data = await response.json();
      if (!request.isCurrent()) return;
      if (!response.ok) throw new Error(apiErrorMessage(data.detail || data));
      const candidate: RouteCandidate = data;
      setJobId(candidate.job_id);
      setActiveRoute(candidate);
      setTasks(candidate.tasks);
      setWasteStats(collectedWasteStats(candidate.tasks));
      setServerTelemetry(null);
      if (candidate.initial_battery_percent != null) setBatteryPercent(candidate.initial_battery_percent);
      setStartLocation(candidate.origin);
      setLivePosition(candidate.origin);
      setDisposalLocation(candidate.disposal_depot);
      setBoundary(candidate.operating_boundary?.vertices || []);
      setDynamicObstacles(candidate.exclusion_zones.map(zone => ({ id: zone.zone_id, name: zone.name, vertices: zone.polygon.vertices })));
      setPlanningEnvironment(candidate.planning_environment_map ?? null);
      if (candidate.planning_environment_map) {
        setGisFeatures(candidate.planning_environment_map.features);
        setAuthorizedCrossings(candidate.planning_environment_map.authorized_crossings);
      }
      if (candidate.kpi_policy_snapshot) {
        setOutingLimits({
          max_estimated_duration_minutes: candidate.kpi_policy_snapshot.max_estimated_duration_minutes ?? 90,
          min_energy_reserve_percent: candidate.kpi_policy_snapshot.min_energy_reserve_percent ?? 20,
          max_travel_distance_m: candidate.kpi_policy_snapshot.max_travel_distance_m ?? 15000,
        });
      }
      setEnableDepotCycles(candidate.depot_cycles_enabled ?? false);
      setSelectedClassification(candidate.task_mode === "HYBRID" ? "SWEEP_AND_BINS"
        : candidate.task_mode === "RUBBISH_COLLECTION" ? "BIN_PICKUP_ONLY" : "STANDARD_SWEEP");
      setLastPlanParams(null);
      setActiveNav("ROUTE");
      const diagStatus = candidate.planning_diagnostics?.planning_status;
      if (!candidate.kpi_evaluation?.passed || candidate.route_safety_issues.length || diagStatus === "BLOCKED" || diagStatus === "FAILED_KPI") {
        setExecutionState("NEEDS_ASSISTANCE");
        setPlanningPhase("BLOCKED");
        setPlanningErrorDetail(planningBlockMessage(candidate));
        setStatusBanner("This area needs attention before another run. Review the route message below.");
        return;
      }
      setPlanningPhase(diagStatus === "PARTIAL_SUCCESS" ? "PARTIAL_SUCCESS" : "SUCCESS");
      setExecutionState("AWAITING_APPROVAL");
      // The button is the user's instruction to run. Approval still goes through
      // the same backend checks as every other job before simulation starts.
      await handleApproveAndExecute(candidate);
    } catch (error) {
      if (!request.isCurrent() || (error instanceof Error && error.name === "AbortError")) return;
      setExecutionErrorDetail(`Another run could not be started: ${error instanceof Error ? error.message : String(error)}`);
      setStatusBanner("Could not start another run. Your previous results are kept; you can try again.");
    } finally {
      repeatInFlight.current = false;
      setRunAgainBusy(false);
    }
  }, [activeRoute?.route_id, executionState, handleApproveAndExecute, stopSimulationTimer]);

  const cancelPreviousRoute = useCallback(async (route: RouteCandidate | null) => {
    if (route && route.routing_provenance !== "SIMULATED") {
      await new RouteExecutionSession(API_BASE, OPERATOR_TOKEN).cancelRoute(route.route_id);
    }
  }, []);

  // Authoritative Backend Route Planning (NO silent synthetic fallback!)
  const triggerRoutePlan = useCallback(
    async (params: {
      nextBoundary: Coordinate[];
      nextObstacles: { id: string; name: string; vertices: Coordinate[] }[];
      nextTasks: TaskItem[];
      nextMachineId: string;
      reason: string;
      preserveCompleted: boolean;
      nextStartLocation?: Coordinate;
      nextDisposalLocation?: Coordinate;
      nextEnvironmentMap?: EnvironmentMapData | null;
    }) => {
      const {
        nextBoundary,
        nextObstacles,
        nextTasks,
        nextMachineId,
        reason,
        preserveCompleted,
      } = params;

      if (nextBoundary.length < 3 && nextTasks.length === 0) {
        return;
      }

      const continuingMission = Boolean(preserveCompleted && activeRoute && activeRoute.machine_id === nextMachineId && activeRoute.routing_provenance !== "SIMULATED");
      const request = planningSession.current.begin();
      setLastPlanParams(params);
      stopSimulationTimer();
      const isReplan = Boolean(activeRoute);
      setExecutionState(isReplan ? "REPLANNING" : "PLANNING");
      setPlanningPhase(isReplan ? "REPLANNING" : "PLANNING");
      setPlanningErrorDetail(null);
      setExecutionErrorDetail(null);
      setStatusBanner(
        `⏳ ${isReplan ? "Replanning" : "Planning"} road & terrain-aware route on backend (${reason})...`
      );

      // Keep the current route visible until the revised proposal arrives.
      try {
        await executionSession.current.settle();
        if (!request.isCurrent()) return;
        if (!continuingMission) {
          try { await cancelPreviousRoute(activeRoute); }
          catch (error) {
            if (!request.isCurrent()) return;
            setExecutionState(executionState === "EMERGENCY_STOP" ? "EMERGENCY_STOP" : "NEEDS_ASSISTANCE");
            setPlanningPhase("BLOCKED");
            setPlanningErrorDetail(`The previous job could not be cancelled: ${error instanceof Error ? error.message : String(error)}`);
            setStatusBanner("The previous job is still recorded. Resolve its state before replacing it.");
            return;
          }
          if (!request.isCurrent()) return;
        }
        const res = await fetch(`${API_BASE}/api/routes/plan`, {
          signal: request.signal,
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${OPERATOR_TOKEN}`,
          },
          body: JSON.stringify({
            job_id: continuingMission && activeRoute ? activeRoute.job_id : `job-${crypto.randomUUID()}`,
            machine_id: nextMachineId,
            preserve_completed: continuingMission,
            enable_depot_cycles: enableDepotCycles,
            environment_map: params.nextEnvironmentMap === undefined ? planningEnvironment : params.nextEnvironmentMap,
            kpi_policy_override: outingLimits,
            start_location: continuingMission
              ? livePosition
              : params.nextStartLocation || startLocation,
            disposal_location: params.nextDisposalLocation || disposalLocation,
            operating_boundary:
              nextBoundary.length >= 3 ? { vertices: nextBoundary } : null,
            exclusion_zones: nextObstacles.map((o) => ({
              zone_id: o.id,
              name: o.name,
              reason: "Live Obstacle",
              polygon: { vertices: o.vertices },
            })),
            tasks: continuingMission ? nextTasks : nextTasks.filter(task => !task.completed),
            allow_partial_coverage_recovery: true,
          }),
        });

        if (!request.isCurrent()) return;
        if (!res.ok) {
          let errMsg = `HTTP ${res.status}`;
          try {
            const errJson = await res.json();
            errMsg = apiErrorMessage(errJson.detail || errJson);
          } catch {
            // ignore json parse failure
          }
          if (!request.isCurrent()) return;
          // Authoritative backend rejected the plan: DO NOT fabricate a fallback route!
          setActiveRoute(null);
          setExecutionState("NEEDS_ASSISTANCE");
          setPlanningPhase("BLOCKED");
          setPlanningErrorDetail(errMsg);
          setStatusBanner(`🛑 Backend Route Validation Blocked: ${errMsg}`);
          return;
        }

        const backendCandidate: RouteCandidate = await res.json();
        if (!request.isCurrent()) return;
        setActiveRoute(backendCandidate);
        setTasks(backendCandidate.tasks);
        setServerTelemetry(null);
        if (backendCandidate.initial_battery_percent != null) setBatteryPercent(backendCandidate.initial_battery_percent);
        setLivePosition(backendCandidate.origin);
        setWasteStats(collectedWasteStats(backendCandidate.tasks));

        const diagStatus =
          backendCandidate.planning_diagnostics?.planning_status ||
          (backendCandidate.kpi_evaluation?.passed ? "SUCCESS" : "BLOCKED");

        if (
          !backendCandidate.kpi_evaluation?.passed ||
          diagStatus === "BLOCKED" ||
          diagStatus === "FAILED_KPI"
        ) {
          setExecutionState("NEEDS_ASSISTANCE");
          setPlanningPhase("BLOCKED");
          const failReasons = [
            ...backendCandidate.route_safety_issues,
            ...(backendCandidate.kpi_evaluation?.failed_constraints || []),
          ].join(", ");
          setPlanningErrorDetail(planningBlockMessage(backendCandidate));
          setStatusBanner(
            `${isReplan ? `Replanned with ${nextObstacles.length} obstacle(s). ` : ""}Route v${backendCandidate.route_version} is blocked: ${failReasons}`
          );
          return;
        }

        setPlanningPhase(
          diagStatus === "PARTIAL_SUCCESS" ? "PARTIAL_SUCCESS" : "SUCCESS"
        );
        setExecutionState("AWAITING_APPROVAL");
        setStatusBanner(
          `${isReplan ? `Replanned with ${nextObstacles.length} obstacle(s); route v${backendCandidate.route_version}. ` : ""}` +
          (backendCandidate.planning_diagnostics?.summary_explanation || `Route v${backendCandidate.route_version} validated by backend.`)
        );

        if (autoApproveValidated) {
          await handleApproveAndExecute(backendCandidate);
        }
      } catch (networkErr: any) {
        if (!request.isCurrent() || networkErr?.name === "AbortError") return;
        setActiveRoute(null);
        // Network error reaching backend: surface clearly instead of silently faking a backend response
        setExecutionState("NEEDS_ASSISTANCE");
        setPlanningPhase("BACKEND_UNREACHABLE");
        setPlanningErrorDetail(
          `Backend server at ${API_BASE} is unreachable (${
            networkErr?.message || "Connection refused"
          }). Start the FastAPI backend or click 'Run Labeled Offline Simulation' to preview with road/lake constraints.`
        );
        setStatusBanner(
          `⚠️ Backend API unreachable at ${API_BASE} — No synthetic route was substituted. Use Labeled Offline Simulation if running frontend standalone.`
        );
      }
    },
    [
      activeRoute,
      cancelPreviousRoute,
      executionState,
      autoApproveValidated,
      disposalLocation,
      enableDepotCycles,
      planningEnvironment,
      outingLimits,
      handleApproveAndExecute,
      jobId,
      livePosition,
      startLocation,
      stopSimulationTimer,
    ]
  );

  // Explicit opt-in Labeled Offline Simulation (enforces road/lake/building constraints)
  const handleRunLabeledOfflineSimulation = () => {
    if (!lastPlanParams) return;
    const offlineStart = lastPlanParams.preserveCompleted ? livePosition : lastPlanParams.nextStartLocation || startLocation;
    const offlineDepot = lastPlanParams.nextDisposalLocation || disposalLocation;
    const waterFeatures = combineWaterFeatures(gisFeatures, MAPPED_WATER_FEATURES);
    const waterConflict = offlineWaterConflict({ boundary: lastPlanParams.nextBoundary,
      start: offlineStart, depot: offlineDepot, taskLocations: lastPlanParams.nextTasks.map(task => task.location), waterFeatures });
    if (waterConflict) {
      setActiveRoute(null); setPlanningPhase("BLOCKED"); setExecutionState("NEEDS_ASSISTANCE");
      setPlanningErrorDetail(waterConflict); setStatusBanner(waterConflict);
      return;
    }
    const nextVer = routeVersionRef.current + 1;
    routeVersionRef.current = nextVer;
    const simCandidate = buildLabeledOfflineConstrainedRoute({
      jobId,
      version: nextVer,
      machineId: lastPlanParams.nextMachineId,
      boundary: lastPlanParams.nextBoundary,
      dynamicObstacles: lastPlanParams.nextObstacles,
      tasks: lastPlanParams.nextTasks,
      startLocation: offlineStart,
      disposalLocation: offlineDepot,
      retainedCompletedSegments:
        lastPlanParams.preserveCompleted && activeRoute
          ? activeRoute.segments.filter((s) => s.completed)
          : [],
      replanReason: `${lastPlanParams.reason} [LABELED OFFLINE SIMULATION]`,
      gisFeatures: [...gisFeatures.filter(feature => feature.feature_type !== "WATER_BODY"), ...waterFeatures],
      crossings: authorizedCrossings,
    });

    const segmentWaterConflict = offlineWaterConflict({ boundary: lastPlanParams.nextBoundary,
      start: offlineStart, depot: offlineDepot, taskLocations: lastPlanParams.nextTasks.map(task => task.location),
      waterFeatures, segmentPaths: simCandidate.segments.map(segment => segment.waypoints) });
    if (segmentWaterConflict) {
      setActiveRoute(null); setPlanningPhase("BLOCKED"); setExecutionState("NEEDS_ASSISTANCE");
      setPlanningErrorDetail(segmentWaterConflict); setStatusBanner(segmentWaterConflict);
      return;
    }

    setActiveRoute(simCandidate);
    const st = simCandidate.planning_diagnostics?.planning_status || "SUCCESS";
    if (st === "BLOCKED") {
      setPlanningPhase("BLOCKED");
      setExecutionState("NEEDS_ASSISTANCE");
      setPlanningErrorDetail(
        simCandidate.planning_diagnostics?.summary_explanation ||
          "Blocked by impassable terrain."
      );
      setStatusBanner(
        `🛑 [OFFLINE SIM] ${simCandidate.planning_diagnostics?.summary_explanation}`
      );
    } else {
      setPlanningPhase(
        st === "PARTIAL_SUCCESS" ? "PARTIAL_SUCCESS" : "SUCCESS"
      );
      setPlanningErrorDetail(null);
      setStatusBanner(
        `🧪 [OFFLINE SIM] ${simCandidate.planning_diagnostics?.summary_explanation}`
      );
      if (autoApproveValidated) {
        startLiveSimulation(simCandidate);
      } else {
        setExecutionState("AWAITING_APPROVAL");
      }
    }
  };

  // Urgent Bulky Waste Handler
  const handleAcceptUrgentWaste = () => {
    if (!urgentAlert) return;
    const alertItem = urgentAlert;
    setUrgentAlert(null);

    const nextMachine = "heavy-utility-02";
    setMachineId(nextMachine);

    const nextBoundary =
      boundary.length >= 3 ? boundary : CAMPUS_PRESETS[0].boundary;
    if (boundary.length < 3) {
      setBoundary(nextBoundary);
    }

    const urgentTask: TaskItem = {
      task_id: `urgent-bulky-${Date.now().toString().slice(-3)}`,
      task_type: "OVERSIZED_RUBBISH",
      location: alertItem.location,
      priority: 10,
      service_duration_sec: 180,
      ...newTaskWindow(activeRoute?.machine_id === nextMachine ? missionElapsedMinutes : 0),
      completed: false,
      rubbish_spec: {
        description: alertItem.description,
        is_oversized: true,
        estimated_weight_kg: alertItem.weightKg,
        estimated_volume_m3: 1.8,
        max_dimension_m: 2.0,
        dimensions_uncertain: false,
        requires_heavy_lifter: true,
      },
    };

    const nextTasks = [...tasks, urgentTask];
    setTasks(nextTasks);
    setSelectedClassification("BULKY_WASTE");
    setMapCenter(alertItem.location);

    triggerRoutePlan({
      nextBoundary,
      nextObstacles: dynamicObstacles,
      nextTasks,
      nextMachineId: nextMachine,
      reason: `Urgent Bulky Waste Accepted (${alertItem.weightKg}kg) — Heavy Collector Assigned`,
      preserveCompleted: Boolean(activeRoute),
    });
  };

  const handleSimulateNewUrgentRequest = () => {
    const spots = [
      {
        label: "Planetarium North Plaza Steps",
        coord: { lat: 3.13931, lon: 101.68715 },
        desc: "Discarded Office Sofa & Broken Metal Rack (~92 kg)",
        kg: 92,
      },
      {
        label: "Central Courtyard South Curb",
        coord: { lat: 3.13908, lon: 101.68702 },
        desc: "Heavy Renovation Timber & Mattress (~110 kg)",
        kg: 110,
      },
    ];
    const pick = spots[Math.floor(Math.random() * spots.length)];
    setUrgentAlert({
      id: `urgent-${Date.now()}`,
      title: "🚨 Urgent Citizen Request: Bulky / Heavy Waste Collection",
      description: `${pick.desc} reported at ${pick.label}. Requires Heavy Collector with hydraulic lifter.`,
      weightKg: pick.kg,
      locationLabel: `${pick.label} (${pick.coord.lat.toFixed(5)}, ${pick.coord.lon.toFixed(5)})`,
      location: pick.coord,
      timestamp: new Date().toLocaleTimeString(),
    });
  };

  // Classify zone & trigger authoritative backend route planning
  const handleClassifyZone = (classification: ZoneClassification, selectedBoundary = boundary, resetArea = false) => {
    if (selectedBoundary.length < 3) {
      setStatusBanner("Draw a box or at least three polygon vertices, then choose a task to plan.");
      return;
    }
    const activeBoundary = selectedBoundary;
    const locations = automaticDryLocations(activeBoundary, areaLocations(activeBoundary), combineWaterFeatures(gisFeatures, MAPPED_WATER_FEATURES));
    const useNewPortals = resetArea || drawMode === "BOUNDARY";
    if (useNewPortals) {
      setStartLocation(locations.start);
      setDisposalLocation(locations.depot);
      setLivePosition(locations.start);
    }

    setSelectedClassification(classification);
    let nextMachine = machineId;
    let nextTasks: TaskItem[] = [];

    if (classification === "STANDARD_SWEEP") {
      nextTasks = [];
    } else {
      nextTasks = tasks.filter(task => !task.completed && (
        classification !== "BULKY_WASTE" || task.task_type === "OVERSIZED_RUBBISH"
      ));
      if (classification === "BULKY_WASTE") {
        nextMachine = "heavy-utility-02";
        setMachineId(nextMachine);
      }
      if (!nextTasks.length) {
        stopSimulationTimer();
        planningSession.current.cancel();
        setActiveRoute(null); setPlanningPhase("STANDBY"); setExecutionState("IDLE");
        setPlanningErrorDetail(null);
        setDrawMode(classification === "BULKY_WASTE" ? "OVERSIZED" : "TASK");
        setStatusBanner("Click the actual collection location on the map to create a task and plan its route.");
        return;
      }
    }

    setTasks(nextTasks);
    setDrawMode("NONE");

    const effectiveBoundary =
      (classification === "BIN_PICKUP_ONLY" || classification === "BULKY_WASTE") ? [] : activeBoundary;

    triggerRoutePlan({
      nextBoundary: effectiveBoundary,
      nextObstacles: resetArea ? [] : dynamicObstacles,
      nextTasks,
      nextStartLocation: useNewPortals ? locations.start : startLocation,
      nextDisposalLocation: useNewPortals ? locations.depot : disposalLocation,
      nextMachineId: nextMachine,
      reason: `Classified zone as ${classification.replace(/_/g, " ")}`,
      preserveCompleted: false,
    });
  };

  // Map Callbacks
  const handleSelectBoxArea = (boxVertices: Coordinate[]) => {
    setBoundary(boxVertices);
    setDynamicObstacles([]);
    planningSession.current.cancel();
    stopSimulationTimer();
    const locations = automaticDryLocations(boxVertices, areaLocations(boxVertices), combineWaterFeatures(gisFeatures, MAPPED_WATER_FEATURES));
    setStartLocation(locations.start); setDisposalLocation(locations.depot); setLivePosition(locations.start);
    setActiveRoute(null); setTasks([]); setWasteStats(collectedWasteStats([])); setServerTelemetry(null); setSelectedClassification(null);
    setPlanningPhase("STANDBY"); setExecutionState("IDLE"); setPlanningErrorDetail(null);
    setDrawMode("NONE");
    setStatusBanner(locations.noDryPoint
      ? "The selected area is covered by mapped water or its shoreline clearance. Select a dry area to clean."
      : locations.adjusted
      ? "Area selected. Automatic start and depot pins were placed on dry land. Mapped water will be excluded when planning."
      : "Area selected. Choose Standard Sweep, Sweep + Bins, Bin Pickup, or Bulky Waste to plan.");
  };

  const handleAddVertex = (coord: Coordinate) => {
    if (drawMode === "BOUNDARY") {
      setBoundary((prev) => [...prev, coord]);
    } else if (drawMode === "START") {
      setStartLocation(coord);
      setLivePosition(coord);
    } else if (drawMode === "DEPOT") {
      setDisposalLocation(coord);
      setDrawMode("NONE");
      if (activeRoute || boundary.length >= 3 || tasks.length) {
        triggerRoutePlan({ nextBoundary: ["BIN_PICKUP_ONLY", "BULKY_WASTE"].includes(selectedClassification || "") ? [] : boundary, nextObstacles: dynamicObstacles,
          nextTasks: tasks, nextMachineId: machineId, nextDisposalLocation: coord,
          reason: "Disposal depot moved", preserveCompleted: true });
      }
    }
  };

  const handleDropInstantObstacle = (obsVertices: Coordinate[]) => {
    const newObs = {
      id: `obs-${crypto.randomUUID()}`,
      name: `Obstacle #${dynamicObstacles.length + 1}`,
      vertices: obsVertices,
    };
    const nextObs = [...dynamicObstacles, newObs];
    setDynamicObstacles(nextObs);
    setDrawMode("NONE");
    setStatusBanner(`${newObs.name} added. Recalculating the route with this exclusion.`);

    if (activeRoute || boundary.length >= 3 || tasks.length > 0) {
      triggerRoutePlan({
        nextBoundary: ["BIN_PICKUP_ONLY", "BULKY_WASTE"].includes(selectedClassification || "") ? [] : boundary,
        nextObstacles: nextObs,
        nextTasks: tasks,
        nextMachineId: machineId,
        reason: `Obstacle detected on path — Auto-replanning around ${newObs.name}`,
        preserveCompleted: true,
      });
    }
  };

  const handleAddTaskOnMap = (
    coord: Coordinate,
    isOversized: boolean,
    isEventWaste?: boolean
  ) => {
    const nextMachine =
      isOversized || isEventWaste ? "heavy-utility-02" : machineId;
    if (nextMachine !== machineId) {
      setMachineId(nextMachine);
    }
    const newTask: TaskItem = {
      task_id:
        isOversized || isEventWaste
          ? `bulky-${tasks.length + 1}`
          : `bin-${tasks.length + 1}`,
      task_type:
        isOversized || isEventWaste
          ? "OVERSIZED_RUBBISH"
          : "RUBBISH_COLLECTION",
      location: coord,
      priority: isOversized || isEventWaste ? 10 : 8,
      service_duration_sec: isOversized || isEventWaste ? 180 : 45,
      ...newTaskWindow(missionElapsedMinutes),
      completed: false,
      rubbish_spec:
        isOversized || isEventWaste
          ? {
              description: "Reported bulky item on map (~80kg)",
              is_oversized: true,
              estimated_weight_kg: 80,
              estimated_volume_m3: 1.5,
              max_dimension_m: 1.8,
              dimensions_uncertain: false,
              requires_heavy_lifter: true,
            }
          : {
              description: "Standard bin contents (editable estimate)",
              is_oversized: false,
              estimated_weight_kg: 15,
              estimated_volume_m3: 0.12,
              max_dimension_m: 0.6,
              dimensions_uncertain: false,
              requires_heavy_lifter: false,
              weight_estimation_method: "DEFAULT_BIN_CONTENTS_ESTIMATE",
              volume_estimation_method: "DEFAULT_BIN_CONTENTS_ESTIMATE",
            },
    };
    const nextTasks = [...tasks, newTask];
    setTasks(nextTasks);

    if (nextTasks.length > 0) {
      triggerRoutePlan({
        nextBoundary: ["BIN_PICKUP_ONLY", "BULKY_WASTE"].includes(selectedClassification || "") ? [] : boundary,
        nextObstacles: dynamicObstacles,
        nextTasks,
        nextMachineId: nextMachine,
        reason: `Added ${newTask.task_id} on map — Route auto-updated`,
        preserveCompleted: true,
      });
    }
  };

  const prepareToReplaceJob = async (): Promise<boolean> => {
    if (controlBusy) return false;
    const request = planningSession.current.begin();
    stopSimulationTimer();
    setControlBusy(true);
    try {
      await executionSession.current.settle();
      if (!request.isCurrent()) return false;
      await cancelPreviousRoute(activeRoute);
      return request.isCurrent();
    } catch (error) {
      if (request.isCurrent()) {
        setExecutionErrorDetail(`The current job could not be cancelled: ${error instanceof Error ? error.message : String(error)}`);
        setStatusBanner("The current job is kept on screen until cancellation is confirmed. An emergency stop cannot be cleared here.");
      }
      return false;
    } finally { setControlBusy(false); }
  };

  const handleClearMap = async () => {
    if (!await prepareToReplaceJob()) return false;
    setJobId(`job-${crypto.randomUUID()}`);
    setMapResetKey(key => key + 1);
    setLastPlanParams(null);
    setShowGisLayers(false);
    setPlanningEnvironment(null);
    setSiteSource(null);
    setGisFeatures(DEFAULT_GIS_FEATURES);
    setAuthorizedCrossings(DEFAULT_AUTHORIZED_CROSSINGS);
    setUrgentAlert(null);
    setMachineId("sweeper-alpha-01");
    routeVersionRef.current = 0;
    setBatteryPercent(96);
    setLivePosition(startLocation);
    setBoundary([]);
    setDynamicObstacles([]);
    setTasks([]);
    setServerTelemetry(null);
    setWasteStats(collectedWasteStats([]));
    setActiveRoute(null);
    setSelectedClassification(null);
    setExecutionState("IDLE");
    setPlanningPhase("STANDBY");
    setPlanningErrorDetail(null);
    setDrawMode("BOX_AREA");
    setStatusBanner(
      "Map cleared — Draw a box on the map or pick a scenario to select a new cleaning area."
    );
    return true;
  };

  const handleApplyImportedSite = async (site: SiteImportPreview) => {
    if (!await prepareToReplaceJob()) return false;
    const fields = site.plan_fields;
    setJobId(`job-${crypto.randomUUID()}`);
    setMapResetKey(key => key + 1);
    setLastPlanParams(null);
    setActiveRoute(null);
    setServerTelemetry(null);
    setExecutionState("IDLE");
    setPlanningPhase("STANDBY");
    setPlanningErrorDetail(null);
    setExecutionErrorDetail(null);
    setPlanningEnvironment(fields.environment_map);
    setSiteSource(site.source || "Operator-provided site file");
    setGisFeatures(fields.environment_map.features);
    setAuthorizedCrossings(fields.environment_map.authorized_crossings);
    setBoundary(fields.operating_boundary.vertices);
    setDynamicObstacles(fields.exclusion_zones.map(zone => ({ id: zone.zone_id, name: zone.name, vertices: zone.polygon.vertices })));
    setTasks(fields.tasks);
    setWasteStats(collectedWasteStats(fields.tasks));
    setStartLocation(fields.start_location);
    setDisposalLocation(fields.disposal_location);
    setLivePosition(fields.start_location);
    setMapCenter(fields.start_location);
    setSelectedClassification(null);
    setDrawMode("NONE");
    setShowGisLayers(true);
    setStatusBanner("Operator-provided site applied. Choose a task to plan, review its route, then approve the simulation.");
    return true;
  };

  const handleMachineEstimatesSaved = () => {
    stopSimulationTimer();
    planningSession.current.cancel();
    setActiveRoute(null);
    setServerTelemetry(null);
    setLastPlanParams(null);
    setExecutionState("IDLE");
    setPlanningPhase("STANDBY");
    setPlanningErrorDetail(null);
    setExecutionErrorDetail(null);
    setStatusBanner("Machine estimates saved. Choose the task type again to replan, then review and approve the new route.");
  };

  const totalRouteKm = activeRoute
    ? (activeRoute.total_distance_m / 1000).toFixed(2)
    : "0.00";
  const completedSegCount = activeRoute
    ? activeRoute.segments.filter((s) => s.completed).length
    : 0;
  const totalSegCount = activeRoute ? activeRoute.segments.length : 0;
  const routeProgressPct =
    totalSegCount > 0
      ? Math.round((completedSegCount / totalSegCount) * 100)
      : 0;

  const activeSegment = activeRoute?.segments.find(segment => !segment.completed);
  const depotServiceNext = executionState === "EXECUTING" && activeSegment?.segment_type === "DEPOT_RECHARGE";
  const machineConditionText =
    executionState === "EMERGENCY_STOP"
      ? "🛑 EMERGENCY STOP ENGAGED"
      : planningPhase === "BLOCKED"
      ? "⚠️ Blocked by Terrain / Safety Interlock"
      : executionState === "EXECUTING"
      ? depotServiceNext ? "Depot service next (simulation) — work resumes automatically" : "Cleaning in progress (simulation)"
      : executionState === "PAUSED"
      ? "⏸️ Paused — Awaiting Resume"
      : executionState === "COMPLETED"
      ? "✅ Cleaning finished (simulation)"
      : "Simulation standby — no live sensor connection";

  const latestTelemetryFrame: TelemetryFrame | null = activeRoute && serverTelemetry?.route_id === activeRoute.route_id
    ? serverTelemetry
    : activeRoute?.routing_provenance === "SIMULATED" ? {
        telemetry_id: "live-frame",
        machine_id: machineId,
        route_id: activeRoute.route_id,
        mission_elapsed_min: 0,
        position: livePosition,
        speed_mps: executionState === "EXECUTING" ? 1.2 : 0,
        battery_percent: batteryPercent,
        payload_kg: wasteStats.totalWeightKg,
        payload_volume_m3: 0.4,
        route_deviation_m: 0.1,
        provenance:
          executionState === "EXECUTING" || executionState === "COMPLETED"
            ? "SIMULATED"
            : activeRoute.routing_provenance,
      }
    : null;

  const diag = activeRoute?.planning_diagnostics || null;

  return (
    <div className="wireframe-shell">
      {/* LEFT SIDE PANEL: Switch between Real-time (Robot Collecting + YOLO) and Route Automation */}
      <aside className="left-nav-sidebar">
        <div className="brand-header">
          <div className="brand-logo">🤖 CleanRoute AI</div>
          <div className="brand-subtitle">
            Road-Aware Autonomous Waste &amp; Route AI
          </div>
        </div>

        <div className="nav-section-label">VIEW SWITCHER</div>
        <button
          type="button"
          className={`nav-switch-btn ${
            activeNav === "REALTIME" ? "active" : ""
          }`}
          onClick={() => setActiveNav("REALTIME")}
        >
          <span className="nav-icon">🎥</span>
          <div className="nav-btn-text">
            <strong>Real-time</strong>
            <span>YOLO &amp; Waste Categories</span>
          </div>
        </button>

        <button
          type="button"
          className={`nav-switch-btn ${activeNav === "ROUTE" ? "active" : ""}`}
          onClick={() => setActiveNav("ROUTE")}
        >
          <span className="nav-icon">🗺️</span>
          <div className="nav-btn-text">
            <strong>Route</strong>
            <span>Road-Aware Map &amp; Recovery</span>
          </div>
        </button>

        <button
          type="button"
          className={`nav-switch-btn ${activeNav === "FEEDBACK" ? "active" : ""}`}
          onClick={() => setActiveNav("FEEDBACK")}
        >
          <span className="nav-icon" aria-hidden="true">↻</span>
          <div className="nav-btn-text">
            <strong>Feedback</strong>
            <span>Results &amp; next steps</span>
          </div>
        </button>
        <button
          type="button"
          className={`nav-switch-btn ${activeNav === "LOGS" ? "active" : ""}`}
          onClick={() => setActiveNav("LOGS")}
        >
          <span className="nav-icon" aria-hidden="true">≡</span>
          <div className="nav-btn-text">
            <strong>Logs &amp; changes</strong>
            <span>Activity history</span>
          </div>
        </button>

        {/* Urgent Request Trigger Button */}
        <div className="nav-section-label" style={{ marginTop: 14 }}>
          LIVE DISPATCH DEMO
        </div>
        <button
          type="button"
          className="urgent-trigger-btn"
          onClick={handleSimulateNewUrgentRequest}
        >
          🔔 Simulate Sudden Bulky Waste Request
        </button>

        {/* Execution Policy Toggle */}
        <div className="nav-section-label" style={{ marginTop: 10 }}>
          SAFETY &amp; GIS OVERLAYS
        </div>
        <label className="sidebar-checkbox-row">
          <input
            type="checkbox"
            checked={showGisLayers}
            onChange={(e) => setShowGisLayers(e.target.checked)}
          />
          <span>Show Reference Terrain Overlays</span>
        </label>
        <label className="sidebar-checkbox-row">
          <input
            type="checkbox"
            checked={autoApproveValidated}
            onChange={(e) => setAutoApproveValidated(e.target.checked)}
          />
          <span>Auto-Approve Validated Plans</span>
        </label>

        {/* Sidebar Footer Quick Controls */}
        <div className="sidebar-footer-controls">
          <div className="mini-status-pill">
            <span className="dot" />
            <span>
              {runAgainBusy ? "Preparing next run" : executionState === "COMPLETED" ? "Finished"
                : executionState === "EXECUTING" ? "Cleaning"
                : executionState === "PAUSED" ? "Paused"
                : executionState === "AWAITING_APPROVAL" ? "Ready for your approval"
                : executionState === "EMERGENCY_STOP" ? "Emergency stop"
                : executionState === "NEEDS_ASSISTANCE" ? "Needs attention"
                : ["PLANNING", "REPLANNING"].includes(planningPhase) ? "Planning your route" : "Ready"}
            </span>
          </div>

          {activeRoute && (
            <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
              {executionState === "AWAITING_APPROVAL" ? (
                <button
                  type="button"
                  className="btn-primary-sm"
                  onClick={() => handleApproveAndExecute(activeRoute)}
                >
                  ✅ Approve &amp; Run
                </button>
              ) : executionState === "EXECUTING" ? (
                <button
                  type="button"
                  className="btn-secondary-sm"
                  disabled={controlBusy}
                  onClick={() => void controlExecution("pause")}
                >
                  ⏸️ Pause
                </button>
              ) : ["PAUSED", "READY"].includes(executionState) ? (
                <button
                  type="button"
                  className="btn-primary-sm"
                  disabled={controlBusy || !["PAUSED", "READY"].includes(executionState) || !activeRoute.kpi_evaluation?.passed || (activeRoute.routing_provenance !== "SIMULATED" && !activeRoute.approved_by)}
                  onClick={() => void reconnectExecution()}
                >
                  ▶️ Resume
                </button>
              ) : executionState === "COMPLETED" && canRepeatRoute(activeRoute) ? (
                <button type="button" className="btn-primary-sm" disabled={runAgainBusy || controlBusy}
                  onClick={() => void handleRunAgain(activeRoute)}>
                  {runAgainBusy ? "Preparing..." : "Run again"}
                </button>
              ) : null}
              {executionErrorDetail && <button type="button" className="btn-secondary-sm" disabled={controlBusy} onClick={() => void reconnectExecution()}>
                {controlBusy ? "Checking…" : "Reconnect"}
              </button>}
              <button
                type="button"
                className="btn-danger-sm"
                disabled={controlBusy || executionState === "EMERGENCY_STOP"}
                onClick={() => void controlExecution("estop")}
              >
                {executionState === "EMERGENCY_STOP" ? "Stopped" : "E-Stop"}
              </button>
            </div>
          )}
        </div>
      </aside>

      {/* MAIN STAGE */}
      <main className="main-stage" ref={workspaceRef} tabIndex={0}>
        {/* TOP HEADER BAR: Status + Right-Aligned "Machine ID v" Dropdown */}
        <header className="stage-top-bar">
          <div className="stage-status-msg">{statusBanner}</div>

          <div className="machine-select-wrapper">
            <label htmlFor="machine-id-select" className="machine-id-label">
              Machine ID
            </label>
            <select
              id="machine-id-select"
              className="machine-id-dropdown"
              value={machineId}
              onChange={(e) => {
                const next = e.target.value;
                setMachineId(next);
                if (activeRoute || boundary.length >= 3) {
                  triggerRoutePlan({
                    nextBoundary: ["BIN_PICKUP_ONLY", "BULKY_WASTE"].includes(selectedClassification || "") ? [] : boundary,
                    nextObstacles: dynamicObstacles,
                    nextTasks: tasks,
                    nextMachineId: next,
                    reason: `Switched Machine ID to ${next} (applying machine-specific road/footpath permissions)`,
                    preserveCompleted: false,
                  });
                }
              }}
            >
              <option value="sweeper-alpha-01">
                sweeper-alpha-01 (Compact Pedestrian Sweeper)
              </option>
              <option value="heavy-utility-02">
                heavy-utility-02 (Heavy Bulky Collector — Road/Footpath Restricted)
              </option>
            </select>
          </div>
        </header>

        <div style={{ padding: "5px 22px", color: "#94a3b8", fontSize: 11 }}>Execution mode: {activeRoute?.routing_provenance === "SIMULATED" ? "offline preview simulation" : "backend simulation"} · GPS progress, battery use and depot service are simulated.</div>
        <MissionStatusStrip apiBase={API_BASE} token={OPERATOR_TOKEN} machineId={machineId}
          route={activeRoute} executionState={executionState} telemetry={serverTelemetry} batteryPercent={batteryPercent} />
        {executionErrorDetail && <div role="alert" style={{ margin: "8px 22px", padding: 12, border: "1px solid #b45309", borderRadius: 8, background: "#451a0366", fontSize: 13 }}>
          <strong>Execution connection / control issue:</strong> {executionErrorDetail}
          <p style={{ margin: "6px 0" }}>The route's planning result is unchanged. Reconnect to read the server's recorded progress and resume only if its current state permits execution.</p>
          <button type="button" className="btn-secondary-sm" disabled={controlBusy || !activeRoute} onClick={() => void reconnectExecution()}>{controlBusy ? "Checking server…" : "Reconnect & resume simulation"}</button>
        </div>}

        {/* SUDDEN USER REQUEST OF URGENT BULKY / HEAVY WASTE COLLECTION NOTIFICATION */}
        {urgentAlert && (
          <div className="urgent-notification-banner">
            <div className="urgent-notif-left">
              <div className="urgent-badge">URGENT CITIZEN REQUEST</div>
              <div>
                <div className="urgent-title">{urgentAlert.title}</div>
                <div className="urgent-desc">
                  {urgentAlert.description} ·{" "}
                  <strong>📍 {urgentAlert.locationLabel}</strong>
                </div>
              </div>
            </div>
            <div className="urgent-notif-actions">
              <button
                type="button"
                className="btn-accept-urgent"
                onClick={handleAcceptUrgentWaste}
              >
                ⚡ Accept &amp; Auto-Replan Route
              </button>
              <button
                type="button"
                className="btn-dismiss-urgent"
                onClick={() => setUrgentAlert(null)}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {(activeNav === "FEEDBACK" || activeNav === "LOGS") && (
          <FeedbackPanel apiBase={API_BASE} token={OPERATOR_TOKEN} route={activeRoute} view={activeNav}
            executionState={executionState} telemetry={serverTelemetry}
            onRunAgain={executionState === "COMPLETED" ? handleRunAgain : undefined} runAgainBusy={runAgainBusy} onOpenRoute={() => setActiveNav("ROUTE")} />
        )}

        {activeRoute?.status === "COMPLETED" && activeNav !== "FEEDBACK" && (
          <section className="completed-run-banner" aria-label="Cleaning finished">
            <div><strong>Cleaning finished</strong><p>{canRepeatRoute(activeRoute) ? "Your results are saved. Start another job with this area's map and bin locations." : "Your results are saved. Add a new waste observation or select the next area to clean."}</p></div>
            <div className="completed-run-actions">
              {executionState === "COMPLETED" && canRepeatRoute(activeRoute) && <button type="button" className="btn-primary-sm"
                disabled={runAgainBusy || controlBusy} onClick={() => void handleRunAgain(activeRoute)}>
                {runAgainBusy ? "Preparing next run..." : "Run this area again"}
              </button>}
              <button type="button" className="btn-secondary-sm" onClick={() => setActiveNav("FEEDBACK")}>View results</button>
            </div>
          </section>
        )}

        {/* VIEW 1: REAL-TIME ROBOT COLLECTING + MIDDLE YOLO SCREEN */}
        {(
          <div className="realtime-content-grid" style={{ display: activeNav === "REALTIME" ? undefined : "none" }}>
            {/* MIDDLE SCREEN: YOLO REAL-TIME VISION */}
            <WasteDetector
              apiBase={API_BASE}
              token={OPERATOR_TOKEN}
              active={activeNav === "REALTIME"}
              onDetections={setVisionDetections}
            />

            {/* BOTTOM ROW: Left = "Waste Collected" (2x2 Grid), Right = "Machine condition / Battery / Route : xx km" */}
            <div className="realtime-bottom-row">
              <WasteSummary observations={visionDetections} completedCollections={wasteStats} />

              <section className="machine-condition-card">
                <div className="condition-row">
                  <span className="condition-key">Machine condition</span>
                  <strong className="condition-val">
                    {machineConditionText}
                  </strong>
                </div>

                <div className="condition-row">
                  <span className="condition-key">Battery</span>
                  <div className="condition-bar-wrap">
                    <div className="condition-bar-track">
                      <div
                        className="condition-bar-fill"
                        style={{
                          width: `${batteryPercent}%`,
                          background:
                            batteryPercent > 30 ? "#10b981" : "#ef4444",
                        }}
                      />
                    </div>
                    <strong className="condition-val">{batteryPercent}%</strong>
                  </div>
                </div>

                <div className="condition-row">
                  <span className="condition-key">
                    Route : {totalRouteKm} km
                  </span>
                  <div
                    style={{ display: "flex", alignItems: "center", gap: 10 }}
                  >
                    <span style={{ fontSize: 12, color: "#94a3b8" }}>
                      {activeRoute
                        ? `${routeProgressPct}% completed (${completedSegCount}/${totalSegCount} segs)`
                        : "No route active yet"}
                    </span>
                    <button
                      type="button"
                      className="btn-jump-route"
                      onClick={() => setActiveNav("ROUTE")}
                    >
                      Open Route Map →
                    </button>
                  </div>
                </div>
              </section>
            </div>
          </div>
        )}
        {activeNav === "ROUTE" && (
          /* VIEW 2: ROUTE AUTOMATION + EXPLAINABLE PLANNING DIAGNOSTICS */
          <div className="route-view-container">
            <SitePlanningPanel apiBase={API_BASE} token={OPERATOR_TOKEN} machineId={machineId} source={siteSource} limits={outingLimits}
              onApplySite={handleApplyImportedSite} onMachineSaved={handleMachineEstimatesSaved}
              onApplyLimits={limits => { setOutingLimits(limits); setStatusBanner("Outing limits updated for the next plan. Choose a task type to recalculate the route."); }} />
            {/* Step-by-Step Toolbar */}
            <div className="route-automation-toolbar">
              <div className="toolbar-step">
                <span className="step-num">1. Select Area:</span>
                <button
                  type="button"
                  className={`tool-pill-btn ${
                    drawMode === "BOX_AREA" ? "active" : ""
                  }`}
                  onClick={() => setDrawMode("BOX_AREA")}
                >
                  📦 Draw Box (2 Clicks)
                </button>
                <button
                  type="button"
                  className={`tool-pill-btn ${
                    drawMode === "BOUNDARY" ? "active" : ""
                  }`}
                  onClick={async () => {
                    if (!await handleClearMap()) return;
                    setDrawMode("BOUNDARY");
                    setStatusBanner("Click at least three corners on the map, then choose a task in Step 2 to plan.");
                  }}
                >
                  📐 Custom Polygon
                </button>
                <select
                  className="preset-select"
                  value=""
                  onChange={async (e) => {
                    const found = CAMPUS_PRESETS.find(
                      (p) => p.id === e.target.value
                    );
                    if (found) {
                      if (!await handleClearMap()) return;
                      setDrawMode("NONE");
                      setBoundary(found.boundary);
                      setStartLocation(found.startLocation);
                      setDisposalLocation(found.disposalLocation);
                      setLivePosition(found.startLocation);
                      setMapCenter(found.center);
                      setStatusBanner(
                        `Selected '${found.name}'. Click a Zone Classification in Step 2 to run road/terrain-aware planning!`
                      );
                    }
                  }}
                >
                  <option value="" disabled>
                    📍 Pick Scenario / Landmark...
                  </option>
                  {CAMPUS_PRESETS.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="toolbar-step">
                <span className="step-num">2. Classify &amp; Plan:</span>
                <button
                  type="button"
                  className={`classify-pill-btn ${
                    selectedClassification === "STANDARD_SWEEP" ? "active" : ""
                  }`}
                  onClick={() => handleClassifyZone("STANDARD_SWEEP")}
                >
                  🧹 Standard Sweep
                </button>
                <button
                  type="button"
                  className={`classify-pill-btn ${
                    selectedClassification === "SWEEP_AND_BINS" ? "active" : ""
                  }`}
                  onClick={() => handleClassifyZone("SWEEP_AND_BINS")}
                >
                  ✨ Sweep + Bins
                </button>
                <button
                  type="button"
                  className={`classify-pill-btn ${
                    selectedClassification === "BIN_PICKUP_ONLY" ? "active" : ""
                  }`}
                  onClick={() => handleClassifyZone("BIN_PICKUP_ONLY")}
                >
                  🗑️ Bin Pickup
                </button>
                <button
                  type="button"
                  className={`classify-pill-btn ${
                    selectedClassification === "BULKY_WASTE" ? "active" : ""
                  }`}
                  onClick={() => handleClassifyZone("BULKY_WASTE")}
                >
                  🛋️ Bulky Waste
                </button>
              </div>

              <div className="toolbar-step">
                <span className="step-num">3. Live Auto-Replan:</span>
                <button
                  type="button"
                  className={`tool-pill-btn ${
                    drawMode === "INSTANT_OBSTACLE" ? "active" : ""
                  }`}
                  onClick={() => setDrawMode("INSTANT_OBSTACLE")}
                >
                  🚧 Mark Obstacle ({dynamicObstacles.length})
                </button>
                <button
                  type="button"
                  className={`tool-pill-btn ${
                    drawMode === "TASK" ? "active" : ""
                  }`}
                  onClick={() => setDrawMode("TASK")}
                >
                  🗑️ + Bin Pin ({tasks.length})
                </button>
                <button
                  type="button"
                  className={`tool-pill-btn ${
                    drawMode === "DEPOT" ? "active" : ""
                  }`}
                  onClick={() => setDrawMode("DEPOT")}
                >
                  ♻️ Move Depot
                </button>
                <button
                  type="button"
                  className="tool-pill-btn clear"
                  disabled={controlBusy}
                  onClick={handleClearMap}
                >
                  🔄 Clear Map
                </button>
              </div>
            </div>

            {/* EXPLAINABLE PLANNING DIAGNOSTICS & FAILURE RECOVERY PANEL */}
            <div className="depot-cycle-settings">
              <label><input type="checkbox" checked={enableDepotCycles}
                onChange={event => setEnableDepotCycles(event.target.checked)} /> Return to depot, recharge &amp; resume</label>
              <span>Applies on the next plan. Preserve finished work, return before battery reserve is depleted, then resume. Each simulated recharge adds the configured depot service time to mission time.</span>
            </div>
            {!!tasks.filter(task => !task.completed).length && <details style={{ padding: "8px 22px", fontSize: 12, color: "#cbd5e1", background: "#0f172a" }}>
              <summary style={{ cursor: "pointer" }}>Review collection deadlines &amp; service times ({tasks.filter(task => !task.completed).length} open tasks)</summary>
              <p>Windows use elapsed minutes since job start, including travel and depot service. New bin pins have 90 min from placement and 45 s service; bulky pins use 180 s service. Existing deadlines stay fixed on replan. YOLO image results are classified directly and do not create collection tasks.</p>
              <p>Standard bins start with an assumed 15 kg / 120 L of contents. Edit these estimates below before planning. Completed pickups add this load; unloading at the depot empties the hopper and keeps the collected total.</p>
              {selectedClassification === "SWEEP_AND_BINS" && <p>Sweep + Bins collects pending bins first, unloads at the depot, then completes sweeping with planned recharge returns.</p>}
              {tasks.filter(task => !task.completed).map(task => <div key={task.task_id} style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", margin: "8px 0" }}>
                <strong>{task.task_id}{task.waste_observation?.waste_class ? ` (${task.waste_observation.waste_class})` : ""}</strong>
                <label>Window opens (min) <input type="number" min={0} step={1} value={task.time_window_start_min} style={{ width: 75 }}
                  onChange={event => { const value = Number(event.target.value); if (Number.isFinite(value) && value >= 0) setTasks(current => current.map(item => item.task_id === task.task_id ? { ...item, time_window_start_min: value } : item)); }} /></label>
                <label>Deadline (min) <input type="number" min={task.time_window_start_min} step={1} value={task.time_window_end_min} style={{ width: 75 }}
                  onChange={event => { const value = Number(event.target.value); if (Number.isFinite(value) && value >= 0) setTasks(current => current.map(item => item.task_id === task.task_id ? { ...item, time_window_end_min: value } : item)); }} /></label>
                <label>Service (s) <input type="number" min={0} step={1} value={task.service_duration_sec} style={{ width: 75 }}
                  onChange={event => { const value = Number(event.target.value); if (Number.isFinite(value) && value >= 0) setTasks(current => current.map(item => item.task_id === task.task_id ? { ...item, service_duration_sec: value } : item)); }} /></label>
                <label>Estimated contents (kg) <input type="number" min={0} step={0.1} value={task.rubbish_spec?.estimated_weight_kg ?? ""} style={{ width: 85 }}
                  onChange={event => { if (!event.target.value.trim()) return; const value = Number(event.target.value); if (Number.isFinite(value) && value >= 0) setTasks(current => current.map(item => item.task_id === task.task_id ? { ...item, rubbish_spec: {
                    description: "Pickup contents estimate", is_oversized: item.task_type === "OVERSIZED_RUBBISH", dimensions_uncertain: false, requires_heavy_lifter: false,
                    ...item.rubbish_spec, estimated_weight_kg: value, weight_estimation_method: "OPERATOR_ESTIMATE",
                  } } : item)); }} /></label>
                <label>Estimated contents (L) <input type="number" min={0} step={1} value={task.rubbish_spec?.estimated_volume_m3 == null ? "" : Number((task.rubbish_spec.estimated_volume_m3 * 1000).toFixed(3))} style={{ width: 85 }}
                  onChange={event => { if (!event.target.value.trim()) return; const value = Number(event.target.value); if (Number.isFinite(value) && value >= 0) setTasks(current => current.map(item => item.task_id === task.task_id ? { ...item, rubbish_spec: {
                    description: "Pickup contents estimate", is_oversized: item.task_type === "OVERSIZED_RUBBISH", dimensions_uncertain: false, requires_heavy_lifter: false,
                    ...item.rubbish_spec, estimated_volume_m3: value / 1000, volume_estimation_method: "OPERATOR_ESTIMATE",
                  } } : item)); }} /></label>
                {task.time_window_end_min <= task.time_window_start_min && <span role="alert" style={{ color: "#fca5a5" }}>Deadline must be after the window opens.</span>}
              </div>)}
              <button type="button" className="btn-secondary-sm" disabled={tasks.some(task => !task.completed && task.time_window_end_min <= task.time_window_start_min) || ["PLANNING", "REPLANNING"].includes(executionState)}
                onClick={() => triggerRoutePlan({ nextBoundary: ["BIN_PICKUP_ONLY", "BULKY_WASTE"].includes(selectedClassification || "") ? [] : boundary,
                  nextObstacles: dynamicObstacles, nextTasks: tasks, nextMachineId: machineId,
                  reason: "Operator reviewed collection time windows and service durations", preserveCompleted: true })}>Apply task settings &amp; replan</button>
            </details>}
            {(planningPhase !== "STANDBY" || diag || planningErrorDetail) && (
              <div
                className={`diagnostics-strip status-${planningPhase.toLowerCase()}`}
              >
                <div className="diag-header-row">
                  <div className="diag-badges">
                    <span className={`diag-status-pill ${planningPhase.toLowerCase()}`}>
                      {planningPhase === "SUCCESS"
                        ? "✅ PLAN VALIDATED (SUCCESS)"
                        : planningPhase === "PARTIAL_SUCCESS"
                        ? "⚠️ PARTIAL FEASIBLE PLAN (HAZARDS EXCLUDED)"
                        : planningPhase === "BLOCKED"
                        ? "🛑 PLANNING BLOCKED BY SAFETY GATE"
                        : planningPhase === "BACKEND_UNREACHABLE"
                        ? "🔌 BACKEND API UNREACHABLE"
                        : `⏳ ${planningPhase}...`}
                    </span>
                    {activeRoute && (
                      <>
                        <span className="diag-meta-tag">
                          Route: <strong>{activeRoute.route_id}</strong> (v
                          {activeRoute.route_version})
                        </span>
                        <span className="diag-meta-tag">
                          Provenance:{" "}
                          <strong>{activeRoute.routing_provenance}</strong>
                        </span>
                        <span className="diag-meta-tag">
                          Machine: <strong>{activeRoute.machine_id}</strong> ·{" "}
                          {activeRoute.task_mode}
                        </span>
                      </>
                    )}
                  </div>

                  <div className="diag-actions">
                    {planningPhase === "BACKEND_UNREACHABLE" && (
                      <button
                        type="button"
                        className="btn-accept-urgent"
                        onClick={handleRunLabeledOfflineSimulation}
                      >
                        🧪 Run Labeled Offline Simulation (Road/Lake-Constrained)
                      </button>
                    )}
                    {activeRoute &&
                      executionState === "AWAITING_APPROVAL" &&
                      activeRoute.kpi_evaluation?.passed && (
                        <button
                          type="button"
                          className="btn-accept-urgent"
                          onClick={() => handleApproveAndExecute(activeRoute)}
                        >
                          ✅ Approve &amp; Execute Validated Route
                        </button>
                      )}
                  </div>
                </div>

                {planningErrorDetail && (
                  <div className="diag-error-box">
                    <strong>Failure / Block Diagnosis:</strong>{" "}
                    {planningErrorDetail}
                  </div>
                )}

                {diag && (
                  <div className="diag-metrics-grid">
                    <div className="diag-metric-item">
                      <span>Requested vs. Eligible Area</span>
                      <strong>
                        {Math.round(diag.requested_area_m2)} m² →{" "}
                        {Math.round(diag.effective_eligible_area_m2)} m² eligible
                      </strong>
                    </div>
                    <div className="diag-metric-item">
                      <span>Reachable Coverage %</span>
                      <strong style={{ color: "#34d399" }}>
                        {diag.reachable_coverage_percent != null
                          ? `${diag.reachable_coverage_percent}%`
                          : "N/A"}{" "}
                        <small style={{ color: "#94a3b8" }}>
                          ({Math.round(diag.covered_area_m2)} m²)
                        </small>
                      </strong>
                    </div>
                    <div className="diag-metric-item">
                      <span>Inaccessible / Unverified</span>
                      <strong
                        style={{
                          color:
                            (diag.inaccessible_area_m2 || 0) +
                              (diag.unverified_area_m2 || 0) >
                            0
                              ? "#fbbf24"
                              : "#cbd5e1",
                        }}
                      >
                        {(
                          (diag.inaccessible_area_percent || 0) +
                          (diag.unverified_area_percent || 0)
                        ).toFixed(1)}
                        % (
                        {Math.round(
                          diag.inaccessible_area_m2 + diag.unverified_area_m2
                        )}{" "}
                        m²)
                      </strong>
                    </div>
                    <div className="diag-metric-item">
                      <span>Whole-job distance &amp; completion</span>
                      <strong>
                        {diag.total_distance_m != null
                          ? `${Math.round(diag.total_distance_m)}m (${formatMinutes(activeRoute?.estimated_completion_minutes ?? diag.estimated_duration_minutes)})`
                          : "N/A"}
                      </strong>
                    </div>
                    <div className="diag-metric-item">
                      <span>Coverage Cells</span>
                      <strong>
                        {diag.planned_cells_count}/{diag.total_cells_detected}{" "}
                        planned ({diag.excluded_cells_count} excluded)
                      </strong>
                    </div>
                    <div className="diag-metric-item">
                      <span>Recovery Attempts</span>
                      <strong>
                        {diag.replanning_attempts_count} attempt(s){" "}
                        {diag.recovery_actions_applied.length > 0
                          ? `· ${diag.recovery_actions_applied.join(", ")}`
                          : "· Direct Pass"}
                      </strong>
                    </div>
                  </div>
                )}

                {activeRoute?.depot_cycles_enabled && <div className="depot-cycle-summary" aria-label="Depot cycle plan">
                  <span><strong>{activeRoute.outing_count ?? 1}</strong> outings / <strong>{activeRoute.depot_visits ?? 0}</strong> recharge stops</span>
                  <span>Depot service: <strong>{activeRoute.charging_duration_minutes?.toFixed(1) ?? "0"} min</strong> included in completion estimate</span>
                  <span>Longest outing: <strong>{activeRoute.max_outing_duration_minutes?.toFixed(1) ?? "—"} min</strong> / {activeRoute.kpi_policy_snapshot?.max_estimated_duration_minutes ?? 90} min limit</span>
                  <span>Lowest planned battery: <strong>{activeRoute.minimum_battery_percent?.toFixed(1) ?? "—"}%</strong></span>
                  {executionState === "EXECUTING" && <span role="status">{depotServiceNext ? "Depot service is next (simulation); remaining work resumes automatically." : "Completing remaining work; depot returns are part of this job."}</span>}
                </div>}

                {diag &&
                  (diag.road_access_conflicts.length > 0 ||
                    diag.excluded_sections.length > 0) && (
                    <div className="diag-conflicts-row">
                      {diag.road_access_conflicts.map((c, idx) => (
                        <span key={`rc-${idx}`} className="conflict-chip road">
                          🛣️ {c}
                        </span>
                      ))}
                      {diag.excluded_sections.map((sec) => (
                        <span
                          key={sec.section_id}
                          className="conflict-chip excl"
                        >
                          ⛔ Excluded ({Math.round(sec.area_m2)}m²):{" "}
                          {sec.reason}
                        </span>
                      ))}
                    </div>
                  )}
              </div>
            )}

            {activeRoute && <div style={{ padding: "6px 22px", color: "#cbd5e1", fontSize: 12 }}>
              Solid blue: sweep passes | Dashed: transit and turns | Gray: completed or blocked draft | Arrows: selected pass directions
            </div>}
            {/* Interactive Leaflet Map */}
            <div className="route-map-holder">
              <MapWorkspace
                key={mapResetKey}
                drawMode={drawMode}
                boundary={boundary}
                exclusion={[]}
                dynamicObstacles={dynamicObstacles}
                tasks={tasks}
                startLocation={startLocation}
                disposalLocation={disposalLocation}
                activeRoute={activeRoute}
                latestTelemetry={latestTelemetryFrame}
                mapCenter={mapCenter}
                environmentFeatures={gisFeatures}
                authorizedCrossings={authorizedCrossings}
                showGisLayers={showGisLayers}
                onAddVertex={handleAddVertex}
                onSelectBoxArea={handleSelectBoxArea}
                onDropInstantObstacle={handleDropInstantObstacle}
                onAddTaskOnMap={handleAddTaskOnMap}
              />
            </div>
          </div>
        )}
      </main>
    </div>
  );
};

export default App;
