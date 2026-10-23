import React from "react";
import type { WasteObservation } from "./types/api";
import type { collectedWasteStats } from "./routeExecution";
import { summarizeVisionDetections } from "./visionClassification";

export default function WasteSummary({ observations, completedCollections }: {
  observations: WasteObservation[];
  completedCollections: ReturnType<typeof collectedWasteStats>;
}) {
  const counts = summarizeVisionDetections(observations);
  return <section className="waste-collected-card" aria-label="Waste classification summary">
    <div className="section-box-title"><span>Waste classified</span><span className="weight-pill">{counts.totalItems} detected items</span></div>
    <p className="waste-summary-note">Latest photo or camera frame. Counts update automatically and replace the previous image's counts.</p>
    <div className="waste-2x2-grid" aria-live="polite">
      <div className="waste-cell waste-cell-plastic"><span className="waste-cell-label">Plastic &amp; Cans</span><strong className="waste-cell-val">{counts.plasticAndCans} items</strong></div>
      <div className="waste-cell waste-cell-paper"><span className="waste-cell-label">Paper &amp; Packaging</span><strong className="waste-cell-val">{counts.paperAndPackaging} items</strong></div>
      <div className="waste-cell waste-cell-others"><span className="waste-cell-label">Others <small>Other litter, uncertain labels &amp; unknown materials</small></span><strong className="waste-cell-val">{counts.others} items</strong></div>
    </div>
    <p className="waste-summary-note">{counts.uncertainItems > 0 ? `${counts.uncertainItems} uncertain item${counts.uncertainItems === 1 ? " is" : "s are"} included in Others. ` : ""}Image classification does not confirm physical collection or measure weight.</p>
    <div className="completed-pickup-summary" aria-label="Completed job pickups">
      <strong>Completed job pickups</strong>
      <span>Standard bins emptied: <b>{completedCollections.binsEmptied}</b></span>
      <span>Bulky items: <b>{completedCollections.bulkyItems}</b></span>
      <span>Total collected this run (estimated): <b>{completedCollections.unknownWeightCollections && completedCollections.totalWeightKg === 0
        ? "Weight not recorded" : `${completedCollections.totalWeightKg} kg`}</b></span>
      {!!completedCollections.unknownWeightCollections && <span>Weight not recorded for {completedCollections.unknownWeightCollections} completed pickups.</span>}
      <p className="waste-summary-note">This total stays after depot unloading. Waste on board shows only what the hopper currently holds. Weight uses pickup estimates, including the editable default for standard bins.</p>
    </div>
  </section>;
}
