import React, { useEffect, useState } from "react";
import L from "leaflet";
import {
  MapContainer,
  TileLayer,
  Polygon,
  Polyline,
  Marker,
  Popup,
  CircleMarker,
  Pane,
  useMap,
  useMapEvents,
} from "react-leaflet";
import {
  AuthorizedCrossing,
  Coordinate,
  DrawMode,
  RouteCandidate,
  TaskItem,
  TelemetryFrame,
  TerrainFeature,
} from "./types/api";
import { MAPPED_WATER_FEATURES } from "./mappedWater";
import { boundaryTouchesWater, combineWaterFeatures, pathTouchesWater, pointTouchesWater } from "./waterSafety";

interface DynamicObstacleItem {
  id: string;
  name: string;
  vertices: Coordinate[];
}

interface MapWorkspaceProps {
  drawMode: DrawMode;
  boundary: Coordinate[];
  exclusion: Coordinate[];
  dynamicObstacles: DynamicObstacleItem[];
  tasks: TaskItem[];
  startLocation: Coordinate;
  disposalLocation: Coordinate;
  activeRoute: RouteCandidate | null;
  latestTelemetry: TelemetryFrame | null;
  mapCenter: Coordinate;
  environmentFeatures?: TerrainFeature[];
  authorizedCrossings?: AuthorizedCrossing[];
  showGisLayers?: boolean;
  onAddVertex: (coord: Coordinate) => void;
  onSelectBoxArea: (boxVertices: Coordinate[]) => void;
  onDropInstantObstacle: (obstacleVertices: Coordinate[]) => void;
  onAddTaskOnMap: (
    coord: Coordinate,
    isOversized: boolean,
    isEventWaste?: boolean
  ) => void;
}

const ROUTE_STATUS_STYLES: Record<
  string,
  { color: string; dashArray?: string; weight: number }
> = {
  PROPOSED: { color: "#0284c7", weight: 4 },
  DRAFT_FAILED_KPI: { color: "#ef4444", dashArray: "5, 7", weight: 3 },
  APPROVED: { color: "#0284c7", weight: 4 },
  EXECUTING: { color: "#0284c7", weight: 4 },
  COMPLETED: { color: "#10b981", weight: 4 },
  SUPERSEDED: { color: "#94a3b8", dashArray: "3, 6", weight: 2 },
};

const FEATURE_STYLES: Record<
  string,
  { color: string; fillColor: string; fillOpacity: number; dashArray?: string; label: string }
> = {
  PUBLIC_ROAD: {
    color: "#f59e0b",
    fillColor: "#d97706",
    fillOpacity: 0.28,
    dashArray: "6, 4",
    label: "🛣️ Prohibited Public Road",
  },
  WATER_BODY: {
    color: "#0284c7",
    fillColor: "#0369a1",
    fillOpacity: 0.38,
    label: "🌊 Impassable Water / Lake",
  },
  BUILDING: {
    color: "#e11d48",
    fillColor: "#be123c",
    fillOpacity: 0.35,
    label: "🏛️ Impassable Building Footprint",
  },
  RESTRICTED_ZONE: {
    color: "#dc2626",
    fillColor: "#ef4444",
    fillOpacity: 0.35,
    dashArray: "4, 4",
    label: "⛔ Restricted Compound",
  },
  PEDESTRIAN_WALKWAY: {
    color: "#10b981",
    fillColor: "#059669",
    fillOpacity: 0.14,
    dashArray: "3, 5",
    label: "🚶 Narrow Pedestrian Footpath (Light Sweeper Only)",
  },
};

function makePillIcon(
  label: string,
  bg: string,
  border = "#ffffff",
  pulse = false
): L.DivIcon {
  return L.divIcon({
    className: "custom-div-icon",
    html: `<div style="
      background:${bg};
      color:#ffffff;
      border:2px solid ${border};
      border-radius:999px;
      min-width:30px;
      height:26px;
      padding:0 10px;
      display:inline-flex;
      align-items:center;
      justify-content:center;
      font-size:11px;
      font-weight:700;
      letter-spacing:0.2px;
      box-shadow:${
        pulse
          ? "0 0 0 5px rgba(16, 185, 129, 0.32), 0 4px 12px rgba(0,0,0,0.45)"
          : "0 3px 8px rgba(0,0,0,0.35)"
      };
      white-space:nowrap;
    ">${label}</div>`,
    iconSize: [30, 26],
    iconAnchor: [15, 13],
  });
}

function makeDirectionChevronIcon(
  angleDeg: number,
  color: string,
  stepLabel?: string
): L.DivIcon {
  return L.divIcon({
    className: "custom-chevron-icon",
    html: `<div style="
      display:inline-flex;
      align-items:center;
      justify-content:center;
      width:20px;
      height:20px;
      border-radius:999px;
      background:rgba(15, 23, 42, 0.85);
      border:1.5px solid ${color};
      color:${color};
      font-size:11px;
      font-weight:900;
      transform:rotate(${angleDeg.toFixed(1)}deg);
      box-shadow:0 1px 4px rgba(0,0,0,0.4);
      pointer-events:none;
    " title="${stepLabel || ""}">➤</div>`,
    iconSize: [20, 20],
    iconAnchor: [10, 10],
  });
}

function ClickAndHoverHandler({
  drawMode,
  boxStart,
  setBoxStart,
  onAddVertex,
  onSelectBoxArea,
  onDropInstantObstacle,
  onAddTaskOnMap,
  onHover,
}: {
  drawMode: DrawMode;
  boxStart: Coordinate | null;
  setBoxStart: (c: Coordinate | null) => void;
  onAddVertex: (c: Coordinate) => void;
  onSelectBoxArea: (verts: Coordinate[]) => void;
  onDropInstantObstacle: (verts: Coordinate[]) => void;
  onAddTaskOnMap: (c: Coordinate, isOver: boolean, isEvt?: boolean) => void;
  onHover: (c: Coordinate | null) => void;
}) {
  useMapEvents({
    click(e) {
      const coord: Coordinate = {
        lat: Number(e.latlng.lat.toFixed(6)),
        lon: Number(e.latlng.lng.toFixed(6)),
      };

      if (drawMode === "BOX_AREA") {
        if (!boxStart) {
          setBoxStart(coord);
        } else {
          const minLat = Math.min(boxStart.lat, coord.lat);
          const maxLat = Math.max(boxStart.lat, coord.lat);
          const minLon = Math.min(boxStart.lon, coord.lon);
          const maxLon = Math.max(boxStart.lon, coord.lon);
          setBoxStart(null);
          if (maxLat - minLat > 0.00003 && maxLon - minLon > 0.00003) {
            onSelectBoxArea([
              { lat: minLat, lon: minLon },
              { lat: minLat, lon: maxLon },
              { lat: maxLat, lon: maxLon },
              { lat: maxLat, lon: minLon },
            ]);
          }
        }
        return;
      }

      if (drawMode === "INSTANT_OBSTACLE") {
        const d = 0.000035;
        onDropInstantObstacle([
          { lat: Number((coord.lat - d).toFixed(6)), lon: Number((coord.lon - d).toFixed(6)) },
          { lat: Number((coord.lat - d).toFixed(6)), lon: Number((coord.lon + d).toFixed(6)) },
          { lat: Number((coord.lat + d).toFixed(6)), lon: Number((coord.lon + d).toFixed(6)) },
          { lat: Number((coord.lat + d).toFixed(6)), lon: Number((coord.lon - d).toFixed(6)) },
        ]);
        return;
      }

      if (drawMode === "TASK") {
        onAddTaskOnMap(coord, false, false);
        return;
      }

      if (drawMode === "OVERSIZED" || drawMode === "EVENT_WASTE") {
        onAddTaskOnMap(coord, true, drawMode === "EVENT_WASTE");
        return;
      }

      onAddVertex(coord);
    },
    mousemove(e) {
      if (drawMode !== "BOX_AREA" || !boxStart) return;
      onHover({
        lat: Number(e.latlng.lat.toFixed(6)),
        lon: Number(e.latlng.lng.toFixed(6)),
      });
    },
    mouseout() {
      onHover(null);
    },
  });
  return null;
}

function MapCenterUpdater({ center }: { center: Coordinate }) {
  const map = useMap();
  useEffect(() => {
    map.setView([center.lat, center.lon], map.getZoom(), { animate: true });
  }, [center.lat, center.lon, map]);
  return null;
}

function MapPlacementController({ drawMode }: { drawMode: DrawMode }) {
  const map = useMap();
  const isPlacing = drawMode !== "NONE" && drawMode !== "SELECT_ZONE";

  useEffect(() => {
    const container = map.getContainer();
    container.classList.toggle("map-placement-active", isPlacing);
    if (isPlacing) map.closePopup();
    return () => container.classList.remove("map-placement-active");
  }, [isPlacing, map]);

  return (
    <style>{`
      .map-placement-active { cursor: crosshair; }
      .map-placement-active .leaflet-overlay-pane *,
      .map-placement-active .leaflet-water-safety-pane *,
      .map-placement-active .leaflet-marker-pane *,
      .map-placement-active .leaflet-shadow-pane *,
      .map-placement-active .leaflet-popup-pane * {
        pointer-events: none !important;
      }
    `}</style>
  );
}

export const MapWorkspace: React.FC<MapWorkspaceProps> = ({
  drawMode,
  boundary,
  exclusion,
  dynamicObstacles,
  tasks,
  startLocation,
  disposalLocation,
  activeRoute,
  latestTelemetry,
  mapCenter,
  environmentFeatures = [],
  authorizedCrossings = [],
  showGisLayers = false,
  onAddVertex,
  onSelectBoxArea,
  onDropInstantObstacle,
  onAddTaskOnMap,
}) => {
  const [boxStart, setBoxStart] = useState<Coordinate | null>(null);
  const [hoverCoord, setHoverCoord] = useState<Coordinate | null>(null);

  useEffect(() => {
    setBoxStart(null);
  }, [drawMode]);

  const hasMission = boundary.length >= 3 || tasks.length > 0 || activeRoute !== null;

  const placementHint = drawMode === "INSTANT_OBSTACLE"
    ? "Click anywhere on the map or route to add an obstacle and replan."
    : drawMode === "BOX_AREA"
    ? boxStart ? "Click the opposite corner to finish the cleaning area." : "Click the first corner of the cleaning area."
    : drawMode === "DEPOT"
    ? "Click the map to move the depot and replan."
    : drawMode === "TASK"
    ? "Click the map to place a collection stop."
    : drawMode === "OVERSIZED" || drawMode === "EVENT_WASTE"
    ? "Click the map to place a bulky waste collection stop."
    : drawMode === "BOUNDARY"
    ? "Click the map to add cleaning boundary vertices."
    : null;

  const routeStyle = activeRoute
    ? ROUTE_STATUS_STYLES[activeRoute.status] || ROUTE_STATUS_STYLES.PROPOSED
    : ROUTE_STATUS_STYLES.PROPOSED;

  const displayBoundary =
    boundary.length >= 3
      ? boundary
      : activeRoute?.operating_boundary?.vertices || [];

  const displayTasks =
    activeRoute && activeRoute.tasks.length > 0 ? activeRoute.tasks : tasks;
  const effectiveStart = activeRoute?.origin || startLocation;
  const effectiveDepot = activeRoute?.disposal_depot || disposalLocation;
  const robotPosition =
    latestTelemetry?.position || effectiveStart || mapCenter;

  const previewBoxVertices: Coordinate[] =
    drawMode === "BOX_AREA" && boxStart && hoverCoord
      ? [
          { lat: boxStart.lat, lon: boxStart.lon },
          { lat: boxStart.lat, lon: hoverCoord.lon },
          { lat: hoverCoord.lat, lon: hoverCoord.lon },
          { lat: hoverCoord.lat, lon: boxStart.lon },
        ]
      : [];

  const coverageCells =
    activeRoute?.planning_diagnostics?.coverage_cells ||
    activeRoute?.coverage_cells ||
    [];
  const excludedSections =
    activeRoute?.planning_diagnostics?.excluded_sections ||
    activeRoute?.unreachable_sections ||
    [];

  const waterFeatures = combineWaterFeatures(environmentFeatures, MAPPED_WATER_FEATURES);
  const hasRelevantWater = waterFeatures.some(water => boundaryTouchesWater(displayBoundary, water) ||
    [effectiveStart, effectiveDepot, ...displayTasks.map(task => task.location)].some(point => pointTouchesWater(point, water)) ||
    activeRoute?.segments.some(segment => pathTouchesWater(segment.waypoints, water)));

  const sweeps = activeRoute?.segments.filter(segment => segment.segment_type === "SWEEP_SWATH") || [];
  const arrowStride = Math.max(1, Math.ceil(sweeps.length / 12));
  const arrowSegments = new Set(sweeps.filter((_, index) => index % arrowStride === 0).map(segment => segment.segment_id));

  return (
    <MapContainer
      center={[mapCenter.lat, mapCenter.lon]}
      zoom={17}
      style={{ height: "100%", width: "100%" }}
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        maxZoom={21}
      />
      <MapCenterUpdater center={mapCenter} />
      <MapPlacementController drawMode={drawMode} />
      {placementHint && (
        <div
          role="status"
          style={{ position: "absolute", top: 10, left: 55, right: 15, zIndex: 800, pointerEvents: "none" }}
        >
          <span style={{ display: "inline-block", background: "#0f172a", color: "#e2e8f0", border: "1px solid #38bdf8", borderRadius: 6, padding: "8px 12px", fontSize: 12 }}>
            {placementHint}
          </span>
        </div>
      )}
      {hasRelevantWater && <div role="note" style={{ position: "absolute", bottom: 24, left: 12, zIndex: 800,
        pointerEvents: "none", background: "#082f49", color: "#e0f2fe", border: "1px solid #38bdf8",
        padding: "7px 10px", borderRadius: 6, fontSize: 12 }}>
        Mapped water is excluded automatically. Move any pins placed in water.
      </div>}
      <ClickAndHoverHandler
        drawMode={drawMode}
        boxStart={boxStart}
        setBoxStart={setBoxStart}
        onAddVertex={onAddVertex}
        onSelectBoxArea={onSelectBoxArea}
        onDropInstantObstacle={onDropInstantObstacle}
        onAddTaskOnMap={onAddTaskOnMap}
        onHover={setHoverCoord}
      />

      {/* Authoritative GIS Terrain Layers: Roads, Lakes, Buildings, Walkways */}
      {showGisLayers &&
        environmentFeatures.filter(feature => feature.feature_type !== "WATER_BODY").map((feat) => {
          const st = FEATURE_STYLES[feat.feature_type] || FEATURE_STYLES.RESTRICTED_ZONE;
          return (
            <Polygon
              key={feat.feature_id}
              positions={feat.polygon.vertices.map((c) => [c.lat, c.lon])}
              pathOptions={{
                color: st.color,
                weight: 2,
                dashArray: st.dashArray,
                fillColor: st.fillColor,
                fillOpacity: st.fillOpacity,
              }}
            >
              <Popup>
                <strong>{st.label}</strong>
                <br />
                {feat.name}
                <br />
                Clearance Buffer: {feat.clearance_buffer_m}m
              </Popup>
            </Polygon>
          );
        })}

      {/* Water is a permanent safety layer, independent of optional reference overlays. */}
      <Pane name="water-safety" style={{ zIndex: 430 }}>
        {waterFeatures.map(feature => <Polygon
          key={feature.feature_id}
          positions={feature.polygon.vertices.map(point => [point.lat, point.lon])}
          pathOptions={{ color: "#075985", weight: 3, dashArray: "8, 4", fillColor: "#38bdf8",
            fillOpacity: 0.2, className: "water-safety-exclusion" }}
        >
          <Popup>
            <strong>Water — no robot access</strong><br />
            {feature.name}<br />
            Shoreline clearance: {feature.clearance_buffer_m} m. Water is excluded from cleaning and travel.<br />
            <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">Map data © OpenStreetMap contributors</a>
          </Popup>
        </Polygon>)}
      </Pane>

      {/* Authorized Crossings */}
      {showGisLayers &&
        authorizedCrossings.map((xing) => (
          <React.Fragment key={xing.crossing_id}>
            {xing.corridor_polygon && (
              <Polygon
                positions={xing.corridor_polygon.vertices.map((c) => [
                  c.lat,
                  c.lon,
                ])}
                pathOptions={{
                  color: "#10b981",
                  weight: 2.5,
                  dashArray: "4, 4",
                  fillColor: "#34d399",
                  fillOpacity: 0.35,
                }}
              >
                <Popup>
                  <strong>🌉 {xing.name}</strong>
                  <br />
                  Type: {xing.crossing_type} · Width: {xing.width_m}m
                  <br />
                  Allowed Machines: {xing.allowed_machine_ids.join(", ")}
                </Popup>
              </Polygon>
            )}
            <Polyline
              positions={[
                [xing.start_point.lat, xing.start_point.lon],
                [xing.end_point.lat, xing.end_point.lon],
              ]}
              pathOptions={{
                color: "#34d399",
                weight: 4,
                dashArray: "6, 6",
              }}
            />
          </React.Fragment>
        ))}

      {/* 2-Click Rectangular Box Live Preview */}
      {previewBoxVertices.length === 4 && (
        <Polygon
          positions={previewBoxVertices.map((c) => [c.lat, c.lon])}
          pathOptions={{
            color: "#38bdf8",
            weight: 2.5,
            dashArray: "6, 6",
            fillColor: "#38bdf8",
            fillOpacity: 0.2,
          }}
        />
      )}

      {boxStart && (
        <CircleMarker
              interactive={false}
          center={[boxStart.lat, boxStart.lon]}
          radius={7}
          pathOptions={{
            color: "#0284c7",
            fillColor: "#ffffff",
            fillOpacity: 1,
          }}
        />
      )}

      {/* Requested Cleaning Boundary (User-Drawn Polygon) */}
      {displayBoundary.length >= 3 && (
        <Polygon
          positions={displayBoundary.map((c) => [c.lat, c.lon])}
          pathOptions={{
            color: "#38bdf8",
            weight: 2.5,
            dashArray: "8, 6",
            fillColor: "#0284c7",
            fillOpacity: 0.08,
          }}
        >
          <Popup>
            <strong>Requested Cleaning Boundary</strong>
            <br />
            Requested Area:{" "}
            {Math.round(activeRoute?.requested_area_m2 || activeRoute?.target_area_m2 || 0)}{" "}
            m²
          </Popup>
        </Polygon>
      )}

      {/* Derived Eligible Coverage Cells (Reachable vs Isolated) */}
      {coverageCells.map((cell) => (
        <Polygon
          key={cell.cell_id}
          positions={cell.polygon.vertices.map((c) => [c.lat, c.lon])}
          pathOptions={{
            color: cell.reachable ? "#10b981" : "#f43f5e",
            weight: 2,
            dashArray: cell.reachable ? undefined : "4, 4",
            fillColor: cell.reachable ? "#10b981" : "#ef4444",
            fillOpacity: cell.reachable ? 0.14 : 0.25,
          }}
        >
          <Popup>
            <strong>
              Coverage {cell.cell_id.toUpperCase()} —{" "}
              {cell.reachable ? "✓ Reachable & Eligible" : "⛔ Excluded / Unreachable"}
            </strong>
            <br />
            Eligible Area: {Math.round(cell.area_m2)} m² · Swaths:{" "}
            {cell.swath_count}
            {cell.exclusion_reason && (
              <>
                <br />
                Reason: {cell.exclusion_reason}
              </>
            )}
          </Popup>
        </Polygon>
      ))}

      {/* Excluded / Unreachable Section Warning Markers */}
      {excludedSections.map((sec) => (
        <Marker
          key={sec.section_id}
          position={[sec.centroid.lat, sec.centroid.lon]}
          icon={makePillIcon(
            `⚠️ Excluded (${Math.round(sec.area_m2)}m²)`,
            "#b91c1c",
            "#fecaca"
          )}
        >
          <Popup>
            <strong>Excluded / Unreachable Section</strong>
            <br />
            Area: {sec.area_m2} m²
            <br />
            Reason: {sec.reason}
          </Popup>
        </Marker>
      ))}

      {/* Vertex handles while drawing custom polygon */}
      {drawMode === "BOUNDARY" &&
        boundary.map((v, i) => (
          <CircleMarker
              interactive={false}
            key={`bv-${i}`}
            center={[v.lat, v.lon]}
            radius={5}
            pathOptions={{
              color: "#0284c7",
              fillColor: "#ffffff",
              fillOpacity: 1,
            }}
          />
        ))}

      {/* Explicit Restricted / Dynamic Obstacle Zones */}
      {exclusion.length >= 3 && (
        <Polygon
          positions={exclusion.map((c) => [c.lat, c.lon])}
          pathOptions={{
            color: "#dc2626",
            weight: 2,
            fillColor: "#ef4444",
            fillOpacity: 0.38,
          }}
        />
      )}

      {dynamicObstacles.map((obs) => (
        <Polygon
          key={obs.id}
          positions={obs.vertices.map((c) => [c.lat, c.lon])}
          pathOptions={{
            color: "#e11d48",
            weight: 2.5,
            dashArray: "4, 4",
            fillColor: "#f43f5e",
            fillOpacity: 0.42,
          }}
        >
          <Popup>
            <strong>🚧 Dynamic Obstacle: {obs.name}</strong>
          </Popup>
        </Polygon>
      ))}

      {/* Planned & Completed Cleaning Route Segments + Directional Flow Chevrons */}
      {activeRoute?.segments.map((seg, idx) => {
        if (seg.segment_type === "DEPOT_RECHARGE") return null; // Service stops are listed on the depot marker.
        if (!seg.waypoints || seg.waypoints.length < 2) return null;
        const positions = seg.waypoints.map(
          (w) => [w.lat, w.lon] as [number, number]
        );
        const isCompleted = seg.completed;
        const isSweep = seg.segment_type === "SWEEP_SWATH";
        const isTurn = seg.segment_type === "TURN_CONNECTOR";
        const isReturn = seg.segment_type === "RETURN_TO_BASE";
        const isTransit = !isSweep;
        const isCrossing = Boolean(seg.crossing_id);

        const isBlockedDraft = activeRoute.status === "DRAFT_FAILED_KPI";
        const segColor = isBlockedDraft ? "#94a3b8" : isCompleted
          ? "#64748b"
          : isCrossing
          ? "#f59e0b"
          : isReturn
          ? "#a855f7"
          : isTurn
          ? "#38bdf8"
          : "#0ea5e9";

        // Compute midpoint & screen angle for directional chevron on sweep passes
        const w0 = seg.waypoints[0];
        const wN = seg.waypoints[seg.waypoints.length - 1];
        const midLat = (w0.lat + wN.lat) / 2.0;
        const midLon = (w0.lon + wN.lon) / 2.0;
        const dy = wN.lat - w0.lat;
        const dx = (wN.lon - w0.lon) * Math.cos((midLat * Math.PI) / 180.0);
        const angleDeg = (-Math.atan2(dy, dx) * 180.0) / Math.PI;

        return (
          <React.Fragment key={seg.segment_id}>
            {isSweep && (
              <Polyline
                positions={positions}
                pathOptions={{
                  color: isCompleted ? "#64748b" : "#38bdf8",
                  weight: isBlockedDraft ? 4 : 10,
                  opacity: isBlockedDraft ? 0.08 : 0.24,
                  lineCap: "round",
                }}
              />
            )}
            <Polyline
              positions={positions}
              pathOptions={{
                color: segColor,
                dashArray: isTransit && !isReturn
                  ? "5, 5"
                  : isReturn
                  ? "7, 6"
                  : isSweep ? undefined : routeStyle.dashArray,
                weight: isSweep ? (isBlockedDraft ? 1.5 : 3.5) : isCrossing ? 5 : isReturn ? 3.5 : 2.5,
                opacity: isBlockedDraft ? 0.55 : isCompleted ? 0.45 : isTurn ? 0.7 : 0.96,
              }}
            >
              <Popup>
                <strong>
                  Step #{idx + 1}:{" "}
                  {isSweep
                    ? `Sweep Swath (${seg.cell_id || "cell"})`
                    : isCrossing
                    ? `Authorized Road Crossing (${seg.crossing_id})`
                    : isTurn
                    ? "Serpentine Turn Connector"
                    : isReturn
                    ? "Return to Depot"
                    : "Graph Transit Leg"}
                </strong>
                <br />
                Distance: {Math.round(seg.distance_m)}m · Est.{" "}
                {Math.round(seg.estimated_duration_sec)}s
                <br />
                Provenance: <code>{seg.provenance}</code> · Status:{" "}
                <strong>{seg.completed ? "✓ Completed" : "Planned"}</strong>
              </Popup>
            </Polyline>
            {isSweep && arrowSegments.has(seg.segment_id) && !isCompleted && seg.distance_m >= 8 && (
              <Marker
                position={[midLat, midLon]}
                icon={makeDirectionChevronIcon(
                  angleDeg,
                  isCompleted ? "#34d399" : "#38bdf8",
                  `Pass #${idx + 1}`
                )}
                interactive={false}
              />
            )}
          </React.Fragment>
        );
      })}

      {/* Start Point Marker */}
      {hasMission && effectiveStart && (
        <Marker
          position={[effectiveStart.lat, effectiveStart.lon]}
          icon={makePillIcon("🏁 Start", "#0284c7")}
        >
          <Popup>
            <strong>Validated Origin Access Point</strong>
            <br />
            {effectiveStart.lat.toFixed(5)}, {effectiveStart.lon.toFixed(5)}
          </Popup>
        </Marker>
      )}

      {/* Disposal Depot Marker */}
      {hasMission && effectiveDepot && (
        <Marker
          position={[effectiveDepot.lat, effectiveDepot.lon]}
          icon={makePillIcon("♻️ Depot", "#7c3aed")}
        >
          <Popup>
            <strong>Validated Waste Disposal &amp; Recharge Depot</strong>
            <br />
            {effectiveDepot.lat.toFixed(5)}, {effectiveDepot.lon.toFixed(5)}
            {activeRoute?.depot_cycles_enabled && <>
              <br />{activeRoute.outing_count ?? 1} outings; {activeRoute.depot_visits ?? 0} simulated recharge stops.
              <br />Service total: {activeRoute.charging_duration_minutes?.toFixed(1) ?? "0"} min.
              <ul>{activeRoute.segments.filter(segment => segment.segment_type === "DEPOT_RECHARGE").map((segment, index) => <li key={segment.segment_id}>
                DEPOT_RECHARGE #{index + 1}: {(segment.estimated_duration_sec / 60).toFixed(1)} min — {segment.completed ? "Completed" : "Planned"}
              </li>)}</ul>
            </>}
          </Popup>
        </Marker>
      )}

      {/* Bin & Bulky Waste Pins */}
      {displayTasks.map((t, idx) => {
        const isOversized =
          t.task_type === "OVERSIZED_RUBBISH" || t.rubbish_spec?.is_oversized;
        const bg = t.completed
          ? "#10b981"
          : isOversized
          ? "#e11d48"
          : "#2563eb";
        const label = t.completed
          ? `✓ Done #${idx + 1}`
          : isOversized
          ? `🛋️ Bulky #${idx + 1}`
          : `🗑️ Bin #${idx + 1}`;
        return (
          <Marker
            key={t.task_id}
            position={[t.location.lat, t.location.lon]}
            icon={makePillIcon(label, bg)}
          >
            <Popup>
              <strong>
                {isOversized ? "Bulky Waste Pickup" : t.waste_observation?.waste_class || "Bin Collection Stop"} #
                {idx + 1}
              </strong>
              <br />
              {t.rubbish_spec?.description || t.task_id}
              <br />
              Collection window: {t.time_window_start_min.toFixed(1)}–{t.time_window_end_min.toFixed(1)} min since job start
              <br />Service: {t.service_duration_sec.toFixed(0)} s (must finish by deadline)
              <br />
              Status:{" "}
              <strong>{t.completed ? "✓ Collected" : activeRoute?.skipped_tasks.some(item => item.task_id === t.task_id) ? "Not assigned — review planning diagnosis" : activeRoute?.task_schedule.some(entry => entry.task_id === t.task_id) ? "Scheduled" : "Pending plan"}</strong>
              {activeRoute?.task_schedule.filter(entry => entry.task_id === t.task_id).map(entry => <React.Fragment key={entry.task_id}>
                <br />Arrival: {entry.arrival_min.toFixed(1)} min; service finish: {entry.service_end_min.toFixed(1)} min.
              </React.Fragment>)}
            </Popup>
          </Marker>
        );
      })}

      {/* Live Robot Marker */}
      {hasMission && <Marker
        position={[robotPosition.lat, robotPosition.lon]}
        icon={makePillIcon(
          latestTelemetry?.provenance === "MEASURED_TELEMETRY"
            ? "🤖 Robot (Live Telemetry)"
            : latestTelemetry?.provenance === "SIMULATED"
            ? "🤖 Robot (Sim)"
            : "🤖 Robot Standby",
          "#059669",
          "#ffffff",
          true
        )}
      >
        <Popup>
          <strong>Autonomous Cleaning Robot</strong>
          <br />
          Position: {robotPosition.lat.toFixed(5)},{" "}
          {robotPosition.lon.toFixed(5)}
          <br />
          Provenance:{" "}
          <code>{latestTelemetry?.provenance || "PLANNED_ORIGIN"}</code>
        </Popup>
      </Marker>}
    </MapContainer>
  );
};

export default MapWorkspace;
