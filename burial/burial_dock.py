# -*- coding: utf-8 -*-
"""Burial Planner (beta) dock — plan strip + workflow tabs + profile pane.

Single-instance, raise-if-open (Planner behaviour). Top strip: plan selector
+ New / Duplicate / Rename / Delete + status badge + GeoPackage path control.
Below: the guided tabs (Plan → Inputs → Exclusions → Plan Builder → Review).
Bottom: the persistent longitudinal profile pane on a collapsible splitter.

All analysis runs on ``QgsApplication.taskManager()`` with non-modal in-dock
progress and a working Stop (resumable) — QGIS stays usable throughout. No
``QApplication.processEvents()`` anywhere in this package.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qgis.core import QgsApplication, QgsCoordinateTransform, QgsProject
from qgis.gui import QgsVertexMarker, QgsRubberBand
from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:
    from qgis.PyQt import sip

    _sip_isdeleted = sip.isdeleted
except Exception:  # pragma: no cover
    try:
        import sip

        _sip_isdeleted = sip.isdeleted
    except Exception:
        def _sip_isdeleted(_obj):
            return False

from ..qgis_compat import (
    GEOMETRY_LINE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    WINDOW_HINT_CLOSE,
    WINDOW_HINT_CUSTOMIZE,
    WINDOW_HINT_MIN_MAX,
    WINDOW_HINT_TITLE,
    WINDOW_TYPE_WINDOW,
)
from ..workbench import store as wb_store_module
from ..workbench.store import WorkbenchStore
from . import analysis_task, generation, map_layers, schema
from .plan_model import PlanModel
from .profile_widget import BurialProfileWidget
from .store import (
    BurialStore,
    default_project_gpkg_path,
    project_gpkg_path,
    set_project_gpkg_path,
)
from .tabs.builder_tab import BuilderTab
from .tabs.inputs_tab import InputsTab
from .tabs.plan_tab import PlanTab
from .tabs.review_tab import ReviewTab
from .tabs.rules_tab import RulesTab

_VERTICAL = getattr(Qt, "Orientation", Qt).Vertical

_STATUS_STYLES = {
    schema.PLAN_STATUS_DRAFT: "background:#e8f5e9;color:#1b5e20;padding:2px 8px;",
    schema.PLAN_STATUS_STALE: "background:#fff3cd;color:#7a4f00;padding:2px 8px;",
    schema.PLAN_STATUS_ISSUED: "background:#e3f2fd;color:#0d47a1;padding:2px 8px;",
}


class BurialPlannerDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Burial Planner (beta)", parent)
        self.iface = iface
        self.canvas = iface.mapCanvas() if iface is not None else None
        self.setObjectName("SubseaCableToolsBurialPlannerDock")
        self._loading = False
        self._task: Optional[analysis_task.BurialAnalysisTask] = None
        self._profile_task: Optional[analysis_task.ProfileSamplingTask] = None
        self._profile_generation = 0
        self._generate_after_analysis = False
        self._marker = None
        self._band = None

        path = project_gpkg_path() or default_project_gpkg_path()
        self.store, path, error = self._open_store_with_recovery(path)
        self.store_ready = not error
        if not self.store_ready:
            QMessageBox.warning(None, "Burial Planner",
                                "Could not open or migrate the Burial Planner "
                                f"GeoPackage.\n{error}")
        elif not project_gpkg_path():
            set_project_gpkg_path(path)

        self.model = PlanModel(self.store, self.workbench_store())
        self.model.storeError.connect(self._on_store_error)

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.addLayout(self._build_plan_strip())

        self.splitter = QSplitter(_VERTICAL)
        self.tabs = QTabWidget()
        self.plan_tab = PlanTab(self.model)
        self.inputs_tab = InputsTab(self.model, self.workbench_store)
        self.rules_tab = RulesTab(self.model, self)
        self.builder_tab = BuilderTab(self.model, self)
        self.review_tab = ReviewTab(self.model, self)
        self.tabs.addTab(self.plan_tab, "Plan")
        self.tabs.addTab(self.inputs_tab, "Inputs")
        self.tabs.addTab(self.rules_tab, "Exclusions")
        self.tabs.addTab(self.builder_tab, "Plan Builder")
        self.tabs.addTab(self.review_tab, "Review && Export")
        self.splitter.addWidget(self.tabs)

        profile_pane = QWidget()
        profile_layout = QVBoxLayout(profile_pane)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_status_row = QHBoxLayout()
        self.profile_status = QLabel("Bathymetry profile")
        profile_status_row.addWidget(self.profile_status, 1)
        self.profile_drag_toggle = QCheckBox("Allow PLDN/PLUP dragging")
        self.profile_drag_toggle.setChecked(bool(QSettings().value(
            "SubseaCableTools/BurialPlanner/profile_drag_enabled", False,
            type=bool)))
        self.profile_drag_toggle.setToolTip(
            "When enabled, unlocked PLDN/PLUP lines can be dragged on the "
            "profile. Changes are saved on release and can be undone with "
            "Ctrl+Z in Plan Builder.")
        self.profile_drag_toggle.setStyleSheet(
            "color: #b36b00; font-weight: 600;"
            if self.profile_drag_toggle.isChecked() else "")
        self.profile_drag_toggle.toggled.connect(self._profile_drag_toggled)
        profile_status_row.addWidget(self.profile_drag_toggle)
        self.profile_progress = QProgressBar()
        self.profile_progress.setRange(0, 100)
        self.profile_progress.setMaximumWidth(240)
        self.profile_progress.setVisible(False)
        profile_status_row.addWidget(self.profile_progress)
        self.profile_cancel = QPushButton("Stop profile refresh")
        self.profile_cancel.setVisible(False)
        self.profile_cancel.clicked.connect(self._cancel_profile_refresh)
        profile_status_row.addWidget(self.profile_cancel)
        profile_layout.addLayout(profile_status_row)
        self.profile = BurialProfileWidget()
        self.profile.kpHovered.connect(self._on_profile_hover)
        self.profile.kpClicked.connect(self.goto_kp)
        self.profile.eventMoveRequested.connect(self._on_profile_event_moved)
        profile_layout.addWidget(self.profile, 1)
        self.splitter.addWidget(profile_pane)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        outer.addWidget(self.splitter, 1)
        self.setWidget(container)

        self.model.planChanged.connect(self._refresh_strip)
        self.model.planChanged.connect(self._refresh_profile)
        self.model.eventsChanged.connect(self._refresh_profile_events)
        self.model.inputsChanged.connect(self._refresh_profile)
        self.topLevelChanged.connect(self._top_level_changed)

        self.refresh_plans()

    # -- store lifecycle ------------------------------------------------------
    def _open_store_with_recovery(self, path: str):
        try:
            store = BurialStore(path)
            store.migrate()
            return store, path, None
        except Exception as first_error:
            fallback = default_project_gpkg_path()
            if fallback != path:
                try:
                    store = BurialStore(fallback)
                    store.migrate()
                    set_project_gpkg_path(fallback)
                    return store, fallback, None
                except Exception:
                    pass
            return BurialStore(path), path, str(first_error)

    def workbench_store(self) -> Optional[WorkbenchStore]:
        """Read-only access to the project's Workbench registry, if present."""
        try:
            path = wb_store_module.project_gpkg_path() \
                or wb_store_module.default_project_gpkg_path()
            if not path:
                return None
            store = WorkbenchStore(path)
            return store if store.exists() else None
        except Exception:
            return None

    def _on_store_error(self, message: str) -> None:
        QMessageBox.warning(self, "Burial Planner", message)

    # -- plan strip -----------------------------------------------------------
    def _build_plan_strip(self) -> QHBoxLayout:
        strip = QHBoxLayout()
        strip.addWidget(QLabel("Plan:"))
        self.plan_combo = QComboBox()
        self.plan_combo.currentIndexChanged.connect(self._plan_selected)
        strip.addWidget(self.plan_combo, 1)
        for label, slot in (("New", self._new_plan), ("Duplicate", self._duplicate_plan),
                            ("Rename", self._rename_plan), ("Delete", self._delete_plan)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            strip.addWidget(button)
        self.method_label = QLabel("")
        strip.addWidget(self.method_label)
        self.status_badge = QLabel("")
        strip.addWidget(self.status_badge)
        self.gpkg_button = QPushButton("GeoPackage…")
        self.gpkg_button.setToolTip(self.store.gpkg_path)
        self.gpkg_button.clicked.connect(self._pick_gpkg)
        strip.addWidget(self.gpkg_button)
        return strip

    def refresh_plans(self, select_id: str = "") -> None:
        plans = self.store.list_plans() if self.store_ready else []
        selected = select_id or self.model.plan_id
        self._loading = True
        try:
            self.plan_combo.clear()
            for plan in plans:
                self.plan_combo.addItem(plan.get("name") or "Plan", plan.get("plan_id"))
            index = self.plan_combo.findData(selected)
            self.plan_combo.setCurrentIndex(index if index >= 0 else (0 if plans else -1))
        finally:
            self._loading = False
        self._plan_selected()

    def _plan_selected(self) -> None:
        if self._loading:
            return
        plan_id = self.plan_combo.currentData() or ""
        if plan_id and plan_id != self.model.plan_id:
            self.model.load_plan(plan_id)
            map_layers.ensure_plan_layers(QgsProject.instance(),
                                          self.store.gpkg_path, self.model.plan)
        elif not plan_id:
            self.model.close_plan()
        self._refresh_strip()

    def _refresh_strip(self) -> None:
        plan = self.model.plan
        self.method_label.setText(
            schema.METHOD_LABELS.get(plan.get("method") or "", ""))
        status = plan.get("status") or ""
        self.status_badge.setText(status)
        self.status_badge.setStyleSheet(_STATUS_STYLES.get(status, ""))
        self.status_badge.setVisible(bool(status and plan))
        self.gpkg_button.setToolTip(self.store.gpkg_path)
        index = self.plan_combo.findData(self.model.plan_id)
        if index >= 0 and self.plan_combo.currentIndex() != index:
            self._loading = True
            try:
                self.plan_combo.setCurrentIndex(index)
            finally:
                self._loading = False
        if index >= 0 and self.plan_combo.itemText(index) != (plan.get("name") or "Plan"):
            self.plan_combo.setItemText(index, plan.get("name") or "Plan")

    def _new_plan(self) -> None:
        if not self.store_ready:
            return
        name, ok = QInputDialog.getText(self, "New burial plan", "Name:")
        if not ok or not name.strip():
            return
        labels = [schema.METHOD_LABELS[m] for m in schema.METHODS]
        label, ok = QInputDialog.getItem(self, "New burial plan", "Method:",
                                         labels, 0, False)
        if not ok:
            return
        method = schema.METHODS[labels.index(label)]
        plan_id = self.model.create_plan(name.strip(), method)
        if plan_id:
            self.refresh_plans(plan_id)

    def _duplicate_plan(self) -> None:
        if not self.model.plan:
            return
        proposed = f"{self.model.plan.get('name') or 'Plan'} copy"
        name, ok = QInputDialog.getText(self, "Duplicate plan", "Copy name:",
                                        text=proposed)
        if not ok or not name.strip():
            return
        try:
            new_id = self.store.duplicate_plan(self.model.plan_id, name.strip())
        except Exception as exc:
            QMessageBox.warning(self, "Burial Planner", f"Could not duplicate: {exc}")
            return
        self.refresh_plans(new_id)

    def _rename_plan(self) -> None:
        if not self.model.plan:
            return
        name, ok = QInputDialog.getText(self, "Rename plan", "Name:",
                                        text=self.model.plan.get("name") or "")
        if ok and name.strip():
            self.model.update_plan({"name": name.strip()}, reason="rename")
            self.refresh_plans(self.model.plan_id)

    def _delete_plan(self) -> None:
        if not self.model.plan:
            return
        name = self.model.plan.get("name") or "this plan"
        answer = QMessageBox.question(
            self, "Delete plan",
            f"Delete '{name}'? Its events, sections, rules, inputs and change "
            "log are removed from the GeoPackage; its map layers are removed "
            "from the project.",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer != MESSAGE_BOX_YES:
            return
        plan = dict(self.model.plan)
        try:
            map_layers.remove_plan_layers(QgsProject.instance(),
                                          self.store.gpkg_path, plan)
            self.store.delete_plan(plan.get("plan_id") or "")
        except Exception as exc:
            QMessageBox.warning(self, "Burial Planner", f"Could not delete: {exc}")
            return
        self.model.close_plan()
        self.refresh_plans()

    def _pick_gpkg(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Burial Planner GeoPackage", self.store.gpkg_path,
            "GeoPackage (*.gpkg)")
        if not path:
            return
        store, path, error = self._open_store_with_recovery(path)
        if error:
            QMessageBox.warning(self, "Burial Planner",
                                f"Could not open or create the GeoPackage:\n{error}")
            return
        self.store = store
        self.store_ready = True
        set_project_gpkg_path(path)
        self.model.store = store
        self.model.close_plan()
        self.refresh_plans()

    # -- analysis orchestration -----------------------------------------------
    def request_analysis(self) -> None:
        self._start_analysis(generate=False)

    def request_generation(self) -> None:
        self._start_analysis(generate=True)

    def cancel_analysis(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _start_analysis(self, generate: bool) -> None:
        if self._task is not None:
            self.builder_tab.analysis_message("An analysis is already running.")
            return
        if not self.model.plan:
            return
        if self.model.route is None:
            QMessageBox.warning(self, "Burial Planner",
                                self.model.route_error
                                or "Set the plan's route on the Inputs tab first.")
            return
        params = self.model.gen_params()
        if params.scope.length_km <= 1e-6:
            QMessageBox.warning(self, "Burial Planner",
                                "Set the plan scope on the Inputs tab first.")
            return
        stations = params.scope.length_km * 1000.0 / max(params.coarse_step_m, 1.0)
        if stations > 500000:
            answer = QMessageBox.question(
                self, "Large analysis",
                f"About {stations:,.0f} stations will be sampled. Consider a "
                "larger sample step or a smaller scope. Continue anyway?",
                MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
            if answer != MESSAGE_BOX_YES:
                return
        work, warnings = analysis_task.build_work(
            self.model.route, self.model.distance, self.model.plan,
            self.model.rules, self.model.inputs, self.model.depth_config(),
            params, self.model.acq_cache, self.model.current_rpl_fingerprint())
        if warnings:
            self.builder_tab.analysis_message("  ·  ".join(warnings))
        self._generate_after_analysis = generate
        self._task = analysis_task.BurialAnalysisTask(work, self._analysis_finished)
        self._task.progressMessage.connect(self.rules_tab.set_progress)
        self._task.progressMessage.connect(self.builder_tab.analysis_message)
        self._task.progressChanged.connect(self.builder_tab.analysis_progress)
        self.builder_tab.analysis_started()
        QgsApplication.taskManager().addTask(self._task)

    def _analysis_finished(self, task: analysis_task.BurialAnalysisTask) -> None:
        self._task = None
        if task.cancelled:
            self.builder_tab.analysis_finished(
                "Stopped — completed rules stay cached; run again to resume.")
            return
        if task.error:
            self.builder_tab.analysis_finished(f"Analysis failed: {task.error}")
            return

        params = self.model.gen_params()
        acquisitions: List[generation.RuleAcquisition] = []
        rule_hits: Dict[str, List] = {}
        for result in task.results:
            if not result.error and result.cache_key:
                self.model.acq_cache[result.cache_key] = (result.footprint,
                                                          result.nodata)
            acquisitions.append(generation.RuleAcquisition(
                result.rule_row, result.footprint, result.nodata, result.error))
            rule_hits[str(result.rule_row.get("rule_id"))] = [
                (iv.start_km, iv.end_km) for iv in result.footprint]

        resolved, influence, nodata, warnings = generation.resolve_stack(
            params, acquisitions)
        verdicts = resolved.per_method.get(params.method, [])
        message = "  ·  ".join(warnings) if warnings else \
            f"Exclusion stack current over KP " \
            f"{schema.format_kp(params.scope.start_km)}-" \
            f"{schema.format_kp(params.scope.end_km)}."
        self.rules_tab.set_results(rule_hits, verdicts, message)

        if not self._generate_after_analysis:
            # Fire-bar refresh only: update profile overlays from resolution.
            out = generation.GenerationOutput()
            out.excluded = [v for v in verdicts if v.status == "excluded"]
            out.screening = [v for v in verdicts if v.status == "risk"]
            out.influence = influence
            out.insufficient = nodata
            self.model.context = generation.context_from_dict(
                generation.context_to_dict(out))
            self._refresh_profile_overlays()
            self.builder_tab.analysis_finished("Exclusions recomputed.")
            return

        # Full generation on the acquired (and refined) intervals.
        generation_id = schema.new_id()
        proposal = [e for e in self.model.events
                    if e.get("source") == schema.EVENT_SOURCE_CLIENT]
        service = self.model.depth_service()
        route = self.model.route

        def position_fn(kp: float):
            point = route.point_at_kp(kp, clamp=True) if route else None
            return (point.y(), point.x()) if point is not None else (None, None)

        def depth_fn(kp: float):
            point = route.point_at_kp(kp, clamp=True) if route else None
            if point is None or not service.is_available():
                return None
            return service.sample(point.y(), point.x())

        output = generation.generate(
            params, acquisitions,
            existing_events=self.model.events,
            position_fn=position_fn, depth_fn=depth_fn,
            previous_sections=self.model.sections,
            proposal_events=proposal or None,
            plan_id=self.model.plan_id, generation_id=generation_id)

        fingerprints = {str(r.rule_row.get("rule_id")): r.cache_key
                        for r in task.results}
        if self.model.apply_generation(output, params,
                                       [dict(r.rule_row) for r in task.results],
                                       fingerprints, generation_id):
            self.builder_tab.show_diff(output.summary, output.proposal_diff)
            status = "Generation complete."
            if output.warnings:
                status += "  " + "  ·  ".join(output.warnings[:3])
            self.builder_tab.analysis_finished(status)
            self._refresh_profile()
        else:
            self.builder_tab.analysis_finished("Generation could not be saved.")

    # -- profile pane ---------------------------------------------------------
    def _refresh_profile(self) -> None:
        plan = self.model.plan
        self._cancel_profile_refresh(silent=True)
        self._profile_generation += 1
        generation_id = self._profile_generation
        if not plan or self.model.route is None:
            self.profile.clear()
            self.profile_status.setText(
                self.model.route_error or "Select a plan route to display its profile.")
            return
        params = self.model.gen_params()
        scope = params.scope
        self.profile.set_scope(scope.start_km, scope.end_km)
        self.profile.set_profile([])
        self._refresh_profile_overlays()
        self._refresh_profile_events()
        config = self.model.depth_config()
        if not config.is_configured():
            self.profile_status.setText(
                "No manual bathymetry configured. Choose a raster or contour source on Inputs.")
            return
        if scope.length_km <= 0:
            self.profile_status.setText("Set a non-zero scope to display the bathymetry profile.")
            return

        # DepthSnapshot only clones providers/feature sources here.  Contour
        # iteration, indexing and all route sampling happen inside QgsTask.
        snapshot = analysis_task.DepthSnapshot(config, QgsProject.instance())
        step_m = max(5.0, scope.length_km * 1000.0 / 3000.0)
        task = analysis_task.ProfileSamplingTask(
            self.model.route, snapshot, scope.start_km, scope.end_km, step_m,
            lambda finished, token=generation_id: self._profile_finished(finished, token))
        self._profile_task = task
        task.progressMessage.connect(
            lambda message, active=task: self._profile_message(active, message))
        task.progressChanged.connect(
            lambda pct, active=task: self._profile_progress_changed(active, pct))
        self.profile_progress.setValue(0)
        self.profile_progress.setVisible(True)
        self.profile_cancel.setVisible(True)
        self.profile_status.setText("Starting bathymetry profile refresh…")
        QgsApplication.taskManager().addTask(task)

    def _profile_message(self, task: analysis_task.ProfileSamplingTask,
                         message: str) -> None:
        if self._profile_task is task:
            self.profile_status.setText(message)

    def _profile_progress_changed(self, task: analysis_task.ProfileSamplingTask,
                                  pct: float) -> None:
        if self._profile_task is task:
            self.profile_progress.setValue(int(pct))

    def _cancel_profile_refresh(self, _checked=False, silent: bool = False) -> None:
        task = self._profile_task
        if task is not None:
            task.cancel()
            self._profile_task = None
            if not silent:
                self.profile_status.setText("Profile refresh stopping…")
        self.profile_progress.setVisible(False)
        self.profile_cancel.setVisible(False)

    def _profile_finished(self, task: analysis_task.ProfileSamplingTask,
                          generation_id: int) -> None:
        if generation_id != self._profile_generation:
            return
        if self._profile_task is task:
            self._profile_task = None
        self.profile_progress.setVisible(False)
        self.profile_cancel.setVisible(False)
        if task.cancelled:
            self.profile_status.setText("Bathymetry profile refresh cancelled.")
            return
        if task.error:
            self.profile_status.setText(f"Bathymetry profile unavailable: {task.error}")
            return
        self.profile.set_profile(task.series)
        if task.series:
            self.profile_status.setText(
                f"Bathymetry profile ready — {len(task.series):,} samples.")
        else:
            self.profile_status.setText(
                "Bathymetry sources have no coverage within the selected scope.")

    def _refresh_profile_overlays(self) -> None:
        self.profile.set_overlays(self.model.context)

    def _refresh_profile_events(self) -> None:
        self.profile.set_events(
            self.model.events, self.model.method,
            editable=self.profile_drag_toggle.isChecked())

    def _profile_drag_toggled(self, enabled: bool) -> None:
        QSettings().setValue(
            "SubseaCableTools/BurialPlanner/profile_drag_enabled", bool(enabled))
        self.profile_drag_toggle.setStyleSheet(
            "color: #b36b00; font-weight: 600;" if enabled else "")
        self._refresh_profile_events()

    def _on_profile_event_moved(self, event_id: str, new_kp: float) -> None:
        try:
            self.model.move_event(event_id, round(new_kp, 3), "profile drag")
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))
            self.profile.revert_event_line(event_id)

    # -- map sync -------------------------------------------------------------
    def _canvas_point(self, kp: float):
        if self.model.route is None or self.canvas is None:
            return None
        point = self.model.route.point_at_kp(kp, clamp=True)
        if point is None:
            return None
        try:
            from qgis.core import QgsCoordinateReferenceSystem

            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
            return transform.transform(point)
        except Exception:
            return point

    def _ensure_marker(self):
        if self._marker is None or _sip_isdeleted(self._marker):
            self._marker = QgsVertexMarker(self.canvas)
            self._marker.setColor(Qt.GlobalColor.blue)
            self._marker.setIconSize(12)
            self._marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self._marker.setPenWidth(2)
        return self._marker

    def _on_profile_hover(self, kp: float) -> None:
        point = self._canvas_point(kp)
        if point is None:
            return
        marker = self._ensure_marker()
        marker.setCenter(point)
        marker.show()

    def highlight_kp(self, kp: float) -> None:
        point = self._canvas_point(kp)
        if point is None:
            return
        marker = self._ensure_marker()
        marker.setCenter(point)
        marker.show()

    def highlight_range(self, start_kp: float, end_kp: float):
        if self.canvas is None or self.model.route is None:
            return
        geom = self.model.route.extract_segment(start_kp, end_kp)
        if geom is None or geom.isEmpty():
            return
        try:
            from qgis.core import QgsCoordinateReferenceSystem

            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
            geom = type(geom)(geom)
            geom.transform(transform)
        except Exception:
            pass
        if self._band is None or _sip_isdeleted(self._band):
            self._band = QgsRubberBand(self.canvas, GEOMETRY_LINE)
            self._band.setColor(Qt.GlobalColor.yellow)
            self._band.setWidth(3)
        self._band.setToGeometry(geom, None)
        self._band.show()
        return geom

    def goto_kp(self, kp: float) -> None:
        point = self._canvas_point(kp)
        if point is None or self.canvas is None:
            return
        self.highlight_kp(kp)
        self.profile.focus_kp(kp)
        self.canvas.setCenter(point)
        self.canvas.refresh()

    def goto_range(self, start_kp: float, end_kp: float) -> None:
        """Zoom both map and profile to a selected plan section."""
        geom = self.highlight_range(start_kp, end_kp)
        self.profile.focus_range(start_kp, end_kp)
        if geom is None or self.canvas is None:
            return
        extent = geom.boundingBox()
        padding = max(extent.width(), extent.height()) * 0.12
        if padding <= 0:
            padding = max(float(self.canvas.mapUnitsPerPixel()) * 40.0, 1e-9)
        extent.grow(padding)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def show_plan_scope(self) -> None:
        if not self.model.plan:
            return
        scope = self.model.gen_params().scope
        self.profile.reset_scope_view()
        geom = self.highlight_range(scope.start_km, scope.end_km)
        if geom is not None and self.canvas is not None:
            extent = geom.boundingBox()
            extent.grow(max(extent.width(), extent.height()) * 0.05)
            self.canvas.setExtent(extent)
            self.canvas.refresh()

    # -- window management ----------------------------------------------------
    def _top_level_changed(self, floating: bool) -> None:
        if floating:
            self.setWindowFlags(WINDOW_TYPE_WINDOW | WINDOW_HINT_CUSTOMIZE
                                | WINDOW_HINT_TITLE | WINDOW_HINT_MIN_MAX
                                | WINDOW_HINT_CLOSE)
            self.show()

    def refresh(self) -> None:
        """Re-read the current project's store on every open."""
        path = project_gpkg_path() or default_project_gpkg_path()
        if path != self.store.gpkg_path:
            store, path, error = self._open_store_with_recovery(path)
            if not error:
                self.store = store
                self.store_ready = True
                self.model.store = store
                self.model.workbench_store = self.workbench_store()
                self.model.close_plan()
        else:
            # The Workbench may have been created or changed after this dock
            # opened. Always refresh the read-only route/revision handle.
            self.model.workbench_store = self.workbench_store()
        self.refresh_plans(self.model.plan_id)

    def shutdown(self) -> None:
        """Transient artefacts only — never deletes data or registry rows."""
        self.cancel_analysis()
        self._cancel_profile_refresh(silent=True)
        for item in (self._marker, self._band):
            if item is not None and not _sip_isdeleted(item):
                item.hide()
                item.deleteLater()
        self._marker = None
        self._band = None

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
