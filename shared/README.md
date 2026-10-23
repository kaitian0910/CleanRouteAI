# Local water safety data

`perdana-water.json` contains 17 mapped surface-water exclusion polygons around
Perdana Botanical Gardens, Kuala Lumpur. The backend and frontend use the same
file. It replaces the previous approximate lake/channel rectangles.

Source: [OpenStreetMap contributors](https://www.openstreetmap.org/relation/8429206),
downloaded 23 October 2026 via the [OSM map API](https://api.openstreetmap.org/api/0.6/map?bbox=101.683,3.137,101.691,3.146).
Data is available under the [Open Database License](https://www.openstreetmap.org/copyright).
Keep this attribution and the JSON metadata with redistributed data. Metadata
records source object links, versions, timestamps and the download SHA-256.

The coverage box is longitude 101.683–101.691, latitude 3.137–3.146. The built-in
reference map excludes unknown areas outside that box. An imported site retains
its declared access footprint, but cannot erase known water exclusions. The
snapshot is not a live map feed or a field survey. Missing waterways, changing
water levels, bridges, surface conditions and permissions require site data.

Lake islands are conservatively excluded with the lake: this application has no
verified access to them. Open stream centre-lines do not specify bank widths, so
the extractor does not invent water-area polygons from them. Other built-in road,
building and footpath geometry remains illustrative.

The planner adds a 1 m shoreline margin, then checks the machine footprint. A
sweeping pass also checks brush width. Start, depot and pickup coordinates in
water are rejected rather than moved to a different location. Water avoidance is
independent of the reference-overlay checkbox, route approval and PPO settings.

To rebuild from a reviewed OSM map API XML download:

```powershell
.\venv\Scripts\python.exe scripts/extract_water_snapshot.py input.osm shared/perdana-water.json
```

Review source coverage, snapshot date/version, omitted relation members and the
regression tests before replacing the bundled safety map.
