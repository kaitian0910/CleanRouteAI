import type { ExecutionState, RouteCandidate, TaskItem, TelemetryFrame } from "./types/api";

export interface ExecutionSnapshot {
  route: RouteCandidate;
  metrics: { execution_state: ExecutionState; telemetry_frames: TelemetryFrame[] };
}

export class RouteApiError extends Error {
  constructor(message: string, public readonly status: number) { super(message); }
}

// Serial requests keep simulation progress on the server. Stopping invalidates UI
// callbacks, while settle() waits for any already accepted server step to finish.
export class RouteExecutionSession {
  private revision = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private inFlight: Promise<void> = Promise.resolve();
  constructor(private apiBase: string, private token: string,
    private request: typeof fetch = (input, init) => globalThis.fetch(input, init)) {}

  stop() { this.revision++; clearTimeout(this.timer); }
  async settle() { await this.inFlight; }

  async json(path: string, body?: unknown) {
    const response = await this.request(`${this.apiBase}/api${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const data = await response.json();
    if (!response.ok) throw new RouteApiError(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`, response.status);
    return data;
  }

  async cancelRoute(routeId: string) {
    try {
      return await this.json(`/routes/${routeId}/cancel`, {});
    } catch (error) {
      // Development reloads erase in-memory routes. A confirmed missing route
      // must not prevent a fresh plan; all other control failures still block it.
      if (error instanceof RouteApiError && error.status === 404) return null;
      throw error;
    }
  }

  async snapshot(routeId: string): Promise<ExecutionSnapshot> {
    const [route, metrics] = await Promise.all([
      this.json(`/routes/${routeId}`), this.json(`/routes/${routeId}/metrics`),
    ]);
    return { route, metrics };
  }

  start(route: RouteCandidate, onUpdate: (snapshot: ExecutionSnapshot) => void,
        onError: (error: Error) => void, delayMs = 1000) {
    this.stop();
    const revision = this.revision;
    const prior = this.inFlight;
    const current = () => revision === this.revision;
    const tick = () => {
      if (!current()) return;
      this.inFlight = (async () => {
        await prior;
        if (!current()) return;
        try {
          // Reconcile with the server before advancing, including after a lost
          // response or refresh. A failed request may already have completed.
          const before = await this.snapshot(route.route_id);
          if (!current()) return;
          const state = before.metrics.execution_state;
          if (state === "COMPLETED") { onUpdate(before); return; }
          if (!["READY", "PAUSED", "EXECUTING"].includes(state)) {
            onUpdate(before);
            throw new Error(`Execution cannot continue while the server is ${state}. Refresh the route and resolve its current state.`);
          }
          const action = state === "READY" ? "execute" : "resume";
          const completed = before.route.segments.filter(segment => segment.completed).length;
          await this.json(`/routes/${route.route_id}/${action}`, {
            stop_after_segments: 1,
            expected_completed_segments: completed,
            idempotency_key: `${route.route_id}:step:${completed}`,
          });
          const snapshot = await this.snapshot(route.route_id);
          if (!current()) return;
          onUpdate(snapshot);
          if (snapshot.metrics.execution_state === "PAUSED") {
            this.timer = setTimeout(tick, delayMs);
          }
        } catch (error) {
          if (current()) onError(error instanceof Error ? error : new Error(String(error)));
        }
      })();
    };
    tick();
  }
}

export function collectedWasteStats(tasks: TaskItem[]) {
  const stats = { plasticAndCans: 0, paperAndPackaging: 0, binsEmptied: 0, bulkyItems: 0, others: 0, totalWeightKg: 0, unknownWeightCollections: 0 };
  const plasticAndCanLabels = new Set(["bottle", "cap", "plasticbag", "wrapper", "can", "plasticcup", "plasticbagwrapper", "bottlecaplid"]);
  const paperLabels = new Set(["paper", "cardboard", "papercardboard"]);
  for (const task of tasks.filter(item => item.completed)) {
    const label = task.waste_observation?.waste_class?.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (task.task_type === "OVERSIZED_RUBBISH") stats.bulkyItems++;
    else if (label && paperLabels.has(label)) stats.paperAndPackaging++;
    else if (label && plasticAndCanLabels.has(label)) stats.plasticAndCans++;
    else if (task.waste_observation) stats.others++;
    else if (task.task_type === "RUBBISH_COLLECTION") stats.binsEmptied++;
    if (["RUBBISH_COLLECTION", "OVERSIZED_RUBBISH"].includes(task.task_type) && task.rubbish_spec?.estimated_weight_kg == null) stats.unknownWeightCollections++;
    stats.totalWeightKg += task.rubbish_spec?.estimated_weight_kg || 0;
  }
  stats.totalWeightKg = Math.round(stats.totalWeightKg * 1000) / 1000;
  return stats;
}
