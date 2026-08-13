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
  Generate stops resampling too; the 1 m boundary-refinement predicate
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
- **Influence zones and `extend_m` do not invalidate the acquisition cache**
  — they are resolution-time interval ops (§14.4's "reorder/toggle re-runs
  only resolution" extended to the zone parameters).
- **Rule-set JSON format**: `{"format": "subsea_cable_tools.burial.rule_set",
  "version": 1, "rules": [...bp_rule rows sans plan_id...]}` — the
  organisation-owned parameter-set vehicle (§ hard constraints).
- **WD-band editor is a JSON field in v1** (validated, tooltip documents the
  keys) — a grid editor is UI sugar that can come later without schema change.
- **Duplicate plan does not copy generations or the change log** — they
  describe the original's history; the copy starts a fresh audit trail with
  `supersedes_id` lineage.
- **Insufficient-information sections auto-assign the Insufficient
  Information conclusion** (the one conclusion §5 allows to be trivially
  derived); everything else stays user-assigned.
- **Fallback (non-Workbench) routes** are fingerprinted by normalised source
  path only, so stale detection is weaker there — the Inputs tab notes that
  Workbench registration is recommended.

## Cross-check against the acceptance walkthrough (§16)

Automated coverage: events/alternation/locking (test_burial_events), the
full §12 pipeline incl. screening, influence flags, min-length, no-data,
determinism, proposal diff (test_burial_generation), store round-trip /
duplicate / rollback / migrate-backup (test_burial_store), scoped sampling,
signed+banded thresholds, buffer_field, cancellation, direction mapping and
an end-to-end build_work → task.run → generate run with a warm-cache re-run
(test_burial_task), CSV round-trip + import scan (test_burial_io), and the
shared-engine extensions with the original Assessment behaviour untouched
(test_rules_engine / test_rules_inputs). The interactive walkthrough steps
(dock UX, profile drag, map sync) need a manual pass in QGIS.
