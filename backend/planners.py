import heapq
import math
from typing import Dict, List, Optional, Set, Tuple
import httpx
from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.ops import nearest_points, unary_union
from shapely.prepared import prep

try:
    import networkx as nx
    HAS_NETWORKX = True
except ImportError:
    nx = None  # type: ignore[assignment]
    HAS_NETWORKX = False

from .config import CompatBaseModel as BaseModel, settings
from .models import (
    AuthorizedCrossing,
    Coordinate,
    CoverageCellSummary,
    DataProvenance,
    EnvironmentMapData,
    ExclusionZone,
    GeoPolygon,
    MachineProfile,
    RouteSegment,
    TaskItem,
    TaskScheduleEntry,
    TerrainFeature,
    TerrainFeatureType,
    TraversabilityEdge,
    TraversabilityNode,
    UnreachableSection,
)
from .water_map import mapped_water_features, water_map_coverage, water_snapshot_version

try:
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


def _shortest_path_fallback(
    nodes: List[Tuple[float, float]],
    edges: List[Tuple[Tuple[float, float], Tuple[float, float], float]],
    source: Tuple[float, float],
    target: Tuple[float, float],
) -> Tuple[Optional[List[Tuple[float, float]]], float]:
    adj: Dict[Tuple[float, float], List[Tuple[Tuple[float, float], float]]] = {
        n: [] for n in nodes
    }
    for u, v, w in edges:
        adj.setdefault(u, []).append((v, w))
        adj.setdefault(v, []).append((u, w))
    if source not in adj or target not in adj:
        return None, float("inf")
    dist: Dict[Tuple[float, float], float] = {source: 0.0}
    prev: Dict[Tuple[float, float], Tuple[float, float]] = {}
    pq: List[Tuple[float, Tuple[float, float]]] = [(0.0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == target:
            path = [target]
            curr = target
            while curr in prev:
                curr = prev[curr]
                path.append(curr)
            path.reverse()
            return path, d
        if d > dist.get(u, float("inf")):
            continue
        for nxt, w in adj.get(u, []):
            nd = d + w
            if nd < dist.get(nxt, float("inf")):
                dist[nxt] = nd
                prev[nxt] = u
                heapq.heappush(pq, (nd, nxt))
    return None, float("inf")


def _extract_polygons(geom) -> List[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area >= 1.0 else []
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        out: List[Polygon] = []
        for child in geom.geoms:
            out.extend(_extract_polygons(child))
        return out
    return []


class DefaultCampusEnvironmentProvider:
    """Local OSM water exclusions with illustrative non-water reference terrain."""

    @staticmethod
    def get_default_environment_map() -> EnvironmentMapData:
        features = [
            TerrainFeature(
                feature_id="road-jalan-damansara-west",
                name="Persiaran Tuanku Ja'afar / Jalan Damansara Highway",
                feature_type=TerrainFeatureType.PUBLIC_ROAD,
                clearance_buffer_m=1.2,
                polygon=GeoPolygon(
                    vertices=[
                        Coordinate(lat=3.13800, lon=101.68410),
                        Coordinate(lat=3.13800, lon=101.68482),
                        Coordinate(lat=3.14160, lon=101.68482),
                        Coordinate(lat=3.14160, lon=101.68410),
                    ]
                ),
            ),
            TerrainFeature(
                feature_id="road-jalan-damansara-south",
                name="Jalan Damansara Southern Arterial",
                feature_type=TerrainFeatureType.PUBLIC_ROAD,
                clearance_buffer_m=1.2,
                polygon=GeoPolygon(
                    vertices=[
                        Coordinate(lat=3.13760, lon=101.68410),
                        Coordinate(lat=3.13760, lon=101.68840),
                        Coordinate(lat=3.13825, lon=101.68840),
                        Coordinate(lat=3.13825, lon=101.68410),
                    ]
                ),
            ),
            TerrainFeature(
                feature_id="road-jalan-perdana-northeast",
                name="Jalan Perdana Roadway",
                feature_type=TerrainFeatureType.PUBLIC_ROAD,
                clearance_buffer_m=1.0,
                polygon=GeoPolygon(
                    vertices=[
                        Coordinate(lat=3.14018, lon=101.68775),
                        Coordinate(lat=3.14018, lon=101.68900),
                        Coordinate(lat=3.14065, lon=101.68900),
                        Coordinate(lat=3.14065, lon=101.68775),
                    ]
                ),
            ),
            *mapped_water_features(),
            TerrainFeature(
                feature_id="bldg-national-space-agency",
                name="National Space Agency & Planetarium Complex",
                feature_type=TerrainFeatureType.BUILDING,
                clearance_buffer_m=0.6,
                polygon=GeoPolygon(
                    vertices=[
                        Coordinate(lat=3.13962, lon=101.68795),
                        Coordinate(lat=3.13962, lon=101.68858),
                        Coordinate(lat=3.14002, lon=101.68858),
                        Coordinate(lat=3.14002, lon=101.68795),
                    ]
                ),
            ),
            TerrainFeature(
                feature_id="bldg-stesen-satelit",
                name="Stesen Satelit Building",
                feature_type=TerrainFeatureType.BUILDING,
                clearance_buffer_m=0.5,
                polygon=GeoPolygon(
                    vertices=[
                        Coordinate(lat=3.13958, lon=101.68755),
                        Coordinate(lat=3.13958, lon=101.68782),
                        Coordinate(lat=3.13982, lon=101.68782),
                        Coordinate(lat=3.13982, lon=101.68755),
                    ]
                ),
            ),
            TerrainFeature(
                feature_id="walkway-north-botanical-footpath",
                name="Mini Stonehenge Narrow Botanical Footpath",
                feature_type=TerrainFeatureType.PEDESTRIAN_WALKWAY,
                min_clearance_width_m=1.8,
                allowed_machine_ids=["sweeper-alpha-01"],
                polygon=GeoPolygon(
                    vertices=[
                        Coordinate(lat=3.14005, lon=101.68705),
                        Coordinate(lat=3.14005, lon=101.68750),
                        Coordinate(lat=3.14045, lon=101.68750),
                        Coordinate(lat=3.14045, lon=101.68705),
                    ]
                ),
            ),
        ]

        crossings = [
            AuthorizedCrossing(
                crossing_id="xing-damansara-ped-bridge",
                name="Jalan Damansara Signalized Pedestrian/Sweeper Crossing",
                crossing_type="PEDESTRIAN_CROSSWALK",
                start_point=Coordinate(lat=3.13910, lon=101.68485),
                end_point=Coordinate(lat=3.13910, lon=101.68405),
                corridor_polygon=GeoPolygon(
                    vertices=[
                        Coordinate(lat=3.13904, lon=101.68405),
                        Coordinate(lat=3.13904, lon=101.68485),
                        Coordinate(lat=3.13916, lon=101.68485),
                        Coordinate(lat=3.13916, lon=101.68405),
                    ]
                ),
                width_m=2.2,
                max_weight_kg=250.0,
                allowed_machine_ids=["sweeper-alpha-01"],
                allows_heavy_collector=False,
                risk_score=0.12,
            ),
            AuthorizedCrossing(
                crossing_id="xing-damansara-heavy-service",
                name="South Signalized Heavy Service Crossing",
                crossing_type="SIGNALIZED_SERVICE_CROSSING",
                start_point=Coordinate(lat=3.13830, lon=101.68640),
                end_point=Coordinate(lat=3.13755, lon=101.68640),
                corridor_polygon=GeoPolygon(
                    vertices=[
                        Coordinate(lat=3.13755, lon=101.68632),
                        Coordinate(lat=3.13755, lon=101.68648),
                        Coordinate(lat=3.13830, lon=101.68648),
                        Coordinate(lat=3.13830, lon=101.68632),
                    ]
                ),
                width_m=3.6,
                max_weight_kg=600.0,
                allowed_machine_ids=["sweeper-alpha-01", "heavy-utility-02"],
                allows_heavy_collector=True,
                risk_score=0.18,
            ),
        ]

        return EnvironmentMapData(
            map_id="kl-perdana-planetarium-gis",
            features=features,
            authorized_crossings=crossings,
            hazard_coverage_boundary=water_map_coverage(),
            water_map_version=water_snapshot_version(),
        )


class TraversabilityGraph:
    """Explicit graph representation of traversable campus space, portals, and authorized crossings."""

    def __init__(
        self,
        origin: Coordinate,
        machine: MachineProfile,
        free_navigation_space,
        impassable_hazard_union,
        prohibited_road_union,
        prohibited_surface_union,
        environment_map: Optional[EnvironmentMapData] = None,
        use_authorized_crossings: bool = True,
        requested_boundary_poly: Optional[Polygon] = None,
    ) -> None:
        self.origin = origin
        self.machine = machine
        self.free_navigation_space = free_navigation_space
        self.impassable_hazard_union = impassable_hazard_union
        self.prohibited_road_union = prohibited_road_union
        self.prohibited_surface_union = prohibited_surface_union
        self.environment_map = environment_map
        self.use_authorized_crossings = use_authorized_crossings
        self.requested_boundary_poly = requested_boundary_poly
        self.hazard_coverage_scope = (
            environment_map.hazard_coverage_boundary.to_shapely_local(origin)
            if environment_map and environment_map.hazard_coverage_boundary is not None
            else None
        )
        self.require_verified_terrain = bool(environment_map and environment_map.require_verified_terrain)
        self.verified_scope = (unary_union([p.to_shapely_local(origin)
            for p in environment_map.verified_traversable_polygons])
            if self.require_verified_terrain and environment_map.verified_traversable_polygons else Polygon())
        if self.require_verified_terrain:
            uncertain = [feature.polygon.to_shapely_local(origin)
                for feature in environment_map.features
                if feature.feature_type == TerrainFeatureType.UNVERIFIED_TERRAIN]
            if uncertain:
                self.impassable_hazard_union = unary_union([self.impassable_hazard_union] + uncertain)
            # Transit may leave the cleaning boundary only through the explicitly
            # supplied access footprint, including the route to an external depot.
            free_navigation_space = self.verified_scope.difference(self.impassable_hazard_union).difference(
                self.prohibited_road_union).difference(self.prohibited_surface_union)
            self.free_navigation_space = free_navigation_space

        # A bounded hazard layer only describes hazards inside its coverage.
        # Space outside that boundary is unknown, even when no feature is listed.
        if self.hazard_coverage_scope is not None:
            free_navigation_space = free_navigation_space.intersection(self.hazard_coverage_scope)
            self.free_navigation_space = free_navigation_space

        self.permitted_crossings: List[
            Tuple[AuthorizedCrossing, Tuple[float, float], Tuple[float, float], Polygon]
        ] = []
        self.prohibited_crossing_reasons: List[str] = []

        if environment_map and environment_map.authorized_crossings:
            for xing in environment_map.authorized_crossings:
                s_loc = xing.start_point.to_local_meters(origin)
                e_loc = xing.end_point.to_local_meters(origin)
                if xing.corridor_polygon is not None:
                    corr = xing.corridor_polygon.to_shapely_local(origin)
                else:
                    corr = LineString([s_loc, e_loc]).buffer(
                        max(xing.width_m / 2.0, 1.0)
                    )

                if (
                    not use_authorized_crossings
                    or not machine.allow_authorized_road_crossing
                ):
                    self.prohibited_crossing_reasons.append(
                        f"Authorized crossing '{xing.name}' disabled by current policy or machine profile."
                    )
                    continue
                if (
                    xing.allowed_machine_ids
                    and machine.machine_id not in xing.allowed_machine_ids
                ):
                    self.prohibited_crossing_reasons.append(
                        f"Crossing '{xing.name}' prohibits machine '{machine.machine_id}' (allowed: {', '.join(xing.allowed_machine_ids)})."
                    )
                    continue
                if (
                    machine.requires_heavy_rated_crossing
                    and not xing.allows_heavy_collector
                ):
                    self.prohibited_crossing_reasons.append(
                        f"Crossing '{xing.name}' is not rated for heavy collector '{machine.machine_id}'."
                    )
                    continue
                if machine.clearance_width_m > xing.width_m:
                    self.prohibited_crossing_reasons.append(
                        f"Crossing '{xing.name}' width ({xing.width_m}m) is narrower than machine clearance ({machine.clearance_width_m}m)."
                    )
                    continue
                if not self.impassable_hazard_union.is_empty and LineString(
                    [s_loc, e_loc]
                ).intersects(self.impassable_hazard_union.buffer(-0.02)):
                    self.prohibited_crossing_reasons.append(
                        f"Crossing '{xing.name}' is currently blocked by an obstacle or impassable hazard."
                    )
                    continue
                if self.require_verified_terrain and not self.verified_scope.buffer(0.03).covers(corr):
                    self.prohibited_crossing_reasons.append(
                        f"Crossing '{xing.name}' leaves the operator-declared traversable footprint."
                    )
                    continue
                self.permitted_crossings.append((xing, s_loc, e_loc, corr))

        crossing_corridors = [c[3] for c in self.permitted_crossings]
        if free_navigation_space.is_empty:
            self.transit_allowed_space = Polygon()
        elif crossing_corridors:
            self.transit_allowed_space = unary_union(
                [free_navigation_space] + crossing_corridors
            ).buffer(0.05)
        else:
            self.transit_allowed_space = free_navigation_space.buffer(0.05)
        if self.hazard_coverage_scope is not None:
            self.transit_allowed_space = self.transit_allowed_space.intersection(self.hazard_coverage_scope)

        # These geometries are immutable for this graph. Preparing them once
        # keeps every edge check against the complete hazard layer inexpensive.
        self._hazard_core = self.impassable_hazard_union.buffer(-0.02)
        self._road_core = self.prohibited_road_union.buffer(-0.02)
        self._surface_core = self.prohibited_surface_union.buffer(-0.02)
        self._hazard_check = prep(self._hazard_core)
        self._road_check = prep(self._road_core)
        self._surface_check = prep(self._surface_core)
        self._verified_check = prep(self.verified_scope.buffer(0.03))
        self._coverage_check = (prep(self.hazard_coverage_scope.buffer(0.02))
                                if self.hazard_coverage_scope is not None else None)
        self._transit_check = prep(self.transit_allowed_space)

    def is_segment_permitted(
        self,
        p1: Tuple[float, float],
        p2: Tuple[float, float],
        require_within_boundary: bool = True,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        line = LineString([p1, p2])
        if self._coverage_check is not None and not self._coverage_check.covers(
            line.buffer(self.machine.clearance_width_m / 2.0)
        ):
            return False, "outside_mapped_hazard_coverage", None
        if self.require_verified_terrain and not self._verified_check.covers(
            line.buffer(self.machine.clearance_width_m / 2.0)
        ):
            return False, "outside_declared_traversable_footprint", None
        if line.length < 1e-4:
            return True, None, None

        body_clearance = self.machine.clearance_width_m / 2.0
        if require_within_boundary and self.requested_boundary_poly is not None:
            allowed_boundary = self.requested_boundary_poly.buffer(0.03)
            # Permit a short entry/exit at a boundary portal, as the independent
            # validator does, while keeping the rest of the leg inside the area.
            for endpoint in (p1, p2):
                point = Point(endpoint)
                if self.requested_boundary_poly.boundary.distance(point) <= 0.03:
                    allowed_boundary = allowed_boundary.union(point.buffer(2.0 * body_clearance + 0.03))
            if not allowed_boundary.covers(line.buffer(body_clearance)):
                return False, "clearance_envelope_leaves_operating_boundary", None
        if not self.impassable_hazard_union.is_empty:
            if self._hazard_check.intersects(line.buffer(body_clearance)):
                return False, "impassable_hazard_intersection", None

        if not self.prohibited_surface_union.is_empty:
            if self._surface_check.intersects(line):
                return False, "prohibited_surface_for_machine", None

        crossing_used: Optional[str] = None
        if not self.prohibited_road_union.is_empty and self._road_check.intersects(line):
            matched_xing = None
            for xing, s_loc, e_loc, corr in self.permitted_crossings:
                road_part = line.intersection(self.prohibited_road_union)
                if corr.buffer(0.5).covers(road_part):
                    matched_xing = xing.crossing_id
                    break
            if matched_xing is None:
                return False, "prohibited_road_crossing", None
            crossing_used = matched_xing

        if require_within_boundary and not self.transit_allowed_space.is_empty:
            if not self._transit_check.covers(line):
                return False, "outside_traversable_space", None

        return True, None, crossing_used

    def validate_portal_access(
        self,
        coord: Coordinate,
        role_label: str,
        allow_snap: bool = False,
        max_snap_m: float = 18.0,
    ) -> Tuple[Optional[Coordinate], Optional[str], bool]:
        """Validate the actual start/depot footprint and its access connection.

        A small boundary adjustment must never move a machine out of water or
        bypass an obstacle between the supplied position and the adjusted portal.
        """
        pt_xy = coord.to_local_meters(self.origin)
        pt = Point(pt_xy)

        if self.hazard_coverage_scope is not None and not self.hazard_coverage_scope.buffer(0.02).covers(
            pt.buffer(self.machine.clearance_width_m / 2.0)
        ):
            return (
                None,
                f"{role_label}_access_unreachable: location ({coord.lat:.5f}, {coord.lon:.5f}) is outside mapped area; import site geometry covering the position and access route.",
                False,
            )

        in_hazard = (
            not self.impassable_hazard_union.is_empty
            and pt.buffer(self.machine.clearance_width_m / 2.0).intersects(
                self.impassable_hazard_union.buffer(0.05)
            )
        )
        in_road = (
            not self.prohibited_road_union.is_empty
            and self.prohibited_road_union.buffer(0.05).covers(pt)
        )
        in_prohibited_surf = (
            not self.prohibited_surface_union.is_empty
            and self.prohibited_surface_union.buffer(0.05).covers(pt)
        )

        # Start/depot coordinates describe an actual position. Snapping a point
        # on water to land would hide an unsafe input and omit the trip to shore.
        if in_hazard:
            return (
                None,
                f"{role_label}_access_unreachable: location ({coord.lat:.5f}, {coord.lon:.5f}) overlaps an impassable hazard or its clearance buffer. Choose an accessible position on safe ground.",
                False,
            )

        if not (in_hazard or in_road or in_prohibited_surf):
            if (
                (self.require_verified_terrain and self.verified_scope.buffer(0.03).covers(
                    pt.buffer(self.machine.clearance_width_m / 2.0)))
                or (not self.require_verified_terrain and (self.transit_allowed_space.is_empty
                    or self.transit_allowed_space.buffer(0.5).covers(pt)))
            ):
                return coord, None, False
            nearest_pt = nearest_points(self.transit_allowed_space, pt)[0]
            ok, _, _ = self.is_segment_permitted(
                pt_xy, (nearest_pt.x, nearest_pt.y), require_within_boundary=False
            )
            if ok:
                return coord, None, False

        if allow_snap and not self.transit_allowed_space.is_empty:
            safe_snap_space = self.transit_allowed_space.buffer(
                -self.machine.clearance_width_m / 2.0 - 0.1
            )
            if not self.impassable_hazard_union.is_empty:
                safe_snap_space = safe_snap_space.difference(
                    self.impassable_hazard_union.buffer(
                        self.machine.clearance_width_m / 2.0 + 0.25
                    )
                )
            if not self.prohibited_road_union.is_empty:
                safe_snap_space = safe_snap_space.difference(
                    self.prohibited_road_union.buffer(
                        self.machine.road_clearance_buffer_m + 0.25
                    )
                )
            polys = _extract_polygons(
                safe_snap_space
                if not safe_snap_space.is_empty
                else self.transit_allowed_space
            )
            best_pt: Optional[Tuple[float, float]] = None
            best_d = float("inf")
            for poly in polys:
                proj = nearest_points(poly, pt)[0]
                centroid = poly.representative_point()
                dx = centroid.x - proj.x
                dy = centroid.y - proj.y
                norm = math.hypot(dx, dy) or 1.0
                cand = (proj.x + 0.6 * dx / norm, proj.y + 0.6 * dy / norm)
                d = math.hypot(cand[0] - pt_xy[0], cand[1] - pt_xy[1])
                connection_ok, _, _ = self.is_segment_permitted(
                    pt_xy, cand, require_within_boundary=False
                )
                if d <= max_snap_m and d < best_d and connection_ok:
                    best_d = d
                    best_pt = cand
            if best_pt is not None:
                return (
                    Coordinate.from_local_meters(
                        best_pt[0], best_pt[1], self.origin
                    ),
                    None,
                    True,
                )

        return (
            None,
            f"{role_label}_access_unreachable: location ({coord.lat:.5f}, {coord.lon:.5f}) has no valid obstacle-free access connection to the traversability graph.",
            False,
        )

    def find_shortest_permitted_path(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        require_within_boundary: bool = True,
        prefer_perimeter: bool = False,
        _search_expansion: int = 0,
    ) -> Tuple[Optional[List[Tuple[float, float]]], float, List[str]]:
        """Find shortest path over permitted graph edges between two local-meter points."""
        body_radius = self.machine.clearance_width_m / 2.0

        def usable_node(node: Tuple[float, float]) -> bool:
            point = Point(node)
            footprint = point.buffer(body_radius)
            if self._coverage_check is not None and not self._coverage_check.covers(footprint):
                return False
            if self.require_verified_terrain and not self._verified_check.covers(footprint):
                return False
            if self._hazard_check.intersects(footprint) or self._surface_check.intersects(footprint):
                return False
            if require_within_boundary and not self.transit_allowed_space.is_empty and not self._transit_check.covers(point):
                return False
            if self._road_check.intersects(point) and not any(
                corridor.buffer(0.5).covers(point) for _, _, _, corridor in self.permitted_crossings
            ):
                return False
            return True

        if not usable_node(start) or not usable_node(end):
            return None, float("inf"), []

        if not prefer_perimeter:
            ok, _, xing_direct = self.is_segment_permitted(
                start, end, require_within_boundary=require_within_boundary
            )
            if ok:
                dist = math.hypot(end[0] - start[0], end[1] - start[1])
                return [start, end], dist, ([xing_direct] if xing_direct else [])

        if self.require_verified_terrain and not any(
            poly.buffer(0.03).covers(Point(start)) and poly.buffer(0.03).covers(Point(end))
            for poly in _extract_polygons(self.transit_allowed_space)
        ):
            return None, float("inf"), []

        # Start with the relevant local geometry. Grow the search if a larger
        # detour is necessary; the final attempt includes the full declared/map
        # footprint. Every candidate edge still checks the complete hazard set.
        margin = max(10.0, body_radius * 3.0, math.dist(start, end) * 0.25) * (4 ** _search_expansion)
        # Horizontal/vertical lines have a degenerate polygon envelope. Buffering
        # that envelope with mitred joins can produce a triangle and omit an
        # entire side of an otherwise reachable detour. Construct the bounds
        # explicitly so axis-aligned collection legs get the same search area.
        search_window = box(
            min(start[0], end[0]) - margin,
            min(start[1], end[1]) - margin,
            max(start[0], end[0]) + margin,
            max(start[1], end[1]) + margin,
        )
        if _search_expansion >= 3:
            if self.require_verified_terrain:
                search_window = self.verified_scope
            elif self.hazard_coverage_scope is not None:
                search_window = self.hazard_coverage_scope
        if self.require_verified_terrain:
            search_window = search_window.intersection(self.verified_scope)
        if self.hazard_coverage_scope is not None:
            search_window = search_window.intersection(self.hazard_coverage_scope)

        nodes: List[Tuple[float, float]] = [start, end]
        crossing_edges: Dict[
            Tuple[Tuple[float, float], Tuple[float, float]], str
        ] = {}

        for xing, s_loc, e_loc, _ in self.permitted_crossings:
            if not search_window.covers(Point(s_loc)) or not search_window.covers(Point(e_loc)):
                continue
            s_r = (round(s_loc[0], 3), round(s_loc[1], 3))
            e_r = (round(e_loc[0], 3), round(e_loc[1], 3))
            nodes.extend([s_r, e_r])
            crossing_edges[(s_r, e_r)] = xing.crossing_id
            crossing_edges[(e_r, s_r)] = xing.crossing_id

        if self.environment_map and self.environment_map.nav_nodes:
            for nav_node in self.environment_map.nav_nodes:
                n_xy = nav_node.location.to_local_meters(self.origin)
                if search_window.covers(Point(n_xy)):
                    nodes.append((round(n_xy[0], 3), round(n_xy[1], 3)))

        perimeter_rings: List[LineString] = []

        def add_vertices(geom) -> None:
            for poly in _extract_polygons(geom.intersection(search_window)):
                simp = poly.simplify(0.15, preserve_topology=True)
                ext_coords = list(simp.exterior.coords)[:-1]
                perimeter_rings.append(LineString(simp.exterior.coords))
                for x, y in ext_coords:
                    nodes.append((round(float(x), 3), round(float(y), 3)))
                for ring in simp.interiors:
                    for x, y in list(ring.coords)[:-1]:
                        nodes.append((round(float(x), 3), round(float(y), 3)))

        vertex_space = self.free_navigation_space
        if require_within_boundary and self.requested_boundary_poly is not None:
            vertex_space = vertex_space.intersection(self.requested_boundary_poly)
        add_vertices(vertex_space.buffer(-self.machine.clearance_width_m / 2.0 - 0.05)
                     if self.require_verified_terrain or self.hazard_coverage_scope is not None or require_within_boundary
                     else vertex_space)
        if (
            not require_within_boundary
            and not self.require_verified_terrain
            and not self.impassable_hazard_union.is_empty
        ):
            add_vertices(
                self.impassable_hazard_union.buffer(
                    self.machine.clearance_width_m
                )
            )

        start_node = (round(start[0], 3), round(start[1], 3))
        end_node = (round(end[0], 3), round(end[1], 3))
        nodes[0] = start_node
        nodes[1] = end_node
        nodes = [node for node in dict.fromkeys(nodes) if usable_node(node)]

        edges: List[Tuple[Tuple[float, float], Tuple[float, float], float]] = []
        edge_crossings: Dict[
            Tuple[Tuple[float, float], Tuple[float, float]], str
        ] = {}

        for (u, v), xid in crossing_edges.items():
            if u not in nodes or v not in nodes or not self.is_segment_permitted(
                u, v, require_within_boundary=False
            )[0]:
                continue
            d = math.hypot(v[0] - u[0], v[1] - u[1])
            edges.append((u, v, d))
            edge_crossings[(u, v)] = xid

        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                if (a, b) in crossing_edges:
                    continue
                seg_ok, _, xid = self.is_segment_permitted(
                    a, b, require_within_boundary=require_within_boundary
                )
                if seg_ok:
                    d = math.hypot(b[0] - a[0], b[1] - a[1])
                    # When prefer_perimeter is True (e.g. RETURN_TO_BASE), penalize chords that cut across the middle of the swept zone
                    weight = d
                    if prefer_perimeter and perimeter_rings:
                        mid = Point((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
                        dist_to_perim = min(r.distance(mid) for r in perimeter_rings)
                        if dist_to_perim > 3.5:
                            weight = d * 2.4
                    edges.append((a, b, weight))
                    if xid:
                        edge_crossings[(a, b)] = xid
                        edge_crossings[(b, a)] = xid

        path, _ = _shortest_path_fallback(nodes, edges, start_node, end_node)
        if not path:
            if _search_expansion < 3:
                return self.find_shortest_permitted_path(
                    start, end, require_within_boundary=require_within_boundary,
                    prefer_perimeter=prefer_perimeter, _search_expansion=_search_expansion + 1,
                )
            return None, float("inf"), []

        # Compute actual geometric distance along path
        actual_dist = 0.0
        for idx in range(len(path) - 1):
            actual_dist += math.hypot(
                path[idx + 1][0] - path[idx][0], path[idx + 1][1] - path[idx][1]
            )

        used_xings: List[str] = []
        for idx in range(len(path) - 1):
            pair = (path[idx], path[idx + 1])
            if pair in edge_crossings and edge_crossings[pair] not in used_xings:
                used_xings.append(edge_crossings[pair])

        return path, actual_dist, used_xings


class CoveragePlanResult(BaseModel):
    segments: List[RouteSegment]
    requested_area_m2: float = 0.0
    effective_eligible_area_m2: float = 0.0
    inaccessible_area_m2: float = 0.0
    unverified_area_m2: float = 0.0
    target_area_m2: float
    covered_area_m2: float
    repeated_area_m2: float
    missed_area_m2: float
    coverage_percent: float
    repeated_coverage_percent: float
    sweep_distance_m: float
    turn_distance_m: float
    unreachable_sections: List[UnreachableSection]
    excluded_sections: List[UnreachableSection] = []
    coverage_cells: List[CoverageCellSummary] = []
    road_access_conflicts: List[str] = []
    authorized_crossings_used: List[str] = []
    environment_features_applied: List[TerrainFeature] = []
    route_safety_issues: List[str] = []


class CoveragePlanner:
    """Mode A: Road-Aware & Terrain-Aware Cellular Boustrophedon Coverage Planner."""

    @staticmethod
    def _ordered_swaths(
        polygon: Polygon, step: float, inset: float, start: Tuple[float, float]
    ) -> List[LineString]:
        """Sweep connected scanline blocks before travelling around an obstacle.

        A hole splits a scanline into multiple intervals. Alternating across every
        interval on every row makes the robot circle the hole repeatedly. Split
        blocks when the interval topology changes, then finish each block with
        adjacent back-and-forth passes. Both row directions and entry ends are
        available so the first pass can start close to the current position.
        """
        minx, miny, maxx, maxy = polygon.bounds
        blocks: List[List[LineString]] = []
        active: List[List[LineString]] = []
        y = miny + inset
        while y <= maxy - inset + 1e-6:
            scan = LineString([(minx - 10.0, y), (maxx + 10.0, y)]).intersection(polygon)
            pieces = [scan] if isinstance(scan, LineString) else list(getattr(scan, "geoms", []))
            lines = sorted(
                (ln for ln in pieces if isinstance(ln, LineString) and ln.length >= 0.5),
                key=lambda ln: ln.bounds[0],
            )
            continuing = len(lines) == len(active) and all(
                min(ln.bounds[2], block[-1].bounds[2])
                > max(ln.bounds[0], block[-1].bounds[0])
                for ln, block in zip(lines, active)
            )
            if not continuing:
                active = [[] for _ in lines]
                blocks.extend(active)
            for block, ln in zip(active, lines):
                coords = list(ln.coords)
                if coords[0][0] > coords[-1][0]:
                    coords.reverse()
                block.append(LineString(coords))
            y += step

        ordered: List[LineString] = []
        current = start
        remaining = list(blocks)
        while remaining:
            # These four choices preserve adjacent passes within each block.
            choices = []
            for index, block in enumerate(remaining):
                for reverse_rows in (False, True):
                    first = block[-1] if reverse_rows else block[0]
                    for reverse_first in (False, True):
                        entry = first.coords[-1] if reverse_first else first.coords[0]
                        choices.append((math.dist(current, entry), index, reverse_rows, reverse_first))
            _, index, reverse_rows, reverse_first = min(choices)
            block = remaining.pop(index)
            if reverse_rows:
                block = list(reversed(block))
            for row, line in enumerate(block):
                coords = list(line.coords)
                if reverse_first != bool(row % 2):
                    coords.reverse()
                ordered.append(LineString(coords))
            current = ordered[-1].coords[-1]
        return ordered

    @staticmethod
    def _safe_connector_path(
        start: Tuple[float, float], end: Tuple[float, float], safe_space
    ) -> Optional[List[Tuple[float, float]]]:
        """Build a visibility-graph connector wholly inside eroded free space."""
        allowed = safe_space.buffer(0.02)
        direct = LineString([start, end])
        if allowed.covers(direct):
            return [start, end]
        if not allowed.covers(Point(start)) or not allowed.covers(Point(end)):
            return None

        nodes: List[Tuple[float, float]] = [start, end]

        def add_polygon_vertices(geometry) -> None:
            if isinstance(geometry, Polygon):
                nodes.extend(
                    (float(x), float(y))
                    for x, y in list(geometry.exterior.coords)[:-1]
                )
                for ring in geometry.interiors:
                    nodes.extend(
                        (float(x), float(y)) for x, y in list(ring.coords)[:-1]
                    )
            elif hasattr(geometry, "geoms"):
                for child in geometry.geoms:
                    add_polygon_vertices(child)

        add_polygon_vertices(safe_space.simplify(0.05, preserve_topology=True))
        nodes = list(dict.fromkeys((round(x, 3), round(y, 3)) for x, y in nodes))
        edges: List[Tuple[Tuple[float, float], Tuple[float, float], float]] = []
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                line = LineString([a, b])
                if allowed.covers(line):
                    edges.append((a, b, line.length))
        path, _ = _shortest_path_fallback(nodes, edges, nodes[0], nodes[1])
        if path:
            # Graph rounding must not introduce gaps between route segments.
            path[0], path[-1] = start, end
        return path

    def plan_coverage(
        self,
        boundary: GeoPolygon,
        exclusion_zones: List[ExclusionZone],
        machine: MachineProfile,
        sweep_angle_deg: float = 0.0,
        overlap_ratio: Optional[float] = None,
        completed_segments: Optional[List[RouteSegment]] = None,
        environment_map: Optional[EnvironmentMapData] = None,
        origin_coord: Optional[Coordinate] = None,
        prune_unreachable_cells: bool = False,
        use_authorized_crossings: bool = True,
        reorder_cells: bool = True,
    ) -> CoveragePlanResult:
        origin = boundary.vertices[0]
        outer_poly = boundary.to_shapely_local(origin)
        requested_area_m2 = max(outer_poly.area, 0.01)

        env_map = environment_map
        if env_map is None and settings.enable_default_campus_terrain_layer:
            env_map = DefaultCampusEnvironmentProvider.get_default_environment_map()

        obs_polys = [z.polygon.to_shapely_local(origin) for z in exclusion_zones]
        water_polys: List[Polygon] = []
        building_polys: List[Polygon] = []
        prohibited_road_polys: List[Polygon] = []
        prohibited_road_sweep_polys: List[Polygon] = []
        restricted_feat_polys: List[Polygon] = []
        prohibited_surface_polys: List[Polygon] = []
        unverified_feat_polys: List[Polygon] = []

        applied_features: List[TerrainFeature] = []
        road_conflicts: List[str] = []
        excluded_sections: List[UnreachableSection] = []
        unreachable: List[UnreachableSection] = []
        safety_issues: List[str] = []

        if env_map:
            for feat in env_map.features:
                f_poly = feat.polygon.to_shapely_local(origin)
                if not f_poly.intersects(outer_poly.buffer(5.0)):
                    continue
                overlap_poly = outer_poly.intersection(f_poly)
                overlap_area = (
                    overlap_poly.area if not overlap_poly.is_empty else 0.0
                )
                if overlap_area > 0.25:
                    applied_features.append(feat)
                rep_pt = (
                    overlap_poly.representative_point()
                    if overlap_area > 0.25
                    else f_poly.representative_point()
                )
                rep_coord = Coordinate.from_local_meters(
                    rep_pt.x, rep_pt.y, origin
                )

                if feat.feature_type == TerrainFeatureType.WATER_BODY:
                    buf_poly = f_poly.buffer(max(feat.clearance_buffer_m, 0.4))
                    water_polys.append(buf_poly)
                    if overlap_area >= 1.0:
                        excluded_sections.append(
                            UnreachableSection(
                                section_id=f"excl-water-{feat.feature_id}",
                                reason=f"water_body_exclusion: {feat.name} is impassable water",
                                area_m2=round(overlap_area, 2),
                                centroid=rep_coord,
                            )
                        )
                elif feat.feature_type == TerrainFeatureType.BUILDING:
                    buf_poly = f_poly.buffer(max(feat.clearance_buffer_m, 0.4))
                    building_polys.append(buf_poly)
                    if overlap_area >= 1.0:
                        excluded_sections.append(
                            UnreachableSection(
                                section_id=f"excl-bldg-{feat.feature_id}",
                                reason=f"building_footprint_exclusion: {feat.name}",
                                area_m2=round(overlap_area, 2),
                                centroid=rep_coord,
                            )
                        )
                elif feat.feature_type == TerrainFeatureType.RESTRICTED_ZONE:
                    buf_poly = f_poly.buffer(max(feat.clearance_buffer_m, 0.2))
                    restricted_feat_polys.append(buf_poly)
                    if overlap_area >= 1.0:
                        excluded_sections.append(
                            UnreachableSection(
                                section_id=f"excl-restr-{feat.feature_id}",
                                reason=f"restricted_zone_exclusion: {feat.name}",
                                area_m2=round(overlap_area, 2),
                                centroid=rep_coord,
                            )
                        )
                elif feat.feature_type == TerrainFeatureType.PUBLIC_ROAD:
                    if not machine.allow_public_road_sweeping:
                        curb_buf = max(
                            feat.clearance_buffer_m,
                            machine.road_clearance_buffer_m,
                        )
                        prohibited_road_polys.append(f_poly)
                        prohibited_road_sweep_polys.append(
                            f_poly.buffer(curb_buf)
                        )
                        if overlap_area >= 1.0:
                            msg = (
                                f"Road-access conflict: '{feat.name}' (PUBLIC_ROAD) excluded from sweeping "
                                f"with {curb_buf:.1f}m curb clearance for machine '{machine.machine_id}'."
                            )
                            road_conflicts.append(msg)
                            excluded_sections.append(
                                UnreachableSection(
                                    section_id=f"excl-road-{feat.feature_id}",
                                    reason=f"prohibited_road_exclusion: {feat.name}",
                                    area_m2=round(overlap_area, 2),
                                    centroid=rep_coord,
                                )
                            )
                elif feat.feature_type == TerrainFeatureType.UNVERIFIED_TERRAIN:
                    unverified_feat_polys.append(f_poly)
                    if overlap_area >= 1.0:
                        excluded_sections.append(
                            UnreachableSection(
                                section_id=f"excl-unverified-{feat.feature_id}",
                                reason=f"unverified_terrain: {feat.name} lacks verified map data",
                                area_m2=round(overlap_area, 2),
                                centroid=rep_coord,
                            )
                        )
                else:
                    surface_allowed = (
                        feat.feature_type.value
                        in machine.allowed_surface_classes
                    )
                    machine_allowed = (
                        feat.allowed_machine_ids is None
                        or machine.machine_id in feat.allowed_machine_ids
                    )
                    width_allowed = (
                        machine.clearance_width_m <= feat.min_clearance_width_m
                    )
                    slope_allowed = feat.max_slope_deg <= machine.max_slope_deg
                    if not (
                        surface_allowed
                        and machine_allowed
                        and width_allowed
                        and slope_allowed
                    ):
                        prohibited_surface_polys.append(f_poly)
                        if overlap_area >= 1.0:
                            reason_detail = f"surface '{feat.feature_type.value}' on '{feat.name}' restricted for '{machine.machine_id}'"
                            road_conflicts.append(
                                f"Machine access restriction: {reason_detail}"
                            )
                            excluded_sections.append(
                                UnreachableSection(
                                    section_id=f"excl-surf-{feat.feature_id}",
                                    reason=f"machine_access_restriction: {reason_detail}",
                                    area_m2=round(overlap_area, 2),
                                    centroid=rep_coord,
                                )
                            )

        if env_map and env_map.require_verified_terrain:
            if env_map.verified_traversable_polygons:
                verified_union = unary_union(
                    [
                        vp.to_shapely_local(origin)
                        for vp in env_map.verified_traversable_polygons
                    ]
                )
                unv_diff = outer_poly.difference(verified_union)
            else:
                unv_diff = outer_poly
            if not unv_diff.is_empty and unv_diff.area >= 1.0:
                unverified_feat_polys.append(unv_diff)
                rp = unv_diff.representative_point()
                excluded_sections.append(
                    UnreachableSection(
                        section_id="excl-unverified-envelope",
                        reason="unverified_terrain: area outside verified traversable map envelope",
                        area_m2=round(unv_diff.area, 2),
                        centroid=Coordinate.from_local_meters(
                            rp.x, rp.y, origin
                        ),
                    )
                )

        if env_map and env_map.hazard_coverage_boundary is not None:
            mapped_scope = env_map.hazard_coverage_boundary.to_shapely_local(origin)
            outside_mapped_scope = outer_poly.difference(mapped_scope)
            if not outside_mapped_scope.is_empty and outside_mapped_scope.area > 0:
                unverified_feat_polys.append(outside_mapped_scope)
                rp = outside_mapped_scope.representative_point()
                excluded_sections.append(
                    UnreachableSection(
                        section_id="excl-outside-mapped-area",
                        reason="unverified_terrain: outside mapped area; import site geometry",
                        area_m2=round(outside_mapped_scope.area, 2),
                        centroid=Coordinate.from_local_meters(rp.x, rp.y, origin),
                    )
                )

        impassable_hazard_union = (
            unary_union(
                obs_polys + water_polys + building_polys + restricted_feat_polys
            )
            if (obs_polys or water_polys or building_polys or restricted_feat_polys)
            else Polygon()
        )
        prohibited_road_union = (
            unary_union(prohibited_road_polys)
            if prohibited_road_polys
            else Polygon()
        )
        prohibited_road_sweep_union = (
            unary_union(prohibited_road_sweep_polys)
            if prohibited_road_sweep_polys
            else Polygon()
        )
        prohibited_surface_union = (
            unary_union(prohibited_surface_polys)
            if prohibited_surface_polys
            else Polygon()
        )
        unverified_union = (
            unary_union(unverified_feat_polys)
            if unverified_feat_polys
            else Polygon()
        )

        unverified_in_poly = (
            outer_poly.intersection(unverified_union)
            if not unverified_union.is_empty
            else Polygon()
        )
        unverified_area_m2 = round(unverified_in_poly.area, 2)

        all_inaccessible_union = unary_union(
            [
                g
                for g in [
                    impassable_hazard_union,
                    prohibited_road_sweep_union,
                    prohibited_surface_union,
                ]
                if not g.is_empty
            ]
        )
        inaccessible_in_poly = (
            outer_poly.difference(unverified_in_poly).intersection(
                all_inaccessible_union
            )
            if not all_inaccessible_union.is_empty
            else Polygon()
        )
        inaccessible_area_m2 = round(inaccessible_in_poly.area, 2)

        free_space = outer_poly.difference(all_inaccessible_union).difference(
            unverified_in_poly
        )
        effective_eligible_area_m2 = round(max(free_space.area, 0.0), 2)

        safe_route_space = outer_poly.buffer(-machine.clearance_width_m / 2.0)
        if not impassable_hazard_union.is_empty:
            safe_route_space = safe_route_space.difference(
                impassable_hazard_union.buffer(max(machine.working_width_m, machine.clearance_width_m) / 2.0)
            )
        if not prohibited_road_sweep_union.is_empty:
            safe_route_space = safe_route_space.difference(
                prohibited_road_sweep_union
            )
        if not prohibited_surface_union.is_empty:
            safe_route_space = safe_route_space.difference(
                prohibited_surface_union
            )
        if not unverified_in_poly.is_empty:
            safe_route_space = safe_route_space.difference(unverified_in_poly)
        if env_map and env_map.require_verified_terrain:
            declared = (unary_union([p.to_shapely_local(origin) for p in env_map.verified_traversable_polygons])
                        if env_map.verified_traversable_polygons else Polygon())
            safe_route_space = safe_route_space.intersection(declared.buffer(-machine.clearance_width_m / 2.0))
        if env_map and env_map.hazard_coverage_boundary is not None:
            mapped_scope = env_map.hazard_coverage_boundary.to_shapely_local(origin)
            safe_route_space = safe_route_space.intersection(
                mapped_scope.buffer(-max(machine.working_width_m, machine.clearance_width_m) / 2.0)
            )

        # Physical coverage must use the brush width, regardless of map scale.
        # Inflating it to simplify the display understates travel and energy while
        # counting unswept gaps as cleaned.
        eff_working_width = machine.working_width_m

        already_swept = Polygon()
        retained: List[RouteSegment] = []
        if completed_segments:
            bufs = []
            for s in completed_segments:
                if (
                    s.completed
                    and s.segment_type == "SWEEP_SWATH"
                    and len(s.waypoints) >= 2
                ):
                    pts = [w.to_local_meters(origin) for w in s.waypoints]
                    bufs.append(
                        LineString(pts).buffer(
                            min(s.swath_width_m or eff_working_width, eff_working_width) / 2.0
                        )
                    )
                    retained.append(s)
            if bufs:
                already_swept = unary_union(bufs).intersection(free_space)

        remaining = free_space.difference(already_swept)
        nav_core = safe_route_space.intersection(remaining)

        nav_graph = TraversabilityGraph(
            origin=origin,
            machine=machine,
            free_navigation_space=safe_route_space,
            impassable_hazard_union=impassable_hazard_union,
            prohibited_road_union=prohibited_road_union,
            prohibited_surface_union=prohibited_surface_union,
            environment_map=env_map,
            use_authorized_crossings=use_authorized_crossings,
            requested_boundary_poly=outer_poly,
        )
        road_conflicts.extend(nav_graph.prohibited_crossing_reasons)

        if nav_core.is_empty or free_space.area < 4.0:
            if free_space.area < 4.0 and requested_area_m2 >= 4.0:
                safety_issues.append(
                    "no_eligible_traversable_area: entire requested polygon is inaccessible or unverified"
                )
            return CoveragePlanResult(
                segments=retained,
                requested_area_m2=round(requested_area_m2, 2),
                effective_eligible_area_m2=effective_eligible_area_m2,
                inaccessible_area_m2=inaccessible_area_m2,
                unverified_area_m2=unverified_area_m2,
                target_area_m2=round(
                    max(effective_eligible_area_m2, requested_area_m2), 2
                ),
                covered_area_m2=round(already_swept.area, 2),
                repeated_area_m2=0.0,
                missed_area_m2=round(
                    max(0.0, effective_eligible_area_m2 - already_swept.area), 2
                ),
                coverage_percent=0.0,
                repeated_coverage_percent=0.0,
                sweep_distance_m=0.0,
                turn_distance_m=0.0,
                unreachable_sections=unreachable + excluded_sections,
                excluded_sections=excluded_sections,
                coverage_cells=[],
                road_access_conflicts=road_conflicts,
                authorized_crossings_used=[],
                environment_features_applied=applied_features,
                route_safety_issues=safety_issues,
            )

        raw_cell_polys = _extract_polygons(nav_core)
        raw_cell_polys.sort(key=lambda p: (-p.area, p.bounds[0], p.bounds[1]))

        start_ref_coord = origin_coord or origin
        start_xy = start_ref_coord.to_local_meters(origin)
        start_pt = Point(start_xy)

        anchor_idx = 0
        best_anchor_dist = float("inf")
        for idx, c_poly in enumerate(raw_cell_polys):
            if c_poly.buffer(1.0).covers(start_pt):
                anchor_idx = idx
                best_anchor_dist = 0.0
                break
            rep = c_poly.representative_point()
            ok, _, _ = nav_graph.is_segment_permitted(
                start_xy, (rep.x, rep.y), require_within_boundary=False
            )
            d = c_poly.distance(start_pt)
            if ok and d < best_anchor_dist:
                best_anchor_dist = d
                anchor_idx = idx

        anchor_rep = raw_cell_polys[anchor_idx].representative_point()
        anchor_xy = (anchor_rep.x, anchor_rep.y)

        reachable_cells: List[Tuple[str, Polygon]] = []
        cell_summaries: List[CoverageCellSummary] = []

        for idx, c_poly in enumerate(raw_cell_polys):
            cid = f"cell-{idx + 1}"
            rep = c_poly.representative_point()
            rep_xy = (rep.x, rep.y)
            rep_coord = Coordinate.from_local_meters(rep.x, rep.y, origin)
            geo_cell = GeoPolygon.from_shapely_local(c_poly, origin) or boundary

            if idx == anchor_idx:
                is_reach = True
            else:
                path_pts, _, _ = nav_graph.find_shortest_permitted_path(
                    anchor_xy, rep_xy, require_within_boundary=True
                )
                is_reach = path_pts is not None

            if is_reach:
                reachable_cells.append((cid, c_poly))
                cell_summaries.append(
                    CoverageCellSummary(
                        cell_id=cid,
                        polygon=geo_cell,
                        area_m2=round(c_poly.area, 2),
                        reachable=True,
                        swept=True,
                        surface_class="PEDESTRIAN_PLAZA",
                    )
                )
            else:
                reason_str = "sweep_connector_unreachable: no validated path between adjacent swaths or disconnected cell"
                cell_summaries.append(
                    CoverageCellSummary(
                        cell_id=cid,
                        polygon=geo_cell,
                        area_m2=round(c_poly.area, 2),
                        reachable=False,
                        swept=False,
                        surface_class="ISOLATED_CELL",
                        exclusion_reason=reason_str,
                    )
                )
                unreach_sec = UnreachableSection(
                    section_id=cid,
                    reason=reason_str,
                    area_m2=round(c_poly.area, 2),
                    centroid=rep_coord,
                )
                excluded_sections.append(unreach_sec)
                inaccessible_area_m2 = round(
                    inaccessible_area_m2 + c_poly.area, 2
                )
                if not prune_unreachable_cells:
                    safety_issues.append(reason_str)
                    unreachable.append(unreach_sec)

        if reorder_cells and len(reachable_cells) > 1:
            ordered: List[Tuple[str, Polygon]] = []
            remaining_cells = list(reachable_cells)
            curr_xy = start_xy
            while remaining_cells:
                best_i = 0
                best_d = float("inf")
                for i, (_, cp) in enumerate(remaining_cells):
                    rp = cp.representative_point()
                    _, d_val, _ = nav_graph.find_shortest_permitted_path(
                        curr_xy, (rp.x, rp.y), require_within_boundary=True
                    )
                    if d_val < best_d:
                        best_d = d_val
                        best_i = i
                picked = remaining_cells.pop(best_i)
                ordered.append(picked)
                rp = picked[1].representative_point()
                curr_xy = (rp.x, rp.y)
            reachable_cells = ordered

        eff_overlap = (
            overlap_ratio
            if overlap_ratio is not None
            else machine.default_overlap_ratio
        )
        eff_overlap = max(0.0, min(eff_overlap, 0.65))
        step = max(0.25, eff_working_width * (1.0 - eff_overlap))

        segments: List[RouteSegment] = list(retained)
        swath_polys: List[Polygon] = (
            [already_swept] if not already_swept.is_empty else []
        )
        tot_sweep = sum(
            s.distance_m for s in retained if s.segment_type == "SWEEP_SWATH"
        )
        tot_turn = sum(
            s.distance_m for s in retained if s.segment_type == "TURN_CONNECTOR"
        )
        raw_area = already_swept.area
        prev_end: Optional[Tuple[float, float]] = None
        prev_cell_id: Optional[str] = None
        crossings_used: List[str] = []

        for cid, cell_poly in reachable_cells:
            centroid = cell_poly.centroid
            rot_poly = affinity.rotate(
                cell_poly, -sweep_angle_deg, origin=centroid, use_radians=False
            )
            inset = min(step * 0.02, eff_working_width * 0.02)
            rotated_start = affinity.rotate(
                Point(prev_end if prev_end is not None else start_xy),
                -sweep_angle_deg, origin=centroid, use_radians=False,
            )
            cell_swaths = [
                affinity.rotate(ln, sweep_angle_deg, origin=centroid, use_radians=False)
                for ln in self._ordered_swaths(rot_poly, step, inset, (rotated_start.x, rotated_start.y))
            ]

            if (
                eff_overlap >= 0.25
                and isinstance(cell_poly, Polygon)
                and cell_poly.exterior.length > 2.0
            ):
                cell_swaths.append(LineString(cell_poly.exterior.coords))

            for summary in cell_summaries:
                if summary.cell_id == cid:
                    summary.swath_count = len(cell_swaths)

            for ln in cell_swaths:
                coords = list(ln.coords)
                if prev_end is not None:
                    if prev_cell_id == cid:
                        connector_points = self._safe_connector_path(
                            prev_end, coords[0], cell_poly
                        )
                        xing_list: List[str] = []
                    else:
                        connector_points, _, xing_list = (
                            nav_graph.find_shortest_permitted_path(
                                prev_end, coords[0], require_within_boundary=True
                            )
                        )
                    if not connector_points:
                        connector = LineString([prev_end, coords[0]])
                        midpoint = connector.interpolate(0.5, normalized=True)
                        issue = "sweep_connector_unreachable: no validated path between adjacent swaths"
                        safety_issues.append(issue)
                        unreachable.append(
                            UnreachableSection(
                                section_id=f"connector-{len(safety_issues)}",
                                reason=issue,
                                area_m2=0.0,
                                centroid=Coordinate.from_local_meters(
                                    midpoint.x, midpoint.y, origin
                                ),
                            )
                        )
                    else:
                        for xid in xing_list:
                            if xid not in crossings_used:
                                crossings_used.append(xid)
                        connector = LineString(connector_points)
                        arc = connector.length
                        is_crossing_leg = len(xing_list) > 0
                        segments.append(
                            RouteSegment(
                                segment_type=(
                                    "TRANSIT"
                                    if is_crossing_leg
                                    else "TURN_CONNECTOR"
                                ),
                                waypoints=[
                                    Coordinate.from_local_meters(
                                        p[0], p[1], origin
                                    )
                                    for p in connector_points
                                ],
                                distance_m=round(arc, 2),
                                estimated_duration_sec=round(
                                    arc
                                    / max(machine.sweep_speed_mps * 0.75, 0.2),
                                    2,
                                ),
                                cell_id=cid,
                                surface_class=(
                                    "AUTHORIZED_CROSSING"
                                    if is_crossing_leg
                                    else "PEDESTRIAN_PLAZA"
                                ),
                                crossing_id=(
                                    xing_list[0] if xing_list else None
                                ),
                            )
                        )
                        tot_turn += arc

                segments.append(
                    RouteSegment(
                        segment_type="SWEEP_SWATH",
                        waypoints=[
                            Coordinate.from_local_meters(p[0], p[1], origin)
                            for p in coords
                        ],
                        distance_m=round(ln.length, 2),
                        estimated_duration_sec=round(
                            ln.length / machine.sweep_speed_mps, 2
                        ),
                        swath_width_m=eff_working_width,
                        cell_id=cid,
                        surface_class="PEDESTRIAN_PLAZA",
                    )
                )
                tot_sweep += ln.length
                buf = ln.buffer(eff_working_width / 2.0).intersection(
                    free_space
                )
                swath_polys.append(buf)
                raw_area += buf.area
                prev_end = coords[-1]
                prev_cell_id = cid

        unreachable_cell_area = sum(
            c.area_m2 for c in cell_summaries if not c.reachable
        )
        if (
            prune_unreachable_cells
            and unreachable_cell_area > 0
            and nav_core.area > 0
        ):
            reachable_ratio = sum(
                c.area_m2 for c in cell_summaries if c.reachable
            ) / max(nav_core.area, 0.01)
            reachable_target_area_m2 = max(
                free_space.area * reachable_ratio, 0.01
            )
        else:
            reachable_target_area_m2 = max(free_space.area, 0.01)

        effective_eligible_area_m2 = round(reachable_target_area_m2, 2)
        target_area_m2 = round(reachable_target_area_m2, 2)

        cov_union = (
            unary_union(swath_polys).intersection(free_space)
            if swath_polys
            else Polygon()
        )
        cov_area = min(target_area_m2, cov_union.area)
        missed_area = max(0.0, target_area_m2 - cov_area)
        rep_area = max(0.0, raw_area - cov_union.area)
        cov_pct = min(100.0, (cov_area / target_area_m2) * 100.0)
        rep_pct = min(100.0, (rep_area / max(cov_area, 1.0)) * 100.0)

        return CoveragePlanResult(
            segments=segments,
            requested_area_m2=round(requested_area_m2, 2),
            effective_eligible_area_m2=effective_eligible_area_m2,
            inaccessible_area_m2=round(
                max(
                    0.0,
                    requested_area_m2
                    - effective_eligible_area_m2
                    - unverified_area_m2,
                ),
                2,
            ),
            unverified_area_m2=unverified_area_m2,
            target_area_m2=round(target_area_m2, 2),
            covered_area_m2=round(cov_area, 2),
            repeated_area_m2=round(rep_area, 2),
            missed_area_m2=round(missed_area, 2),
            coverage_percent=round(cov_pct, 2),
            repeated_coverage_percent=round(rep_pct, 2),
            sweep_distance_m=round(tot_sweep, 2),
            turn_distance_m=round(tot_turn, 2),
            unreachable_sections=unreachable,
            excluded_sections=excluded_sections,
            coverage_cells=cell_summaries,
            road_access_conflicts=road_conflicts,
            authorized_crossings_used=crossings_used,
            environment_features_applied=applied_features,
            route_safety_issues=safety_issues,
        )


class RoutingServiceUnavailableError(Exception):
    pass


class DistanceMatrixResult(BaseModel):
    distance_matrix_m: List[List[float]]
    duration_matrix_sec: List[List[float]]
    provenance: DataProvenance


class ValhallaRoutingService:
    """Mode B Geographic Route Planner via self-hosted Valhalla or Road/Terrain-Aware TraversabilityGraph."""

    def __init__(self, valhalla_url: str = settings.valhalla_url) -> None:
        self.valhalla_url = valhalla_url
        self._force_offline_for_test: bool = False
        self._valhalla_unavailable_for_session: bool = False

    def compute_matrix_and_paths(
        self,
        locations: List[Coordinate],
        cruise_speed_mps: float,
        nav_graph: Optional[TraversabilityGraph] = None,
    ) -> Tuple[DistanceMatrixResult, Dict[Tuple[int, int], List[Coordinate]]]:
        if (
            not self._force_offline_for_test
            and not self._valhalla_unavailable_for_session
            and nav_graph is None
        ):
            try:
                pts = [{"lat": c.lat, "lon": c.lon} for c in locations]
                with httpx.Client(timeout=1.5) as client:
                    r = client.post(
                        f"{self.valhalla_url}/sources_to_targets",
                        json={
                            "sources": pts,
                            "targets": pts,
                            "costing": "auto",
                            "units": "kilometers",
                        },
                    )
                    r.raise_for_status()
                    data = r.json()
                    n = len(locations)
                    dm = [[0.0] * n for _ in range(n)]
                    tm = [[0.0] * n for _ in range(n)]
                    paths: Dict[Tuple[int, int], List[Coordinate]] = {}
                    for i, row in enumerate(data["sources_to_targets"]):
                        for j, cell in enumerate(row):
                            if (
                                cell.get("distance") is None
                                or cell.get("time") is None
                            ):
                                raise RoutingServiceUnavailableError(
                                    f"No mapped route from point {i} to {j}."
                                )
                            dm[i][j] = float(cell["distance"]) * 1000.0
                            tm[i][j] = float(cell["time"])
                            if i == j:
                                paths[(i, j)] = [locations[i]]
                                continue
                            route_response = client.post(
                                f"{self.valhalla_url}/route",
                                json={
                                    "locations": [pts[i], pts[j]],
                                    "costing": "auto",
                                    "units": "kilometers",
                                    "shape_format": "polyline6",
                                },
                            )
                            route_response.raise_for_status()
                            trip = route_response.json().get("trip", {})
                            shape = trip.get("legs", [{}])[0].get("shape")
                            decoded = (
                                self._decode_polyline6(shape) if shape else []
                            )
                            if len(decoded) < 2:
                                raise RoutingServiceUnavailableError(
                                    f"Valhalla returned no route geometry from point {i} to {j}."
                                )
                            paths[(i, j)] = decoded
                    return (
                        DistanceMatrixResult(
                            distance_matrix_m=dm,
                            duration_matrix_sec=tm,
                            provenance=DataProvenance.PLANNED,
                        ),
                        paths,
                    )
            except Exception as exc:
                if not settings.allow_labeled_demo_routing:
                    raise RoutingServiceUnavailableError(str(exc)) from exc
                self._valhalla_unavailable_for_session = True
        elif not settings.allow_labeled_demo_routing:
            raise RoutingServiceUnavailableError(
                "Simulated Valhalla outage and fallback disabled."
            )

        origin = locations[0]
        local_pts = [c.to_local_meters(origin) for c in locations]

        if nav_graph is not None:
            n = len(locations)
            dm = [[0.0] * n for _ in range(n)]
            tm = [[0.0] * n for _ in range(n)]
            paths = {}
            for i in range(n):
                u = locations[i].to_local_meters(nav_graph.origin)
                for j in range(n):
                    if i == j:
                        paths[(i, j)] = [locations[i]]
                        continue
                    v = locations[j].to_local_meters(nav_graph.origin)
                    internal_leg = nav_graph.requested_boundary_poly is not None and all(
                        nav_graph.requested_boundary_poly.buffer(0.02).covers(Point(point)) for point in (u, v)
                    )
                    p_nodes, raw_len, _ = (
                        nav_graph.find_shortest_permitted_path(
                            u, v, require_within_boundary=internal_leg
                        )
                    )
                    if p_nodes is None:
                        dm[i][j] = 1e9
                        tm[i][j] = 1e9
                        paths[(i, j)] = []
                    else:
                        dist = max(raw_len * 1.04, 1.0)
                        dm[i][j] = round(dist, 2)
                        tm[i][j] = round(dist / max(cruise_speed_mps, 0.2), 2)
                        paths[(i, j)] = [
                            Coordinate.from_local_meters(
                                px, py, nav_graph.origin
                            )
                            for (px, py) in p_nodes
                        ]
            return (
                DistanceMatrixResult(
                    distance_matrix_m=dm,
                    duration_matrix_sec=tm,
                    provenance=DataProvenance.SYNTHETIC_FALLBACK_GRAPH,
                ),
                paths,
            )

        xs = sorted(set(round(p[0], 2) for p in local_pts))
        ys = sorted(set(round(p[1], 2) for p in local_pts))
        grid_nodes = [(x, y) for x in xs for y in ys]
        grid_edges: List[Tuple[Tuple[float, float], Tuple[float, float], float]] = []
        for ix, x in enumerate(xs):
            for iy, y in enumerate(ys):
                if ix + 1 < len(xs):
                    grid_edges.append(
                        ((x, y), (xs[ix + 1], y), abs(xs[ix + 1] - x))
                    )
                if iy + 1 < len(ys):
                    grid_edges.append(
                        ((x, y), (x, ys[iy + 1]), abs(ys[iy + 1] - y))
                    )

        n = len(locations)
        dm = [[0.0] * n for _ in range(n)]
        tm = [[0.0] * n for _ in range(n)]
        paths = {}
        for i in range(n):
            u = (round(local_pts[i][0], 2), round(local_pts[i][1], 2))
            for j in range(n):
                if i == j:
                    paths[(i, j)] = [locations[i]]
                    continue
                v = (round(local_pts[j][0], 2), round(local_pts[j][1], 2))
                p_nodes_opt, raw_len = _shortest_path_fallback(
                    grid_nodes, grid_edges, u, v
                )
                p_nodes = p_nodes_opt or [u, v]
                dist = max(raw_len * 1.08, 1.0)
                dm[i][j] = round(dist, 2)
                tm[i][j] = round(dist / max(cruise_speed_mps, 0.2), 2)
                paths[(i, j)] = [
                    Coordinate.from_local_meters(px, py, origin)
                    for (px, py) in p_nodes
                ]

        return (
            DistanceMatrixResult(
                distance_matrix_m=dm,
                duration_matrix_sec=tm,
                provenance=DataProvenance.SYNTHETIC_FALLBACK_GRAPH,
            ),
            paths,
        )

    @staticmethod
    def _decode_polyline6(encoded: str) -> List[Coordinate]:
        coords: List[Coordinate] = []
        index = lat = lon = 0
        while index < len(encoded):
            values = []
            for _ in range(2):
                result = shift = 0
                while True:
                    b = ord(encoded[index]) - 63
                    index += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                values.append(~(result >> 1) if result & 1 else result >> 1)
            lat += values[0]
            lon += values[1]
            coords.append(Coordinate(lat=lat / 1_000_000, lon=lon / 1_000_000))
        return coords


class OptimizedCollectionPlan(BaseModel):
    ordered_tasks: List[TaskItem]
    skipped_optional_tasks: List[TaskItem]
    unreachable_tasks: List[TaskItem] = []
    task_schedule: List[TaskScheduleEntry] = []
    segments: List[RouteSegment]
    total_transit_distance_m: float
    disposal_trips: int
    peak_payload_kg: float = 0.0
    peak_payload_volume_m3: float = 0.0


class RouteOptimizationEngine:
    """Deterministic collection scheduler with windows, service time, graph connectivity, and payload limits."""

    def optimize_collection_route(
        self,
        start_location: Coordinate,
        disposal_depot: Coordinate,
        tasks: List[TaskItem],
        machine: MachineProfile,
        dist_matrix_m: List[List[float]],
        time_matrix_sec: List[List[float]],
        paths: Dict[Tuple[int, int], List[Coordinate]],
        provenance: DataProvenance,
        prune_lowest_priority_if_over_budget: bool = False,
        start_elapsed_min: float = 0.0,
        preferred_task_id: Optional[str] = None,
    ) -> OptimizedCollectionPlan:
        active = [t for t in tasks if not t.completed]
        if not active:
            return OptimizedCollectionPlan(
                ordered_tasks=[],
                skipped_optional_tasks=[],
                unreachable_tasks=[],
                task_schedule=[],
                segments=[],
                total_transit_distance_m=0.0,
                disposal_trips=0,
                peak_payload_kg=machine.current_payload_kg,
                peak_payload_volume_m3=machine.current_payload_volume_m3,
            )

        depot_idx = len(active) + 1
        unreachable_indices: Set[int] = set()
        for i in range(len(active)):
            node = i + 1
            if dist_matrix_m[0][node] >= 1e8 or dist_matrix_m[node][depot_idx] >= 1e8:
                unreachable_indices.add(i)

        ordered_idx, schedule, disposal_flags = self._schedule_indices(
            active,
            dist_matrix_m,
            time_matrix_sec,
            machine,
            start_elapsed_min,
            unreachable_indices=unreachable_indices,
            preferred_task_id=preferred_task_id,
        )
        selected = set(ordered_idx)
        unreachable_tasks = [
            task for i, task in enumerate(active) if i in unreachable_indices
        ]
        skipped = [
            task
            for i, task in enumerate(active)
            if i not in selected and i not in unreachable_indices
        ]
        segments: List[RouteSegment] = []
        ordered_tasks: List[TaskItem] = []
        curr_idx = 0
        tot_dist = 0.0
        trips = 0

        for position, idx in enumerate(ordered_idx):
            t = active[idx]
            node = idx + 1
            if disposal_flags[position]:
                depot_d = dist_matrix_m[curr_idx][depot_idx]
                depot_t = time_matrix_sec[curr_idx][depot_idx]
                segments.append(
                    RouteSegment(
                        segment_type="RETURN_TO_BASE",
                        waypoints=paths.get(
                            (curr_idx, depot_idx), [disposal_depot]
                        ),
                        distance_m=round(depot_d, 2),
                        estimated_duration_sec=round(depot_t, 2),
                        target_task_id=t.task_id,
                        provenance=provenance,
                    )
                )
                tot_dist += depot_d
                trips += 1
                curr_idx = depot_idx
            leg_d = dist_matrix_m[curr_idx][node]
            leg_t = time_matrix_sec[curr_idx][node]
            segments.append(
                RouteSegment(
                    segment_type="TRANSIT",
                    waypoints=paths.get((curr_idx, node), [t.location]),
                    distance_m=round(leg_d, 2),
                    estimated_duration_sec=round(leg_t, 2),
                    target_task_id=t.task_id,
                    provenance=provenance,
                )
            )
            if schedule[position].waiting_min > 0.001:
                segments.append(
                    RouteSegment(
                        segment_type="WAIT_FOR_WINDOW",
                        waypoints=[t.location],
                        distance_m=0.0,
                        estimated_duration_sec=round(
                            schedule[position].waiting_min * 60.0, 2
                        ),
                        target_task_id=t.task_id,
                    )
                )
            segments.append(
                RouteSegment(
                    segment_type="COLLECTION_STOP",
                    waypoints=[t.location],
                    distance_m=0.0,
                    estimated_duration_sec=round(t.service_duration_sec, 2),
                    target_task_id=t.task_id,
                )
            )
            tot_dist += leg_d
            curr_idx = node
            ordered_tasks.append(t)

        if ordered_idx:
            ret_d = dist_matrix_m[curr_idx][depot_idx]
            ret_t = time_matrix_sec[curr_idx][depot_idx]
            if ret_d < 1e8 and paths.get((curr_idx, depot_idx)):
                segments.append(
                    RouteSegment(
                        segment_type="RETURN_TO_BASE",
                        waypoints=paths.get(
                            (curr_idx, depot_idx), [disposal_depot]
                        ),
                        distance_m=round(ret_d, 2),
                        estimated_duration_sec=round(ret_t, 2),
                        provenance=provenance,
                    )
                )
                tot_dist += ret_d
                trips += 1

        peak_payload_kg = machine.current_payload_kg
        peak_payload_volume = machine.current_payload_volume_m3
        payload_kg = machine.current_payload_kg
        payload_volume = machine.current_payload_volume_m3
        for idx, dump in zip(ordered_idx, disposal_flags):
            if dump:
                payload_kg = 0.0
                payload_volume = 0.0
            spec = active[idx].rubbish_spec
            if spec:
                payload_kg += spec.estimated_weight_kg or 0.0
                payload_volume += spec.estimated_volume_m3 or 0.0
            peak_payload_kg = max(peak_payload_kg, payload_kg)
            peak_payload_volume = max(peak_payload_volume, payload_volume)

        return OptimizedCollectionPlan(
            ordered_tasks=ordered_tasks,
            skipped_optional_tasks=skipped,
            unreachable_tasks=unreachable_tasks,
            task_schedule=schedule,
            segments=segments,
            total_transit_distance_m=round(tot_dist, 2),
            disposal_trips=trips,
            peak_payload_kg=peak_payload_kg,
            peak_payload_volume_m3=peak_payload_volume,
        )

    def _schedule_indices(
        self,
        tasks: List[TaskItem],
        dm: List[List[float]],
        tm: List[List[float]],
        machine: MachineProfile,
        start_elapsed_min: float = 0.0,
        unreachable_indices: Optional[Set[int]] = None,
        preferred_task_id: Optional[str] = None,
    ) -> Tuple[List[int], List[TaskScheduleEntry], List[bool]]:
        unreach = unreachable_indices or set()
        depot = len(tasks) + 1
        budget = 16000
        visited = 0
        best_order: List[int] = []
        best_records: List[Tuple[int, float, float, float, float, bool, float, float]] = []
        best_rank: Tuple[object, ...] = (-1, -1, -1, -1, float("-inf"), ())

        def task_load(index: int) -> Tuple[float, float]:
            spec = tasks[index].rubbish_spec
            return (
                (spec.estimated_weight_kg or 0.0) if spec else 0.0,
                (spec.estimated_volume_m3 or 0.0) if spec else 0.0,
            )

        def rank_for(
            order: List[int],
            records: List[Tuple[int, float, float, float, float, bool, float, float]],
            elapsed: float,
            current_node: int,
        ) -> Tuple[object, ...]:
            finish = elapsed + tm[current_node][depot] / 60.0
            priority_position_score = sum(
                tasks[index].priority * (len(order) - position)
                for position, index in enumerate(order)
            )
            return (
                len(order),
                sum(tasks[i].priority for i in order),
                # A policy may suggest the next stop, but serving more work and
                # retaining task priority always take precedence over that hint.
                int(bool(order) and tasks[order[0]].task_id == preferred_task_id),
                priority_position_score,
                -finish,
                tuple(-i for i in order),
            )

        initial_candidates = tuple(i for i in range(len(tasks)) if i not in unreach)
        if not initial_candidates:
            return [], [], []

        def greedy_schedule(prefer_first: bool = False):
            order: List[int] = []
            records: List[Tuple[int, float, float, float, float, bool, float, float]] = []
            remaining = set(initial_candidates)
            elapsed = start_elapsed_min
            current_node = 0
            load_kg = machine.current_payload_kg
            load_vol = machine.current_payload_volume_m3
            while remaining:
                choices = []
                for i in remaining:
                    task = tasks[i]
                    weight, volume = task_load(i)
                    dump = (
                        load_kg + weight > machine.max_payload_kg
                        or load_vol + volume > machine.max_payload_volume_m3
                    )
                    travel = (
                        (tm[current_node][depot] + tm[depot][i + 1])
                        if dump
                        else tm[current_node][i + 1]
                    )
                    if travel >= 1e8:
                        continue
                    arrival = elapsed + travel / 60.0
                    start = max(arrival, task.time_window_start_min)
                    end = start + task.service_duration_sec / 60.0
                    if end <= task.time_window_end_min + 1e-9:
                        slack = task.time_window_end_min - end
                        choices.append(
                            (
                                (
                                    int(prefer_first and not order and task.task_id != preferred_task_id),
                                    -task.priority, slack, travel, i,
                                ),
                                i,
                                arrival,
                                start,
                                end,
                                dump,
                                weight,
                                volume,
                            )
                        )
                if not choices:
                    break
                _, i, arrival, start, end, dump, weight, volume = min(
                    choices, key=lambda c: c[0]
                )
                if dump:
                    load_kg = 0.0
                    load_vol = 0.0
                    current_node = depot
                load_kg += weight
                load_vol += volume
                records.append(
                    (
                        i,
                        arrival,
                        start,
                        end,
                        max(0.0, start - arrival),
                        dump,
                        load_kg,
                        load_vol,
                    )
                )
                order.append(i)
                remaining.remove(i)
                elapsed = end
                current_node = i + 1
            return order, records, elapsed, current_node

        if len(initial_candidates) > 9:
            baseline = greedy_schedule()
            best_order, best_records = baseline[:2]
            if preferred_task_id in {tasks[i].task_id for i in initial_candidates}:
                preferred = greedy_schedule(prefer_first=True)
                if rank_for(*preferred) > rank_for(*baseline):
                    best_order, best_records = preferred[:2]
        else:
            if preferred_task_id in {tasks[i].task_id for i in initial_candidates}:
                # Seed the bounded search so a feasible late-index suggestion
                # can still be considered if the visit budget is exhausted.
                preferred = greedy_schedule(prefer_first=True)
                best_order, best_records = preferred[:2]
                best_rank = rank_for(*preferred)

            def visit(
                remaining: Tuple[int, ...],
                order: List[int],
                records: List[Tuple[int, float, float, float, float, bool, float, float]],
                elapsed: float,
                current_node: int,
                load_kg: float,
                load_vol: float,
            ) -> None:
                nonlocal visited, best_order, best_records, best_rank
                if visited >= budget:
                    return
                visited += 1
                rank = rank_for(order, records, elapsed, current_node)
                if rank > best_rank:
                    best_rank = rank
                    best_order = list(order)
                    best_records = list(records)
                possible = []
                for i in remaining:
                    task = tasks[i]
                    weight, volume = task_load(i)
                    dump = (
                        load_kg + weight > machine.max_payload_kg
                        or load_vol + volume > machine.max_payload_volume_m3
                    )
                    travel = (
                        (tm[current_node][depot] + tm[depot][i + 1])
                        if dump
                        else tm[current_node][i + 1]
                    )
                    if travel >= 1e8:
                        continue
                    arrival = elapsed + travel / 60.0
                    start = max(arrival, task.time_window_start_min)
                    end = start + task.service_duration_sec / 60.0
                    if end <= task.time_window_end_min + 1e-9:
                        possible.append(
                            (
                                tasks[i].time_window_end_min,
                                -task.priority,
                                travel,
                                i,
                                arrival,
                                start,
                                end,
                                dump,
                                weight,
                                volume,
                            )
                        )
                for _, _, _, i, arrival, start, end, dump, weight, volume in sorted(
                    possible
                ):
                    next_kg = 0.0 if dump else load_kg
                    next_vol = 0.0 if dump else load_vol
                    next_kg += weight
                    next_vol += volume
                    visit(
                        tuple(j for j in remaining if j != i),
                        order + [i],
                        records
                        + [
                            (
                                i,
                                arrival,
                                start,
                                end,
                                max(0.0, start - arrival),
                                dump,
                                next_kg,
                                next_vol,
                            )
                        ],
                        end,
                        i + 1,
                        next_kg,
                        next_vol,
                    )

            visit(
                initial_candidates,
                [],
                [],
                start_elapsed_min,
                0,
                machine.current_payload_kg,
                machine.current_payload_volume_m3,
            )

        schedule = [
            TaskScheduleEntry(
                task_id=tasks[i].task_id,
                arrival_min=round(arrival, 3),
                service_start_min=round(start, 3),
                service_end_min=round(end, 3),
                waiting_min=round(waiting, 3),
                window_start_min=tasks[i].time_window_start_min,
                window_end_min=tasks[i].time_window_end_min,
                priority=tasks[i].priority,
                disposal_before=dump,
            )
            for i, arrival, start, end, waiting, dump, _, _ in best_records
        ]
        disposal_flags = [record[5] for record in best_records]
        return best_order, schedule, disposal_flags
