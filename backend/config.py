import copy
import json
import os
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional
import pydantic
from pydantic import BaseModel as _PydanticBaseModel, Field

PYDANTIC_V2 = int(getattr(pydantic, "__version__", "1").split(".")[0]) >= 2


class CompatBaseModel(_PydanticBaseModel):
    """Pydantic v1 / v2 compatibility base model."""

    if not PYDANTIC_V2:
        def model_dump(self, *, mode: str = "python", exclude: Any = None, **kwargs: Any) -> Dict[str, Any]:
            if mode == "json":
                return json.loads(self.json(exclude=exclude, **kwargs))
            return self.dict(exclude=exclude, **kwargs)

        def model_copy(self, *, update: Optional[Dict[str, Any]] = None, deep: bool = False) -> Any:
            copied = self.copy(update=update, deep=deep)
            return self.__class__.parse_obj(copied.dict())

        @classmethod
        def model_validate(cls, obj: Any) -> Any:
            if isinstance(obj, cls):
                return obj
            return cls.parse_obj(obj)


class KPIPolicyConfig(CompatBaseModel):
    """Operator-configurable, versioned KPI acceptance policy."""
    policy_version: str = Field(default="v1.3.0")
    min_coverage_percent: float = Field(default=92.0, ge=0.0, le=100.0)
    max_missed_coverage_percent: float = Field(default=8.0, ge=0.0, le=100.0)
    max_estimated_duration_minutes: float = Field(default=90.0, gt=0.0)
    max_travel_distance_m: float = Field(default=15000.0, gt=0.0)
    min_energy_reserve_percent: float = Field(default=20.0, ge=5.0, le=95.0)
    max_payload_utilization_percent: float = Field(default=95.0, ge=10.0, le=100.0)
    required_collection_completion_rate: float = Field(default=100.0, ge=0.0, le=100.0)
    max_route_deviation_m: float = Field(default=5.0, gt=0.0)
    max_repeated_coverage_percent: float = Field(default=18.0, ge=0.0, le=100.0)
    max_depot_cycles: int = Field(default=32, ge=0, le=200)


class OptimizationLoopConfig(CompatBaseModel):
    """Bounded feedback loop parameters."""
    max_iterations: int = Field(default=6, ge=1, le=20)
    timeout_seconds: float = Field(default=15.0, gt=0.5)
    min_improvement_epsilon: float = Field(default=0.25, ge=0.0)
    candidate_sweep_angles_deg: List[float] = Field(
        default_factory=lambda: [0.0, 90.0, 45.0, 135.0, 30.0]
    )
    overlap_step_increment: float = Field(default=0.03, ge=0.01, le=0.25)
    max_depot_snap_distance_m: float = Field(default=18.0, ge=0.0, le=100.0)


class RLRewardConfig(CompatBaseModel):
    """CleanRoute AI KPI reward v1, separate from the G3 grid-training reward."""
    coverage: float = 40.0
    assignment: float = 25.0
    battery: float = 10.0
    distance: float = 10.0
    duration: float = 10.0
    repeated: float = 5.0
    hard_failure: float = 25.0


class AppSettings(CompatBaseModel):
    app_name: str = "CleanRoute AI"
    environment: str = "development"
    database_url: Optional[str] = None
    valhalla_url: str = "http://localhost:8002"
    nominatim_url: Optional[str] = None
    local_llm_url: Optional[str] = None  # Optional self-hosted Ollama endpoint
    allow_labeled_demo_routing: bool = True
    enable_hardware_ros2_adapter: bool = False  # Never enable real actuation by default
    enable_default_campus_terrain_layer: bool = True
    telemetry_retention_days: int = 90
    operator_token: Optional[str] = "dev-operator-token"
    supervisor_token: Optional[str] = "dev-supervisor-token"
    admin_token: Optional[str] = "dev-admin-token"
    internal_service_token: Optional[str] = "dev-service-token"
    cors_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://localhost:3000",
            "http://localhost:8000",
            "*",
        ]
    )
    kpi_policy: KPIPolicyConfig = Field(default_factory=KPIPolicyConfig)
    optimization: OptimizationLoopConfig = Field(default_factory=OptimizationLoopConfig)
    rl_reward: RLRewardConfig = Field(default_factory=RLRewardConfig)


def load_settings(environ=None) -> AppSettings:
    """Read documented local configuration without exposing credential values."""
    source = os.environ if environ is None else environ
    values = {}

    def read(name):
        value = source[name]
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string environment value.")
        return value

    def http_url(value, name, *, origin=False):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
                raise ValueError()
            parsed.port  # Also validate a supplied port without echoing its value.
            if origin and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise ValueError()
        except ValueError:
            raise ValueError(f"{name} requires an HTTP or HTTPS {'origin' if origin else 'URL'} without embedded credentials.") from None
        return value

    strings = {"CLEANROUTEAI_ENVIRONMENT": "environment", "CLEANROUTEAI_OPERATOR_TOKEN": "operator_token",
        "CLEANROUTEAI_SUPERVISOR_TOKEN": "supervisor_token", "CLEANROUTEAI_ADMIN_TOKEN": "admin_token",
        "CLEANROUTEAI_INTERNAL_SERVICE_TOKEN": "internal_service_token"}
    for name, field in strings.items():
        if name in source:
            value = read(name)
            if field == "environment":
                if not value.strip():
                    raise ValueError(f"{name} cannot be empty.")
                values[field] = value.strip()
            else:
                values[field] = value if value.strip() else None
    if "CLEANROUTEAI_VALHALLA_URL" in source:
        values["valhalla_url"] = http_url(read("CLEANROUTEAI_VALHALLA_URL").strip(), "CLEANROUTEAI_VALHALLA_URL")
    booleans = {"CLEANROUTEAI_ALLOW_LABELED_DEMO_ROUTING": "allow_labeled_demo_routing",
                "CLEANROUTEAI_ENABLE_DEFAULT_CAMPUS_TERRAIN_LAYER": "enable_default_campus_terrain_layer"}
    for name, field in booleans.items():
        if name in source:
            value = read(name).strip().lower()
            if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError(f"{name} must be a boolean (true/false, 1/0, yes/no, or on/off).")
            values[field] = value in {"true", "1", "yes", "on"}
    if "CLEANROUTEAI_CORS_ORIGINS" in source:
        raw = read("CLEANROUTEAI_CORS_ORIGINS").strip()
        try:
            origins = json.loads(raw) if raw.startswith("[") else [part.strip() for part in raw.split(",") if part.strip()]
        except ValueError:
            raise ValueError("CLEANROUTEAI_CORS_ORIGINS must be a JSON string list or comma-separated HTTP(S) origins.") from None
        if not isinstance(origins, list) or any(not isinstance(value, str) for value in origins):
            raise ValueError("CLEANROUTEAI_CORS_ORIGINS must contain only origin strings.")
        values["cors_origins"] = [value if value == "*" else http_url(value, "CLEANROUTEAI_CORS_ORIGINS", origin=True) for value in origins]
    # Environment variables deliberately cannot enable a hardware adapter.
    return AppSettings(**values)


settings = load_settings()
