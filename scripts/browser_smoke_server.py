"""Isolated HTTP fixture for the real-browser execution smoke test."""
from backend import main
from backend.orchestrator import AgenticWorkflowOrchestrator

main.orchestrator = AgenticWorkflowOrchestrator()
main.orchestrator.routing_engine._force_offline_for_test = True
# Force several genuine simulated service cycles on a compact test site.
main.orchestrator.machines["sweeper-alpha-01"].battery_capacity_kwh = 0.4
app = main.app
