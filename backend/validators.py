import math
import re
from typing import Dict, List, Optional, Sequence, Tuple
from pydantic import Field
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from .config import CompatBaseModel as BaseModel, settings
from .models import (
    AuthorizedCrossing,
    Coordinate,
    EnvironmentMapData,
    ExclusionZone,
    GeoPolygon,
    MachineProfile,
    PlanRequest,
    RouteCandidate,
    RouteSegment,
    RubbishItemSpec,
    TaskItem,
    TaskType,
    TerrainFeature,
    TerrainFeatureType,
)
from .water_map import merge_mapped_water


class LocationValidationError(Exception):
    pass


class ParsedCommandIntent(BaseModel):
    inferred_task_type: TaskType = TaskType.AREA_SWEEP
    avoid_restricted_zones: bool = False
    optimize_for_speed: bool = False
    detected_oversized_item: Optional[RubbishItemSpec] = None
    emergency_override_attempted: bool = False
    notes: List[str] = Field(default_factory=list)


class RequestInterpreter:
    OVERSIZED_KEYWORDS = {
        "sofa": (85.0, 1.45, 2.1, True),
        "couch": (85.0, 1.45, 2.1, True),
        "mattress": (45.0, 0.95, 2.0, True),
        "refrigerator": (110.0, 1.2, 1.8, True),
        "furniture": (None, None, None, True),
        "oversized": (None, None, None, True),
    }

    def parse(self, text: Optional[str]) -> ParsedCommandIntent:
        if not text or not text.strip():
            return ParsedCommandIntent()
        lowered = text.lower()
        intent = ParsedCommandIntent()

        if any(
            p in lowered
            for p in [
                "ignore emergency stop",
                "override emergency",
                "bypass safety",
                "ignore payload",
            ]
        ):
            intent.emergency_override_attempted = True

        for kw, (weight, vol, max_dim, needs_lifter) in self.OVERSIZED_KEYWORDS.items():
            if kw in lowered:
                intent.inferred_task_type = TaskType.OVERSIZED_RUBBISH
                w_m = re.search(r"(\d+(?:\.\d+)?)\s*kg", lowered)
                v_m = re.search(r"(\d+(?:\.\d+)?)\s*m3", lowered)
                d_m = re.search(r"(\d+(?:\.\d+)?)\s*m\b", lowered)
                rw = float(w_m.group(1)) if w_m else weight
                rv = float(v_m.group(1)) if v_m else vol
                rd = float(d_m.group(1)) if d_m else max_dim
                intent.detected_oversized_item = RubbishItemSpec(
                    description=f"Reported {kw} ({text.strip()})",
                    is_oversized=True,
                    estimated_weight_kg=rw,
                    estimated_volume_m3=rv,
                    max_dimension_m=rd,
                    dimensions_uncertain=(rw is None or rv is None),
                    requires_heavy_lifter=needs_lifter or (rw or 0.0) > 50.0,
                )
                break

        if intent.inferred_task_type != TaskType.OVERSIZED_RUBBISH:
            has_sweep = any(
                w in lowered
                for w in ["sweep", "clean", "car park", "campus", "area", "zone", "patrol"]
            )
            has_collect = any(
                w in lowered for w in ["collect", "rubbish", "bin", "pickup"]
            )
            if has_sweep and has_collect:
                intent.inferred_task_type = TaskType.HYBRID
            elif has_collect:
                intent.inferred_task_type = TaskType.RUBBISH_COLLECTION
            elif "patrol" in lowered:
                intent.inferred_task_type = TaskType.PATROL

        return intent


class TaskAndLocationValidator:
    def validate_plan_request(
        self,
        req: PlanRequest,
        intent: ParsedCommandIntent,
        environment_map: Optional[EnvironmentMapData] = None,
    ) -> TaskType:
        seen: Dict[str, TaskItem] = {}
        unique_tasks: List[TaskItem] = []
        req.deduplicated_task_ids = []
        for task in req.tasks:
            key = (
                task.idempotency_key
                or (
                    task.waste_observation.detection_id
                    if task.waste_observation
                    else None
                )
                or f"task:{task.task_id}"
            )
            previous = seen.get(key)
            if previous is None:
                seen[key] = task
                unique_tasks.append(task)
                continue
            same_report = previous.model_dump(
                exclude={"task_id", "idempotency_key"}
            ) == task.model_dump(exclude={"task_id", "idempotency_key"})
            if not same_report:
                raise LocationValidationError(
                    f"Duplicate task idempotency key '{key}' has conflicting task data."
                )
            req.deduplicated_task_ids.append(task.task_id)
        req.tasks = unique_tasks
        has_boundary = req.operating_boundary is not None
        has_tasks = len(req.tasks) > 0
        if has_boundary and has_tasks:
            mode = TaskType.HYBRID
        elif has_tasks and not has_boundary:
            mode = TaskType.RUBBISH_COLLECTION
        elif has_boundary:
            mode = (
                TaskType.PATROL
                if intent.inferred_task_type == TaskType.PATROL
                else TaskType.AREA_SWEEP
            )
        else:
            mode = intent.inferred_task_type

        if (
            mode in (TaskType.AREA_SWEEP, TaskType.PATROL, TaskType.HYBRID)
            and not req.operating_boundary
        ):
            raise LocationValidationError(
                f"Place '{req.place_name or 'unspecified'}' has no operating polygon boundary. Draw or supply one."
            )
        if mode in (TaskType.RUBBISH_COLLECTION, TaskType.OVERSIZED_RUBBISH) and not req.tasks:
            raise LocationValidationError(
                "Collection routing requires at least one collection task."
            )

        if not req.start_location:
            req.start_location = (
                req.operating_boundary.vertices[0]
                if req.operating_boundary
                else req.tasks[0].location
            )
        if not req.disposal_location:
            req.disposal_location = req.start_location
        if req.start_location is None or req.disposal_location is None:
            raise LocationValidationError("A start and disposal location are required.")

        origin = (
            req.operating_boundary.vertices[0]
            if req.operating_boundary
            else req.start_location
        )
        boundary = (
            req.operating_boundary.to_shapely_local(origin)
            if req.operating_boundary
            else None
        )
        exclusions = [
            zone.polygon.to_shapely_local(origin) for zone in req.exclusion_zones
        ]
        exclusion_union = unary_union(exclusions) if exclusions else Polygon()
        environment_map = environment_map or req.environment_map
        hazard_coverage = (
            environment_map.hazard_coverage_boundary.to_shapely_local(origin)
            if environment_map and environment_map.hazard_coverage_boundary else None
        )
        water_zones = [
            (feature, feature.polygon.to_shapely_local(origin))
            for feature in (environment_map.features if environment_map else [])
            if feature.feature_type == TerrainFeatureType.WATER_BODY
        ]

        def check_mapped_location(label: str, point: Point) -> None:
            if hazard_coverage is not None and not hazard_coverage.buffer(0.02).covers(point):
                raise LocationValidationError(
                    f"{label} is outside available hazard map coverage. "
                    "Import a site map with an explicit traversable footprint before planning here."
                )
            for feature, water in water_zones:
                if water.covers(point):
                    raise LocationValidationError(
                        f"{label} is inside water '{feature.name}'. Place it on accessible dry land."
                    )
                if water.buffer(feature.clearance_buffer_m).covers(point):
                    raise LocationValidationError(
                        f"{label} is within the shoreline safety margin of '{feature.name}'. "
                        "Move it farther from the water onto accessible dry land."
                    )

        if boundary is not None:
            clipped_zones: List[ExclusionZone] = []
            for zone, zone_poly in zip(req.exclusion_zones, exclusions):
                if boundary.buffer(0.05).covers(zone_poly):
                    clipped_zones.append(zone)
                elif boundary.intersects(zone_poly):
                    inter = boundary.intersection(zone_poly)
                    clipped_geo = (
                        GeoPolygon.from_shapely_local(inter, origin)
                        if isinstance(inter, Polygon)
                        else None
                    )
                    if clipped_geo is not None:
                        clipped_zones.append(
                            zone.model_copy(update={"polygon": clipped_geo})
                        )
            req.exclusion_zones = clipped_zones

        for task in req.tasks:
            if task.completed:
                continue
            if task.waste_observation:
                world_location = task.waste_observation.world_location
                if world_location is None:
                    raise LocationValidationError(
                        f"Waste detection '{task.waste_observation.detection_id}' has no validated world/map location."
                    )
                if world_location != task.location:
                    raise LocationValidationError(
                        f"Waste detection '{task.waste_observation.detection_id}' location does not match its task location."
                    )
            point = Point(task.location.to_local_meters(origin))
            check_mapped_location(f"Task '{task.task_id}'", point)
            if boundary is not None and not boundary.buffer(0.02).covers(point):
                raise LocationValidationError(
                    f"Task '{task.task_id}' is outside the operating boundary."
                )
            if not exclusion_union.is_empty and exclusion_union.covers(point):
                raise LocationValidationError(
                    f"Task '{task.task_id}' is inside a restricted area."
                )
        for label, location in (
            ("Start", req.start_location),
            ("Disposal depot", req.disposal_location),
        ):
            point = Point(location.to_local_meters(origin))
            check_mapped_location(f"{label} point", point)
            if not exclusion_union.is_empty and exclusion_union.covers(point):
                raise LocationValidationError(
                    f"{label} point is inside a restricted area."
                )
        return mode

    def validate_route_geometry(
        self,
        candidate: RouteCandidate,
        machine: MachineProfile,
        segments_to_check: Optional[Sequence[RouteSegment]] = None,
        environment_map: Optional[EnvironmentMapData] = None,
    ) -> List[str]:
        """Independently check every emitted route segment against boundary, exclusions,
        water bodies, buildings, prohibited roads, authorized crossings, and machine access rules.
        """
        origin = (
            candidate.operating_boundary.vertices[0]
            if candidate.operating_boundary
            else candidate.origin
        )
        boundary = (
            candidate.operating_boundary.to_shapely_local(origin)
            if candidate.operating_boundary
            else None
        )
        excluded = [
            zone.polygon.to_shapely_local(origin) for zone in candidate.exclusion_zones
        ]
        body_clearance = machine.clearance_width_m / 2.0
        tool_radius = machine.working_width_m / 2.0
        issues: List[str] = []
        environment_map = environment_map or getattr(candidate, "planning_environment_map", None)
        if settings.enable_default_campus_terrain_layer:
            # Approval and execution may validate old stored candidates. Their
            # saved map must not suppress the current mandatory local water map.
            environment_map = merge_mapped_water(
                environment_map or EnvironmentMapData(
                    map_id="candidate-feature-snapshot",
                    features=list(candidate.environment_features_applied or []),
                )
            )
        hazard_coverage = (
            environment_map.hazard_coverage_boundary.to_shapely_local(origin)
            if environment_map and environment_map.hazard_coverage_boundary else None
        )
        hazard_coverage_tolerance = hazard_coverage.buffer(0.03) if hazard_coverage is not None else None
        declared_scope = None
        if environment_map is not None and environment_map.require_verified_terrain:
            declared_scope = (unary_union([polygon.to_shapely_local(origin)
                for polygon in environment_map.verified_traversable_polygons])
                if environment_map.verified_traversable_polygons else Polygon())

        # Gather environmental features from candidate or passed environment_map
        features: List[TerrainFeature] = list(candidate.environment_features_applied or [])
        crossings: List[AuthorizedCrossing] = []
        if environment_map is not None:
            features = list(environment_map.features)
            crossings = list(environment_map.authorized_crossings)
        # One candidate can contain thousands of swaths. Terrain projection and
        # shoreline buffering are invariant across all of them.
        terrain_geometries = []
        for feature in features:
            terrain = feature.polygon.to_shapely_local(origin)
            shoreline = (
                terrain.buffer(feature.clearance_buffer_m).buffer(-0.02)
                if feature.feature_type == TerrainFeatureType.WATER_BODY else None
            )
            terrain_geometries.append((feature, terrain, terrain.buffer(-0.05), shoreline))

        permitted_crossing_corridors: List[Polygon] = []
        for xing in crossings:
            if not machine.allow_authorized_road_crossing:
                continue
            if xing.allowed_machine_ids and machine.machine_id not in xing.allowed_machine_ids:
                continue
            if machine.requires_heavy_rated_crossing and not xing.allows_heavy_collector:
                continue
            if machine.clearance_width_m > xing.width_m:
                continue
            if xing.corridor_polygon is not None:
                permitted_crossing_corridors.append(
                    xing.corridor_polygon.to_shapely_local(origin)
                )
            else:
                s_xy = xing.start_point.to_local_meters(origin)
                e_xy = xing.end_point.to_local_meters(origin)
                permitted_crossing_corridors.append(
                    LineString([s_xy, e_xy]).buffer(max(xing.width_m / 2.0, 1.0))
                )

        for segment in (
            segments_to_check if segments_to_check is not None else candidate.segments
        ):
            if segment.completed:
                continue
            if not segment.waypoints:
                issues.append(f"segment {segment.segment_id}: no geometry to validate")
                continue
            points = [
                Point(point.to_local_meters(origin)) for point in segment.waypoints
            ]
            geometry = (
                points[0]
                if len(points) == 1
                else LineString([(p.x, p.y) for p in points])
            )
            footprint_radius = (
                max(body_clearance, tool_radius)
                if segment.segment_type == "SWEEP_SWATH" else body_clearance
            )
            footprint = geometry.buffer(footprint_radius)
            if hazard_coverage_tolerance is not None and not hazard_coverage_tolerance.covers(
                footprint
            ):
                issues.append(
                    f"segment {segment.segment_id}: clearance envelope leaves available hazard map coverage; "
                    "import a site map with an explicit traversable footprint"
                )
            if declared_scope is not None and not declared_scope.buffer(0.03).covers(geometry.buffer(body_clearance)):
                issues.append(f"segment {segment.segment_id}: clearance envelope leaves operator-declared traversable footprint")
            endpoints_inside = (
                [
                    boundary.buffer(0.03).covers(points[0]),
                    boundary.buffer(0.03).covers(points[-1]),
                ]
                if boundary is not None
                else [False, False]
            )
            must_stay_inside = segment.segment_type in {
                "SWEEP_SWATH",
                "TURN_CONNECTOR",
                "COLLECTION_STOP",
                "WAIT_FOR_WINDOW",
            } or (
                segment.segment_type in {"TRANSIT", "RETURN_TO_BASE"}
                and boundary is not None
                and all(endpoints_inside)
                and segment.target_task_id is not None
                and segment.crossing_id is None
            )
            if boundary is not None and must_stay_inside:
                envelope = geometry.buffer(body_clearance)
                allowed_boundary = boundary.buffer(0.03)
                if segment.segment_type in {"TRANSIT", "RETURN_TO_BASE"}:
                    # Start/depot points may lie on the drawn boundary. Permit
                    # their short ingress/egress clearance, as for sweep access,
                    # without exempting the rest of a collection transit leg.
                    for portal in (candidate.origin, candidate.disposal_depot):
                        portal_point = Point(portal.to_local_meters(origin))
                        if any(endpoint.distance(portal_point) <= 0.03 for endpoint in (points[0], points[-1])):
                            allowed_boundary = allowed_boundary.union(portal_point.buffer(2.0 * body_clearance + 0.03))
                if not allowed_boundary.covers(envelope):
                    issues.append(
                        f"segment {segment.segment_id}: clearance envelope leaves operating boundary"
                    )
            if excluded:
                exclusion_clearance = (
                    tool_radius
                    if segment.segment_type == "SWEEP_SWATH"
                    else body_clearance
                )
                envelope = geometry.buffer(exclusion_clearance)
                for zone, zone_poly in zip(candidate.exclusion_zones, excluded):
                    if envelope.intersects(zone_poly.buffer(-0.02)):
                        issues.append(
                            f"segment {segment.segment_id}: clearance envelope intersects restricted zone '{zone.name}'"
                        )
                        break

            # Independent terrain & road safety validation
            for feat, feat_poly, feature_interior, shoreline in terrain_geometries:
                if feat.feature_type == TerrainFeatureType.WATER_BODY:
                    # Independently validate the machine's entire footprint,
                    # including stationary collection and depot-service stops.
                    # A dry centreline alone does not establish shore safety.
                    # WGS84 waypoints are rounded to seven decimal places;
                    # allow only the existing 2 cm numerical tolerance.
                    if footprint.intersects(shoreline):
                        issues.append(
                            f"segment {segment.segment_id}: clearance envelope intersects impassable "
                            f"WATER_BODY '{feat.name}' or its shoreline safety margin"
                        )
                elif feat.feature_type in (
                    TerrainFeatureType.BUILDING,
                    TerrainFeatureType.RESTRICTED_ZONE,
                    TerrainFeatureType.UNVERIFIED_TERRAIN,
                ):
                    if geometry.buffer(0.1).intersects(feature_interior):
                        issues.append(
                            f"segment {segment.segment_id}: intersects impassable {feat.feature_type.value} '{feat.name}'"
                        )
                elif feat.feature_type == TerrainFeatureType.PUBLIC_ROAD:
                    if segment.segment_type == "SWEEP_SWATH" and not machine.allow_public_road_sweeping:
                        if geometry.buffer(tool_radius).intersects(feature_interior):
                            issues.append(
                                f"segment {segment.segment_id}: sweep swath enters prohibited road '{feat.name}'"
                            )
                    elif not machine.allow_public_road_transit:
                        if geometry.intersects(feature_interior):
                            road_overlap = geometry.intersection(feat_poly)
                            covered_by_crossing = any(
                                corr.buffer(0.5).covers(road_overlap)
                                for corr in permitted_crossing_corridors
                            )
                            if not covered_by_crossing:
                                issues.append(
                                    f"segment {segment.segment_id}: unauthorized crossing of public road '{feat.name}'"
                                )
                else:
                    surface_ok = feat.feature_type.value in machine.allowed_surface_classes
                    machine_ok = (
                        feat.allowed_machine_ids is None
                        or machine.machine_id in feat.allowed_machine_ids
                    )
                    width_ok = machine.clearance_width_m <= feat.min_clearance_width_m
                    if not (surface_ok and machine_ok and width_ok):
                        if geometry.intersects(feature_interior):
                            issues.append(
                                f"segment {segment.segment_id}: machine '{machine.machine_id}' prohibited on '{feat.name}' ({feat.feature_type.value})"
                            )

        return issues


class CapabilityCheckResult(BaseModel):
    feasible: bool
    requires_human_assistance: bool = False
    requires_dimension_clarification: bool = False
    rejection_reasons: List[str] = Field(default_factory=list)
    recommended_actions: List[str] = Field(default_factory=list)
    additional_service_time_sec: float = 0.0


class MachineCapabilityValidator:
    def validate_single_task(
        self, machine: MachineProfile, task: TaskItem
    ) -> CapabilityCheckResult:
        res = CapabilityCheckResult(feasible=True)
        spec = task.rubbish_spec
        if task.task_type == TaskType.OVERSIZED_RUBBISH and spec is None:
            res.feasible = False
            res.requires_human_assistance = True
            res.requires_dimension_clarification = True
            res.rejection_reasons.append("oversized_item_details_required")
            res.recommended_actions.append(
                "Provide estimated weight, volume, dimensions, and handling requirements."
            )
            return res
        if not spec:
            return res

        if spec.is_oversized and (
            spec.dimensions_uncertain
            or spec.estimated_weight_kg is None
            or spec.estimated_volume_m3 is None
            or spec.max_dimension_m is None
        ):
            res.feasible = False
            res.requires_human_assistance = True
            res.requires_dimension_clarification = True
            res.rejection_reasons.append("oversized_item_dimensions_or_weight_unknown")
            res.recommended_actions.append(
                "Provide estimated weight (kg) and volume (m^3)."
            )
            return res

        w = spec.estimated_weight_kg or 0.0
        v = spec.estimated_volume_m3 or 0.0
        d = spec.max_dimension_m or 0.0

        if spec.requires_heavy_lifter and not machine.has_heavy_lifter:
            res.feasible = False
            res.requires_human_assistance = True
            res.rejection_reasons.append("missing_required_heavy_lifter_equipment")
            res.recommended_actions.append(
                "Dispatch heavy collection vehicle with hydraulic lifter."
            )
        if w > machine.max_payload_kg:
            res.feasible = False
            res.requires_human_assistance = True
            res.rejection_reasons.append(
                f"item_weight_{w}kg_exceeds_max_payload_{machine.max_payload_kg}kg"
            )
        if v > machine.max_payload_volume_m3:
            res.feasible = False
            res.requires_human_assistance = True
            res.rejection_reasons.append(
                f"item_volume_{v}m3_exceeds_hopper_{machine.max_payload_volume_m3}m3"
            )
        if d > machine.max_item_length_m:
            res.feasible = False
            res.requires_human_assistance = True
            res.rejection_reasons.append(
                f"item_length_{d}m_exceeds_clearance_{machine.max_item_length_m}m"
            )

        if res.feasible and spec.is_oversized:
            res.additional_service_time_sec = 180.0
        return res

    def validate_task_batch(
        self, machine: MachineProfile, tasks: List[TaskItem]
    ) -> Tuple[List[TaskItem], List[Tuple[TaskItem, CapabilityCheckResult]], int]:
        feasible: List[TaskItem] = []
        rejected: List[Tuple[TaskItem, CapabilityCheckResult]] = []
        tot_w = machine.current_payload_kg
        tot_v = machine.current_payload_volume_m3

        for t in tasks:
            if t.completed:
                feasible.append(t)
                continue
            chk = self.validate_single_task(machine, t)
            if not chk.feasible:
                rejected.append((t, chk))
            else:
                t.service_duration_sec += chk.additional_service_time_sec
                feasible.append(t)
                if t.rubbish_spec:
                    tot_w += t.rubbish_spec.estimated_weight_kg or 0.0
                    tot_v += t.rubbish_spec.estimated_volume_m3 or 0.0

        trips = max(
            1,
            math.ceil(max(tot_w, 0.01) / machine.max_payload_kg),
            math.ceil(max(tot_v, 0.01) / machine.max_payload_volume_m3),
        )
        return feasible, rejected, trips
