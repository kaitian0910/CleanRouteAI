"""Operator activity survives filtering of per-segment state transitions."""
from .test_machine_interlocks import api, approve, plan


def test_activity_keeps_user_pause_completion_and_emergency_stop(api):
    _, client, headers = api
    route = plan(api, "operator-activity")
    assert approve(api, route).status_code == 200
    path = f"/api/routes/{route['route_id']}"
    assert client.post(path + "/execute", headers=headers, json={"stop_after_segments": 1}).status_code == 200
    assert client.post(path + "/pause", headers=headers).status_code == 200
    assert client.post(path + "/resume", headers=headers, json={}).status_code == 200
    assert client.get(path, headers=headers).json()["status"] == "COMPLETED"
    assert client.post("/api/events", headers=headers, json={
        "job_id": route["job_id"], "route_id": route["route_id"], "event_type": "EMERGENCY_STOP",
    }).status_code == 200
    logs = client.get(path + "/metrics", headers=headers).json()
    actions = [entry["action"] for entry in logs["audit_log"]]
    assert {"PLAN_SELECTED", "APPROVE_ROUTE", "PAUSE_JOB", "COMPLETE_JOB", "EMERGENCY_STOP"} <= set(actions)
    assert not any(action.startswith("STATE:") for action in actions)
    assert any(entry["action"].startswith("STATE:") for entry in logs["technical_audit_log"])
