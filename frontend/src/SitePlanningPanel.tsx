import React, { useEffect, useRef, useState } from "react";
import type { Coordinate, EnvironmentMapData, ExclusionZone, GeoPolygon, TaskItem } from "./types/api";
import { apiErrorMessage } from "./planningSession";

export interface SiteImportPreview {
  plan_fields: {
    operating_boundary: GeoPolygon;
    exclusion_zones: ExclusionZone[];
    environment_map: EnvironmentMapData;
    start_location: Coordinate;
    disposal_location: Coordinate;
    tasks: TaskItem[];
  };
  machine_profile?: Record<string, unknown> | null;
  source: string;
  warnings: string[];
}

export interface OutingLimits {
  max_estimated_duration_minutes: number;
  min_energy_reserve_percent: number;
  max_travel_distance_m: number;
}

const MACHINE_FIELDS = [
  ["working_width_m", "Cleaning width", "m", 0.01, undefined],
  ["clearance_width_m", "Required clearance", "m", 0.01, undefined],
  ["sweep_speed_mps", "Sweeping speed", "m/s", 0.01, undefined],
  ["cruise_speed_mps", "Travel speed", "m/s", 0.01, undefined],
  ["battery_capacity_kwh", "Battery capacity", "kWh", 0.01, undefined],
  ["energy_consumption_kwh_per_km_sweep", "Sweeping energy use", "kWh/km", 0, undefined],
  ["energy_consumption_kwh_per_km_travel", "Travel energy use", "kWh/km", 0, undefined],
  ["depot_service_minutes", "Recharge / battery change time", "min", 0.01, 240],
  ["recharged_battery_percent", "Battery after depot service", "%", 0.01, 100],
] as const;
type MachineField = typeof MACHINE_FIELDS[number][0];
type MachineParameters = Record<MachineField, number>;
interface MachineProfile extends MachineParameters { machine_id: string; name: string }

export function parseSiteGeoJson(text: string): Record<string, unknown> {
  if (new TextEncoder().encode(text).length > 2 * 1024 * 1024) throw new Error("Site file exceeds the 2 MiB limit.");
  const data: unknown = JSON.parse(text);
  if (!data || typeof data !== "object" || (data as Record<string, unknown>).type !== "FeatureCollection" || !Array.isArray((data as Record<string, unknown>).features)) {
    throw new Error("Choose a GeoJSON FeatureCollection with the site boundary and labeled features.");
  }
  return data as Record<string, unknown>;
}

export function validateOutingLimits(limits: OutingLimits): string | null {
  if (!Number.isFinite(limits.max_estimated_duration_minutes) || limits.max_estimated_duration_minutes <= 0) return "Outing duration must be greater than zero.";
  if (!Number.isFinite(limits.min_energy_reserve_percent) || limits.min_energy_reserve_percent < 5 || limits.min_energy_reserve_percent > 95) return "Battery reserve must be between 5% and 95%.";
  if (!Number.isFinite(limits.max_travel_distance_m) || limits.max_travel_distance_m <= 0) return "Outing travel limit must be greater than zero.";
  return null;
}

interface Props {
  apiBase: string;
  token: string;
  machineId: string;
  source: string | null;
  limits: OutingLimits;
  onApplySite: (site: SiteImportPreview) => void | boolean | Promise<void | boolean>;
  onMachineSaved: () => void;
  onApplyLimits: (limits: OutingLimits) => void;
}

export default function SitePlanningPanel({ apiBase, token, machineId, source, limits, onApplySite, onMachineSaved, onApplyLimits }: Props) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<SiteImportPreview | null>(null);
  const [fileName, setFileName] = useState("");
  const [parameters, setParameters] = useState<MachineParameters | null>(null);
  const [machineName, setMachineName] = useState(machineId);
  const [draftLimits, setDraftLimits] = useState(limits);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const importController = useRef<AbortController | null>(null);
  const saveController = useRef<AbortController | null>(null);
  useEffect(() => { setDraftLimits(limits); }, [limits]);
  useEffect(() => () => { importController.current?.abort(); saveController.current?.abort(); }, []);

  useEffect(() => {
    if (!open) return;
    let current = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    setParameters(null);
    setError(null);
    fetch(`${apiBase}/api/machines`, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then(async response => { const data = await response.json(); if (!response.ok) throw new Error(apiErrorMessage(data.detail ?? data)); return data; })
      .then((data: MachineProfile[]) => {
        if (!current) return;
        const profile = data.find(item => item.machine_id === machineId);
        if (!profile) throw new Error("No planning profile was returned for this machine.");
        setMachineName(profile.name);
        setParameters(Object.fromEntries(MACHINE_FIELDS.map(([key]) => [key, profile[key]])) as MachineParameters);
      }).catch(reason => { if (current) setError(controller.signal.aborted ? "Machine profile request timed out. Reopen to retry." : reason.message); })
      .finally(() => window.clearTimeout(timeout));
    return () => { current = false; controller.abort(); window.clearTimeout(timeout); };
  }, [open, apiBase, token, machineId]);

  async function request(path: string, body: unknown, signal: AbortSignal) {
    const response = await fetch(`${apiBase}${path}`, { method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
    const data = await response.json();
    if (!response.ok) throw new Error(apiErrorMessage(data.detail ?? data));
    return data;
  }

  async function importSite(file: File) {
    importController.current?.abort();
    const controller = new AbortController();
    importController.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    setBusy("import"); setError(null); setNotice(null); setPreview(null); setFileName(file.name);
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error("Site file exceeds the 2 MiB limit.");
      const geojson = parseSiteGeoJson(await file.text());
      const result = await request("/api/planning/site/import", { geojson }, controller.signal);
      if (controller.signal.aborted) return;
      setPreview(result);
    } catch (reason) { if (importController.current === controller) setError(controller.signal.aborted ? "Site import timed out or was cancelled. Choose the file again to retry." : reason instanceof Error ? reason.message : "Site import failed."); }
    finally { window.clearTimeout(timeout); if (importController.current === controller) setBusy(null); }
  }

  async function saveMachine() {
    if (!parameters) return;
    const invalid = MACHINE_FIELDS.find(([key, , , min, max]) => !Number.isFinite(parameters[key]) || parameters[key] < min || (max != null && parameters[key] > max));
    if (invalid) { setError(`Check ${invalid[1].toLowerCase()} and its allowed range.`); return; }
    const controller = new AbortController(); saveController.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    setBusy("machine"); setError(null); setNotice(null);
    try {
      const result = await request(`/api/machines/${encodeURIComponent(machineId)}/planning-profile`, { parameters }, controller.signal);
      if (result.machine) setParameters(Object.fromEntries(MACHINE_FIELDS.map(([key]) => [key, result.machine[key]])) as MachineParameters);
      if (result.invalidated_route_ids?.length) onMachineSaved();
      setNotice(result.invalidated_route_ids?.length ? "Machine estimates saved. Replan and review the new route before running." : "Machine estimates saved. No existing route was invalidated.");
    } catch (reason) { setError(controller.signal.aborted ? "Save could not be confirmed. Reopen the panel to read the current profile before retrying." : reason instanceof Error ? reason.message : "Machine save failed."); }
    finally { window.clearTimeout(timeout); setBusy(null); }
  }

  return <details className="advanced-planning-inputs" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Advanced planning inputs <span>{source ? "· imported site" : "· optional"}</span></summary>
    {open && <div className="advanced-planning-body">
      <p>Draw an area on the map for everyday planning, or optionally import your site and adjust the estimates below.</p>
      <section><h3>Actual site file</h3>
        <p>Choose a GeoJSON site file (up to 2 MiB). Imported locations and terrain are operator-provided planning inputs, awaiting field verification.</p>
        <input aria-label="Actual site file" type="file" accept=".json,.geojson,application/geo+json,application/json" disabled={busy !== null} onChange={event => { const file = event.target.files?.[0]; if (file) void importSite(file); event.currentTarget.value = ""; }} />
        <p>Active source: {source ?? "Illustrative campus reference; not a surveyed site."}</p>
        {preview && <div className="site-import-preview"><strong>{fileName}</strong>
          <p>{preview.plan_fields.operating_boundary.vertices.length} boundary corners · {preview.plan_fields.exclusion_zones.length} exclusions · {preview.plan_fields.environment_map.features.length} terrain features · {preview.plan_fields.tasks.length} collection tasks</p>
          <p>Start {preview.plan_fields.start_location.lat.toFixed(5)}, {preview.plan_fields.start_location.lon.toFixed(5)} · Depot {preview.plan_fields.disposal_location.lat.toFixed(5)}, {preview.plan_fields.disposal_location.lon.toFixed(5)}</p>
          {!!preview.warnings?.length && <ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
          {preview.machine_profile && <p>The file also contains machine information. <button type="button" className="btn-secondary-sm" disabled={!parameters || busy !== null} onClick={() => {
            setParameters(current => current ? Object.fromEntries(MACHINE_FIELDS.map(([key]) => [key, typeof preview.machine_profile?.[key] === "number" ? preview.machine_profile[key] : current[key]])) as MachineParameters : null);
            setNotice("Imported machine values loaded into the editor. Review and save explicitly to use them.");
          }}>Load imported machine values for review</button></p>}
          <button type="button" className="btn-secondary-sm" disabled={busy !== null} onClick={async () => {
            setBusy("apply");
            try {
              if (await onApplySite(preview) === false) return;
              setNotice("Site applied to the map. Choose a task to plan and review its route."); setPreview(null);
            } finally { setBusy(null); }
          }}>Apply site to map</button>
        </div>}
      </section>
      <section><h3>Machine planning estimates</h3><p>{machineName}. Use measured specifications where available. Saving requires replanning existing routes.</p>
        {parameters ? <div className="planning-input-fields">{MACHINE_FIELDS.map(([key, name, unit, min, max]) => <label key={key}>{name} ({unit})
          <input type="number" min={min} max={max} step="any" disabled={busy !== null} value={Number.isFinite(parameters[key]) ? parameters[key] : ""} onChange={event => setParameters(current => current ? { ...current, [key]: event.target.value === "" ? Number.NaN : Number(event.target.value) } : null)} />
        </label>)}</div> : <p>Machine profile unavailable or loading.</p>}
        <button type="button" className="btn-secondary-sm" disabled={!parameters || busy !== null} onClick={() => void saveMachine()}>Save machine estimates</button>
      </section>
      <section><h3>Outing limits</h3><p>The time or distance limit may cause a depot return before battery reserve is reached. These limits apply to the next plan; recharge time contributes to whole-job completion.</p>
        <div className="planning-input-fields">
          <label>Maximum outing duration (min)<input type="number" min={0.1} step="any" value={draftLimits.max_estimated_duration_minutes} onChange={event => setDraftLimits(current => ({ ...current, max_estimated_duration_minutes: Number(event.target.value) }))} /></label>
          <label>Minimum battery reserve (%)<input type="number" min={5} max={95} step="any" value={draftLimits.min_energy_reserve_percent} onChange={event => setDraftLimits(current => ({ ...current, min_energy_reserve_percent: Number(event.target.value) }))} /></label>
          <label>Maximum outing travel (m)<input type="number" min={1} step="any" value={draftLimits.max_travel_distance_m} onChange={event => setDraftLimits(current => ({ ...current, max_travel_distance_m: Number(event.target.value) }))} /></label>
        </div>
        <button type="button" className="btn-secondary-sm" disabled={busy !== null} onClick={() => { const issue = validateOutingLimits(draftLimits); if (issue) { setError(issue); return; } onApplyLimits(draftLimits); setError(null); setNotice("Outing limits set for the next plan. Replan to apply them."); }}>Apply next-plan limits</button>
      </section>
      {busy && <p role="status">{busy === "import" ? "Reading and checking the site…" : busy === "apply" ? "Confirming the previous job is closed…" : "Saving machine estimates…"}</p>}
      {error && <p role="alert" className="advanced-planning-error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </div>}
  </details>;
}
