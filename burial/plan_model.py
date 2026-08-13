# -*- coding: utf-8 -*-
"""PlanModel — the in-memory burial plan while the dock is open.

Single source of truth for the open plan (spec §13): views subscribe to its
signals; every mutating action validates the invariants, writes the store
transactionally (Planner-style error surfacing on failure), appends exactly
one change-log row with before/after JSON, rebuilds the derived sections and
refreshes the map layers in place.

Signals:
    planChanged      plan header (name/scope/direction/status/…)
    inputsChanged    registered inputs
    rulesChanged     exclusion stack
    eventsChanged    events (and therefore sections)
    sectionsChanged  sections only (conclusions, splits, …)
    logChanged       change log appended
    storeError(str)  a store write failed; state kept in memory
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QObject, pyqtSignal

from ..workbench.depth_service import DepthService, DepthSourceConfig
from ..workbench.rules_engine import Interval
from . import change_log, events as ev, generation, io_csv, map_layers, schema
from .analysis_task import build_route_frame
from .profile_data import PlanProfile
from .store import BurialStore


_BUILDER_UNDO_ACTIONS = {
    change_log.ACTION_ADD_EVENT,
    change_log.ACTION_MOVE_EVENT,
    change_log.ACTION_EDIT_EVENT,
    change_log.ACTION_DELETE_EVENT,
    change_log.ACTION_CONFIRM_EVENT,
    change_log.ACTION_LOCK_EVENT,
    change_log.ACTION_SPLIT_SECTION,
    change_log.ACTION_INSERT_SECTION,
    change_log.ACTION_MERGE_SECTIONS,
    change_log.ACTION_SET_CONCLUSION,
    change_log.ACTION_EDIT_SECTION,
}


class PlanModel(QObject):
    planChanged = pyqtSignal()
    inputsChanged = pyqtSignal()
    rulesChanged = pyqtSignal()
    eventsChanged = pyqtSignal()
    sectionsChanged = pyqtSignal()
    logChanged = pyqtSignal()
    storeError = pyqtSignal(str)

    def __init__(self, store: BurialStore, workbench_store=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.workbench_store = workbench_store
        self.plan: Dict = {}
        self.inputs: List[Dict] = []
        self.rules: List[Dict] = []
        self.events: List[Dict] = []
        self.sections: List[Dict] = []
        self.context = generation.ResolutionContext()
        self.route = None            # RouteFrame over the plan's RPL (WGS84)
        self.distance = None
        self.resolved_rpl_id = ""
        self.route_notice = ""
        self.acq_cache: Dict[str, Tuple[List[Interval], List[Interval]]] = {}
        self.route_error = ""
        self.bathy_profile: Optional[PlanProfile] = None

    # -- store write wrapper (Planner pattern) -------------------------------
    def _store_write(self, action: str, func: Callable, *args, **kwargs):
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            self.storeError.emit(
                f"Could not {action}: the Burial Planner GeoPackage could not "
                f"be written.\n{self.store.gpkg_path}\n\n{exc}")
            return False, None
        return True, result

    # -- loading -------------------------------------------------------------
    @property
    def plan_id(self) -> str:
        return str(self.plan.get("plan_id") or "")

    @property
    def method(self) -> str:
        return self.plan.get("method") or schema.METHOD_PLOUGH

    @property
    def direction(self) -> int:
        return int(self.plan.get("direction") or 1)

    def gen_params(self, params_json: Optional[Dict] = None) -> generation.GenParams:
        stored = params_json
        if stored is None:
            try:
                stored = json.loads(self.plan.get("params_json") or "{}")
            except (ValueError, TypeError):
                stored = {}
        stored = stored or {}
        return generation.GenParams(
            scope_start_kp=float(self.plan.get("scope_start_kp") or 0.0),
            scope_end_kp=float(self.plan.get("scope_end_kp") or 0.0),
            direction=self.direction,
            method=self.method,
            min_section_km=float(stored.get("min_section_km", 0.5)),
            coarse_step_m=float(stored.get("coarse_step_m", 50.0)),
            refine_tol_m=float(stored.get("refine_tol_m", 1.0)),
            sliver_tol_km=float(stored.get("sliver_tol_km", 0.0)),
            cross_offset_m=float(stored.get("cross_offset_m", 0.0)),
            profile_step_m=float(stored.get("profile_step_m", 0.0)),
        )

    def load_plan(self, plan_id: str) -> bool:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            return False
        self.plan = plan
        self.inputs = self.store.list_inputs(plan_id)
        self.rules = self.store.list_rules(plan_id)
        self.events = self.store.list_events(plan_id)
        self.sections = self.store.list_sections(plan_id)
        self.acq_cache.clear()
        self.bathy_profile = PlanProfile.from_row(
            self.store.get_plan_profile(plan_id))
        self._load_context()
        self._load_route()
        self._check_stale()
        self.planChanged.emit()
        self.inputsChanged.emit()
        self.rulesChanged.emit()
        self.eventsChanged.emit()
        self.sectionsChanged.emit()
        return True

    def close_plan(self) -> None:
        self.plan = {}
        self.inputs = []
        self.rules = []
        self.events = []
        self.sections = []
        self.context = generation.ResolutionContext()
        self.route = None
        self.resolved_rpl_id = ""
        self.route_notice = ""
        self.acq_cache.clear()
        self.bathy_profile = None
        self.planChanged.emit()

    def _load_context(self) -> None:
        self.context = generation.ResolutionContext()
        active = self.store.active_generation(self.plan_id)
        if not active:
            return
        try:
            summary = json.loads(active.get("summary_json") or "{}")
        except (ValueError, TypeError):
            summary = {}
        self.context = generation.context_from_dict(summary.get("context"))

    def _load_route(self) -> None:
        """Route geometry from the Workbench RPL, or any registered line layer."""
        self.route = None
        self.distance = None
        self.resolved_rpl_id = ""
        self.route_notice = ""
        self.route_error = ""
        project = QgsProject.instance()
        lines_layer = None
        rpl_id = self.plan.get("rpl_id") or ""
        if rpl_id and self.workbench_store is not None:
            try:
                rpl, matched_snapshot = self._resolve_workbench_rpl()
                if rpl:
                    self.resolved_rpl_id = str(rpl.get("rpl_id") or "")
                    lines_layer = self.workbench_store.open_layer(
                        rpl.get("lines_layer") or "")
                    if matched_snapshot:
                        self.route_notice = (
                            "The saved RPL UUID was not present. A unique "
                            "Workbench RPL with the same name and revision "
                            "was matched; click Set route to save the relink.")
            except Exception:
                lines_layer = None
        if lines_layer is None:
            # Fallback: a project line layer captured at plan creation.
            source = self.plan.get("rpl_gpkg_path") or ""
            if source:
                from qgis.core import QgsVectorLayer

                candidate = QgsVectorLayer(source, "bp_route", "ogr")
                if candidate.isValid():
                    lines_layer = candidate
        if lines_layer is None or not lines_layer.isValid():
            self.route_error = "The plan's route layer could not be opened."
            return
        try:
            self.route, self.distance = build_route_frame(lines_layer, project)
        except Exception as exc:
            self.route_error = f"Route could not be built: {exc}"

    def _resolve_workbench_rpl(self):
        """Return ``(row, matched_snapshot)`` for the plan's Workbench RPL."""
        if self.workbench_store is None:
            return None, False
        wanted_id = str(self.plan.get("rpl_id") or "")
        exact = self.workbench_store.get_rpl(wanted_id) if wanted_id else None
        if exact is not None:
            return exact, False
        wanted_name = str(self.plan.get("rpl_name") or "").strip().casefold()
        wanted_revision = str(
            self.plan.get("rpl_revision") or "").strip().casefold()
        if not wanted_name:
            return None, False
        matches = [
            row for row in self.workbench_store.list_rpls()
            if str(row.get("name") or "").strip().casefold() == wanted_name
            and (not wanted_revision or
                 str(row.get("rev_label") or "").strip().casefold()
                 == wanted_revision)
        ]
        return (matches[0], True) if len(matches) == 1 else (None, False)

    def current_rpl_fingerprint(self) -> str:
        if self.plan.get("rpl_id") and self.workbench_store is not None:
            try:
                rpl, _matched_snapshot = self._resolve_workbench_rpl()
            except Exception:
                rpl = None
            return map_layers.rpl_fingerprint(
                rpl, getattr(self.workbench_store, "gpkg_path", ""))
        return self.plan.get("rpl_fingerprint") or ""

    def _check_stale(self) -> None:
        """Mark the plan stale when the RPL changed since it was anchored."""
        stored = self.plan.get("rpl_fingerprint") or ""
        current = self.current_rpl_fingerprint()
        if stored and current and stored != current \
                and self.plan.get("status") != schema.PLAN_STATUS_STALE:
            self.plan["status"] = schema.PLAN_STATUS_STALE
            self._store_write("update the plan status", self.store.save_plan, self.plan)

    def mark_stale(self) -> None:
        if self.plan and self.plan.get("status") != schema.PLAN_STATUS_STALE \
                and self.store.active_generation(self.plan_id):
            self.plan["status"] = schema.PLAN_STATUS_STALE
            self._store_write("update the plan status", self.store.save_plan, self.plan)
            self.planChanged.emit()

    # -- depth ---------------------------------------------------------------
    def depth_config(self) -> DepthSourceConfig:
        """Return only the Burial Planner's explicitly registered source.

        Workbench RPL depth sources are intentionally not inherited. Their
        settings may suit occasional point edits but are not necessarily
        appropriate for a whole-plan longitudinal profile.
        """
        for row in self.inputs:
            if row.get("role") == schema.INPUT_ROLE_BATHY:
                try:
                    return DepthSourceConfig(json.loads(row.get("config_json") or "{}"))
                except (ValueError, TypeError):
                    return DepthSourceConfig({})
        return DepthSourceConfig({})

    def depth_service(self) -> DepthService:
        return DepthService(self.depth_config(), QgsProject.instance())

    def depth_fingerprint(self) -> str:
        """Combined fingerprint of the configured bathymetry layers."""
        return map_layers.depth_config_fingerprint(
            QgsProject.instance(), self.depth_config())

    # -- sampled plan profile ------------------------------------------------
    def resolve_profile_step_m(self, params: Optional[generation.GenParams] = None
                               ) -> float:
        """The station step the plan profile should be sampled at (m).

        Manual (``profile_step_m`` > 0) wins; Auto follows the smallest
        configured bathymetry raster cell (contours fall back to 5 m) — the
        finest scale the data actually contains. Both are clamped to the
        analysis step (so the stored series stays reusable by Generate),
        floored at 2 m, and never allowed past ~500k stations.
        """
        params = params or self.gen_params()
        if params.profile_step_m > 0:
            step = float(params.profile_step_m)
        else:
            cell = map_layers.min_raster_cell_size_m(
                QgsProject.instance(), self.depth_config())
            step = float(cell) if cell else 5.0
        step = min(max(step, 2.0), max(params.coarse_step_m, 2.0))
        floor = params.scope.length_km * 1000.0 / 500000.0
        return round(max(step, floor), 3)

    def resolve_cross_offset_m(self, params: Optional[generation.GenParams] = None
                               ) -> float:
        """Cross-slope half-span in metres.

        An entered value represents the burial vehicle's half track width.
        Auto follows the stored-profile resolution, giving a local terrain
        cross slope instead of silently averaging across the much wider
        coarse rule-search step.
        """
        params = params or self.gen_params()
        if params.cross_offset_m > 0:
            return float(params.cross_offset_m)
        return self.resolve_profile_step_m(params)

    def profile_state(self) -> str:
        """'missing' | 'current' | 'stale' for the persisted plan profile."""
        profile = self.bathy_profile
        if profile is None or not profile.kps:
            return "missing"
        params = self.gen_params()
        scope = params.scope
        current = profile.is_current(
            self.current_rpl_fingerprint(), self.depth_fingerprint(),
            scope.start_km, scope.end_km,
            self.resolve_cross_offset_m(params))
        # A changed target step (manual override, different raster cell,
        # tighter analysis step) also warrants a resample.
        current = current and abs(
            profile.step_m - self.resolve_profile_step_m(params)) < 0.01
        return "current" if current else "stale"

    def save_profile(self, profile: PlanProfile) -> bool:
        """Persist one sampling pass (derived data — not change-logged)."""
        if not self.plan_id:
            return False
        ok, _ = self._store_write(
            "save the sampled profile", self.store.save_plan_profile,
            profile.to_row(self.plan_id))
        if ok:
            self.bathy_profile = profile
        return ok

    def _stamp_position(self, event: Dict) -> None:
        """kp is the sole edit surface; lat/lon/depth are derived (spec §13)."""
        kp = float(event.get("kp") or 0.0)
        lat = lon = depth = None
        if self.route is not None:
            point = self.route.point_at_kp(kp, clamp=True)
            if point is not None:
                lat, lon = point.y(), point.x()
                service = self.depth_service()
                if service.is_available():
                    depth = service.sample(lat, lon)
        event["lat"] = lat
        event["lon"] = lon
        event["depth_m"] = depth

    # -- plan CRUD -----------------------------------------------------------
    def create_plan(self, name: str, method: str, description: str = "",
                    rpl_row: Optional[Dict] = None) -> Optional[str]:
        plan = {
            "plan_id": schema.new_id(),
            "name": name,
            "description": description,
            "notes": "",
            "method": method,
            "rpl_id": (rpl_row or {}).get("rpl_id") or "",
            "rpl_name": (rpl_row or {}).get("name") or "",
            "rpl_revision": (rpl_row or {}).get("rev_label") or "",
            "rpl_gpkg_path": (rpl_row or {}).get("gpkg_path") or "",
            "rpl_fingerprint": "",
            "scope_start_kp": 0.0,
            "scope_end_kp": 0.0,
            "direction": 1,
            "target_burial_m": None,
            "status": schema.PLAN_STATUS_DRAFT,
            "rev_label": "Rev 1",
            "supersedes_id": "",
        }
        ok, plan_id = self._store_write("create the plan", self.store.save_plan, plan)
        if not ok:
            return None
        self.store.append_change(plan_id, change_log.ACTION_CREATE_PLAN, plan_id,
                                 after={schema.TABLE_PLAN: [plan]})
        self.load_plan(plan_id)
        return plan_id

    def update_plan(self, updates: Dict, reason: str = "") -> bool:
        if not self.plan:
            return False
        before = dict(self.plan)
        self.plan.update(updates)
        ok, _ = self._store_write("save the plan", self.store.save_plan, self.plan)
        if not ok:
            self.plan = before
            return False
        self.store.append_change(
            self.plan_id, change_log.ACTION_EDIT_PLAN, self.plan_id,
            before={schema.TABLE_PLAN: [before]},
            after={schema.TABLE_PLAN: [dict(self.plan)]}, reason=reason)
        self.logChanged.emit()
        changed_keys = set(updates)
        if "rpl_id" in changed_keys or "rpl_gpkg_path" in changed_keys:
            self._load_route()
        if {"scope_start_kp", "scope_end_kp", "direction", "rpl_id",
            "rpl_gpkg_path"} & changed_keys:
            self.mark_stale()
        self.planChanged.emit()
        return True

    def update_gen_params(self, updates: Dict, reason: str = "") -> bool:
        """Patch selected workflow parameters without overwriting other tabs.

        Bathymetry preparation, exclusion analysis and candidate generation
        deliberately expose different parts of ``params_json``. Each tab must
        preserve the values owned by the others when it applies its settings.
        """
        if not self.plan:
            return False
        try:
            stored = json.loads(self.plan.get("params_json") or "{}")
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored.update(updates)
        saved = self.update_plan(
            {"params_json": json.dumps(stored)}, reason=reason)
        if saved:
            self.mark_stale()
        return saved

    # -- inputs / rules ------------------------------------------------------
    def save_input(self, row: Dict) -> bool:
        before_row = self.store.get_input(row.get("input_id") or "")
        row = dict(row)
        row["plan_id"] = self.plan_id
        ok, input_id = self._store_write("save the input", self.store.save_input, row)
        if not ok:
            return False
        row["input_id"] = input_id
        self.store.append_change(
            self.plan_id, change_log.ACTION_SET_INPUT, input_id,
            before={schema.TABLE_INPUT: [before_row] if before_row else []},
            after={schema.TABLE_INPUT: [row]})
        self.inputs = self.store.list_inputs(self.plan_id)
        self.mark_stale()
        self.inputsChanged.emit()
        self.logChanged.emit()
        return True

    def delete_input(self, input_id: str) -> bool:
        before_row = self.store.get_input(input_id)
        ok, _ = self._store_write("delete the input", self.store.delete_input, input_id)
        if not ok:
            return False
        self.store.append_change(
            self.plan_id, change_log.ACTION_DELETE_INPUT, input_id,
            before={schema.TABLE_INPUT: [before_row] if before_row else []},
            after={schema.TABLE_INPUT: []})
        self.inputs = self.store.list_inputs(self.plan_id)
        self.mark_stale()
        self.inputsChanged.emit()
        self.logChanged.emit()
        return True

    def save_rules(self, rules: List[Dict], target_id: str = "",
                   action: str = change_log.ACTION_EDIT_RULE) -> bool:
        before_rules = self.store.list_rules(self.plan_id)
        ok, _ = self._store_write("save the rules", self.store.save_rules,
                                  self.plan_id, rules)
        if not ok:
            return False
        self.rules = self.store.list_rules(self.plan_id)
        self.store.append_change(
            self.plan_id, action, target_id,
            before={schema.TABLE_RULE: before_rules},
            after={schema.TABLE_RULE: [dict(r) for r in self.rules]})
        self.mark_stale()
        self.rulesChanged.emit()
        self.logChanged.emit()
        return True

    # -- events --------------------------------------------------------------
    def _scope_bounds(self) -> Tuple[float, float]:
        return (float(self.plan.get("scope_start_kp") or 0.0),
                float(self.plan.get("scope_end_kp") or 0.0))

    def _write_events_and_sections(self, action: str, target_id: str,
                                   new_events: List[Dict], reason: str) -> bool:
        """One logged, store-written event mutation + derived section rebuild."""
        before = {
            schema.TABLE_EVENT: [dict(e) for e in self.events],
            schema.TABLE_SECTION: [dict(s) for s in self.sections],
        }
        new_events = ev.sort_events(new_events, self.direction)
        new_sections = self._derive_sections(new_events)
        ok, _ = self._store_write("save the events", self.store.save_events,
                                  self.plan_id, new_events)
        if not ok:
            return False
        ok, _ = self._store_write("save the sections", self.store.save_sections,
                                  self.plan_id, new_sections)
        if not ok:
            return False
        self.events = self.store.list_events(self.plan_id)
        self.sections = self.store.list_sections(self.plan_id)
        self.store.append_change(
            self.plan_id, action, target_id, before=before,
            after={
                schema.TABLE_EVENT: [dict(e) for e in self.events],
                schema.TABLE_SECTION: [dict(s) for s in self.sections],
            }, reason=reason)
        self.refresh_layers()
        self.eventsChanged.emit()
        self.sectionsChanged.emit()
        self.logChanged.emit()
        return True

    def _derive_sections(self, events: List[Dict]) -> List[Dict]:
        params = self.gen_params(self._active_params() or None)
        rule_names = {str(r.get("rule_id")): (r.get("name") or "") for r in self.rules}
        return generation.build_sections(
            events, params, self.context.excluded, self.context.screening,
            self.context.influence, self.context.insufficient,
            self.context.dropped_short, rule_names,
            previous_sections=self.sections, plan_id=self.plan_id)

    def _active_params(self) -> Dict:
        active = self.store.active_generation(self.plan_id)
        if not active:
            return {}
        try:
            return json.loads(active.get("params_json") or "{}")
        except (ValueError, TypeError):
            return {}

    def add_event(self, kp: float, event_type: str, note: str = "",
                  reason: str = "", source: str = schema.EVENT_SOURCE_MANUAL) -> Optional[str]:
        lo, hi = self._scope_bounds()
        event = {
            "event_id": schema.new_id(),
            "plan_id": self.plan_id,
            "generation_id": "",
            "seq": 0,
            "event_type": event_type,
            "kp": float(kp),
            "end_kp": None,
            "source": source,
            "status": schema.EVENT_STATUS_CANDIDATE,
            "locked": 0,
            "notes": note,
        }
        self._stamp_position(event)
        candidate = [dict(e) for e in self.events] + [event]
        result = ev.validate_events(candidate, lo, hi, self.direction, self.method)
        if result.errors:
            raise ValueError(result.errors[0])
        if self._write_events_and_sections(change_log.ACTION_ADD_EVENT,
                                           event["event_id"], candidate, reason):
            return event["event_id"]
        return None

    def move_event(self, event_id: str, new_kp: float, reason: str = "") -> bool:
        target = next((event for event in self.events
                       if event.get("event_id") == event_id), None)
        if target is None:
            raise ValueError("Event not found.")
        if int(target.get("locked") or 0):
            raise ValueError("Unlock the event before moving it.")
        lo, hi = self._scope_bounds()
        message = ev.check_move(self.events, event_id, new_kp, lo, hi,
                                self.direction, self.method)
        if message:
            raise ValueError(message)
        moved = []
        for event in self.events:
            copy = dict(event)
            if copy.get("event_id") == event_id:
                copy["kp"] = float(new_kp)
                self._stamp_position(copy)
            moved.append(copy)
        return self._write_events_and_sections(change_log.ACTION_MOVE_EVENT,
                                               event_id, moved, reason)

    def delete_event(self, event_id: str, reason: str = "") -> bool:
        return self.delete_events([event_id], reason)

    def delete_events(self, event_ids: List[str], reason: str = "") -> bool:
        """Delete a valid event selection atomically as one undoable edit."""
        wanted = {str(event_id) for event_id in event_ids if event_id}
        locked = [event for event in self.events
                  if str(event.get("event_id") or "") in wanted
                  and int(event.get("locked") or 0)]
        if locked:
            raise ValueError("Locked events cannot be deleted — unlock them first.")
        remaining = [dict(event) for event in self.events
                     if str(event.get("event_id") or "") not in wanted]
        lo, hi = self._scope_bounds()
        result = ev.validate_events(
            remaining, lo, hi, self.direction, self.method)
        if result.errors:
            raise ValueError(result.errors[0])
        return self._write_events_and_sections(change_log.ACTION_DELETE_EVENT,
                                               ",".join(sorted(wanted)),
                                               remaining, reason)

    def set_event_status(self, event_ids: List[str], status: str,
                         action: str = change_log.ACTION_CONFIRM_EVENT) -> bool:
        wanted = set(event_ids)
        updated = []
        for event in self.events:
            copy = dict(event)
            if copy.get("event_id") in wanted:
                copy["status"] = status
            updated.append(copy)
        return self._write_events_and_sections(
            action, ",".join(sorted(wanted)), updated, "")

    def set_event_notes(self, event_id: str, notes: str) -> bool:
        updated = []
        for event in self.events:
            copy = dict(event)
            if copy.get("event_id") == event_id:
                copy["notes"] = notes
            updated.append(copy)
        return self._write_events_and_sections(
            change_log.ACTION_EDIT_EVENT, event_id, updated, "notes edit")

    def set_event_locked(self, event_ids: List[str], locked: bool) -> bool:
        wanted = set(event_ids)
        updated = []
        for event in self.events:
            copy = dict(event)
            if copy.get("event_id") in wanted:
                copy["locked"] = 1 if locked else 0
            updated.append(copy)
        return self._write_events_and_sections(
            change_log.ACTION_LOCK_EVENT, ",".join(sorted(wanted)), updated, "")

    def import_events(self, imported: List[Dict], label: str,
                      client_proposal: bool = False) -> bool:
        lo, hi = self._scope_bounds()
        prepared = []
        for event in imported:
            copy = dict(event)
            copy["plan_id"] = self.plan_id
            if client_proposal:
                copy["source"] = schema.EVENT_SOURCE_CLIENT
            self._stamp_position(copy)
            prepared.append(copy)
        candidate = [dict(e) for e in self.events] + prepared
        result = ev.validate_events(candidate, lo, hi, self.direction, self.method)
        if result.errors:
            raise ValueError(result.errors[0])
        return self._write_events_and_sections(
            change_log.ACTION_IMPORT, label, candidate,
            "client proposal" if client_proposal else "")

    # -- section operations --------------------------------------------------
    def update_section(self, section_id: str, updates: Dict,
                       action: str = change_log.ACTION_SET_CONCLUSION) -> bool:
        before_rows = [dict(s) for s in self.sections
                       if s.get("section_id") == section_id]
        if not before_rows:
            return False
        updated = []
        for section in self.sections:
            copy = dict(section)
            if copy.get("section_id") == section_id:
                copy.update(updates)
            updated.append(copy)
        ok, _ = self._store_write("save the section", self.store.save_sections,
                                  self.plan_id, updated)
        if not ok:
            return False
        self.sections = self.store.list_sections(self.plan_id)
        self.store.append_change(
            self.plan_id, action, section_id,
            before={schema.TABLE_SECTION: before_rows},
            after={schema.TABLE_SECTION: [
                dict(s) for s in self.sections if s.get("section_id") == section_id]})
        self.refresh_layers()
        self.sectionsChanged.emit()
        self.logChanged.emit()
        return True

    def insert_opposite_section(self, section_id: str, start_kp: float,
                                end_kp: float, reason: str = "") -> bool:
        """Insert a skip inside burial, or burial inside a skip."""
        section = next((s for s in self.sections
                        if s.get("section_id") == section_id), None)
        if section is None:
            raise ValueError("Section not found.")
        section_start = float(section.get("start_kp") or 0.0)
        section_end = float(section.get("end_kp") or 0.0)
        lo, hi = sorted((float(start_kp), float(end_kp)))
        if not (section_start < lo < hi < section_end):
            raise ValueError(
                "The inserted start and end KPs must lie inside the selected section.")
        specs = ev.opposite_section_boundary_specs(
            section.get("kind") or "", lo, hi, self.direction)
        added = []
        for event_type, kp in specs:
            event = {
                "event_id": schema.new_id(), "plan_id": self.plan_id,
                "generation_id": "", "seq": 0, "event_type": event_type,
                "kp": float(kp), "end_kp": None,
                "source": schema.EVENT_SOURCE_MANUAL,
                "status": schema.EVENT_STATUS_CANDIDATE, "locked": 0, "notes": "",
            }
            self._stamp_position(event)
            added.append(event)
        candidate = [dict(e) for e in self.events] + added
        scope_lo, scope_hi = self._scope_bounds()
        result = ev.validate_events(
            candidate, scope_lo, scope_hi, self.direction, self.method)
        if result.errors:
            raise ValueError(result.errors[0])
        return self._write_events_and_sections(
            change_log.ACTION_INSERT_SECTION, section_id, candidate, reason)

    def split_section_at(self, section_id: str, kp: float, reason: str = "") -> bool:
        """Compatibility helper: insert a visible 1 m opposite-kind range."""
        section = next((s for s in self.sections
                        if s.get("section_id") == section_id), None)
        if section is None:
            raise ValueError("Section not found.")
        start = float(section.get("start_kp") or 0.0)
        end = float(section.get("end_kp") or 0.0)
        if not (start + 0.0005 < kp < end - 0.0005):
            raise ValueError("The split KP must lie inside the section.")
        return self.insert_opposite_section(
            section_id, float(kp) - 0.0005, float(kp) + 0.0005, reason)

    def merge_sections(self, section_ids: List[str], reason: str = "") -> bool:
        """Merge selected burial sections or selected skips."""
        remaining, _removed, _kind = ev.merge_section_events(
            self.events, self.sections, section_ids)
        lo, hi = self._scope_bounds()
        result = ev.validate_events(
            remaining, lo, hi, self.direction, self.method)
        if result.errors:
            raise ValueError(result.errors[0])
        return self._write_events_and_sections(
            change_log.ACTION_MERGE_SECTIONS, ",".join(section_ids), remaining, reason)

    # -- generation ----------------------------------------------------------
    def apply_generation(self, output: generation.GenerationOutput,
                         params: generation.GenParams, rule_rows: List[Dict],
                         inputs_fingerprints: Dict[str, str],
                         generation_id: str) -> bool:
        """Persist one algorithm run atomically-ish: snapshot row + events +
        sections + layers together, one logged change (main thread)."""
        before = {
            schema.TABLE_EVENT: [dict(e) for e in self.events],
            schema.TABLE_SECTION: [dict(s) for s in self.sections],
            schema.TABLE_GENERATION: [dict(g) for g in
                                      self.store.list_generations(self.plan_id)],
        }
        summary = dict(output.summary)
        summary["context"] = generation.context_to_dict(output)
        gen_row = {
            "generation_id": generation_id,
            "plan_id": self.plan_id,
            "active": 1,
            "rules_snapshot_json": generation.rules_snapshot(rule_rows),
            "params_json": json.dumps(params.to_dict()),
            "inputs_fingerprint_json": json.dumps(inputs_fingerprints),
            "summary_json": json.dumps(summary),
            "proposal_diff_json": json.dumps(output.proposal_diff or {}),
        }
        ok, _ = self._store_write("save the generation", self.store.save_generation, gen_row)
        if not ok:
            return False
        ok, _ = self._store_write("save the events", self.store.save_events,
                                  self.plan_id, output.events)
        if not ok:
            return False
        ok, _ = self._store_write("save the sections", self.store.save_sections,
                                  self.plan_id, output.sections)
        if not ok:
            return False
        self.context = generation.context_from_dict(summary["context"])
        self.events = self.store.list_events(self.plan_id)
        self.sections = self.store.list_sections(self.plan_id)
        self.plan["status"] = schema.PLAN_STATUS_DRAFT
        self.plan["rpl_fingerprint"] = self.current_rpl_fingerprint()
        self._store_write("save the plan", self.store.save_plan, self.plan)
        self.store.append_change(
            self.plan_id, change_log.ACTION_GENERATE, generation_id, before=before,
            after={
                schema.TABLE_EVENT: [dict(e) for e in self.events],
                schema.TABLE_SECTION: [dict(s) for s in self.sections],
                schema.TABLE_GENERATION: [gen_row],
            })
        self.refresh_layers()
        self.planChanged.emit()
        self.eventsChanged.emit()
        self.sectionsChanged.emit()
        self.logChanged.emit()
        return True

    # -- rollback ------------------------------------------------------------
    def last_undoable_builder_change(self) -> Optional[Dict]:
        """Latest effective change when it is a safe Plan Builder edit."""
        if not self.plan_id:
            return None
        entry = change_log.latest_effective_entry(
            self.store.list_change_log(self.plan_id))
        if entry is None or entry.get("action") not in _BUILDER_UNDO_ACTIONS:
            return None
        return entry

    def undo_last_builder_edit(self) -> Optional[Dict]:
        """Undo one Plan Builder edit without reloading route/bathymetry."""
        entry = self.last_undoable_builder_change()
        if entry is None:
            return None
        ok, _ = self._store_write(
            "undo the last Plan Builder edit", self.store.rollback_to,
            self.plan_id, entry.get("change_id") or "")
        if not ok:
            return None
        self.events = self.store.list_events(self.plan_id)
        self.sections = self.store.list_sections(self.plan_id)
        self.refresh_layers()
        self.eventsChanged.emit()
        self.sectionsChanged.emit()
        self.logChanged.emit()
        return entry

    def rollback_to(self, change_id: str) -> bool:
        ok, _ = self._store_write("roll back", self.store.rollback_to,
                                  self.plan_id, change_id)
        if not ok:
            return False
        self.load_plan(self.plan_id)
        self.refresh_layers()
        self.logChanged.emit()
        return True

    # -- layers --------------------------------------------------------------
    def refresh_layers(self) -> None:
        if not self.plan or self.route is None:
            return
        try:
            map_layers.write_plan_layers(self.store, self.plan, self.sections,
                                         self.events, self.route)
            map_layers.ensure_plan_layers(QgsProject.instance(),
                                          self.store.gpkg_path, self.plan)
        except Exception as exc:
            self.storeError.emit(f"Plan layers could not be refreshed: {exc}")

    # -- export --------------------------------------------------------------
    def export_events_csv(self) -> str:
        active = self.store.active_generation(self.plan_id) or {}
        return io_csv.events_csv(self.plan, self.events,
                                 active.get("generation_id") or "")

    def export_sections_csv(self) -> str:
        active = self.store.active_generation(self.plan_id) or {}
        return io_csv.sections_csv(self.plan, self.sections,
                                   active.get("generation_id") or "")

    def export_inputs_csv(self) -> str:
        return io_csv.inputs_csv(self.plan, self.inputs)
