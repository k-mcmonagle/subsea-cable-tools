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
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..workbench.depth_service import DepthService, DepthSourceConfig
from ..workbench import rules_engine as eng
from ..workbench.rules_engine import Interval
from . import (change_log, events as ev, generation, io_csv, map_layers,
               path_data, path_layers, risk, schema, tools)
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
    change_log.ACTION_DELETE_SECTION,
    change_log.ACTION_DISMISS_INSUFFICIENT,
    change_log.ACTION_RESOLVE_INSUFFICIENT,
    change_log.ACTION_SET_CONCLUSION,
    change_log.ACTION_EDIT_SECTION,
}


class PlanModel(QObject):
    planChanged = pyqtSignal()
    inputsChanged = pyqtSignal()
    rulesChanged = pyqtSignal()
    eventsChanged = pyqtSignal()
    sectionsChanged = pyqtSignal()
    riskChanged = pyqtSignal()       # risk checks and/or hazards
    toolsChanged = pyqtSignal()      # project-scoped Burial Tools registry
    pathsChanged = pyqtSignal()      # result and/or its current/stale state
    laybacksChanged = pyqtSignal()   # project-scoped layback profiles
    vesselsChanged = pyqtSignal()    # project-scoped vessel registry
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
        self.risk_checks: List[Dict] = []
        self.hazards: List[Dict] = []
        self.tools: List[Dict] = []  # project-scoped, survives close_plan
        self.layback_profiles: List[Dict] = []  # project-scoped
        self.vessels: List[Dict] = []           # project-scoped
        self.path_result: Optional[Dict] = None
        self.context = generation.ResolutionContext()
        self.route = None            # RouteFrame over the plan's RPL (WGS84)
        self.distance = None
        self.resolved_rpl_id = ""
        self.route_notice = ""
        self.acq_cache: Dict[str, Tuple[List[Interval], List[Interval]]] = {}
        self.route_error = ""
        self.bathy_profile: Optional[PlanProfile] = None
        # Section route-slice WKT memo (cleared when the route changes) and
        # the debounced layer-refresh machinery: rapid edits coalesce into
        # one spatial-layer rewrite instead of one per keystroke.
        self._segment_wkt_cache: Dict = {}
        self._pending_layer_parts: set = set()
        self._layer_timer = QTimer(self)
        self._layer_timer.setSingleShot(True)
        self._layer_timer.setInterval(150)
        self._layer_timer.timeout.connect(self._flush_layer_refresh)
        self._depth_config_cache: Optional[Tuple[str, DepthSourceConfig]] = None
        self._profile_cache_key: Optional[Tuple[str, str, str]] = None
        self.refresh_tools(emit=False)
        self.refresh_layback_profiles(emit=False)

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

    def _store_transaction(self, action: str, func: Callable):
        """Run ``func`` inside one store transaction (atomic in SQL mode)."""
        def wrapped():
            with self.store.transaction():
                return func()
        return self._store_write(action, wrapped)

    # -- loading -------------------------------------------------------------
    @property
    def plan_id(self) -> str:
        return str(self.plan.get("plan_id") or "")

    @property
    def method(self) -> str:
        # Legacy ids (rov_jet) are healed by migration; normalise anyway so
        # an un-migrated row can never leak the old vocabulary into the UI.
        return schema.normalise_method(
            self.plan.get("method") or "") or schema.METHOD_PLOUGH

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
        # ``refine_tol_m`` has never been user-editable. Treat the former
        # fixed 1 m value as the current fixed default so existing plans gain
        # the precision fix on their next recompute/generation.
        refine_tol_m = float(stored.get(
            "refine_tol_m", generation.BOUNDARY_REFINE_TOL_M))
        if abs(refine_tol_m - 1.0) < 1e-12:
            refine_tol_m = generation.BOUNDARY_REFINE_TOL_M
        return generation.GenParams(
            scope_start_kp=float(self.plan.get("scope_start_kp") or 0.0),
            scope_end_kp=float(self.plan.get("scope_end_kp") or 0.0),
            direction=self.direction,
            method=self.method,
            min_section_km=float(stored.get("min_section_km", 0.5)),
            coarse_step_m=float(stored.get("coarse_step_m", 50.0)),
            refine_tol_m=refine_tol_m,
            sliver_tol_km=float(stored.get("sliver_tol_km", 0.0)),
            cross_offset_m=float(stored.get("cross_offset_m", 0.0)),
            profile_step_m=float(stored.get("profile_step_m", 0.0)),
            dismissed_insufficient=generation.resolution_entries(
                stored.get("dismissed_insufficient")),
        )

    def load_plan(self, plan_id: str) -> bool:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            return False
        # A debounced layer write may still be pending for the plan being
        # left; flush it (against the old plan/route state) rather than
        # dropping it, or that plan's map layers stay stale on disk.
        self._flush_layer_refresh()
        self.plan = plan
        self.inputs = self.store.list_inputs(plan_id)
        self._depth_config_cache = None
        self.rules = self.store.list_rules(plan_id)
        self.events = self.store.list_events(plan_id)
        self.sections = self.store.list_sections(plan_id)
        self.risk_checks = self.store.list_risk_checks(plan_id)
        self.hazards = self.store.list_hazards(plan_id)
        self.path_result = self.store.get_path_result(plan_id)
        self.acq_cache.clear()
        self._load_profile(plan_id)
        self._load_context()
        self._load_route()
        self._check_stale()
        self.planChanged.emit()
        self.inputsChanged.emit()
        self.rulesChanged.emit()
        self.eventsChanged.emit()
        self.sectionsChanged.emit()
        self.riskChanged.emit()
        self.pathsChanged.emit()
        return True

    def _load_profile(self, plan_id: str) -> None:
        """Load the persisted bathymetry profile, reusing the parsed arrays
        when the stored row is unchanged (parsing a 500k-station JSON blob
        is expensive; reopening the same plan must not pay it twice)."""
        row = self.store.get_plan_profile(plan_id)
        if row is None:
            self.bathy_profile = None
            self._profile_cache_key = None
            return
        key = (plan_id, str(row.get("profile_id") or ""),
               str(row.get("sampled_utc") or ""))
        if self._profile_cache_key == key and self.bathy_profile is not None:
            return
        self.bathy_profile = PlanProfile.from_row(row)
        self._profile_cache_key = key

    def close_plan(self) -> None:
        self._flush_layer_refresh()  # keep a pending write, not drop it
        self.plan = {}
        self.inputs = []
        self.rules = []
        self.events = []
        self.sections = []
        self.risk_checks = []
        self.hazards = []
        self.path_result = None
        self.context = generation.ResolutionContext()
        self.route = None
        self.resolved_rpl_id = ""
        self.route_notice = ""
        self.acq_cache.clear()
        self.bathy_profile = None
        self._profile_cache_key = None
        self._depth_config_cache = None
        self.planChanged.emit()
        self.pathsChanged.emit()

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
        self._segment_wkt_cache.clear()
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

    def _has_derived_state(self) -> bool:
        """Whether the plan carries results computed from its current inputs.

        A duplicated plan has events/sections but no generation rows (those
        are deliberately not copied), so checking active_generation alone
        left duplicates unable to ever show as stale.
        """
        try:
            return bool(self.store.active_generation(self.plan_id)
                        or self.store.list_events(self.plan_id)
                        or self.store.list_sections(self.plan_id))
        except Exception:
            return False

    def mark_stale(self) -> None:
        if self.plan and self.plan.get("status") != schema.PLAN_STATUS_STALE \
                and self._has_derived_state():
            self.plan["status"] = schema.PLAN_STATUS_STALE
            self._store_write("update the plan status", self.store.save_plan, self.plan)
            self.planChanged.emit()

    # -- depth ---------------------------------------------------------------
    def depth_config(self) -> DepthSourceConfig:
        """Return only the Burial Planner's explicitly registered source.

        Workbench RPL depth sources are intentionally not inherited. Their
        settings may suit occasional point edits but are not necessarily
        appropriate for a whole-plan longitudinal profile.

        Memoised on the raw config JSON — this is called many times per
        Generate/refresh and re-parsing the same string each time added up.
        """
        raw = ""
        for row in self.inputs:
            if row.get("role") == schema.INPUT_ROLE_BATHY:
                raw = row.get("config_json") or "{}"
                break
        cached = self._depth_config_cache
        if cached is not None and cached[0] == raw:
            return cached[1]
        if raw:
            try:
                config = DepthSourceConfig(json.loads(raw))
            except (ValueError, TypeError):
                config = DepthSourceConfig({})
        else:
            config = DepthSourceConfig({})
        self._depth_config_cache = (raw, config)
        return config

    def depth_service(self) -> DepthService:
        return DepthService(self.depth_config(), QgsProject.instance())

    def depth_fingerprint(self) -> str:
        """Combined fingerprint of the configured bathymetry layers."""
        return map_layers.depth_config_fingerprint(
            QgsProject.instance(), self.depth_config())

    def depth_at_kp(self, kp: float) -> Optional[float]:
        """Water depth magnitude at a KP for resolution-time consumers
        (water-depth-scaled Exclusion Area extensions).

        Prefers the persisted plan profile (fast interpolation over the
        stored samples); falls back to sampling the configured bathymetry at
        the route position.
        """
        profile = self.bathy_profile
        if profile is not None and profile.kps:
            value = profile.depth_at(kp)
            if value is not None:
                return abs(float(value))
        if self.route is not None:
            point = self.route.point_at_kp(kp, clamp=True)
            if point is not None:
                service = self.depth_service()
                if service.is_available():
                    value = service.sample(point.y(), point.x())
                    if value is not None:
                        return abs(float(value))
        return None

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
            self._profile_cache_key = None  # reparse on next plan load
            if self.path_result:
                self.refresh_path_layers()
                self.pathsChanged.emit()
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
    # -- burial tools (project-scoped registry) ------------------------------
    def refresh_tools(self, emit: bool = True) -> None:
        try:
            self.tools = self.store.list_tools()
        except Exception as exc:
            # Keep the previous list rather than silently rendering every
            # assignment as "(unregistered tool)" in exports. getattr: the
            # error report must never itself raise on a broken store handle.
            self.storeError.emit(
                f"The Burial Tools registry could not be read:\n"
                f"{getattr(self.store, 'gpkg_path', '')}\n\n{exc}")
        if emit:
            self.toolsChanged.emit()

    def save_tool(self, row: Dict) -> str:
        """Create/update a registry tool. Project-scoped: not part of any
        plan's change log (the Planner vessels model); the row itself carries
        source_ref + modified_utc for traceability."""
        ids = self.save_tools([row])
        return ids[0] if ids else ""

    def save_tools(self, rows: List[Dict]) -> List[str]:
        """Bulk create/update: one table write, one signal, one layer refresh
        (a registry JSON import would otherwise rewrite per tool)."""
        if not rows:
            return []
        try:
            self.store.ensure_created()
        except Exception as exc:
            self.storeError.emit(
                f"Could not create the Burial Planner GeoPackage:\n{exc}")
            return []
        ok, tool_ids = self._store_write("save the burial tools",
                                         self.store.save_tools, rows)
        if not ok:
            return []
        self._after_tools_changed()
        return list(tool_ids or [])

    def delete_tool(self, tool_id: str) -> bool:
        """Remove a registry tool. Plans referencing it keep their ids and
        render "(unregistered tool)" — nothing in a plan is edited."""
        ok, _ = self._store_write("delete the burial tool",
                                  self.store.delete_tool, tool_id)
        if ok:
            self._after_tools_changed()
        return ok

    def _after_tools_changed(self) -> None:
        self.refresh_tools()
        # The sections map layer bakes the resolved tool text in at write
        # time — re-write it so renames/deletes show on the map too.
        self.refresh_layers(parts=("sections",))
        if self.path_result:
            self.refresh_path_layers()
        self.pathsChanged.emit()

    def default_tool(self) -> Tuple[str, str]:
        """The plan's default (tool_id, tool_config_id)."""
        return tools.plan_default_tool(self.plan)

    # -- installation paths -------------------------------------------------
    def refresh_layback_profiles(self, emit: bool = True) -> None:
        try:
            self.layback_profiles = self.store.list_layback_profiles()
        except Exception as exc:
            self.storeError.emit(
                f"The layback profile registry could not be read:\n"
                f"{getattr(self.store, 'gpkg_path', '')}\n\n{exc}")
        self.refresh_vessels(emit=emit)
        if emit:
            self.laybacksChanged.emit()
            self.pathsChanged.emit()

    def refresh_vessels(self, emit: bool = True) -> None:
        try:
            self.vessels = self.store.list_vessels()
        except Exception as exc:
            self.storeError.emit(
                f"The vessel registry could not be read:\n"
                f"{getattr(self.store, 'gpkg_path', '')}\n\n{exc}")
        if emit:
            self.vesselsChanged.emit()

    def vessel(self, vessel_id: str) -> Optional[Dict]:
        wanted = str(vessel_id or "")
        return next((row for row in self.vessels
                     if str(row.get("vessel_id") or "") == wanted), None)

    def save_vessel(self, row: Dict) -> str:
        try:
            self.store.ensure_created()
        except Exception as exc:
            self.storeError.emit(
                f"Could not create the Burial Planner GeoPackage:\n{exc}")
            return ""
        ok, vessel_id = self._store_write(
            "save the vessel", self.store.save_vessel, row)
        if not ok:
            return ""
        self.refresh_vessels()
        self.pathsChanged.emit()
        return str(vessel_id or "")

    def delete_vessel(self, vessel_id: str) -> bool:
        ok, _ = self._store_write(
            "delete the vessel", self.store.delete_vessel, vessel_id)
        if ok:
            self.refresh_vessels()
            self.pathsChanged.emit()
        return ok

    def layback_profile(self, layback_id: str) -> Optional[Dict]:
        wanted = str(layback_id or "")
        return next((row for row in self.layback_profiles
                     if str(row.get("layback_id") or "") == wanted), None)

    def save_layback_profile(self, row: Dict) -> str:
        try:
            self.store.ensure_created()
        except Exception as exc:
            self.storeError.emit(
                f"Could not create the Burial Planner GeoPackage:\n{exc}")
            return ""
        ok, layback_id = self._store_write(
            "save the layback profile", self.store.save_layback_profile, row)
        if not ok:
            return ""
        self.refresh_layback_profiles()
        if self.path_result:
            self.refresh_path_layers()
        return str(layback_id or "")

    def delete_layback_profile(self, layback_id: str) -> bool:
        ok, _ = self._store_write(
            "delete the layback profile", self.store.delete_layback_profile,
            layback_id)
        if ok:
            self.refresh_layback_profiles()
            if self.path_result:
                self.refresh_path_layers()
        return ok

    def path_config(self) -> Dict:
        return path_data.config_from_plan(self.plan)

    def path_fingerprints(self, config: Optional[Dict] = None) -> Dict[str, str]:
        config = dict(config or self.path_config())
        tool, tool_config = path_data.effective_tool_and_config(
            self.plan, self.tools)
        layback = self.layback_profile(config.get("layback_id") or "")
        depth_basis = self.depth_fingerprint()
        if len(path_data.layback_points(layback)) > 1 \
                or config.get("radius_rules"):
            profile = self.bathy_profile
            if profile is not None and self.profile_state() == "current":
                depth_basis += (f"|profile:{profile.sampled_utc}:"
                                f"{profile.step_m:g}")
            else:
                depth_basis += "|live-sampling"
        return path_data.build_fingerprints(
            self.plan, self.current_rpl_fingerprint(), tool, tool_config,
            config, layback, depth_basis)

    def path_state(self, config: Optional[Dict] = None) -> Dict[str, str]:
        return path_data.result_state(
            self.path_result, self.path_fingerprints(config))

    def save_path_result(self, row: Dict) -> bool:
        """Persist one derived result, then refresh its map caches."""
        if not self.plan_id:
            return False
        row = dict(row)
        row["plan_id"] = self.plan_id
        ok, _ = self._store_write(
            "save the installation paths", self.store.save_path_result, row)
        if not ok:
            return False
        self.path_result = self.store.get_path_result(self.plan_id)
        self.refresh_path_layers()
        self.pathsChanged.emit()
        return True

    def delete_path_result(self) -> bool:
        if not self.plan_id:
            return False
        ok, _ = self._store_write(
            "clear the installation paths", self.store.delete_path_result,
            self.plan_id)
        if not ok:
            return False
        self.path_result = None
        self.refresh_path_layers()
        self.pathsChanged.emit()
        return True

    def refresh_path_layers(self, ensure: bool = True) -> None:
        if not self.plan:
            return
        try:
            path_layers.write_path_layers(
                self.store, self.plan, self.path_result, self.path_state())
            if ensure:
                path_layers.ensure_path_layers(
                    QgsProject.instance(), self.store.gpkg_path, self.plan)
        except Exception as exc:
            self.storeError.emit(
                f"Installation path layers could not be refreshed: {exc}")

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
        if (before.get("name"), before.get("rev_label")) != (
                self.plan.get("name"), self.plan.get("rev_label")):
            # The spatial layer names embed the plan name and revision:
            # retire the old-named project layers and rewrite under the new
            # names, or the map keeps showing the stale pre-rename layers
            # alongside the new ones forever.
            map_layers.remove_plan_layers(QgsProject.instance(),
                                          self.store.gpkg_path, before)
            self.refresh_layers(immediate=True)
        if "rpl_id" in changed_keys or "rpl_gpkg_path" in changed_keys:
            self._load_route()
        if {"scope_start_kp", "scope_end_kp", "direction", "rpl_id",
            "rpl_gpkg_path"} & changed_keys:
            self.mark_stale()
        if self.path_result and ({
                "scope_start_kp", "scope_end_kp", "direction", "rpl_id",
                "rpl_gpkg_path", "params_json", "name", "rev_label",
        } & changed_keys):
            self.refresh_path_layers()
            self.pathsChanged.emit()
        self.planChanged.emit()
        return True

    def update_gen_params(self, updates: Dict, reason: str = "",
                          stale: bool = True) -> bool:
        """Patch selected workflow parameters without overwriting other tabs.

        Bathymetry preparation, exclusion analysis and candidate generation
        deliberately expose different parts of ``params_json``. Each tab must
        preserve the values owned by the others when it applies its settings.
        ``stale=False`` is for parameters that do not affect generation
        results (e.g. the default burial tool).
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
        if saved and stale:
            self.mark_stale()
        return saved

    # -- inputs / rules ------------------------------------------------------
    def save_input(self, row: Dict) -> bool:
        before_row = self.store.get_input(row.get("input_id") or "")
        row = dict(row)
        row["plan_id"] = self.plan_id

        def write() -> None:
            input_id = self.store.save_input(row)
            row["input_id"] = input_id
            self.store.append_change(
                self.plan_id, change_log.ACTION_SET_INPUT, input_id,
                before={schema.TABLE_INPUT: [before_row] if before_row else []},
                after={schema.TABLE_INPUT: [row]})
            self.inputs = self.store.list_inputs(self.plan_id)

        ok, _ = self._store_transaction("save the input", write)
        if not ok:
            return False
        self.mark_stale()
        if self.path_result:
            self.refresh_path_layers()
            self.pathsChanged.emit()
        self.inputsChanged.emit()
        self.logChanged.emit()
        return True

    def delete_input(self, input_id: str) -> bool:
        before_row = self.store.get_input(input_id)

        def write() -> None:
            self.store.delete_input(input_id)
            self.store.append_change(
                self.plan_id, change_log.ACTION_DELETE_INPUT, input_id,
                before={schema.TABLE_INPUT: [before_row] if before_row else []},
                after={schema.TABLE_INPUT: []})
            self.inputs = self.store.list_inputs(self.plan_id)

        ok, _ = self._store_transaction("delete the input", write)
        if not ok:
            return False
        self.mark_stale()
        if self.path_result:
            self.refresh_path_layers()
            self.pathsChanged.emit()
        self.inputsChanged.emit()
        self.logChanged.emit()
        return True

    def save_rules(self, rules: List[Dict], target_id: str = "",
                   action: str = change_log.ACTION_EDIT_RULE) -> bool:
        before_rules = self.store.list_rules(self.plan_id)

        def write() -> None:
            self.store.save_rules(self.plan_id, rules)
            self.rules = self.store.list_rules(self.plan_id)
            self.store.append_change(
                self.plan_id, action, target_id,
                before={schema.TABLE_RULE: before_rules},
                after={schema.TABLE_RULE: [dict(r) for r in self.rules]})

        ok, _ = self._store_transaction("save the rules", write)
        if not ok:
            return False
        self.mark_stale()
        self.rulesChanged.emit()
        self.logChanged.emit()
        return True

    # -- risk profile --------------------------------------------------------
    def save_risk_checks(self, checks: List[Dict], target_id: str = "",
                         action: str = change_log.ACTION_EDIT_RISK_CHECK) -> bool:
        before_checks = self.store.list_risk_checks(self.plan_id)

        def write() -> None:
            self.store.save_risk_checks(self.plan_id, checks)
            self.risk_checks = self.store.list_risk_checks(self.plan_id)
            self.store.append_change(
                self.plan_id, action, target_id,
                before={schema.TABLE_RISK_CHECK: before_checks},
                after={schema.TABLE_RISK_CHECK: [dict(c) for c in self.risk_checks]})

        ok, _ = self._store_transaction("save the risk checks", write)
        if not ok:
            return False
        self.riskChanged.emit()
        self.logChanged.emit()
        return True

    def delete_risk_check(self, check_id: str) -> bool:
        """Delete one check and its scanned hazards atomically.

        One transaction and one change-log entry covering both tables (the
        events+sections pattern), so a failure can never leave orphan
        hazards rendered as "manual" and rollback restores both at once.
        """
        wanted = str(check_id or "")
        checks = [dict(c) for c in self.risk_checks
                  if str(c.get("check_id") or "") != wanted]
        if len(checks) == len(self.risk_checks):
            return False
        hazards = [dict(h) for h in self.hazards
                   if str(h.get("check_id") or "") != wanted]
        before = {
            schema.TABLE_RISK_CHECK: [dict(c) for c in self.risk_checks],
            schema.TABLE_HAZARD: [dict(h) for h in self.hazards],
        }

        def write() -> None:
            self.store.save_risk_checks(self.plan_id, checks)
            self.risk_checks = self.store.list_risk_checks(self.plan_id)
            self.store.save_hazards(self.plan_id, risk.sort_hazards(hazards))
            self.hazards = self.store.list_hazards(self.plan_id)
            self.store.append_change(
                self.plan_id, change_log.ACTION_DELETE_RISK_CHECK, wanted,
                before=before,
                after={
                    schema.TABLE_RISK_CHECK: [dict(c) for c in self.risk_checks],
                    schema.TABLE_HAZARD: [dict(h) for h in self.hazards],
                })

        ok, _ = self._store_transaction("delete the risk check", write)
        if not ok:
            return False
        self.refresh_layers(parts=("hazards",))
        self.riskChanged.emit()
        self.logChanged.emit()
        return True

    def _write_hazards(self, action: str, target_id: str,
                       new_hazards: List[Dict], reason: str = "") -> bool:
        """One logged, store-written hazard mutation (atomic in SQL mode)."""
        before = {schema.TABLE_HAZARD: [dict(h) for h in self.hazards]}

        def write() -> None:
            self.store.save_hazards(self.plan_id,
                                    risk.sort_hazards(new_hazards))
            self.hazards = self.store.list_hazards(self.plan_id)
            self.store.append_change(
                self.plan_id, action, target_id, before=before,
                after={schema.TABLE_HAZARD: [dict(h) for h in self.hazards]},
                reason=reason)

        ok, _ = self._store_transaction("save the hazards", write)
        if not ok:
            return False
        self.refresh_layers(parts=("hazards",))
        self.riskChanged.emit()
        self.logChanged.emit()
        return True

    def apply_risk_scan(self, auto_hazards: List[Dict],
                        check_ids: Optional[List[str]] = None) -> bool:
        """Replace scanned hazards with the fresh results, carrying the
        user's review (status/notes/user-set risk) over by feature identity.

        ``check_ids`` limits the replacement to those checks (a single-check
        run must not wipe other checks' findings); manual hazards always
        survive.
        """
        wanted = set(check_ids or [])

        def replaced(hazard: Dict) -> bool:
            if (hazard.get("source") or "") == schema.HAZARD_SOURCE_MANUAL:
                return False
            return not wanted or str(hazard.get("check_id") or "") in wanted

        kept = [dict(h) for h in self.hazards if not replaced(h)]
        previous = [h for h in self.hazards if replaced(h)]
        merged = kept + risk.carry_over_hazards(auto_hazards, previous)
        return self._write_hazards(change_log.ACTION_RISK_SCAN,
                                   ",".join(sorted(wanted)) or "all", merged)

    def add_manual_hazard(self, kp: float, end_kp: Optional[float],
                          label: str, risk_level: str, notes: str = "") -> bool:
        lo, hi = self._scope_bounds()
        lo, hi = min(lo, hi), max(lo, hi)
        if not (lo - 1e-9 <= float(kp) <= hi + 1e-9):
            raise ValueError(
                f"The hazard KP must lie inside the plan scope "
                f"KP {schema.format_kp(lo)}-{schema.format_kp(hi)}.")
        row = risk.new_hazard_row(
            self.plan_id, "", f"manual-{schema.new_id()[:8]}", label,
            float(kp), float(end_kp) if end_kp is not None else None,
            0.0, False, None, None, None, "",
            source=schema.HAZARD_SOURCE_MANUAL, notes=notes)
        if self.route is not None:
            point = self.route.point_at_kp(float(kp), clamp=True)
            if point is not None:
                row["lat"], row["lon"] = point.y(), point.x()
        row["risk"] = risk_level or ""
        row["risk_source"] = schema.RISK_SOURCE_USER
        return self._write_hazards(change_log.ACTION_ADD_HAZARD,
                                   row["hazard_id"],
                                   [dict(h) for h in self.hazards] + [row])

    def update_hazards(self, hazard_ids: List[str], updates: Dict,
                       action: str = change_log.ACTION_EDIT_HAZARD) -> bool:
        wanted = {str(h) for h in hazard_ids if h}
        if not wanted:
            return False
        changed = False
        rows = []
        for hazard in self.hazards:
            row = dict(hazard)
            if str(row.get("hazard_id") or "") in wanted:
                if "risk" in updates:
                    row["risk_source"] = schema.RISK_SOURCE_USER
                row.update(updates)
                changed = True
            rows.append(row)
        if not changed:
            return False
        return self._write_hazards(action, ",".join(sorted(wanted)), rows)

    def delete_hazards(self, hazard_ids: List[str]) -> bool:
        wanted = {str(h) for h in hazard_ids if h}
        remaining = [dict(h) for h in self.hazards
                     if str(h.get("hazard_id") or "") not in wanted]
        if len(remaining) == len(self.hazards):
            return False
        return self._write_hazards(change_log.ACTION_DELETE_HAZARD,
                                   ",".join(sorted(wanted)), remaining)

    # -- events --------------------------------------------------------------
    def _scope_bounds(self) -> Tuple[float, float]:
        return (float(self.plan.get("scope_start_kp") or 0.0),
                float(self.plan.get("scope_end_kp") or 0.0))

    def _write_events_and_sections(self, action: str, target_id: str,
                                   new_events: List[Dict], reason: str,
                                   note_specs: Optional[List[Tuple[
                                       float, Callable[[str], str]]]] = None,
                                   dismiss: Optional[List[Tuple]] = None
                                   ) -> bool:
        """One logged, store-written event mutation + derived section rebuild.

        Events, sections and the change-log entry commit together (one
        transaction in SQL mode) so a failure can never leave events moved
        with sections still describing the old boundaries.

        ``note_specs`` are ``(kp, apply)`` pairs: after the sections are
        re-derived, ``apply(existing_notes)`` rewrites the Notes of every
        section containing that KP — the mechanism behind the automatic
        audit notes in the Plan Builder Notes columns. A KP sitting exactly
        on a boundary annotates both adjacent sections.

        ``dismiss`` lists no-data KP ranges to remove from the plan's
        Insufficient Information context in the same edit, as
        ``(start, end)`` pairs (resolve as skip) or ``(start, end, kind)``
        entries (kind ``skip`` or ``burial``): the resolution context, the
        plan's persisted resolution list (``params_json``) and the active
        generation's stored context all update together, so the resolved
        range keeps its skip/burial state after reopening the plan and
        across later Generate runs, and the change-log entry rolls all of
        it back.
        """
        before = {
            schema.TABLE_EVENT: [dict(e) for e in self.events],
            schema.TABLE_SECTION: [dict(s) for s in self.sections],
        }
        dismiss_entries = generation.resolution_entries(dismiss)
        dismiss_intervals = [Interval(a, b) for a, b, _k in dismiss_entries]
        plan_before: Optional[Dict] = None
        gen_before: Optional[Dict] = None
        gen_after: Optional[Dict] = None
        old_insufficient: Optional[List[Interval]] = None
        if dismiss_intervals:
            plan_before = dict(self.plan)
            gen_before = self.store.active_generation(self.plan_id)
            old_insufficient = list(self.context.insufficient)
            self.context.insufficient = eng.subtract_intervals(
                self.context.insufficient, dismiss_intervals)
            before[schema.TABLE_PLAN] = [dict(plan_before)]
            if gen_before:
                before[schema.TABLE_GENERATION] = [dict(gen_before)]
            # Persist the resolution for future Generate runs — before the
            # section rebuild, which reads it to tag the resulting
            # sections. New entries are appended so they override earlier
            # overlapping ones (latest decision wins).
            try:
                stored = json.loads(self.plan.get("params_json") or "{}")
            except (TypeError, ValueError):
                stored = {}
            if not isinstance(stored, dict):
                stored = {}
            entries = generation.resolution_entries(
                stored.get("dismissed_insufficient"))
            entries.extend(dismiss_entries)
            stored["dismissed_insufficient"] = \
                generation.normalise_resolutions(entries)
            self.plan["params_json"] = json.dumps(stored)
        new_events = ev.sort_events(new_events, self.direction)
        new_sections = self._derive_sections(new_events)
        tol = 1e-6
        for kp, apply_fn in note_specs or []:
            for section in new_sections:
                try:
                    lo = float(section.get("start_kp"))
                    hi = float(section.get("end_kp"))
                except (TypeError, ValueError):
                    continue
                if lo - tol <= float(kp) <= hi + tol:
                    section["notes"] = apply_fn(section.get("notes") or "")

        if dismiss_intervals:
            if gen_before:
                # Keep the active generation's stored context (and headline
                # lengths) in step with the re-derived sections, or the plan
                # would regrow the II section when reopened.
                try:
                    summary = json.loads(gen_before.get("summary_json") or "{}")
                except (TypeError, ValueError):
                    summary = {}
                if not isinstance(summary, dict):
                    summary = {}
                context = summary.get("context")
                if not isinstance(context, dict):
                    context = {}
                context["insufficient"] = [
                    [iv.start_km, iv.end_km]
                    for iv in self.context.insufficient]
                summary["context"] = context
                summary["burial_km"] = round(sum(
                    float(s.get("length_km") or 0.0) for s in new_sections
                    if s.get("kind") == schema.SECTION_BURIAL), 6)
                scope_km = float(summary.get("scope_km") or 0.0)
                if scope_km > 0:
                    summary["burial_pct"] = round(
                        100.0 * summary["burial_km"] / scope_km, 2)
                summary["skip_km"] = round(sum(
                    float(s.get("length_km") or 0.0) for s in new_sections
                    if s.get("kind") == schema.SECTION_SKIP), 6)
                summary["insufficient_km"] = round(sum(
                    float(s.get("length_km") or 0.0) for s in new_sections
                    if s.get("kind") == schema.SECTION_INSUFFICIENT), 6)
                summary["section_count"] = len(new_sections)
                gen_after = dict(gen_before)
                gen_after["summary_json"] = json.dumps(summary)

        def write() -> None:
            if dismiss_intervals:
                self.store.save_plan(self.plan)
                if gen_after:
                    self.store.save_generation(gen_after)
            self.store.save_events(self.plan_id, new_events)
            self.store.save_sections(self.plan_id, new_sections)
            self.events = self.store.list_events(self.plan_id)
            self.sections = self.store.list_sections(self.plan_id)
            after = {
                schema.TABLE_EVENT: [dict(e) for e in self.events],
                schema.TABLE_SECTION: [dict(s) for s in self.sections],
            }
            if dismiss_intervals:
                after[schema.TABLE_PLAN] = [dict(self.plan)]
                if gen_after:
                    after[schema.TABLE_GENERATION] = [dict(gen_after)]
            self.store.append_change(
                self.plan_id, action, target_id, before=before,
                after=after, reason=reason)

        ok, _ = self._store_transaction("save the events and sections", write)
        if not ok:
            if dismiss_intervals:
                # Roll the in-memory curation back with the failed write.
                if old_insufficient is not None:
                    self.context.insufficient = old_insufficient
                if plan_before is not None:
                    self.plan = plan_before
            return False
        self.refresh_layers(parts=("sections", "events"))
        self.eventsChanged.emit()
        self.sectionsChanged.emit()
        self.logChanged.emit()
        if dismiss_intervals:
            self.planChanged.emit()
        return True

    def _derive_sections(self, events: List[Dict]) -> List[Dict]:
        params = self.gen_params(self._active_params() or None)
        rule_names = {str(r.get("rule_id")): (r.get("name") or "") for r in self.rules}
        # Resolved no-data ranges come from the *plan* params (live
        # curation), not the active generation's snapshot, which predates
        # any resolutions made since that run.
        live_params = self.gen_params()
        dismissed = generation.dismissed_intervals(live_params)
        resolved_burial = generation.resolved_intervals(
            live_params, generation.RESOLVE_BURIAL)
        return generation.build_sections(
            events, params, self.context.excluded, self.context.screening,
            self.context.influence, self.context.insufficient,
            self.context.dropped_short, rule_names,
            previous_sections=self.sections, plan_id=self.plan_id,
            # Interactive edits move boundaries deliberately; the adjacent
            # sections' notes/conclusions/tools must survive the rebuild.
            carry_by_overlap=True, dismissed=dismissed,
            resolved_burial=resolved_burial)

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
        old_kp = float(target.get("kp") or 0.0)
        label = ev.event_label(target.get("event_type") or "", self.method)
        # "moved" is the confirmation dialog's fallback, not a real reason.
        note_reason = "" if reason in ("", "moved") else reason
        moved = []
        for event in self.events:
            copy = dict(event)
            if copy.get("event_id") == event_id:
                copy["kp"] = float(new_kp)
                copy["notes"] = ev.upsert_move_note(
                    copy.get("notes") or "", label, old_kp, new_kp,
                    note_reason)
                self._stamp_position(copy)
            moved.append(copy)
        # The new KP sits exactly on the shared boundary, so this annotates
        # both adjacent sections; move chains coalesce into one note.
        note_specs = [(float(new_kp),
                       lambda existing: ev.upsert_move_note(
                           existing, label, old_kp, new_kp, note_reason))]
        return self._write_events_and_sections(change_log.ACTION_MOVE_EVENT,
                                               event_id, moved, reason,
                                               note_specs)

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
        note_specs = []
        for event in self.events:
            if str(event.get("event_id") or "") not in wanted:
                continue
            kp = float(event.get("kp") or 0.0)
            text = ev.audit_note(
                f"{ev.event_label(event.get('event_type') or '', self.method)}"
                f" at KP {schema.format_kp(kp)} deleted", reason)
            note_specs.append(
                (kp, lambda existing, t=text: ev.append_note(existing, t)))
        return self._write_events_and_sections(change_log.ACTION_DELETE_EVENT,
                                               ",".join(sorted(wanted)),
                                               remaining, reason, note_specs)

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
        if "tool_id" in updates:
            # Model-level invariant for every writer (combo, import, bulk
            # edit): changing the tool clears a configuration that belonged
            # to the previous tool and stamps the section method with the
            # tool's type ("" = inherit the plan default/method).
            updates = dict(updates)
            tool = tools.tool_by_id(self.tools, updates.get("tool_id") or "")
            updates.setdefault("tool_config_id", "")
            updates["method"] = schema.normalise_method(
                (tool or {}).get("tool_type") or "")
        updated = []
        for section in self.sections:
            copy = dict(section)
            if copy.get("section_id") == section_id:
                copy.update(updates)
            updated.append(copy)

        def write() -> None:
            self.store.save_sections(self.plan_id, updated)
            self.sections = self.store.list_sections(self.plan_id)
            self.store.append_change(
                self.plan_id, action, section_id,
                before={schema.TABLE_SECTION: before_rows},
                after={schema.TABLE_SECTION: [
                    dict(s) for s in self.sections
                    if s.get("section_id") == section_id]})

        ok, _ = self._store_transaction("save the section", write)
        if not ok:
            return False
        self.refresh_layers(parts=("sections",))
        self.sectionsChanged.emit()
        self.logChanged.emit()
        return True

    def assign_skip_handling(self, transit_max_km: float,
                             overwrite: bool = False) -> int:
        """Auto-assign skip handling by length as one undoable edit.

        Skips ≤ ``transit_max_km`` become mid-water transits, longer skips
        recover-to-deck; only TBC skips change unless ``overwrite``.
        Returns the number of skips changed (-1 when the store write failed).
        """
        if not self.plan:
            return 0
        updated, changed = generation.assign_skip_handling(
            self.sections, transit_max_km, overwrite)
        if not changed:
            return 0
        before = {schema.TABLE_SECTION: [dict(s) for s in self.sections]}

        def write() -> None:
            self.store.save_sections(self.plan_id, updated)
            self.sections = self.store.list_sections(self.plan_id)
            self.store.append_change(
                self.plan_id, change_log.ACTION_EDIT_SECTION,
                "skip_handling_auto", before=before,
                after={schema.TABLE_SECTION: [dict(s) for s in self.sections]},
                reason=f"auto-assign skip handling (mid-water transit ≤ "
                       f"{float(transit_max_km):g} km)")

        ok, _ = self._store_transaction("save the sections", write)
        if not ok:
            return -1
        self.refresh_layers(parts=("sections",))
        self.sectionsChanged.emit()
        self.logChanged.emit()
        return changed

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
        inserted_kind = (schema.SECTION_SKIP
                         if section.get("kind") == schema.SECTION_BURIAL
                         else schema.SECTION_BURIAL)
        text = ev.audit_note(
            f"{schema.section_kind_label(inserted_kind, self.method)} KP "
            f"{schema.format_kp(lo)}-{schema.format_kp(hi)} inserted", reason)
        note_specs = [((lo + hi) / 2.0,
                       lambda existing: ev.append_note(existing, text))]
        return self._write_events_and_sections(
            change_log.ACTION_INSERT_SECTION, section_id, candidate, reason,
            note_specs)

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
        """Merge selected burial sections or selected skips.

        Insufficient Information sections merge only when explicitly part
        of the selection: their no-data ranges are dismissed (persistently —
        see ``_write_events_and_sections``) so the merged neighbour extends
        across them.
        """
        remaining, _removed, kind, dismissed, moved = ev.merge_section_events(
            self.events, self.sections, section_ids, self.method)
        for event in moved:
            # A boundary event extended across an edge II range: its lat/
            # lon/depth are derived from the KP and must follow the move.
            self._stamp_position(event)
        lo, hi = self._scope_bounds()
        result = ev.validate_events(
            remaining, lo, hi, self.direction, self.method)
        if result.errors:
            raise ValueError(result.errors[0])
        wanted = {str(section_id) for section_id in section_ids if section_id}
        selected = [s for s in self.sections
                    if str(s.get("section_id") or "") in wanted]
        span_lo = min(float(s.get("start_kp") or 0.0) for s in selected)
        span_hi = max(float(s.get("end_kp") or 0.0) for s in selected)
        text = (f"{len(selected)} sections merged KP "
                f"{schema.format_kp(span_lo)}-{schema.format_kp(span_hi)}")
        if dismissed:
            ranges = ", ".join(
                f"KP {schema.format_kp(a)}-{schema.format_kp(b)}"
                for a, b in dismissed)
            text += f" (Insufficient Information dismissed {ranges})"
        audit = ev.audit_note(text, reason)
        note_specs = [((span_lo + span_hi) / 2.0,
                       self._fold_notes_fn(span_lo, span_hi, audit))]
        # A range merged into a burial section is a burial resolution: the
        # engineer accepted burial across the gap, so later Generate runs
        # must keep it burial (a skip merge keeps today's skip dismissal).
        resolve_kind = (generation.RESOLVE_BURIAL
                        if kind == schema.SECTION_BURIAL
                        else generation.RESOLVE_SKIP)
        return self._write_events_and_sections(
            change_log.ACTION_MERGE_SECTIONS, ",".join(section_ids),
            remaining, reason, note_specs,
            dismiss=[(a, b, resolve_kind) for a, b in dismissed] or None)

    def delete_section(self, section_id: str, reason: str = "") -> bool:
        """Delete a burial section or skip outright.

        Its boundary events are removed and the neighbouring sections merge
        into one; the notes of the removed section and its neighbours are
        folded into the merged section together with an audit note naming
        the removed KP range.

        Deleting an Insufficient Information section removes no events:
        its no-data range is dismissed (persistently) and the range becomes
        a skip, coalescing with any adjacent skips.
        """
        target = next((s for s in self.sections
                       if str(s.get("section_id") or "") == str(section_id)),
                      None)
        if target is not None and \
                target.get("kind") == schema.SECTION_INSUFFICIENT:
            return self._dismiss_insufficient_section(target, reason)
        remaining, _removed, section = ev.delete_section_events(
            self.events, self.sections, section_id, self.method)
        lo, hi = self._scope_bounds()
        result = ev.validate_events(
            remaining, lo, hi, self.direction, self.method)
        if result.errors:
            raise ValueError(result.errors[0])
        start = float(section.get("start_kp") or 0.0)
        end = float(section.get("end_kp") or 0.0)
        audit = ev.audit_note(
            f"{schema.section_kind_label(section.get('kind') or '', self.method)}"
            f" KP {schema.format_kp(start)}-{schema.format_kp(end)} removed, "
            "neighbours merged", reason)
        # The neighbours either side merge into the survivor, so the notes
        # fold spans from the left neighbour's start to the right one's end.
        span_lo, span_hi = start, end
        for other in self.sections:
            try:
                other_lo = float(other.get("start_kp"))
                other_hi = float(other.get("end_kp"))
            except (TypeError, ValueError):
                continue
            if abs(other_hi - start) <= 1e-6:
                span_lo = min(span_lo, other_lo)
            if abs(other_lo - end) <= 1e-6:
                span_hi = max(span_hi, other_hi)
        note_specs = [((start + end) / 2.0,
                       self._fold_notes_fn(span_lo, span_hi, audit))]
        return self._write_events_and_sections(
            change_log.ACTION_DELETE_SECTION, section_id, remaining, reason,
            note_specs)

    def _dismiss_insufficient_section(self, section: Dict,
                                      reason: str = "") -> bool:
        """Dismiss one Insufficient Information section's no-data range.

        No events change: the range simply stops being reported as II and
        becomes a skip (coalescing with adjacent skips). The dismissal is
        persisted with the plan, so the range stays a skip across later
        Generate runs; the edit is one undoable change-log entry.
        """
        start = float(section.get("start_kp") or 0.0)
        end = float(section.get("end_kp") or 0.0)
        audit = ev.audit_note(
            f"Insufficient Information KP {schema.format_kp(start)}-"
            f"{schema.format_kp(end)} dismissed", reason)
        note_specs = [((start + end) / 2.0,
                       self._fold_notes_fn(start, end, audit))]
        return self._write_events_and_sections(
            change_log.ACTION_DISMISS_INSUFFICIENT,
            str(section.get("section_id") or ""),
            [dict(e) for e in self.events], reason, note_specs,
            dismiss=[(start, end)])

    def resolve_insufficient_sections(self, section_ids: List[str],
                                      as_kind: str, reason: str = "") -> bool:
        """Resolve Insufficient Information sections as skip or burial.

        One undoable change-log entry covers every selected range. Resolving
        as skip is a (bulk) dismissal: no events change, the ranges become
        skips coalescing with their neighbours. Resolving as burial inserts
        or extends boundary events so the ranges become burial sections,
        merging with abutting burial sections; the resulting sections carry
        an ``insufficient_override`` flag because there is still no data
        there. Both resolutions persist across later Generate runs and
        reset on Generate (fresh).
        """
        wanted = {str(section_id) for section_id in section_ids if section_id}
        selected = [s for s in self.sections
                    if str(s.get("section_id") or "") in wanted]
        if len(selected) != len(wanted) or not selected:
            raise ValueError("Section not found.")
        if any(s.get("kind") != schema.SECTION_INSUFFICIENT
               for s in selected):
            raise ValueError(
                "Only Insufficient Information sections can be resolved.")
        if as_kind not in (schema.SECTION_BURIAL, schema.SECTION_SKIP):
            raise ValueError("Resolve as a burial section or a skip.")
        ranges = sorted((float(s.get("start_kp") or 0.0),
                         float(s.get("end_kp") or 0.0)) for s in selected)
        kind_label = schema.section_kind_label(as_kind, self.method)
        note_specs = []
        for start, end in ranges:
            audit = ev.audit_note(
                f"Insufficient Information KP {schema.format_kp(start)}-"
                f"{schema.format_kp(end)} resolved as {kind_label} "
                "(no data)", reason)
            note_specs.append(((start + end) / 2.0,
                               self._fold_notes_fn(start, end, audit)))
        target = ",".join(sorted(wanted))
        if as_kind == schema.SECTION_SKIP:
            return self._write_events_and_sections(
                change_log.ACTION_DISMISS_INSUFFICIENT, target,
                [dict(e) for e in self.events], reason, note_specs,
                dismiss=[(a, b, generation.RESOLVE_SKIP)
                         for a, b in ranges])
        remaining, specs, _removed = ev.resolve_insufficient_events(
            self.events, self.sections, list(wanted), self.direction)
        for event_type, kp in specs:
            event = {
                "event_id": schema.new_id(), "plan_id": self.plan_id,
                "generation_id": "", "seq": 0, "event_type": event_type,
                "kp": float(kp), "end_kp": None,
                "source": schema.EVENT_SOURCE_MANUAL,
                "status": schema.EVENT_STATUS_CANDIDATE, "locked": 0,
                "notes": "",
            }
            self._stamp_position(event)
            remaining.append(event)
        lo, hi = self._scope_bounds()
        result = ev.validate_events(
            remaining, lo, hi, self.direction, self.method)
        if result.errors:
            raise ValueError(result.errors[0])
        return self._write_events_and_sections(
            change_log.ACTION_RESOLVE_INSUFFICIENT, target,
            remaining, reason, note_specs,
            dismiss=[(a, b, generation.RESOLVE_BURIAL) for a, b in ranges])

    def _fold_notes_fn(self, span_lo: float, span_hi: float,
                       audit: str) -> Callable[[str], str]:
        """Notes rewriter folding the manual notes of every current section
        strictly inside ``[span_lo, span_hi]`` into the target, then
        appending the audit note. Sections replaced by a merge/delete lose
        their rows, so their notes would otherwise silently disappear;
        sections that merely touch the span boundary survive unchanged and
        keep their own notes."""
        tol = 1e-6
        old_notes: List[str] = []
        for section in sorted(self.sections,
                              key=lambda s: float(s.get("start_kp") or 0.0)):
            try:
                lo = float(section.get("start_kp"))
                hi = float(section.get("end_kp"))
            except (TypeError, ValueError):
                continue
            if hi <= span_lo + tol or lo >= span_hi - tol:
                continue
            note = (section.get("notes") or "").strip()
            if note:
                old_notes.append(note)

        def apply(existing: str) -> str:
            out = existing
            for note in old_notes:
                if note not in out:
                    out = ev.append_note(out, note)
            return ev.append_note(out, audit)

        return apply

    # -- generation ----------------------------------------------------------
    def apply_generation(self, output: generation.GenerationOutput,
                         params: generation.GenParams, rule_rows: List[Dict],
                         inputs_fingerprints: Dict[str, str],
                         generation_id: str) -> bool:
        """Persist one algorithm run atomically: snapshot row + events +
        sections + change log in one transaction (main thread)."""
        # Snapshot only the generation rows that this run actually changes
        # (the currently-active ones flip to inactive). Snapshotting every
        # historic generation made each Generate's log entry grow with the
        # plan's history — every prior row carries its own full context.
        previously_active = [
            dict(g) for g in self.store.list_generations(self.plan_id)
            if int(g.get("active") or 0)]
        before = {
            schema.TABLE_EVENT: [dict(e) for e in self.events],
            schema.TABLE_SECTION: [dict(s) for s in self.sections],
            schema.TABLE_GENERATION: previously_active,
            schema.TABLE_PLAN: [dict(self.plan)],
        }
        # The run's dismissed-II list is authoritative: a fresh run passes
        # an empty list (dismissals are curation and a fresh run drops
        # curation), a normal run carries the plan's list through. Sync it
        # back so the plan and the run can never disagree.
        try:
            stored_params = json.loads(self.plan.get("params_json") or "{}")
        except (TypeError, ValueError):
            stored_params = {}
        if not isinstance(stored_params, dict):
            stored_params = {}
        run_entries = generation.normalise_resolutions(
            params.dismissed_insufficient)
        if run_entries or "dismissed_insufficient" in stored_params:
            stored_params["dismissed_insufficient"] = run_entries
            self.plan["params_json"] = json.dumps(stored_params)
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

        def write() -> None:
            self.store.save_generation(gen_row)
            self.store.save_events(self.plan_id, output.events)
            self.store.save_sections(self.plan_id, output.sections)
            self.events = self.store.list_events(self.plan_id)
            self.sections = self.store.list_sections(self.plan_id)
            self.plan["status"] = schema.PLAN_STATUS_DRAFT
            self.plan["rpl_fingerprint"] = self.current_rpl_fingerprint()
            self.store.save_plan(self.plan)
            deactivated = [dict(row, active=0) for row in previously_active]
            self.store.append_change(
                self.plan_id, change_log.ACTION_GENERATE, generation_id,
                before=before,
                after={
                    schema.TABLE_EVENT: [dict(e) for e in self.events],
                    schema.TABLE_SECTION: [dict(s) for s in self.sections],
                    schema.TABLE_GENERATION: deactivated + [gen_row],
                    schema.TABLE_PLAN: [dict(self.plan)],
                })

        ok, _ = self._store_transaction("save the generation", write)
        if not ok:
            return False
        self.context = generation.context_from_dict(summary["context"])
        self.refresh_layers(parts=("sections", "events"))
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
        # An Insufficient Information dismissal (or a merge that included
        # one) also rewrote the plan params and the active generation's
        # stored context; reload them so the II range truly comes back.
        touched = set()
        for payload in (entry.get("before_json"), entry.get("after_json")):
            try:
                data = json.loads(payload or "{}")
            except (TypeError, ValueError):
                data = {}
            if isinstance(data, dict):
                touched.update(data)
        if schema.TABLE_PLAN in touched:
            self.plan = self.store.get_plan(self.plan_id) or self.plan
        if schema.TABLE_GENERATION in touched:
            self._load_context()
        self.refresh_layers()
        self.eventsChanged.emit()
        self.sectionsChanged.emit()
        self.logChanged.emit()
        if schema.TABLE_PLAN in touched:
            self.planChanged.emit()
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
    def refresh_layers(self, parts=None, immediate: bool = False) -> None:
        """Schedule a spatial-layer refresh (debounced, per changed part).

        ``parts`` names the layers whose data changed ("sections",
        "events", "hazards"); None refreshes all three. Rapid consecutive
        edits coalesce into one write ~150 ms after the last one; pass
        ``immediate=True`` to flush synchronously.
        """
        if not self.plan or self.route is None:
            return
        self._pending_layer_parts.update(
            map_layers.ALL_PLAN_LAYER_PARTS if parts is None else parts)
        if immediate:
            self._flush_layer_refresh()
        else:
            self._layer_timer.start()

    def _flush_layer_refresh(self) -> None:
        self._layer_timer.stop()
        parts = sorted(self._pending_layer_parts)
        self._pending_layer_parts = set()
        if not parts or not self.plan or self.route is None:
            return
        try:
            map_layers.write_plan_layers(
                self.store, self.plan, self.sections, self.events, self.route,
                hazards=self.hazards, risk_checks=self.risk_checks,
                tools=self.tools, parts=parts,
                segment_wkt_cache=self._segment_wkt_cache)
            map_layers.ensure_plan_layers(QgsProject.instance(),
                                          self.store.gpkg_path, self.plan,
                                          parts=parts)
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
                                   active.get("generation_id") or "",
                                   tools=self.tools)

    def export_inputs_csv(self) -> str:
        return io_csv.inputs_csv(self.plan, self.inputs)

    def export_hazards_csv(self) -> str:
        return io_csv.hazards_csv(self.plan, self.hazards, self.risk_checks)
