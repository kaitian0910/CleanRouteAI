"""Policy hints stay subordinate to deterministic collection feasibility."""

import pytest

from backend.models import (
    Coordinate, DataProvenance, MachineProfile, RubbishItemSpec, TaskItem, TaskType,
)
from backend.planners import RouteOptimizationEngine


ORIGIN = Coordinate(lat=0.0, lon=0.0)


def bin_task(task_id, priority=5, deadline=480.0, weight=0.0, volume=0.0):
    return TaskItem(
        task_id=task_id,
        task_type=TaskType.RUBBISH_COLLECTION,
        location=Coordinate(lat=0.0001, lon=0.0001),
        priority=priority,
        service_duration_sec=60.0,
        time_window_end_min=deadline,
        rubbish_spec=RubbishItemSpec(
            estimated_weight_kg=weight, estimated_volume_m3=volume,
        ),
    )


def collect(tasks, preferred=None, machine=None, unreachable=()):
    size = len(tasks) + 2
    distances = [[0.0 if i == j else 10.0 for j in range(size)] for i in range(size)]
    seconds = [[0.0 if i == j else 60.0 for j in range(size)] for i in range(size)]
    for index in unreachable:
        distances[0][index + 1] = 1e9
        seconds[0][index + 1] = 1e9
    paths = {(i, j): [ORIGIN, tasks[0].location] for i in range(size) for j in range(size)}
    return RouteOptimizationEngine().optimize_collection_route(
        ORIGIN, ORIGIN, tasks,
        machine or MachineProfile(machine_id="dispatch-test", name="Dispatch test"),
        distances, seconds, paths,
        provenance=DataProvenance.PLANNED,
        preferred_task_id=preferred,
    )


@pytest.mark.parametrize("count", [2, 9, 10])
def test_preferred_bin_is_first_when_all_tasks_remain_feasible(count):
    tasks = [bin_task(f"bin-{i}", priority=10 if i == 0 else 2) for i in range(count)]
    baseline = collect(tasks)
    advised = collect(tasks, preferred=tasks[-1].task_id)
    assert baseline.ordered_tasks[0].task_id == "bin-0"
    assert advised.ordered_tasks[0].task_id == tasks[-1].task_id
    assert len(advised.ordered_tasks) == count
    assert not advised.skipped_optional_tasks


@pytest.mark.parametrize("count", [2, 9, 10])
def test_full_feasible_order_overrides_policy_that_would_miss_deadline(count):
    tasks = [bin_task("urgent-bin", priority=10, deadline=2.0)]
    tasks.extend(bin_task(f"bin-{i}") for i in range(1, count))
    plan = collect(tasks, preferred=tasks[-1].task_id)
    assert plan.ordered_tasks[0].task_id == "urgent-bin"
    assert len(plan.ordered_tasks) == count
    assert plan.task_schedule[0].service_end_min == 2.0


@pytest.mark.parametrize("weight,volume", [(7.0, 0.0), (0.0, 0.7)])
def test_preferred_order_still_returns_to_depot_before_capacity_is_exceeded(weight, volume):
    tasks = [bin_task("bin-a", weight=weight, volume=volume), bin_task("bin-b", weight=weight, volume=volume)]
    machine = MachineProfile(
        machine_id="capacity-test", name="Capacity test",
        max_payload_kg=10.0, max_payload_volume_m3=1.0,
    )
    plan = collect(tasks, preferred="bin-b", machine=machine)
    assert [task.task_id for task in plan.ordered_tasks] == ["bin-b", "bin-a"]
    assert [entry.disposal_before for entry in plan.task_schedule] == [False, True]
    assert plan.disposal_trips == 2
    assert plan.peak_payload_kg <= machine.max_payload_kg
    assert plan.peak_payload_volume_m3 <= machine.max_payload_volume_m3
    assert [segment.segment_type for segment in plan.segments] == [
        "TRANSIT", "COLLECTION_STOP", "RETURN_TO_BASE",
        "TRANSIT", "COLLECTION_STOP", "RETURN_TO_BASE",
    ]


def test_unknown_or_unreachable_policy_target_does_not_displace_reachable_bins():
    tasks = [bin_task("reachable-bin"), bin_task("unreachable-bin", priority=10)]
    baseline = collect(tasks, unreachable=[1])
    for preferred in ["unknown-bin", "unreachable-bin"]:
        plan = collect(tasks, preferred=preferred, unreachable=[1])
        assert [task.task_id for task in plan.ordered_tasks] == [task.task_id for task in baseline.ordered_tasks]
        assert [task.task_id for task in plan.unreachable_tasks] == ["unreachable-bin"]
