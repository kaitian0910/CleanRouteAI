import type { Coordinate } from "./types/api";

// Clearing or replacing a request invalidates every asynchronous continuation,
// including approval responses that arrive after the map has been reset.
export class PlanningSession {
  private revision = 0;
  private controller: AbortController | null = null;
  cancel() {
    this.revision += 1;
    this.controller?.abort();
    this.controller = null;
  }
  begin() {
    this.cancel();
    this.controller = new AbortController();
    const revision = this.revision;
    return { signal: this.controller.signal, isCurrent: () => revision === this.revision };
  }
  current() {
    const revision = this.revision;
    return () => revision === this.revision;
  }
}

export function apiErrorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((error) => `${error.loc?.join(".") || "Request"}: ${error.msg || "Invalid value"}`).join("; ");
  }
  return JSON.stringify(detail) || "Request failed";
}

export function areaLocations(vertices: Coordinate[]) {
  const minLat = Math.min(...vertices.map(p => p.lat));
  const maxLat = Math.max(...vertices.map(p => p.lat));
  const minLon = Math.min(...vertices.map(p => p.lon));
  const maxLon = Math.max(...vertices.map(p => p.lon));
  const inside = (p: Coordinate) => {
    let result = false;
    for (let i = 0, j = vertices.length - 1; i < vertices.length; j = i++) {
      const a = vertices[i], b = vertices[j];
      if ((a.lat > p.lat) !== (b.lat > p.lat) &&
          p.lon < (b.lon - a.lon) * (p.lat - a.lat) / (b.lat - a.lat) + a.lon) result = !result;
    }
    return result;
  };
  const candidates: Coordinate[] = [];
  for (let y = 1; y < 20; y++) for (let x = 1; x < 20; x++) {
    const p = {lat: minLat + (maxLat - minLat) * y / 20, lon: minLon + (maxLon - minLon) * x / 20};
    if (inside(p)) candidates.push(p);
  }
  const pick = (x: number, y: number) => {
    const target = {lat: minLat + (maxLat - minLat) * y, lon: minLon + (maxLon - minLon) * x};
    return candidates.reduce((best, p) =>
      Math.hypot(p.lat-target.lat, p.lon-target.lon) < Math.hypot(best.lat-target.lat, best.lon-target.lon) ? p : best,
      candidates[0] || vertices[0]);
  };
  return {start: pick(0.15, 0.15), depot: pick(0.85, 0.15), taskA: pick(0.4, 0.5), taskB: pick(0.6, 0.5)};
}
