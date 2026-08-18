# Burial Planner (beta) — implementation decisions

Deviations from and judgement calls on `burial_planner_spec.md` (v0.3), one
line of rationale each, so they can be reviewed alongside the code. Spec
section references in brackets.

## Deviations from the spec

- **[§8] `bp_plan` gains a `params_json` column** for the plan-level
  post-processing parameters (min section length, sliver tolerance, coarse
  step, refine tolerance) — the spec defines these params (§10 Tab 4) but gave
  them no home before the first `bp_generation` row exists.
- **[§12.4] Candidates = scope − excluded − insufficient-information** (the
  spec's formula omits the insufficient class): planning burial through a
  range whose governing data could not be evaluated would contradict the
  guide's Insufficient Information conclusion, so those ranges become
  `insufficient_info` sections rather than candidates.
- **[§12.8] Sections whose kind and boundaries are unchanged carry over their
  state/conclusion/confidence/notes across regeneration** instead of resetting
  to blank — wiping the engineer's assessment of untouched sections on every
  event nudge would make the conclusion workflow unusable; genuinely new or
  moved sections still default to `candidate` / blank.
- **[§10 Tab 4] The rule editor derives `action` from the criterion class**
  (Non-Deviable/Project → exclude, Screening → risk) rather than exposing a
  separate action picker — one decision instead of two that can contradict;
  `allow`-action rules imported from an Assessment keep their engineer's-
  exception semantics through editing.
- **[§10 Tab 2/5] Map-pick for scope KPs and event placement shipped after
  v1** as a one-shot snap-to-route map tool (`kp_pick_tool.py`): *Pick…*
  buttons fill the scope and add-event KP spinboxes, and a profile
  double-click primes the add-event KP — filling the entry surfaces rather
  than mutating the plan directly keeps the existing validation and reason
  prompts as the single write path.
- **[§11.5] Cross-slope shipped as a display series first, not a criterion**:
  the slope panel shows longitudinal, cross and absolute slope from the
  persisted plan profile, with cross slope a two-point difference across a
  configurable ± offset (default auto = analysis step; set it to the
  vehicle's half track width). Signs follow the plugin convention —
  longitudinal +ve = up-slope, cross +ve = deeper to starboard of the
  direction of installation (direction −1 flips it). A cross-slope
  *exclusion rule* remains future work: the display series lets engineers
  see the data before criteria are formalised, and nothing in the schema
  blocks the rule later.
- **[§14.5 / §12.9] Generation writes are atomic per table, not across
  tables**: the store follows the Workbench/Planner whole-layer-rewrite
  pattern which has no cross-table transaction; all generation writes happen
  together in the completion callback and any failure is surfaced, but a
  mid-sequence crash could need the change log/rollback to reconcile. True
  SQLite transactions would be a new persistence pattern for the plugin.
- **[§13] The reason prompt on manual event edits doubles as the confirm**:
  Cancel aborts the edit, OK with empty text proceeds without a reason —
  fewer dialogs than a separate confirm + optional-reason pair.
- **[§14.1] The task's depth sampler is a dedicated `DepthSnapshot`**
  (cloned raster providers + pre-transformed contour features) rather than
  `DepthService`, exactly because DepthService touches live layers; the spec
  mandates the clone pattern, this just names where it lives. `DepthService`
  is still used for main-thread work (profile pane, event depth stamping).
- **[§16] An extra pure test module `tests/test_burial_io.py`** holds the CSV
  round-trip, change-log inversion and the no-new-dependencies import scan
  (the spec folds the scan into the list without naming a home).

## Notable interpretations (spec silent or open)

- **Rollback mechanics**: every change-log entry stores the affected rows per
  table in `before_json`/`after_json`; rollback inverts entries newest-first
  (delete rows added, restore rows before). Simple, exact, and testable
  without replaying actions. Entries originally snapshotted the *complete*
  tables; they now store only the added/removed/modified rows
  (`change_log.delta_tables`) — inversion is per-row keyed, so rollback
  behaviour is unchanged while entries stay O(edit) instead of O(plan). New
  entries are appended through the provider (whole-table rewrite kept as
  fallback) with the next seq cached per plan.
- **Sample once, plan fast**: bathymetry sampling moved from
  every-refresh to one persisted pass per plan (`bp_profile`, schema v3).
  The stored samples carry the fingerprints (route, bathymetry inputs),
  scope and cross offset they were built with; any mismatch marks them
  *stale* — shown, never silently rebuilt — and only *Resample profile*
  (or the automatic first build for a never-sampled plan) reruns sampling.
  The threshold-rule analysis consumes the stored series when current, so
  Generate stops resampling too; the 0.1 m boundary-refinement predicate
  still samples live rasters (bounded evaluations, exact boundaries). An
  empty sampling result never replaces stored samples. Derived data — not
  change-logged, copied on plan duplicate. The station step follows the
  data: Auto = the smallest configured bathymetry raster cell (5 m for
  contour-only sources), manually overridable, clamped between 2 m and the
  analysis step with a ~500k-station ceiling — dense sampling of long
  routes was made viable by the RouteFrame chainage index, which replaced
  the per-call full-route walk in ``point_at_kp`` with a one-off index +
  bisection (bit-identical results, asserted in tests).
- **Report export is one self-contained HTML file** (`report.py`, pure
  python): inline CSS and a base64-embedded profile snapshot, so the report
  survives email/archive without sidecar files, prints acceptably from the
  browser, and needs no PDF dependency (the no-new-dependencies gate).
  Content is formatting-only over registry rows — nothing is recomputed, so
  the report cannot disagree with the tool state it was exported from.
- **Conflict clearing**: a `conflict` event whose KP is no longer inside an
  Exclusion Area after regeneration is reset to `candidate` (with a warning),
  not silently back to `confirmed` — the engineer re-confirms it.
- **Direction-aware signed slope**: acquisition stays direction-ignorant
  (positive slope = shoaling/up-slope with KP); the task swaps down/up-slope
  limits when direction is B→A, and the cache key includes direction only for
  signed-slope rules ("direction where relevant", §14.4). This sign
  convention is plugin-wide (see README "Slope methodology"): the KP
  Mouse profile, Depth Profile default and KP Range Depth + Slope Summary
  all report +ve = up-slope, datum-normalised; side slope +ve = deeper to
  starboard (vehicle leans to starboard).
- **Slope window is distance-based, not station-index based**: the coarse
  slope series differences depths interpolated at kp ± the analysis step, so
  injected stations (route vertices, contour crossings) no longer shrink the
  window and the coarse series measures slope at the same scale as the 1 m
  refinement predicate (which brackets boundaries assuming they agree).
- **Slope rules can evaluate over the vehicle footprint** (`slope_window_m`):
  a slope rule may set the evaluation length to the plough/trencher bearing
  length, so the rule sees the gradient the machine spans rather than
  fine-scale terrain shorter than the vehicle; unset falls back to
  2 × analysis step. The window is part of the rule config, so the
  acquisition cache invalidates when it changes and the refinement predicate
  uses the same window.
- **Burial local slope follows the persisted profile, not coarse rule-search
  spacing**: Auto slope rules and the profile panel difference depths at
  ± one stored profile step. The profile normally follows raster cell size and
  is capped at roughly 500,000 stations, so short steep faces remain visible
  without making thousands of kilometres of route unbounded. An explicit
  `slope_window_m` remains the opt-in vehicle-footprint average. Derived signed
  slope arrays are cached per window within an analysis run and the profile
  step participates in acquisition cache keys.
- **Burial Auto cross offset also follows profile resolution**: Auto samples
  port/starboard at ± one bounded profile step for a local cross-terrain
  angle. An entered cross offset means the burial vehicle's half track width
  and deliberately reports the two-point slope under that physical span.
- **Workflow settings follow the stage that consumes them**: Inputs selects
  route, scope and source layers; the next Bathymetry Profile tab owns profile
  step, cross offset, status and explicit rebuilding; Exclusions owns coarse
  spatial-search spacing, classification sliver cleanup and boundary
  refinement; Plan Builder owns minimum candidate-section length. A missing or
  stale stored profile blocks depth/slope exclusions and directs the user back
  to the preparation stage instead of sampling silently with unreviewed
  defaults.
- **Influence zones and `extend_m` do not invalidate the acquisition cache**
  — they are resolution-time interval ops (§14.4's "reorder/toggle re-runs
  only resolution" extended to the zone parameters).
- **Rule-set JSON format**: `{"format": "subsea_cable_tools.burial.rule_set",
  "version": 1, "rules": [...bp_rule rows sans plan_id...]}` — the
  organisation-owned parameter-set vehicle (§ hard constraints).
- **WD bands edit in a grid** (min WD / max WD / limit, plus down/up-slope
  limit columns shown only for signed slope); blank cells leave that side
  open, blank rows are dropped, and the stored `bands` JSON structure is
  unchanged so existing rules and the engine are untouched. First matching
  band still wins — row order is the precedence.
- **Duplicate plan does not copy generations or the change log** — they
  describe the original's history; the copy starts a fresh audit trail with
  `supersedes_id` lineage.
- **Insufficient-information sections auto-assign the Insufficient
  Information conclusion** (the one conclusion §5 allows to be trivially
  derived); everything else stays user-assigned.
- **Fallback (non-Workbench) routes** are fingerprinted by normalised source
  path only, so stale detection is weaker there — the Inputs tab notes that
  Workbench registration is recommended.
- **Exclusion Area extension is per-side and direction-aware** (before =
  approach, after = departure, like the Constraint Influence Zone); a
  water-depth-multiple extension evaluates the depth **at the footprint
  boundary being extended** (not a scan of the extended range) — simple,
  deterministic, and matches the "1 ×WD stand-off" convention. With no depth
  available at a boundary that side is left unextended with a visible
  warning, never silently guessed. Extension keys stay resolution-time
  (cache-exempt); legacy symmetric `extend_m` is honoured on read and
  rewritten to the new keys on the next edit.
- **Polygon route-corridor buffers are acquisition config** (they change
  where the condition is true), so they participate in the rule cache key —
  and a ×WD corridor appends the bathymetry fingerprint so depth changes
  invalidate it. Stations with no depth under a ×WD corridor degrade to the
  centreline test rather than failing the rule.
- **The per-rule "Excluded sections" bars show the resolved footprint**
  (extension included, via the engine's rule-hit clipping) so the tab shows
  exactly what resolution excludes; raw acquisition remains visible by
  setting the extension to zero.
- **Water depth and slope are separate dialogs over one stored kind**
  (`threshold_profile` + `config.profile` picks the variant) — the schema,
  engine and rule-set JSON are unchanged, so rules keep copying losslessly
  to/from Workbench assessments while each dialog shows only its own fields.
  WD bands live only on the slope dialog: their real use is depth-dependent
  tool slope capability, and banding a depth criterion by depth is circular
  (legacy depth rules with bands keep them silently — unmanaged config keys
  pass through edits).
- **Slope components**: longitudinal keeps signed limits and the evaluation
  length; cross is the two-point difference across the profile's sampled
  ± cross offset, compared as a magnitude (leaning to port or starboard both
  count, so direction of installation cannot change the verdict); absolute
  matches the profile pane's trace including its |longitudinal| fallback
  where cross samples are missing. Cross/absolute boundaries come from
  linear interpolation of the profile-resolution series rather than 1 m
  bisection — bathymetry does not exist off the sampled lines, so re-probing
  would fabricate precision. A slope "search corridor" is deliberately not
  offered for the same reason: terrain is only known along the route and at
  the ± cross offset. The cross offset participates in the rule cache
  fingerprint.
- **Fresh regeneration is explicit, itemised and rollback-able**: normal
  Generate keeps user work by contract; *Regenerate fresh…* confirms with
  exact counts of what will be discarded, keeps client burial-proposal
  events as external reference unless also ticked, and relies on
  ``apply_generation``'s before-snapshot so the change log can restore the
  pre-fresh state — destructive in effect, never in history.
- **`skip_handling` is manual-first with an explicit auto-assign** (TBC /
  Recover to deck / Mid-water transit, plough vocabulary). *Auto-assign skip
  handling…* applies a length policy — mid-water transit up to a threshold,
  recover-to-deck above — but the threshold is user-entered (remembered per
  machine, recorded in the change log), honouring the no-shipped-engineering-
  values rule; existing assignments are never replaced unless overwrite is
  ticked, and the whole assignment is one undoable edit.

- **The map follows the plan selector**: every plan keeps its own
  sections/events/hazards layers, and switching plans checks the selected
  plan's layer-tree nodes while unchecking every other plan's burial layers
  — never removing them, so nothing is lost by switching back and a
  deliberate two-plan comparison can still be re-checked by hand (until the
  next switch re-asserts the selector). Renaming a plan (or changing its
  revision label) retires the old-named project layers and rewrites under
  the new names in the same edit, because the layer names embed both.
- **Background results carry a plan token**: the exclusion analysis and the
  risk scan both record the plan they were started on and their results are
  discarded with a status message when a different plan (or plan file) is
  open at completion — switching plans mid-run must never write one plan's
  results into another. The profile sampler already had this via its
  generation token.
- **The Burial Planner opens as a floating window by default**: the dock is
  sized for a second monitor, and the docked-panel width only suits
  screen-share layouts; re-docking it and closing makes the next open
  docked again (the user's last mode wins), and floating geometry restores
  across sessions.

## Burial Tools registry (schema v6)

- **`bp_tool` is project-scoped and outside the plan change log**: tools are
  shared registry data (the Planner-vessels model) — deleting a plan never
  touches a tool, deleting a tool never edits a plan (dangling references
  render "(unregistered tool)"), and tool edits are not entries in any
  plan's change log; the row carries `source_ref` + `modified_utc` for
  traceability instead. `TABLE_KEYS` includes `bp_tool` for the store's
  generic upsert only — plan rollbacks can never mutate the registry.
- **Configurations are a JSON list on the tool row (`configs_json`), not a
  child table**: configs are always edited with their tool and are small;
  each carries a stable `config_id` so per-section assignments survive
  relabelling. A separate table would add store/rollback plumbing for no
  gain.
- **Per-section tool assignment is curation metadata first**: blank =
  inherit the plan default (`params_json.tool_id`/`tool_config_id`);
  assigning a tool also stamps the reserved `bp_section.method` with the
  tool's type and carries over regeneration. Event labels and kind labels
  still resolve from the **plan** method — switching labels per section
  arrives with mixed-method generation, not before, so a plan never shows
  two vocabularies while generation still evaluates one method.
- **Setting the plan default tool does not mark the plan stale**
  (`update_gen_params(stale=False)`) — it changes presentation and record,
  not generation results.
- **Method-id aliasing is normalised at the boundary and healed at read
  time**: Workbench `"jet"` maps to `"rov_jet"` on rule copy/JSON import,
  and `generation._engine_rule` / `analysis_task.build_work` normalise
  stored `methods_json` too, so rules copied before the fix keep firing.
- **One Trencher method (v7)**: ROV jet and mechanical trenching are not
  distinguished at the planning level, so `rov_jet` folded into `trencher`
  — one alias map (`schema._METHOD_ALIASES`) heals old plans/rules/tools at
  migration and again at read time, and the legacy constant stays defined
  so old exports keep importing. The v5→v6 "all methods of the day" check
  normalises through the same map so chained migrations stay correct.
- **The alter-course risk check is one threshold + one level**: every A/C
  with course change ≥ X° becomes a hazard at the chosen risk level
  (config: `min_course_change_deg` + `default_risk`; no attribute rules).
  The scanner still honours old configs carrying `turn_abs` rules, so
  nothing breaks until such a check is deliberately re-saved through the
  simplified editor. An unassigned level is rejected on OK because
  `scan_route_turns` drops unassessed turns — the check would silently
  record nothing.
- **"All methods" is stored as `[]`, and the v6 migration widens legacy
  lists**: pre-v6 rules were stamped with the full method list of the day
  (`["plough","rov_jet"]`), which adding the trencher method would silently
  narrow — every old rule would stop firing in a trencher plan. The rule
  editor now stamps `[]` (explicit all-methods), and `_migrate_v5_to_v6`
  rewrites any stored list covering the whole pre-v6 set to `[]`.
  A deliberately restricted list (e.g. `["plough"]`) is left alone.
- **Footprints are stored as WKT on the tool row**, normalised to the Import
  Ship Outline body-fixed frame (metres, CRP at origin, front along +Y) —
  a registry JSON travels complete without the source DXF, and the outline
  is ready for KP-placed map display (roadmap: live footprint overlay,
  turning-radius tool path).

## Tool footprint display (Phase 3)

- **The footprint is a transient overlay, not a layer**: scale context is a
  glance-level need, so the canvas rubber band follows the profile
  hover/selection and vanishes with the toggle — no layer churn, nothing
  persisted. Chartlet-grade placements go through the *Place Outline Along
  Route (KP)* Processing algorithm instead.
- **Placement is per-KP UTM with a projected-grid heading**: each placement
  transforms into the local UTM zone (the Dynamic Buffer working-CRS
  pattern) so the outline stays metre-true, and the heading is measured
  between two projected route points ±20 m around the KP — grid
  convergence is handled implicitly rather than corrected explicitly. The
  rotation/heading maths live in pure ``burial/geometry2d.py`` and are
  headless-tested.
- **The KP basis remains the RPL**: the footprint is advisory display; no
  analysis is re-based on where the vehicle body sits (the tool-path /
  turning-radius roadmap will flag deviation rather than re-base KP).
- **The placement algorithm is ellipsoidal-only**: it consumes KPs rather
  than emitting them, so it follows the Burial Planner's WGS84 +
  project-ellipsoid convention instead of exposing the Distance-mode
  parameter of the KP-emitting algorithms.

## Cross-check against the acceptance walkthrough (§16)

Automated coverage: events/alternation/locking (test_burial_events), the
full §12 pipeline incl. screening, influence flags, min-length, no-data,
determinism, proposal diff (test_burial_generation), store round-trip /
duplicate / rollback / migrate-backup (test_burial_store), scoped sampling,
signed+banded thresholds, buffer_field, cancellation, direction mapping and
an end-to-end build_work → task.run → generate run with a warm-cache re-run
(test_burial_task), CSV round-trip + import scan (test_burial_io), the
tools registry / trencher vocabulary / method aliasing / JSON round trip
(test_burial_tools), and the shared-engine extensions with the original
Assessment behaviour untouched (test_rules_engine / test_rules_inputs). The interactive walkthrough steps
(dock UX, profile drag, map sync) need a manual pass in QGIS.
