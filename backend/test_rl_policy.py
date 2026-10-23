import hashlib
import os
from pathlib import Path
import pytest

from backend.models import RouteSegment, Coordinate
from backend.orchestrator import AgenticWorkflowOrchestrator
from backend.rl_policy import G3Policy
from backend.rl_scenarios import six_bin_scenario


def test_observation_schema_order_and_authoritative_resources():
    req = six_bin_scenario()
    machine = AgenticWorkflowOrchestrator().machines[req.machine_id]
    machine.current_battery_percent = 73
    machine.current_payload_kg = 25
    req.tasks[1].completed = True
    req.tasks[0].rl_bin_state.traffic_multiplier = 1.6
    req.tasks[0].rl_bin_state.accident = True
    obs = G3Policy.encode(req, machine)
    assert len(obs) == 48 and all(0 <= value <= 1 for value in obs)
    assert obs[:4] == [0, 0, .73, .1]
    assert obs[4] == pytest.approx(req.tasks[0].rl_bin_state.fill_kg/100)
    assert obs[5] == 0
    assert obs[22] == .8 and obs[28] == 1
    assert obs[40:46] == [0, 1, 0, 0, 0, 0]
    assert obs[46:] == [0, 0]


def test_unknown_telemetry_and_incompatible_domains_fall_back():
    req = six_bin_scenario()
    machine = AgenticWorkflowOrchestrator().machines[req.machine_id]
    req.tasks[0].rl_bin_state = None
    result = G3Policy().recommend(req, machine)
    assert result["proposed_action"] is None
    assert "missing traffic/fill" in result["reason"]
    req = six_bin_scenario()
    req.tasks.pop()
    assert "six" in G3Policy().recommend(req, machine)["reason"]
    req = six_bin_scenario()
    req.use_rl = False
    assert "disabled" in G3Policy(Path("missing.zip")).recommend(req, machine)["reason"]


def test_missing_and_incompatible_checkpoints_do_not_break_status():
    assert not G3Policy(Path(__file__).with_name("not-present.zip")).status()["checkpoint_loaded"]
    # The supplied YOLO model is a valid ZIP, but not an SB3 checkpoint.
    invalid = G3Policy(Path(__file__).parent / "vision_assets" / "best.pt").status()
    assert invalid["mode"] == "DETERMINISTIC_FALLBACK"
    assert invalid["checkpoint_loaded"] is False


def test_real_checkpoint_inference_preserves_weights_and_action_semantics():
    pytest.importorskip("stable_baselines3")
    policy = G3Policy()
    before = hashlib.sha256((policy.checkpoint / "policy.pth").read_bytes()).digest()
    assert policy.status()["checkpoint_loaded"]
    req = six_bin_scenario()
    machine = AgenticWorkflowOrchestrator().machines[req.machine_id]
    first = policy.recommend(req, machine)
    second = policy.recommend(req, machine)
    assert first["status"]["mode"] == "TRAINED_PPO", first
    action = first["proposed_action"]
    assert action == second["proposed_action"]
    assert action["route_mode"] == "DIRECT" and action["action_index"] % 2 == 0
    assert req.tasks[action["action_index"]//2].task_id == action["preferred_task_id"]
    assert policy._model.num_timesteps == 16861
    assert not policy._model.policy.training
    assert hashlib.sha256((policy.checkpoint / "policy.pth").read_bytes()).digest() == before
    import torch
    assert isinstance(policy._model.policy.mlp_extractor.policy_net[1], torch.nn.Tanh)


def test_invalid_probability_distribution_is_rejected():
    req = six_bin_scenario()
    machine = AgenticWorkflowOrchestrator().machines[req.machine_id]
    class Broken:
        def obs_to_tensor(self, obs):
            raise ValueError("invalid model output")
    from types import SimpleNamespace
    policy = G3Policy()
    policy._model = SimpleNamespace(policy=Broken())
    result = policy.recommend(req, machine)
    assert result["status"]["mode"] == "DETERMINISTIC_FALLBACK"
    assert result["proposed_action"] is None


def test_extracted_model_matches_original_archive_predictions():
    pytest.importorskip("stable_baselines3")
    extracted = G3Policy()
    archive = Path(os.environ.get("CLEANROUTEAI_RL_REFERENCE_ARCHIVE", Path(__file__).parent / "weights" / "ppo_model.zip"))
    if not archive.is_file():
        pytest.skip("Optional original archive not installed; set CLEANROUTEAI_RL_REFERENCE_ARCHIVE for equivalence check")
    original = G3Policy(archive)
    req = six_bin_scenario()
    machine = AgenticWorkflowOrchestrator().machines[req.machine_id]
    new = extracted.recommend(req, machine)
    old = original.recommend(req, machine)
    assert new["status"]["checkpoint_loaded"] and old["status"]["checkpoint_loaded"]
    assert new["proposed_action"] == old["proposed_action"]


def test_default_runtime_does_not_open_zip(monkeypatch):
    pytest.importorskip("stable_baselines3")
    def reject_zip(*args, **kwargs):
        raise AssertionError("Runtime must not open the backup archive")
    monkeypatch.setattr("backend.rl_policy.zipfile.ZipFile", reject_zip)
    policy = G3Policy()
    assert policy.status()["checkpoint_loaded"]
