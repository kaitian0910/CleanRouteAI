"""Fixed simulated missions for reproducible integration evaluation, not training."""
from .models import Coordinate, EnvironmentMapData, PlanRequest, RLBinState, RubbishItemSpec, TaskItem, TaskType


def six_bin_scenario(seed=42, use_rl=True):
    import random
    rng = random.Random(seed)
    depot = Coordinate(lat=3.139, lon=101.6869)
    positions = rng.sample([(x, y) for x in range(1, 10) for y in range(1, 10)], 6)
    tasks = []
    for index, (x, y) in enumerate(positions):
        fill = round(rng.uniform(20, 23), 2)
        tasks.append(TaskItem(
            task_id=f"sim-bin-{index+1}", task_type=TaskType.RUBBISH_COLLECTION,
            location=Coordinate.from_local_meters(x*8, y*8, depot), priority=5,
            service_duration_sec=30,
            rubbish_spec=RubbishItemSpec(description="Simulated bin mass and volume", estimated_weight_kg=fill,
                                        estimated_volume_m3=0.04, max_dimension_m=0.4,
                                        weight_estimation_method="SIMULATED_SCENARIO"),
            rl_bin_state=RLBinState(fill_kg=fill, traffic_multiplier=1.0, accident=False),
        ))
    return PlanRequest(
        start_location=depot, disposal_location=depot, tasks=tasks, use_rl=use_rl,
        environment_map=EnvironmentMapData(map_id=f"rl-simulation-{seed}", features=[]),
    )
