"""Frozen inference for the supplied G3 PPO checkpoint. Heavy imports are lazy."""
from __future__ import annotations

import json
import io
from types import SimpleNamespace
import math
import os
from pathlib import Path
from threading import Lock
from time import perf_counter
import zipfile


class G3Policy:
    def __init__(self, checkpoint=None):
        self.checkpoint = Path(checkpoint or os.environ.get("CLEANROUTEAI_RL_CHECKPOINT", Path(__file__).parent / "rl_assets" / "ppo"))
        self._model = None
        self._error = None
        self._lock = Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        if not self.checkpoint.exists():
            raise ValueError("PPO checkpoint missing. Set CLEANROUTEAI_RL_CHECKPOINT or restore backend/rl_assets/ppo/.")
        try:
            import numpy as np
            from gymnasium import spaces
            import torch
            from stable_baselines3.common.policies import ActorCriticPolicy
        except ImportError as exc:
            raise ValueError("Install backend/requirements-rl.txt to enable the supplied PPO checkpoint.") from exc
        if self.checkpoint.is_dir():
            metadata = json.loads((self.checkpoint / "model.json").read_text(encoding="utf-8"))
            if (metadata.get("format_version") != 1 or metadata.get("observation_dim") != 48
                    or metadata.get("action_count") != 14 or metadata.get("net_arch") != [64, 64]
                    or metadata.get("activation") != "Tanh"):
                raise ValueError("Incompatible extracted PPO model configuration.")
            weights = self.checkpoint / "policy.pth"
            timesteps = metadata.get("num_timesteps", 0)
        else:
            # Explicit legacy archive paths remain supported; the default
            # application path uses the extracted files above.
            with zipfile.ZipFile(self.checkpoint) as archive:
                metadata = json.loads(archive.read("data"))
                if (metadata.get("observation_space", {}).get("_shape") != [48]
                        or str(metadata.get("action_space", {}).get("n")) != "14"
                        or metadata.get("policy_kwargs") != {"net_arch": [64, 64]}):
                    raise ValueError("Incompatible G3 PPO checkpoint.")
                weights = io.BytesIO(archive.read("policy.pth"))
                timesteps = metadata.get("num_timesteps", 0)
        policy = ActorCriticPolicy(
            spaces.Box(0, 1, (48,), dtype=np.float32), spaces.Discrete(14),
            lr_schedule=lambda _: 0.0, net_arch=[64, 64], activation_fn=torch.nn.Tanh,
        ).to("cpu")
        policy.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True), strict=True)
        model = SimpleNamespace(policy=policy, num_timesteps=timesteps)
        model.policy.set_training_mode(False)
        self._model = model
        return model

    def _status(self, detail, active=False):
        return {"mode": "TRAINED_PPO" if active else "DETERMINISTIC_FALLBACK",
                "checkpoint_loaded": self._model is not None, "checkpoint": self.checkpoint.name,
                "observation_schema": "g3-six-bin-48-v1/local-meters-v1",
                "action_schema": "g3-dispatch-14-v1/direct-task-subset",
                "detail": detail, "training_enabled": False}

    def status(self):
        with self._lock:
            try:
                self._load()
                return self._status("G3 PPO checkpoint loaded for frozen six-bin dispatch inference. Mission compatibility is checked separately; no sweep-tuning or online training.", True)
            except Exception as exc:
                return self._status(f"PPO unavailable: {exc}")

    @staticmethod
    def encode(req, machine, completed_segments=None):
        if req.operating_boundary is not None:
            raise ValueError("This checkpoint selects bins, not area sweep settings.")
        if len(req.tasks) != 6 or any(task.task_type.value != "RUBBISH_COLLECTION" for task in req.tasks):
            raise ValueError("G3 requires exactly six collection bins with explicit bin telemetry.")
        if not req.start_location or not req.disposal_location:
            raise ValueError("G3 requires the current position and disposal depot.")
        if machine.current_payload_kg > 250 or machine.current_payload_kg < 0:
            raise ValueError("Current load is outside the trained 0–250 kg feature range.")
        if not 0 <= machine.current_battery_percent <= 100:
            raise ValueError("Battery is outside the trained 0–100 percent feature range.")
        # One G3 step is a collection/depot decision, not every transit segment.
        step = sum(segment.completed and segment.segment_type in {"COLLECTION_STOP", "RETURN_TO_BASE"}
                   for segment in (completed_segments or []))
        if step > 200:
            raise ValueError("Recorded simulation steps exceed the trained 200-step horizon.")
        for task in req.tasks:
            if task.rl_bin_state is None or task.rubbish_spec is None or task.rubbish_spec.estimated_weight_kg is None:
                raise ValueError("Each bin needs rl_bin_state and an explicit mass estimate; missing traffic/fill data is not invented.")
            if not task.completed and not math.isclose(task.rl_bin_state.fill_kg, task.rubbish_spec.estimated_weight_kg, abs_tol=0.01):
                raise ValueError("Bin fill_kg and the collection mass estimate disagree.")
        depot = req.disposal_location
        points = [req.start_location.to_local_meters(depot)] + [task.location.to_local_meters(depot) for task in req.tasks]
        if any(x < -0.01 or y < -0.01 for x, y in points):
            raise ValueError("Grid adapter requires bins/current position northeast of the depot, matching G3's (0,0) depot.")
        scale = max(max(x, y) for x, y in points)
        if scale <= 0.01:
            raise ValueError("Bin locations must span a nonzero simulated grid.")
        if len({(round(x, 2), round(y, 2)) for x, y in points[1:]}) != 6:
            raise ValueError("The six G3 bin positions must be distinct.")
        normalized = [(max(0.0, x)/scale, max(0.0, y)/scale) for x, y in points]
        vehicle, bins = normalized[0], normalized[1:]
        fills = [0.0 if task.completed else min(1.0, task.rl_bin_state.fill_kg/100) for task in req.tasks]
        observation = [*vehicle, machine.current_battery_percent/100, machine.current_payload_kg/250,
                       *fills, *[p[0] for p in bins], *[p[1] for p in bins],
                       *[task.rl_bin_state.traffic_multiplier/2 for task in req.tasks],
                       *[float(task.rl_bin_state.accident) for task in req.tasks],
                       *[math.dist(vehicle, point)/math.sqrt(2) for point in bins],
                       *[float(task.completed) for task in req.tasks], step/200,
                       math.hypot(*vehicle)/math.sqrt(2)]
        if len(observation) != 48 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in observation):
            raise ValueError("Encoded observation violates the G3 Box(0,1,48) contract.")
        return observation

    def recommend(self, req, machine, completed_segments=None):
        def fallback(reason):
            return {"status": self._status(reason), "reason": reason, "observation": [], "proposed_action": None, "inference_ms": 0.0}
        if not req.use_rl:
            return fallback("Deterministic baseline requested; PPO inference disabled for this plan.")
        try:
            observation = self.encode(req, machine, completed_segments)
            if machine.current_battery_percent <= 20:
                return fallback("Battery is at the G3 return threshold; deterministic reserve/recovery logic applies.")
            available = [2*i for i, task in enumerate(req.tasks) if not task.completed
                         and task.rl_bin_state.fill_kg > 0 and task.rubbish_spec.estimated_weight_kg <= machine.max_payload_kg]
            if not available:
                return fallback("No pending capacity-feasible direct-bin action is available.")
            with self._lock:
                model = self._load()
                import numpy as np
                import torch
                started = perf_counter()
                with torch.inference_mode():
                    tensor, _ = model.policy.obs_to_tensor(np.asarray(observation, dtype=np.float32))
                    probabilities = model.policy.get_distribution(tensor).distribution.probs[0].cpu().tolist()
                if len(probabilities) != 14 or any(not math.isfinite(v) or v < 0 for v in probabilities):
                    raise ValueError("Invalid policy probability distribution.")
                if sum(probabilities[i] for i in available) <= 0:
                    raise ValueError("No probability mass on supported direct collection actions.")
                raw_action = max(range(14), key=lambda i: probabilities[i])
                action = max(available, key=lambda i: probabilities[i])
                inference_ms = (perf_counter()-started)*1000
            task = req.tasks[action//2]
            reason = ("Frozen PPO dispatch with direct-task action restriction. Geographic features use a common-scale local grid (domain transfer); bin fill/traffic/accident inputs are explicit. "
                      f"Original action {raw_action}; permitted action {action}. Alternate routes/depot actions are unsupported and masked; machine/window/geometry gates remain independent.")
            return {"status": self._status(reason, True), "reason": reason, "observation": observation,
                    "inference_ms": round(inference_ms, 3),
                    "proposed_action": {"action_index": action, "raw_action": raw_action,
                        "task_id": task.task_id, "preferred_task_id": task.task_id, "route_mode": "DIRECT"}}
        except Exception as exc:
            return fallback(f"PPO not applied: {exc}")
