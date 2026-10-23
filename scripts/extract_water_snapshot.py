"""Build the bundled local water exclusions from an OSM map API XML download.

This script reads data only; it never downloads or executes source content.
Usage: python scripts/extract_water_snapshot.py input.osm shared/perdana-water.json
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def extract(source: Path) -> dict:
    payload = source.read_bytes()
    root = ET.fromstring(payload)
    bounds = root.find("bounds").attrib
    nodes = {node.attrib["id"]: {"lat": float(node.attrib["lat"]), "lon": float(node.attrib["lon"])}
             for node in root.findall("node")}
    ways = {way.attrib["id"]: way for way in root.findall("way")}
    features, provenance, included = [], [], set()

    def tags(element):
        return {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}

    def add(way, source_element, name, islands=False):
        refs = [node.attrib["ref"] for node in way.findall("nd")]
        if len(refs) < 4 or refs[0] != refs[-1] or any(ref not in nodes for ref in refs):
            raise ValueError("Incomplete water outline; fetch the full OSM way before updating the safety map.")
        feature_id = f"water-osm-{source_element.tag}-{source_element.attrib['id']}-{way.attrib['id']}"
        features.append({"feature_id": feature_id,
                         "name": name + (" (exclusion includes islands)" if islands else ""),
                         "feature_type": "WATER_BODY", "clearance_buffer_m": 1.0,
                         "polygon": {"vertices": [nodes[ref] for ref in refs[:-1]]}})
        provenance.append({"feature_id": feature_id,
                           "source_url": f"https://www.openstreetmap.org/{source_element.tag}/{source_element.attrib['id']}",
                           "version": int(source_element.attrib["version"]),
                           "timestamp": source_element.attrib.get("timestamp"),
                           "outer_way": int(way.attrib["id"]),
                           "islands_conservatively_excluded": islands})
        included.add(way.attrib["id"])

    for relation in root.findall("relation"):
        values = tags(relation)
        if values.get("natural") != "water" and values.get("waterway") != "riverbank":
            continue
        members = relation.findall("member")
        for member in members:
            if member.attrib.get("type") == "way" and member.attrib.get("role") == "outer":
                way = ways.get(member.attrib["ref"])
                if way is None:
                    raise ValueError("Water relation is missing an outer way.")
                add(way, relation, values.get("name", "Mapped water"),
                    islands=any(item.attrib.get("role") == "inner" for item in members))
    for way in ways.values():
        values = tags(way)
        if way.attrib["id"] not in included and (values.get("natural") == "water" or values.get("waterway") == "riverbank"):
            add(way, way, values.get("name", f"Mapped {values.get('water', 'water')} (OSM {way.attrib['id']})"))
    if not features:
        raise ValueError("No water polygons found; refusing to overwrite the safety map.")
    return {"metadata": {
        "snapshot_id": "perdana-water-2026-10-23", "retrieved_date": "2026-10-23",
        "source": "OpenStreetMap contributors", "license": "ODbL-1.0",
        "license_url": "https://www.openstreetmap.org/copyright",
        "source_url": "https://api.openstreetmap.org/api/0.6/map?bbox=101.683,3.137,101.691,3.146",
        "coverage_bbox": [float(bounds[key]) for key in ("minlon", "minlat", "maxlon", "maxlat")],
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "notes": ["Mapped surface-water polygons, not a survey or a live water sensor.",
                  "Lake islands are conservatively excluded; safe access to them is not established.",
                  "Open stream centre-lines do not establish bank widths and are not converted to guessed water polygons.",
                  "Map omissions, shoreline changes and conditions still require a site safety check."],
        "features": provenance}, "features": features}


if __name__ == "__main__":
    output = Path(sys.argv[2])
    result = extract(Path(sys.argv[1]))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(result['features'])} water exclusion polygons to {output}")
