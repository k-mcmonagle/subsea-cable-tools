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
- **[§10 Tab 2/5] Map-pick for scope KPs and map-pick/profile-click event
  placement are not in v1**; scope is set by spinboxes/full-route and events
  by typed KP, table edit, nudge and profile drag — the remaining pickers are
  a small additive map-tool, deferred to keep v1 reviewable.
- **[§11.5] Cross-slope is deferred** (the spec itself says "confirm before
  building" and it is open question 4); nothing in the schema blocks it.
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

- **Rollback mechanics**: every change-log entry stores the complete affected
  rows per table in `before_json`/`after_json`; rollback inverts entries
  newest-first (delete rows added, restore rows before). Simple, exact, and
  testable without replaying actions.
- **Conflict clearing**: a `conflict` event whose KP is no longer inside an
  Exclusion Area after regeneration is reset to `candidate` (with a warning),
  not silently back to `confirmed` — the engineer re-confirms it.
- **Direction-aware signed slope**: acquisition stays direction-ignorant
  (positive slope = deepening with KP); the task swaps down/up-slope limits
  when direction is B→A, and the cache key includes direction only for
  signed-slope rules ("direction where relevant", §14.4).
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
