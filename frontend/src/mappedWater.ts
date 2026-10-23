import waterMap from "../../shared/perdana-water.json";
import type { TerrainFeature } from "./types/api";
import { interiorWaterExample } from "./waterSafety";

// The backend reads the same attributed shoreline geometry.
export const MAPPED_WATER_FEATURES: TerrainFeature[] = waterMap.features.map(feature => ({
  ...feature, feature_type: "WATER_BODY",
}));

export const MAPPED_LAKE_TEST_AREA = interiorWaterExample(
  MAPPED_WATER_FEATURES.find(feature => feature.feature_id === "water-osm-relation-8429206-39923096"));
