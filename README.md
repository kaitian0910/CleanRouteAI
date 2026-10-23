# CleanRoute AI — Intelligent Waste Detection and Adaptive Route Planning

CleanRoute AI is a local route-planning API and React/Vite operations console for area sweeping, patrol, and collection tasks. Its execution path is a deterministic simulator. It does not command a real vehicle.

A cleaning run contains plan revisions; each revision may compare several route options. **Logs & changes** lists published plans, with their checked options and raw events available under closed technical details. Comparing options is neither executing multiple runs nor training the RL model.

**Clear Map** first confirms cancellation of an unfinished backend job. Applying another site or starting an independent plan also closes the previous job before replacing it. Completed work and logs remain recorded for the running server session. Cancellation releases a machine reservation; it cannot reset an emergency stop. The cancellation API is `POST /api/routes/{route_id}/cancel` and safely targets the job's latest revision.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer

## Start the backend (PowerShell)

From the project root:

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
$env:CLEANROUTEAI_OPERATOR_TOKEN = "local-dev-operator-token"
$env:CLEANROUTEAI_SUPERVISOR_TOKEN = "local-dev-supervisor-token"
$env:CLEANROUTEAI_ADMIN_TOKEN = "local-dev-admin-token"
$env:CLEANROUTEAI_INTERNAL_SERVICE_TOKEN = "local-dev-service-token"
python -m uvicorn backend.main:app --reload
```

If the virtual environment and dependencies are already installed, the normal restart command from the project root is `.\venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000`. Keep this terminal running and run `npm run dev` in a second terminal in `frontend`. Dependency installation is only needed initially or after requirements change.

The API is at `http://localhost:8000`; health is available at `http://localhost:8000/api/health`. Operational endpoints require a bearer token. Use an operator, supervisor, or admin token in the console for planning and simulation. The internal service token is required to ingest telemetry at `POST /api/telemetry`. These sample tokens are for local development only.

The backend uses an explicitly labeled synthetic routing graph when Valhalla is unavailable. Responses identify its provenance as `SYNTHETIC_FALLBACK_GRAPH`. To require Valhalla, set `$env:CLEANROUTEAI_ALLOW_LABELED_DEMO_ROUTING = "false"` and configure `$env:CLEANROUTEAI_VALHALLA_URL` before starting the backend. Geocoding is not configured: use latitude/longitude coordinates.

The backend reads these `CLEANROUTEAI_` settings at startup; restart it after changes. Supported configuration includes `ENVIRONMENT`, the four role `*_TOKEN` values above, `VALHALLA_URL` (HTTP or HTTPS), `ALLOW_LABELED_DEMO_ROUTING`, and `ENABLE_DEFAULT_CAMPUS_TERRAIN_LAYER`. Boolean values accept `true`/`false`, `1`/`0`, `yes`/`no`, or `on`/`off`; invalid values fail startup instead of silently enabling a feature. `CLEANROUTEAI_CORS_ORIGINS` accepts a JSON list of HTTP(S) origins or a comma-separated list. For example, `$env:CLEANROUTEAI_CORS_ORIGINS = '["http://localhost:5173","http://localhost:5174"]'`. Hardware actuation remains disabled through this configuration loader.

## Start the frontend

In a second PowerShell window, from `frontend`:

```powershell
npm install
npm run dev
```

Open the Vite URL printed in the terminal (normally `http://localhost:5173`). The frontend uses `http://localhost:8000/api` by default. Set `$env:VITE_API_BASE` before `npm run dev` to use another API URL.

## Planning and safety behavior

When a job finishes, open **Feedback** for a simple result: cleaning time, cleaned area, collections, battery and recharge stops. Route comparisons and recent finished jobs are shown below. Model schemas, rewards and detailed diagnostics are inside the closed **Technical details** section; demonstration controls are under **Examples**.

A repeat creates a new plan, but the selected path can be unchanged. Feedback now reports how many alternatives were checked, whether sweeping, bin order or travel changed, and previous recorded simulation time separately from the new estimate. Static workload history can suggest a sweep angle even when the next run starts at the depot with a different battery. Completed simulated timing takes priority over unexecuted estimates when choosing this search hint; the planner regenerates and evaluates the route with current resources. This is history-assisted planning, not online PPO training or a guarantee of faster real-world cleaning. The full workload fingerprint still includes live resources for strict result comparisons.

The supplied PPO checkpoint operates on compatible six-bin collection rounds. It cannot choose an area-sweeping pattern, and Feedback states when it was not used. Supporting learned sweeping decisions requires a policy trained for that observation/action contract; changing a route drawing would not establish learning.

Choose **Run this area again** to start a fresh simulated job with the same area, obstacles, depot, bin locations and operating limits. The system checks the current machine battery and payload, plans any needed depot service, validates the new route, then starts it. Past results are kept. This repeats routine bins and area cleaning; collected YOLO detections and bulky objects need a new observation before they become work again. Each click requests one new job; running again is separate from resuming a paused job. If the new route cannot pass its checks, the reason is shown for review.

- Requests validate coordinate ranges, task uniqueness, operating-boundary membership, exclusion-zone conflicts, machine payload limits, and optional task time windows. Repeated task idempotency keys are de-duplicated; conflicting duplicate reports are rejected.
- Area coverage is generated inside a boundary inset by the machine clearance. Exclusions are buffered by the machine working width. The planner routes connectors around restrictions when possible; any remaining unreachable or invalid geometry blocks approval and simulation.
- Collection planning accounts for priority, service time, elapsed-mission-minute time windows, travel, machine weight and volume limits, and disposal trips. A task must finish service by its window end. If the machine arrives before the window opens, the schedule includes waiting. Unscheduled tasks are reported and cause KPI failure when full collection is required.
- The bounded candidate loop compares sweep angles and overlap settings. With depot cycles enabled, alternatives include the detours and depot service in their completion estimates. The independent KPI evaluator checks coverage, missed/repeated coverage, outing duration and distance, battery reserve, payload, collection completion, time windows, and route geometry. Suggestions in candidate rationale do not override failed constraints.
- New waste, missed collections, obstacles with a supplied exclusion polygon, changed time windows, route deviation, low-battery telemetry, and operating-boundary changes can create a versioned replan. Replans start from the latest reported position and elapsed mission minute, retain completed work, and compare remaining distance/time plus coverage, missed area, battery, and open collection tasks. A low-battery replan that cannot meet reserve, an obstacle without a validated polygon, or a sensor fault enters `NEEDS_ASSISTANCE`; an emergency-stop event enters `EMERGENCY_STOP`. Repeated event `idempotency_key` or `detection_id` values return the original event result. Requests tied to stale route versions are rejected.
- Execute/Resume advances the deterministic simulator by one segment per request. Mission elapsed time advances by that segment's estimate, including waits, collection service, and depot service. It checks state, KPI status, task deadline, battery reserve, and payload limits before each segment. The KPI panel separates planned task assignment from tasks actually completed in simulation. E-stop reset is unavailable without a trusted machine adapter.

Coverage uses the machine's actual brush width, including large areas. Sweep blocks are ordered to avoid repeatedly crossing around obstacles and start near the current position. The map samples direction arrows and distinguishes sweep passes, dashed transit, and muted completed travel. Same-machine replans send `preserve_completed: true`, retaining server-recorded completion, position, elapsed time, battery, and payload.

## Return to depot, recharge, and resume

The Route view enables **Return to depot, recharge & resume** by default. This plans the entire job as multiple outings: complete a feasible portion, return along a validated path to the depot, receive simulated depot service, and travel back to the exact start of the next unfinished segment. The job ends after the remaining work and final depot return. Obstacle replans within the same operating boundary retain completed work. A different daily boundary is evaluated as a changed workload.

**Sweep + Bins** schedules the collection round before the long sweep, unloads at the depot, then joins the remaining coverage route. This prevents a several-hour sweep from consuming a bin's 90-minute deadline before collection begins. Collection, travel, waits and recharge share the same elapsed mission clock; genuinely infeasible deadlines still block a proposal. Newly placed pins receive a window starting at the current mission minute. Existing task windows remain unchanged and can be reviewed before replanning.

Default resource gates apply to **each outing**: at most **90 minutes**, **15,000 metres**, and at least **20% battery reserve throughout movement, including the return**. The whole job may exceed 90 minutes and 15,000 metres. Total completion time includes work, transit, waiting, collection, and all depot service. For a replan, the completion estimate is the current elapsed mission time plus the remaining schedule. Existing collection deadlines still apply; a depot visit does not extend a task's time window.

Depot visits unload the payload. Service takes **20 simulated minutes** by default and resets battery to **100%** only when the stationary service segment is completed at the depot. `MachineProfile.depot_service_minutes` and `recharged_battery_percent` configure these simulation assumptions. The console advances this time through the simulator; it does not wait 20 real minutes or communicate with a physical charger. Recharge events appear in **Logs & changes**. A pause stops at a segment boundary, and resuming continues the same remaining schedule.

The plan and Feedback views report outing count, depot visits, service time, minimum battery, longest outing, and total completion time. Invalid or unreachable depot paths, insufficient capacity for even one work portion plus its safe return, failed geometry, missed collection deadlines, or more than **32 planned service stops** keep a proposal blocked. The planner retains the unfinished work instead of silently deleting it. Move the depot, change the task, or reduce the selected area when one of these conditions applies.

API callers opt in with `"enable_depot_cycles": true` on `POST /api/routes/plan`. Its default remains `false` for compatibility; when disabled, duration, distance, and battery limits apply to the single mission, and a large area may need to be divided manually. The UI sends its checked setting with planning and replanning requests. This changes planning and simulation only; it adds no physical charging or actuation.

## Execution recovery and optional planning inputs

The browser execution client reconciles recorded route progress before each step. It preserves native `Window.fetch` binding, uses a stable step request ID and expected completion count, and stops when a response cannot be confirmed. **Reconnect & resume simulation** reads the current backend state before continuing. Retrying the same accepted step cannot complete a second segment. Connection errors appear separately from planning feasibility failures.

The ordinary workflow remains draw an area, place collection tasks, plan, review, and run. **Advanced planning inputs** is optional and collapsed by default:

- Import an operator-provided GeoJSON site, preview it, then apply its boundary, start, depot, exclusions, terrain and collection points. See `backend/examples/illustrative_site.geojson` for the supported format. This example is illustrative, not surveyed. Every feature needs an explicit `properties.role`: `boundary`, `start`, `depot`, `traversable`, `exclusion`, `terrain`, or `bin`. The file must include a boundary, start, depot and at least one traversable polygon. Coordinates use `[longitude, latitude]`. Polygon holes, multipolygons and street-network edges must be converted to the supported forms rather than silently ignored.
- Adjust machine width, clearance, speeds, battery capacity, energy use, service time and recharge level. Saved parameters persist in `backend/runtime/machine_profiles.json`; live battery and payload are not overwritten. Parameter changes invalidate affected proposals and approvals and require replanning. Updates are blocked during execution.
- Set the next plan's outing duration, distance and battery reserve. The default 90-minute limit is an operating policy; it can trigger service while battery remains above reserve. Charging time is a separate configurable estimate.

Imported traversable polygons constrain all travel and return paths, including machine clearance. Importing a file does not verify a survey or grant road access. The built-in map now uses a shared, attributed [OpenStreetMap water snapshot](shared/README.md) for nearby lake and pond outlines. Water is automatically excluded from sweeping, collection travel and depot returns, with a shoreline margin and machine/brush clearance. A drawn boundary or imported traversable polygon cannot remove these known water exclusions. Start, depot and pickup pins in water receive a clear error; generated draw-area defaults choose dry positions when available. Water exclusions remain visible with reference overlays off.

The built-in snapshot has a defined local coverage boundary. Unknown areas beyond it are excluded; import explicit site geometry to plan elsewhere. Other built-in road/building features remain illustrative. The snapshot is not a live water sensor or a complete survey, and map tiles do not supply live obstacle/access data. The simplified offline preview refuses water-overlapping requests and requires the backend's detour solver. Execution remains simulation.

## YOLO waste detection

The supplied model is installed at `backend/vision_assets/best.pt`. The original `best (4).pt.zip` is already a ZIP-based PyTorch checkpoint, so its bytes are preserved with the `.pt` extension rather than extracting its internal tensor files. The original ZIP stays in Downloads and is not used at runtime. It is a YOLO11s detector trained with Ultralytics 8.4.184 on eight classes: Plastic Bag & Wrapper, Cigarette, Bottle, Bottle Cap & Lid, Can, Cup, Paper & Cardboard, and Other Litter.

Install the updated backend requirements in the project virtual environment and restart the backend. Open **Real-time → YOLO & Waste Categories** in the console:

1. Upload a JPEG, PNG, or WebP image (up to 10 MiB / 20 megapixels), or choose **Use webcam** and allow camera access. Webcam access requires localhost or HTTPS.
2. Choose **Add photo** or **Start camera**. Review possible litter; webcam inference sends one frame at a time and displays the analyzed frame so boxes match the image. **Stop camera** keeps a frame for review. Portrait and landscape images fit the preview, with controls always available. A running request shows elapsed time and supports **Cancel check**.
3. **Detection settings** is closed by default. The initial minimum score is **25%**; a 95% setting hides predictions below 95% and does not describe model accuracy. Settings affect the next photo check or camera frame. **Check photo again** applies changes; **Use default setting and check again** explicitly resets a strict filter and reruns detection. No threshold changes silently.
4. Detections automatically update **Waste classified**: Plastic & Cans, Paper & Packaging, or Others. A recognized material with a model score of at least **60%** uses its material category. Lower-scoring, invalid-score or unrecognized predictions go to Others. Cups, cigarettes and other litter also use Others because their material is not established. The 60% categorization cutoff is an application rule, not a measured accuracy guarantee; detections below the selected minimum score are not returned or counted.
5. There is no map pin, collection-task creation or weight/size confirmation in this flow. Counts represent the **latest photo or camera frame**, not a running collection total. Rechecking replaces the prior counts, detection IDs are deduplicated within a result, and a successful empty result shows zero. **Clear image & counts** clears the current image and classification. Route replans do not erase an unchanged visible image's counts.

Detection never creates GPS coordinates from image boxes or increments collected-waste counters. This model detects litter; it is not an obstacle or bulky-furniture detector. The old random bounding boxes and fabricated FPS animation have been removed.

The three material cards count image detections directly. **Completed job pickups** remains separate: bins, bulky items and their operator-estimated collected weight change when the corresponding simulated job tasks complete. Image classification does not confirm physical collection or measure weight, and legacy completed image tasks are not added a second time to current-image category counts.

Standard bin pins now use the existing default contents estimate of **15 kg / 120 L**, rather than adding zero load when no specification is supplied. Edit each pickup's estimated kg and litres in the Route task settings before replanning. The planner and simulator use those same estimates for capacity and unloading. **Waste on board** resets after depot unloading; **Total collected this run** stays cumulative. Old pickups without weight records are labelled as unrecorded rather than assigned a weight after completion.

The backend exposes authenticated `GET /api/vision/status` and `POST /api/vision/detect?confidence=0.25`. The POST accepts multipart form field `image` and returns model name, image dimensions, inference milliseconds, and `WasteObservation` records. Their `world_location` and `rubbish_spec` remain unset; the classification screen does not require them. API clients can still supply separately validated observations for collection planning. For example:

```powershell
curl.exe -X POST "http://localhost:8000/api/vision/detect?confidence=0.25" -H "Authorization: Bearer dev-operator-token" -F "image=@C:\path\to\litter.jpg"
```

CPU inference is the default. To select another local checkpoint or use a configured CUDA device, set these before starting the backend:

```powershell
$env:CLEANROUTEAI_YOLO_MODEL_PATH = "C:\path\to\best.pt"
$env:CLEANROUTEAI_YOLO_DEVICE = "0"
```

Only select `0` when the installed PyTorch build supports your CUDA GPU. Model imports and loading are deferred until vision is used, keeping planning independent of model availability. Missing weights, missing inference dependencies, and inference failures produce actionable status messages / HTTP 503 responses; invalid images produce HTTP 422, and oversized uploads HTTP 413. The implementation follows the [Ultralytics prediction API](https://docs.ultralytics.com/modes/predict/).

## Testing obstacle replanning

Choose **Small obstacle test area (~1,800 m2)** from the Route scenario selector, then **Standard Sweep**. Select **Mark Obstacle** and click inside the planned area. A red exclusion appears, its counter increases, and the planner returns a revised version around it. Placement mode uses a crosshair and disables polygon/marker popups until the click is handled. Completed work is preserved when an executing mission is revised. Approve the passing revision before continuing simulation.

The map retains the current route while recalculation is pending. A blocked proposal is shown as a muted draft with the evaluated resource limits. With **Return to depot, recharge & resume** enabled, large jobs receive multiple outings when safe depot access permits. An obstacle can still make a work portion or depot inaccessible, or cause another hard constraint to fail. With the feature disabled, a large area can still exceed the single-mission duration and battery gates. The small test scenario is convenient for a quick execution test in either mode.

## Reinforcement-learning feedback and logs

Install the optional RL dependencies and restart the backend:

```powershell
python -m pip install -r backend/requirements-rl.txt
```

The compact mission overview shows simulated elapsed time, battery with the plan's configured reserve, waste load and current activity. Load comes from backend telemetry or a labelled machine snapshot; image classifications do not measure hopper weight.

Open **Feedback** in the sidebar. **Summary** shows results and the next-run action; **Plan checks** explains the latest planning decision and displays battery, outing time, payload and access checks; **Tasks** shows recorded priorities, service windows and finished or excluded pickups. The limits belong to the selected plan. Use **Edit limits on Route map** to open the existing planning controls. **Logs & changes** shows published plan revisions and a timestamped activity timeline, with alerts and depot service events. Alternative options and raw audit events stay collapsed. Recent-event counts describe only the loaded log, not lifetime totals.

Policy availability, mission compatibility, candidate rewards and KPI differences remain under **Technical details**. Under **Examples**, use **Run six-bin RL demo** to compare the supplied checkpoint with the existing planner on an explicitly simulated six-bin mission. The demo proposes a route without approving or executing it. Ordinary missions retain the deterministic planner when the policy's six-bin inputs are unavailable. Inference is frozen; no model weights change during missions.

For daily area changes, the planner regenerates feasible sweep alternatives and compares completion time with depot detours and service included. Feedback shows the resulting candidate differences and selection. Selected feasible depot-cycle plans and their eventual simulated completion times are saved locally in `backend/runtime/mission_history.json` (ignored by Git) and exposed by `GET /api/feedback/history`, optionally filtered with `?machine_id=...`. This summary history survives a backend restart. If a later job has the same workload fingerprint, a previously feasible sweep angle can be reevaluated first as a warm start; it must pass the current planner and safety checks again. Fingerprints include area, obstacles, depot/start, task workload and windows, machine resources, terrain, and KPI settings. Compare completion times only for matching conditions.

This is deterministic route optimization and recorded feedback: the supplied six-bin PPO cannot learn area-sweep layouts, sweep order, or recharge decisions from daily execution. Saved planning summaries do not train the model. Online learning, a persistent execution-transition training dataset, and a trained sweep/depot policy are not implemented. A compatible training environment, recorded transitions, and offline training/evaluation are needed for that extension. History read/write problems are surfaced through the history API; the file is local to this installation, rather than a shared database.

For a repeatable paired comparison:

```powershell
python -m backend.evaluate_rl --output backend/rl_evaluation.json
```

## Demo walkthrough

1. Start the backend and frontend, and enter the operator token.
2. Draw an operating boundary; this selects the area without starting execution. Choose the task type and keep **Return to depot, recharge & resume** checked for a job requiring multiple outings. For collection modes, place actual collection pins; the console no longer invents two bin locations. Bin Pickup and Bulky Waste plan collection-only travel. Move Depot replans to the new location. YOLO image results update material categories directly without placing pins or changing the route.
3. For a collection task, adjust its priority, service time, and elapsed-minute time window. Add a waste report by selecting its observed location on the map.
4. Plan the route. Review provenance, candidate rationale, schedule, skipped tasks, unreachable sections, and KPI failures. A route with failed constraints or geometry issues cannot be approved.
5. Select **Approve & Run** on a passing route. The console advances the backend simulator one segment per request and displays its recorded position, battery, task completion, and mission time. Scheduled depot visits reset the simulated battery after service, then work resumes automatically. Inspect total completion time and outing limits separately. Pause stops at a segment boundary; Resume continues. Auto-approval is off by default. E-stop is latched for the machine: clearing the map or creating a new job cannot reset it. No trusted hardware reset adapter is implemented. Restarting the backend creates a fresh in-memory simulator; it is not a physical machine reset.
6. Report an obstacle by drawing its exclusion polygon, mark a collection as missed, or revise a pending task's time window. Inspect the new route version, remaining itinerary, and KPI comparison.

## Teammate extension handoff

- **Waste detection:** `backend/vision.py` runs the local YOLO checkpoint and produces `WasteObservation` records. `visionClassification.ts` groups the latest image results and `WasteSummary.tsx` displays their counts separately from completed job pickups. The screen does not create map tasks or invent physical weight or coordinates. `OperationalEventRequest.waste_observation` remains available for external event producers with separately validated world locations and estimates. Camera-to-world calibration is not included.
- **Reinforcement learning:** the supplied PPO checkpoint uses a six-bin dispatch contract (48 observations, 14 discrete actions). Its constrained task preference is compared with the existing deterministic baseline; it does not predict sweep settings. Missing or incompatible mission data uses explicit fallback. See [the integration report](backend/RL_INTEGRATION.md) for contracts, rewards, exact verification commands, measured comparison and limitations.

## Checks

From the project root:

```powershell
python -m pytest backend -q -p no:cacheprovider
```

From `frontend`:

```powershell
npm run build
npm test
```

To verify native browser execution against isolated local servers, run from the project root (Chrome or Edge and installed frontend/backend dependencies are required):

```powershell
.\venv\Scripts\python.exe scripts/browser_smoke.py
```

This checks planning, approval, collection, an obstacle replan, depot service, final completion, repeat runs, mission status, Feedback tabs and task results, activity timestamps and the rendered Others card using real browser `fetch`. It also verifies direct photo classification with an explicitly labelled response fixture, separately from an actual blank-image YOLO inference check. It uses its own temporary browser profile and server ports. Diagnostic output is under ignored `backend/runtime/`.

## Scope and limitations

This repository includes local YOLO image/webcam detection and a deterministic route-planning simulator. It includes optional frozen PPO dispatch inference, and contains no database persistence layer, geocoder, camera calibration, or ROS 2 hardware adapter. Runtime feedback does not retrain the policy. Vision libraries are not imported by the core planning path. Active jobs, complete route geometries, telemetry, and events are stored in process memory and are lost when the backend restarts; local planning/completion summaries and saved machine planning parameters persist. A history record does not restore a paused job. The synthetic graph is not a road network. Geometry checks validate buffered planar route segments but do not prove vehicle turning-radius, localization, sensor, or physical-world safety. Use a configured routing service and a separately validated hardware/safety stack before any real-world deployment.
