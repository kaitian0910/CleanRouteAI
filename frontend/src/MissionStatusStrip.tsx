import React, { useEffect, useState } from "react";
import type { ExecutionState, RouteCandidate, TelemetryFrame } from "./types/api";
import "./MissionStatusStrip.css";

export interface MissionStatusStripProps {
  apiBase: string;
  token: string;
  machineId: string;
  route: RouteCandidate | null;
  executionState: ExecutionState;
  telemetry: TelemetryFrame | null;
  batteryPercent: number;
}

interface MachineResources {
  machine_id: string;
  max_payload_kg?: number;
  current_payload_kg?: number;
  current_battery_percent?: number;
}

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const numberText = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 1 });

export function missionElapsedText(minutes: unknown): string {
  if (!finite(minutes) || minutes < 0) return "—";
  const rounded = Math.floor(minutes);
  return rounded >= 60 ? `${Math.floor(rounded / 60)}h ${rounded % 60}min` : `${rounded}min`;
}

/** Uses only the selected machine's route and resource snapshot. No image-derived payload. */
export function missionStatusValues(props: MissionStatusStripProps, machine: MachineResources | null = null) {
  const route = props.route?.machine_id === props.machineId ? props.route : null;
  const telemetry = route && props.telemetry?.route_id === route.route_id
    && props.telemetry.machine_id === props.machineId ? props.telemetry : null;
  const profile = machine?.machine_id === props.machineId ? machine : null;
  const state = props.executionState;
  const pendingPlanning = ["VALIDATING", "PLANNING", "EVALUATING", "REPLANNING"].includes(state);
  // A simulator telemetry frame describes the segment just completed. Activity refers
  // to the next unfinished segment; a completed charge must not show as charging.
  const nextSegment = route?.segments?.find(segment => !segment.completed);
  const blocked = route && (route.planning_diagnostics?.planning_status === "BLOCKED"
    || route.kpi_evaluation?.passed === false || route.route_safety_issues?.length > 0);
  let activity = "No active job";
  let tone = "neutral";
  if (state === "EMERGENCY_STOP" || telemetry?.emergency_stop_engaged) {
    activity = "Emergency stop";
    tone = "danger";
  } else if (state === "NEEDS_ASSISTANCE" || (blocked && !pendingPlanning)) {
    activity = "Needs attention";
    tone = "warning";
  } else if (pendingPlanning) {
    activity = state === "REPLANNING" ? "Updating route" : "Planning route";
  } else if (route) {
    if (state === "COMPLETED" || route.status === "COMPLETED") {
      activity = "Cleaning finished";
      tone = "good";
    } else if (state === "PAUSED") {
      activity = "Paused";
      tone = "warning";
    } else if (state === "ABORTED" || route.status === "ABORTED") {
      activity = "Stopped";
      tone = "warning";
    } else if (state === "EXECUTING") {
      activity = ({
        RETURN_TO_BASE: "Returning to depot",
        DEPOT_RECHARGE: "Depot service next",
        COLLECTION_STOP: "Collecting waste",
        WAIT_FOR_WINDOW: "Waiting for service time",
        SWEEP_SWATH: "Cleaning area",
        TURN_CONNECTOR: "Turning",
        TRANSIT: "Travelling to next task",
      } as Record<string, string>)[nextSegment?.segment_type || ""] || "Cleaning in progress";
      tone = "good";
    } else if (state === "AWAITING_APPROVAL" || (route.status === "PROPOSED" && !route.approved_by)) {
      activity = "Waiting for approval";
    } else {
      activity = "Ready to start";
    }
  }
  const battery = telemetry && finite(telemetry.battery_percent) ? telemetry.battery_percent
    : route && finite(props.batteryPercent) ? props.batteryPercent
      : profile && finite(profile.current_battery_percent) ? profile.current_battery_percent : null;
  const configuredReserve = route?.kpi_policy_snapshot?.min_energy_reserve_percent;
  const evaluatedReserve = route?.kpi_evaluation?.metrics?.remaining_energy_reserve_percent?.target_min;
  const reserve = finite(configuredReserve) ? configuredReserve : finite(evaluatedReserve) ? evaluatedReserve : null;
  const payload = telemetry && finite(telemetry.payload_kg) ? telemetry.payload_kg
    : profile && finite(profile.current_payload_kg) ? profile.current_payload_kg : null;
  const capacity = profile && finite(profile.max_payload_kg) && profile.max_payload_kg > 0
    ? profile.max_payload_kg : null;
  const loadPercent = payload !== null && capacity !== null ? payload / capacity * 100 : null;
  const collectedKg = (route?.tasks ?? []).filter(task => task.completed).reduce((total, task) =>
    total + (finite(task.rubbish_spec?.estimated_weight_kg) ? task.rubbish_spec.estimated_weight_kg : 0), 0);
  const unweighedPickups = (route?.tasks ?? []).filter(task => task.completed &&
    ["RUBBISH_COLLECTION", "OVERSIZED_RUBBISH"].includes(task.task_type) && task.rubbish_spec?.estimated_weight_kg == null).length;
  const collectedNote = (collectedKg > 0 ? ` · ${numberText(collectedKg)} kg collected total (estimate)` : "")
    + (unweighedPickups ? ` · ${unweighedPickups} pickup weights not recorded` : "");
  const elapsed = telemetry?.mission_elapsed_min ?? (route && route.status !== "COMPLETED"
    ? route.mission_elapsed_min : null);
  return {
    elapsed: missionElapsedText(elapsed),
    battery: battery === null ? "—" : `${numberText(battery)}%`,
    batteryNote: reserve === null ? route ? "Reserve unavailable" : "No active plan reserve" : `Minimum reserve ${numberText(reserve)}%`,
    batteryLow: battery !== null && reserve !== null && battery < reserve,
    payload: payload === null ? "—" : `${numberText(payload)} kg${capacity !== null ? ` / ${numberText(capacity)} kg` : ""}`,
    payloadNote: capacity === null ? "Capacity unavailable" : payload === null ? "Load unavailable"
      : `${telemetry && payload === 0 && collectedKg > 0 ? "Empty after depot unloading" : `${numberText(loadPercent!)}% by weight`}${telemetry ? "" : " · machine snapshot"}${collectedNote}`,
    payloadFull: loadPercent !== null && loadPercent >= 100,
    activity,
    tone,
  };
}

export function MissionStatusStripView({ values }: { values: ReturnType<typeof missionStatusValues> }) {
  return <section className="mission-status-strip" aria-label="Mission overview — simulation">
    <div className="mission-status-metric">
      <span className="mission-status-label">Mission time</span>
      <strong>{values.elapsed}</strong>
      <small>Simulated elapsed time</small>
    </div>
    <div className={`mission-status-metric${values.batteryLow ? " mission-status-warning" : ""}`}>
      <span className="mission-status-label">Battery</span>
      <strong>{values.battery}</strong>
      <small>{values.batteryNote}</small>
    </div>
    <div className={`mission-status-metric${values.payloadFull ? " mission-status-warning" : ""}`}>
      <span className="mission-status-label">Waste on board</span>
      <strong>{values.payload}</strong>
      <small>{values.payloadNote}</small>
    </div>
    <div className={`mission-status-metric mission-status-activity mission-status-${values.tone}`}>
      <span className="mission-status-label">Current activity</span>
      <strong>{values.activity}</strong>
      <small>Simulation</small>
    </div>
  </section>;
}

export function MissionStatusStrip(props: MissionStatusStripProps) {
  const [profile, setProfile] = useState<MachineResources | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setProfile(null);
    fetch(`${props.apiBase}/api/machines`, {
      headers: { Authorization: `Bearer ${props.token}` }, signal: controller.signal,
    }).then(response => response.ok ? response.json() : null).then((machines: unknown) => {
      if (!controller.signal.aborted && Array.isArray(machines)) {
        setProfile(machines.find(machine => machine?.machine_id === props.machineId) || null);
      }
    }).catch(() => { /* The overview remains useful without an optional capacity. */ });
    return () => controller.abort();
  }, [props.apiBase, props.token, props.machineId, props.route?.route_id]);
  return <MissionStatusStripView values={missionStatusValues(props, profile)} />;
}
