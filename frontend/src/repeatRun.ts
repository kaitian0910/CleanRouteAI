import type { RouteCandidate } from "./types/api";

// A new routine run repeats the area and ordinary bins. A collected object
// needs a new observation before it can become work again.
export function canRepeatRoute(route: RouteCandidate | null): boolean {
  return !!route && route.status === "COMPLETED" && route.routing_provenance !== "SIMULATED" && (
    (route.operating_boundary?.vertices.length ?? 0) >= 3 || route.tasks.some(task =>
      task.task_type === "RUBBISH_COLLECTION" && !task.waste_observation && !task.rubbish_spec?.is_oversized)
  );
}
