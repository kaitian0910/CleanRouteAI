"""Local daily planning feedback. Stores measurements; never trains policy weights."""
from __future__ import annotations

import hashlib
import json
from math import isfinite
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from statistics import median
from uuid import uuid4


def _stable(value):
    """Ignore generated IDs while retaining geometry and operational constraints."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items()
                if not k.endswith("_id") and k != "created_at"}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def layout_fingerprint(request, machine, policy, environment, *, completed_segments=None):
    # Identical area alone is insufficient: battery, machine capabilities, bin
    # workload, depot, obstacles, terrain and KPI limits affect completion time.
    inputs = {"boundary": request.operating_boundary, "start": request.start_location,
              "depot": request.disposal_location, "exclusions": request.exclusion_zones,
              "tasks": request.tasks, "machine": machine, "policy": policy,
              "environment": environment, "cycles": request.enable_depot_cycles,
              "mission_elapsed_min": request.mission_elapsed_min,
              "completed_segments": [segment for segment in (completed_segments or []) if segment.completed]}
    data = json.dumps(_stable(inputs), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()[:20]


def sweep_context_fingerprint(request, machine, policy, environment, *, mode=None):
    """Match a reusable search hint, never a resource or feasibility result.

    Repeats normally start at a different position and charge level. Keep their
    static workload together while leaving the full comparison fingerprint above
    unchanged. Every suggested setting must be regenerated and evaluated.
    """
    robot = machine.model_dump(mode="json")
    for key in ("current_battery_percent", "current_payload_kg", "current_payload_volume_m3"):
        robot.pop(key, None)
    tasks = [task.model_dump(mode="json") for task in request.tasks]
    for task in tasks:
        task["completed"] = False
        task.pop("idempotency_key", None)
    inputs = {"boundary": request.operating_boundary, "depot": request.disposal_location,
              "exclusions": request.exclusion_zones, "tasks": tasks, "machine": robot,
              "policy": policy, "environment": environment, "cycles": request.enable_depot_cycles,
              "mode": getattr(mode, "value", mode)}
    data = json.dumps(_stable(inputs), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()[:20]


class MissionHistory:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = RLock()
        self._records = []
        self.last_error = None
        if path and path.exists():
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    raise ValueError("history must contain a list of records")
                valid = [r for r in records if self._valid_record(r)]
                self._records = valid[-1000:]
                rejected = len(records) - len(valid)
                if rejected:
                    self.last_error = f"Mission history schema error: skipped {rejected} invalid record(s); valid records were retained."
            except (OSError, ValueError, TypeError) as exc:
                self.last_error = f"Could not read mission history: {type(exc).__name__}"

    @staticmethod
    def _valid_record(record):
        if not isinstance(record, dict):
            return False
        required_text = ("route_id", "job_id", "machine_id", "layout_fingerprint", "recorded_at", "status")
        if any(not isinstance(record.get(key), str) or not record[key].strip() for key in required_text):
            return False
        for key in ("estimated_completion_minutes", "actual_completion_minutes", "selected_sweep_angle_deg"):
            value = record.get(key)
            if value is None and key != "estimated_completion_minutes":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                return False
            if key != "selected_sweep_angle_deg" and value < 0:
                return False
        return True

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._records, indent=2), encoding="utf-8")
            temporary.replace(self.path)
            self.last_error = None
        except OSError as exc:
            self.last_error = f"Could not save mission history: {type(exc).__name__}"

    def records(self, machine_id=None):
        with self._lock:
            return [dict(r) for r in reversed(self._records)
                    if machine_id is None or r.get("machine_id") == machine_id][:100]

    def preferred_angle(self, fingerprint, context_fingerprint=None):
        with self._lock:
            comparable = [r for r in self._records if (r.get("layout_fingerprint") == fingerprint
                          or (context_fingerprint is not None and r.get("sweep_context_fingerprint") == context_fingerprint))
                          and r.get("status") in {"PLANNED_FEASIBLE", "COMPLETED_SIMULATION"}]
            if not comparable:
                return None
            # A warm start is only a setting to reevaluate. It grants no safety
            # exemption and does not assert that the old route remains optimal.
            completed = [r for r in comparable if r.get("status") == "COMPLETED_SIMULATION"
                         and isinstance(r.get("actual_completion_minutes"), (int, float))
                         and isfinite(r["actual_completion_minutes"]) and r["actual_completion_minutes"] >= 0
                         and r.get("selected_sweep_angle_deg") is not None]
            if completed:
                angles = {r["selected_sweep_angle_deg"] for r in completed}
                return min(angles, key=lambda angle: (median(r["actual_completion_minutes"] for r in completed
                    if r["selected_sweep_angle_deg"] == angle), angle))
            best = min(comparable, key=lambda r: r["estimated_completion_minutes"])
            return best.get("selected_sweep_angle_deg")

    def record_plan(self, route, fingerprint, context_fingerprint=None):
        if not route.kpi_evaluation or not route.kpi_evaluation.passed:
            return
        with self._lock:
            if any(r["route_id"] == route.route_id for r in self._records):
                return
            coverage = route.kpi_evaluation.metrics.get("coverage_percent")
            self._records.append({
                "record_id": uuid4().hex, "job_id": route.job_id, "route_id": route.route_id,
                "recorded_at": datetime.now(timezone.utc).isoformat(), "machine_id": route.machine_id,
                "layout_fingerprint": fingerprint, "mission_elapsed_minutes": route.mission_elapsed_min,
                "sweep_context_fingerprint": context_fingerprint,
                "estimated_completion_minutes": route.estimated_completion_minutes,
                "actual_completion_minutes": None,
                "coverage_percent": coverage.actual if coverage else None,
                "outing_count": route.outing_count, "depot_visits": route.depot_visits,
                "charging_duration_minutes": route.charging_duration_minutes,
                "selected_sweep_angle_deg": route.sweep_angle_deg,
                "selected_overlap_ratio": route.overlap_ratio, "status": "PLANNED_FEASIBLE",
            })
            self._records = self._records[-1000:]
            self._save()

    def record_completion(self, route, elapsed):
        with self._lock:
            for record in self._records:
                if record["route_id"] == route.route_id:
                    record["actual_completion_minutes"] = elapsed
                    record["mission_elapsed_minutes"] = elapsed
                    record["status"] = "COMPLETED_SIMULATION"
                    self._save()
                    break
