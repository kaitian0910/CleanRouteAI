"""Coverage continuity and depot-budget regression cases."""
from .config import KPIPolicyConfig
from .depot_cycles import _distance, _pieces, plan_depot_cycles
from .models import Coordinate, MachineProfile, RouteCandidate, RouteSegment, TaskItem, TaskType


def _point(x=0.0, y=0.0):
    return Coordinate.from_local_meters(x, y, Coordinate(lat=3.0, lon=101.0))


def _segment(a, b, kind="SWEEP_SWATH", **kwargs):
    length = _distance([a, b])
    return RouteSegment(segment_type=kind, waypoints=[a, b], distance_m=length,
                        estimated_duration_sec=length, swath_width_m=1.6, **kwargs)


def _candidate(segments, **kwargs):
    return RouteCandidate(job_id="battery-outings", route_version=1, machine_id="test",
                          task_mode=TaskType.AREA_SWEEP, origin=_point(), disposal_depot=_point(),
                          segments=segments, **kwargs)


def test_outings_complete_all_work_and_include_depot_waits():
    segments = [_segment(_point(), _point(100)), _segment(_point(100), _point())] * 12
    # Independent objects/ids, matching an ordinary generated work sequence.
    segments = [_segment(s.waypoints[0], s.waypoints[-1]) for s in segments]
    candidate = _candidate(segments)
    machine = MachineProfile(machine_id="test", name="test", battery_capacity_kwh=1,
                             energy_consumption_kwh_per_km_sweep=1,
                             energy_consumption_kwh_per_km_travel=1,
                             cruise_speed_mps=1)
    policy = KPIPolicyConfig(max_estimated_duration_minutes=8, max_travel_distance_m=1000)
    plan_depot_cycles(candidate, machine, policy, lambda a, b: [a, b])
    assert not candidate.route_safety_issues
    assert candidate.depot_visits >= 4
    assert candidate.max_outing_duration_minutes <= 8.00001
    assert candidate.minimum_battery_percent >= policy.min_energy_reserve_percent
    assert candidate.charging_duration_minutes == candidate.depot_visits * machine.depot_service_minutes
    assert candidate.estimated_duration_minutes >= 40 + candidate.charging_duration_minutes - 0.1
    assert [s.segment_id for s in candidate.segments if s.segment_type == "SWEEP_SWATH"] == [s.segment_id for s in segments]
    assert candidate.segments[-1].waypoints[-1] == candidate.disposal_depot


def test_long_polyline_split_preserves_coverage_geometry_and_totals():
    segment = _segment(_point(), _point(930))
    split = _pieces(segment)
    assert len(split) == 5
    assert all(s.distance_m <= 200.0001 and s.swath_width_m == segment.swath_width_m for s in split)
    assert abs(sum(s.distance_m for s in split) - segment.distance_m) < 0.001
    assert split[0].waypoints[0] == segment.waypoints[0]
    assert split[-1].waypoints[-1] == segment.waypoints[-1]
    assert all(a.waypoints[-1] == b.waypoints[0] for a, b in zip(split, split[1:]))


def test_unreachable_depot_never_silently_loses_work():
    segment = _segment(_point(), _point(100))
    candidate = _candidate([segment])
    plan_depot_cycles(candidate, MachineProfile(machine_id="test", name="test"),
                      KPIPolicyConfig(), lambda a, b: None)
    assert "depot_cycle_return_unreachable" in candidate.route_safety_issues
    assert candidate.segments[0].segment_id == segment.segment_id


def test_completed_segments_are_unchanged_and_low_depot_battery_can_charge():
    done = _segment(_point(), _point(), completed=True)
    pending = _segment(_point(), _point(20))
    candidate = _candidate([done, pending])
    machine = MachineProfile(machine_id="test", name="test", current_battery_percent=5)
    plan_depot_cycles(candidate, machine, KPIPolicyConfig(), lambda a, b: [a, b])
    assert candidate.segments[0] == done
    assert candidate.segments[1].segment_type == "DEPOT_RECHARGE"
    assert candidate.minimum_battery_percent >= 20
    assert not candidate.route_safety_issues


def test_collection_window_includes_charging_delay():
    task = TaskItem(task_id="bin", task_type=TaskType.RUBBISH_COLLECTION, location=_point(),
                    service_duration_sec=60, time_window_end_min=10)
    stop = RouteSegment(segment_type="COLLECTION_STOP", waypoints=[_point()], distance_m=0,
                        estimated_duration_sec=60, target_task_id="bin")
    candidate = _candidate([stop], tasks=[task])
    machine = MachineProfile(machine_id="test", name="test", current_battery_percent=5)
    plan_depot_cycles(candidate, machine, KPIPolicyConfig(), lambda a, b: [a, b])
    assert candidate.task_schedule[0].service_end_min == 21
    assert candidate.task_schedule[0].status == "INFEASIBLE"
    assert "depot_cycle_task_window_missed:bin" in candidate.route_safety_issues


def test_replan_keeps_current_outing_budget_without_double_counting_completion():
    done = RouteSegment(segment_type="WAIT_FOR_WINDOW", waypoints=[_point()], distance_m=0,
                        estimated_duration_sec=260, completed=True)
    candidate = _candidate([done, _segment(_point(), _point(100))], mission_elapsed_min=260 / 60)
    machine = MachineProfile(machine_id="test", name="test", cruise_speed_mps=1)
    plan_depot_cycles(candidate, machine, KPIPolicyConfig(max_estimated_duration_minutes=5),
                      lambda a, b: [a, b])
    assert not candidate.route_safety_issues
    assert candidate.segments[0] == done
    assert candidate.segments[1].segment_type == "DEPOT_RECHARGE"
    assert candidate.depot_visits == 1
    assert candidate.max_outing_duration_minutes == 260 / 60
    assert abs(candidate.estimated_completion_minutes - (260 / 60 + 20 + 200 / 60)) < 0.01


def test_replan_keeps_lifetime_depot_visit_cap():
    completed_charge = RouteSegment(segment_type="DEPOT_RECHARGE", waypoints=[_point()],
                                    distance_m=0, estimated_duration_sec=1200, completed=True)
    candidate = _candidate([completed_charge, _segment(_point(), _point(100))])
    machine = MachineProfile(machine_id="test", name="test", current_battery_percent=5)
    plan_depot_cycles(candidate, machine, KPIPolicyConfig(max_depot_cycles=1), lambda a, b: [a, b])
    assert "depot_cycle_limit_exceeded" in candidate.route_safety_issues
    assert candidate.depot_visits == 1  # Whole-job total includes the completed service.
    assert sum(s.segment_type == "DEPOT_RECHARGE" for s in candidate.segments) == 1
