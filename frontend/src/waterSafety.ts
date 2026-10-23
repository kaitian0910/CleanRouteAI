import type { Coordinate, TerrainFeature } from "./types/api";

type Point = [number, number];
const boundsCache = new WeakMap<TerrainFeature, { minLat: number; maxLat: number; minLon: number; maxLon: number }>();

function nearBounds(path: Coordinate[], feature: TerrainFeature, extraClearanceM = 0): boolean {
  if (!path.length || !feature.polygon.vertices.length) return false;
  let bounds = boundsCache.get(feature);
  if (!bounds) {
    const vertices = feature.polygon.vertices;
    bounds = { minLat: Math.min(...vertices.map(point => point.lat)), maxLat: Math.max(...vertices.map(point => point.lat)),
      minLon: Math.min(...vertices.map(point => point.lon)), maxLon: Math.max(...vertices.map(point => point.lon)) };
    boundsCache.set(feature, bounds);
  }
  const margin = Math.max(0, feature.clearance_buffer_m || 0) + extraClearanceM;
  const latMargin = margin / 110540, lonMargin = margin / (111320 * Math.max(0.01, Math.cos(path[0].lat * Math.PI / 180)));
  return Math.max(...path.map(point => point.lat)) >= bounds.minLat - latMargin &&
    Math.min(...path.map(point => point.lat)) <= bounds.maxLat + latMargin &&
    Math.max(...path.map(point => point.lon)) >= bounds.minLon - lonMargin &&
    Math.min(...path.map(point => point.lon)) <= bounds.maxLon + lonMargin;
}

function local(point: Coordinate, origin: Coordinate): Point {
  return [(point.lon - origin.lon) * 111320 * Math.cos(origin.lat * Math.PI / 180),
    (point.lat - origin.lat) * 110540];
}

function pointSegmentDistance(point: Point, a: Point, b: Point): number {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const length = dx * dx + dy * dy;
  const fraction = length ? Math.max(0, Math.min(1,
    ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length)) : 0;
  return Math.hypot(point[0] - a[0] - fraction * dx, point[1] - a[1] - fraction * dy);
}

function cross(a: Point, b: Point, c: Point): number {
  return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
}

function segmentsDistance(a: Point, b: Point, c: Point, d: Point): number {
  const abC = cross(a, b, c), abD = cross(a, b, d);
  const cdA = cross(c, d, a), cdB = cross(c, d, b);
  // Proper crossings, including an intersection in the middle of long segments.
  if (((abC > 0 && abD < 0) || (abC < 0 && abD > 0)) &&
      ((cdA > 0 && cdB < 0) || (cdA < 0 && cdB > 0))) return 0;
  return Math.min(pointSegmentDistance(a, c, d), pointSegmentDistance(b, c, d),
    pointSegmentDistance(c, a, b), pointSegmentDistance(d, a, b));
}

function inside(point: Point, polygon: Point[]): boolean {
  let result = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[i], b = polygon[j];
    if (pointSegmentDistance(point, a, b) < 1e-6) return true;
    if ((a[1] > point[1]) !== (b[1] > point[1]) &&
        point[0] < (b[0] - a[0]) * (point[1] - a[1]) / (b[1] - a[1]) + a[0]) result = !result;
  }
  return result;
}

/** Mapped water remains a safety layer when a custom site omits it. */
export function combineWaterFeatures(features: TerrainFeature[], mapped: TerrainFeature[] = []): TerrainFeature[] {
  const byId = new Map<string, TerrainFeature>();
  for (const feature of [...features, ...mapped]) {
    if (feature.feature_type === "WATER_BODY" && feature.polygon.vertices.length >= 3) {
      byId.set(feature.feature_id, feature);
    }
  }
  return [...byId.values()];
}

export function pointTouchesWater(point: Coordinate, feature: TerrainFeature, extraClearanceM = 0): boolean {
  if (!nearBounds([point], feature, extraClearanceM)) return false;
  const polygon = feature.polygon.vertices.map(vertex => local(vertex, point));
  if (polygon.length < 3) return false;
  if (inside([0, 0], polygon)) return true;
  const clearance = Math.max(0, feature.clearance_buffer_m || 0) + extraClearanceM;
  return polygon.some((vertex, index) => pointSegmentDistance([0, 0], vertex,
    polygon[(index + 1) % polygon.length]) <= clearance + 1e-6);
}

export function pathTouchesWater(path: Coordinate[], feature: TerrainFeature): boolean {
  if (!path.length || feature.polygon.vertices.length < 3 || !nearBounds(path, feature)) return false;
  if (path.some(point => pointTouchesWater(point, feature))) return true;
  const origin = path[0];
  const polygon = feature.polygon.vertices.map(point => local(point, origin));
  const points = path.map(point => local(point, origin));
  const clearance = Math.max(0, feature.clearance_buffer_m || 0);
  return points.slice(1).some((b, index) => polygon.some((c, edge) =>
    segmentsDistance(points[index], b, c, polygon[(edge + 1) % polygon.length]) <= clearance + 1e-6));
}

export function boundaryTouchesWater(boundary: Coordinate[], feature: TerrainFeature): boolean {
  if (boundary.length < 3 || feature.polygon.vertices.length < 3 || !nearBounds(boundary, feature)) return false;
  if (pathTouchesWater([...boundary, boundary[0]], feature)) return true;
  // A lake can be entirely inside the requested area, with neither boundary crossing the other.
  const origin = boundary[0];
  return inside(local(feature.polygon.vertices[0], origin), boundary.map(point => local(point, origin)));
}

/** Build the blocked-water demonstration from the mapped lake itself, not a guessed GPS rectangle. */
export function interiorWaterExample(feature: TerrainFeature | undefined): {
  center: Coordinate; boundary: Coordinate[]; startLocation: Coordinate; disposalLocation: Coordinate;
} | null {
  if (!feature || feature.polygon.vertices.length < 3) return null;
  const origin = feature.polygon.vertices[0], polygon = feature.polygon.vertices.map(point => local(point, origin));
  const minX = Math.min(...polygon.map(point => point[0])), maxX = Math.max(...polygon.map(point => point[0]));
  const minY = Math.min(...polygon.map(point => point[1])), maxY = Math.max(...polygon.map(point => point[1]));
  let best: Point | null = null, bestClearance = 0;
  for (let y = 1; y < 30; y++) for (let x = 1; x < 30; x++) {
    const candidate: Point = [minX + (maxX - minX) * x / 30, minY + (maxY - minY) * y / 30];
    if (!inside(candidate, polygon)) continue;
    const clearance = Math.min(...polygon.map((point, index) =>
      pointSegmentDistance(candidate, point, polygon[(index + 1) % polygon.length])));
    if (clearance > bestClearance) { best = candidate; bestClearance = clearance; }
  }
  if (!best || bestClearance < 1) return null;
  const centerPoint = best;
  const halfSize = Math.min(15, bestClearance / 3);
  const coordinate = (x: number, y: number): Coordinate => ({
    lat: origin.lat + (centerPoint[1] + y) / 110540,
    lon: origin.lon + (centerPoint[0] + x) / (111320 * Math.cos(origin.lat * Math.PI / 180)),
  });
  return { center: coordinate(0, 0), boundary: [coordinate(-halfSize, -halfSize), coordinate(halfSize, -halfSize),
    coordinate(halfSize, halfSize), coordinate(-halfSize, halfSize)],
    startLocation: coordinate(-halfSize / 2, 0), disposalLocation: coordinate(halfSize / 2, 0) };
}

/** The simplified offline scanline preview has no safe shoreline-detour solver. */
export function offlineWaterConflict(params: {
  boundary: Coordinate[]; start: Coordinate; depot: Coordinate; taskLocations: Coordinate[];
  waterFeatures: TerrainFeature[]; segmentPaths?: Coordinate[][];
}): string | null {
  const { boundary, start, depot, taskLocations, waterFeatures, segmentPaths = [] } = params;
  const portals = [start, ...taskLocations, depot];
  const feature = waterFeatures.find(water => boundaryTouchesWater(boundary, water) ||
    portals.some(point => pointTouchesWater(point, water)) ||
    // Offline connectors must never jump across water between otherwise dry pins.
    portals.some((point, index) => portals.slice(index + 1).some(other => pathTouchesWater([point, other], water))) ||
    segmentPaths.some(path => pathTouchesWater(path, water)) ||
    (boundary.length > 0 && pathTouchesWater([start, boundary[0]], water)));
  return feature ? `Offline preview cannot safely route around mapped water (${feature.name}). Start the backend to exclude water and validate a route on dry land. Move any start, depot or collection pin placed in water.` : null;
}

/** Only for generated draw-area defaults; manually placed/imported pins are never moved. */
export function automaticDryLocations<T extends { start: Coordinate; depot: Coordinate }>(
  boundary: Coordinate[], proposed: T, waterFeatures: TerrainFeature[]
): T & { adjusted: boolean; noDryPoint: boolean } {
  if (boundary.length < 3 || !waterFeatures.length) return { ...proposed, adjusted: false, noDryPoint: false };
  const dry = (point: Coordinate) => !waterFeatures.some(water => pointTouchesWater(point, water, 1));
  if (dry(proposed.start) && dry(proposed.depot)) return { ...proposed, adjusted: false, noDryPoint: false };
  const minLat = Math.min(...boundary.map(point => point.lat)), maxLat = Math.max(...boundary.map(point => point.lat));
  const minLon = Math.min(...boundary.map(point => point.lon)), maxLon = Math.max(...boundary.map(point => point.lon));
  const origin = boundary[0], polygon = boundary.map(point => local(point, origin));
  const candidates: Coordinate[] = [];
  for (let y = 1; y < 40; y++) for (let x = 1; x < 40; x++) {
    const point = { lat: minLat + (maxLat - minLat) * y / 40, lon: minLon + (maxLon - minLon) * x / 40 };
    if (inside(local(point, origin), polygon) && dry(point)) candidates.push(point);
  }
  if (!candidates.length) return { ...proposed, adjusted: false, noDryPoint: true };
  const pick = (point: Coordinate) => dry(point) ? point : candidates.reduce((best, candidate) => {
    const a = local(candidate, point), b = local(best, point);
    return Math.hypot(...a) < Math.hypot(...b) ? candidate : best;
  }, candidates[0]);
  return { ...proposed, start: pick(proposed.start), depot: pick(proposed.depot), adjusted: true, noDryPoint: false };
}
