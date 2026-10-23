"""Local simulation-planning parameters; never restores live battery or payload."""
import json
from math import isfinite
from pathlib import Path

from .models import MachineProfile


PARAMETERS = frozenset({"working_width_m", "clearance_width_m", "sweep_speed_mps",
    "cruise_speed_mps", "battery_capacity_kwh", "energy_consumption_kwh_per_km_sweep",
    "energy_consumption_kwh_per_km_travel", "depot_service_minutes", "recharged_battery_percent"})


def validate_parameters(machine, parameters):
    if not isinstance(parameters, dict) or not parameters or set(parameters) - PARAMETERS:
        raise ValueError("Provide only supported planning parameters: " + ", ".join(sorted(PARAMETERS)))
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
           for value in parameters.values()):
        raise ValueError("Planning parameters must be finite numeric values.")
    values = machine.model_dump()
    values.update(parameters)
    try:
        return MachineProfile.model_validate(values)
    except ValueError as exc:
        raise ValueError("Planning parameters violate machine limits: " + str(exc)) from exc


class PlanningProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.parameters = {}
        self.last_error = None

    def restore(self, machines):
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Expected an object keyed by machine ID.")
            invalid = []
            for machine_id, parameters in data.items():
                if machine_id not in machines:
                    invalid.append(machine_id)
                    continue
                try:
                    updated = validate_parameters(machines[machine_id], parameters)
                except ValueError:
                    invalid.append(machine_id)
                    continue
                updated.planning_revision = machines[machine_id].planning_revision + 1
                machines[machine_id] = updated
                self.parameters[machine_id] = {key: getattr(updated, key) for key in PARAMETERS}
            if invalid:
                self.last_error = f"Planning profile schema error: skipped {len(invalid)} invalid or unknown machine profile(s)."
        except (OSError, ValueError, TypeError) as exc:
            self.last_error = f"Could not restore planning profiles: {type(exc).__name__}"

    def save(self, machine):
        next_parameters = dict(self.parameters)
        next_parameters[machine.machine_id] = {key: getattr(machine, key) for key in PARAMETERS}
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(next_parameters, indent=2, allow_nan=False), encoding="utf-8")
                temporary.replace(self.path)
            except (OSError, ValueError) as exc:
                self.last_error = f"Could not save planning profiles: {type(exc).__name__}"
                raise ValueError(self.last_error) from exc
        self.parameters = next_parameters
        self.last_error = None
