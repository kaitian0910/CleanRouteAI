import type { WasteObservation } from "./types/api";

/** Predictions below this score are counted as Others rather than a specific material. */
export const VISION_CLASSIFICATION_CONFIDENCE = 0.6;

export type VisionCategory = "plasticAndCans" | "paperAndPackaging" | "others";
export type VisionClassificationObservation = Partial<Pick<WasteObservation,
  "detection_id" | "waste_class" | "confidence"
>>;

const categories: Readonly<Record<string, VisionCategory>> = {
  plasticbagwrapper: "plasticAndCans",
  bottle: "plasticAndCans",
  bottlecaplid: "plasticAndCans",
  can: "plasticAndCans",
  papercardboard: "paperAndPackaging",
  cup: "others",
  cigarette: "others",
  otherlitter: "others",
};

function knownCategory(observation: VisionClassificationObservation): VisionCategory | undefined {
  const label = observation.waste_class?.toLowerCase().replace(/[^a-z0-9]/g, "");
  return label && Object.prototype.hasOwnProperty.call(categories, label) ? categories[label] : undefined;
}

export function isUncertainVisionObservation(
  observation: VisionClassificationObservation,
  threshold = VISION_CLASSIFICATION_CONFIDENCE,
): boolean {
  const score = observation.confidence;
  return typeof score !== "number" || !Number.isFinite(score) || score < threshold || score > 1
    || knownCategory(observation) === undefined;
}

export function classifyVisionObservation(
  observation: VisionClassificationObservation,
  threshold = VISION_CLASSIFICATION_CONFIDENCE,
): VisionCategory {
  return isUncertainVisionObservation(observation, threshold) ? "others" : knownCategory(observation)!;
}

/** Summarize one inference result. Callers replace the previous result instead of adding camera frames. */
export function summarizeVisionDetections(
  observations: readonly VisionClassificationObservation[],
  threshold = VISION_CLASSIFICATION_CONFIDENCE,
) {
  const counts = { plasticAndCans: 0, paperAndPackaging: 0, others: 0, totalItems: 0, uncertainItems: 0 };
  const seen = new Set<string>();
  for (const observation of observations) {
    const id = observation.detection_id?.trim();
    if (id) {
      if (seen.has(id)) continue;
      seen.add(id);
    }
    counts[classifyVisionObservation(observation, threshold)]++;
    counts.totalItems++;
    if (isUncertainVisionObservation(observation, threshold)) counts.uncertainItems++;
  }
  return counts;
}
