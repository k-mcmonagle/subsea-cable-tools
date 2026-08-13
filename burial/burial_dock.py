# -*- coding: utf-8 -*-
"""Burial Planner (beta) dock — plan strip + workflow tabs + profile pane.

Single-instance, raise-if-open (Planner behaviour). Top strip: plan selector
+ New / Duplicate / Rename / Delete + status badge + GeoPackage path control.
Below: the guided tabs (Plan → Inputs → Bathymetry Profile → Exclusions →
Plan Builder → Review).
Bottom: the persistent longitudinal profile pane on a collapsible splitter.

All analysis runs on ``QgsApplication.taskManager()`` with non-modal in-dock
progress and a working Stop (resumable) — QGIS stays usable throughout. No
``QApplication.processEvents()`` anywhere in this package.
"""

from __future__ import annotations

import os
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
    QMenu,
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
from ..workbench import project_layers as wb_project_layers
from ..workbench import store as wb_store_module
from ..workbench.store import WorkbenchStore
from . import analysis_task, generation, map_layers, profile_data, schema
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
from .tabs.profile_tab import ProfileTab
from .tabs.review_tab import ReviewTab
from .tabs.rules_tab import RulesTab

_VERTICAL = getattr(Qt, "Orientation", Qt).Vertical

_STATUS_STYLES = {
    schema.PLAN_STATUS_DRAFT: "background:#e8f5e9;color:#1b5e20;padding:2px 8px;",
    schema.PLAN_STATUS_STALE: "background:#fff3cd;color:#7a4f00;padding:2px 8px;",
    schema.PLAN_STATUS_ISSUED: "background:#e3f2fd;color:#0d47a1;padding:2px 8px;",
}


def _remove_canvas_item(item) -> None:
    """Detach a QGIS canvas item without assuming it is a QObject.

    ``QgsVertexMarker`` and ``QgsRubberBand`` are QGraphicsItems in supported
    QGIS 3 builds, where ``deleteLater()`` is not available. Removing the item
    from its owning scene and dropping our Python reference is the portable
    disposal path. Every operation is guarded because shutdown may also run
    while QGIS itself is tearing down the canvas.
    """
    if item is None or _sip_isdeleted(item):
        return
    try:
        item.hide()
    except (AttributeError, RuntimeError):
        pass
    try:
        scene = item.scene()
    except (AttributeError, RuntimeError):
        scene = None
    if scene is not None:
        try:
            scene.removeItem(item)
        except (AttributeError, RuntimeError):
            pass


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
        self._generate_fresh = False
        self._fresh_keep_client = True
        self._marker = None
        self._band = None
        self._exclusion_bands: List = []
        self._pick_tool = None
        self._store_recovery_note = ""

        saved_path = project_gpkg_path()
        path = saved_path or default_project_gpkg_path()
        self.store, path, error = self._open_store_with_recovery(
            path, create_if_missing=not bool(saved_path))
        self.store_ready = not error
        if not self.store_ready:
            QMessageBox.warning(None, "Burial Planner",
                                "Could not open or migrate the Burial Planner "
                                f"GeoPackage.\n{error}\n\nUse Plan file → Open "
                                "existing plans to locate the original file.")
        elif not saved_path:
            set_project_gpkg_path(path)
        elif self._store_recovery_note:
            QMessageBox.warning(None, "Burial Planner",
                                self._store_recovery_note)

        self.model = PlanModel(self.store, self.workbench_store())
        self.model.storeError.connect(self._on_store_error)

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.addLayout(self._build_plan_strip())

        self.splitter = QSplitter(_VERTICAL)
        self.tabs = QTabWidget()
        self.plan_tab = PlanTab(self.model)
        self.inputs_tab = InputsTab(self.model, self.workbench_store, dock=self)
        self.profile_tab = ProfileTab(self.model, self)
        self.rules_tab = RulesTab(self.model, self)
        self.builder_tab = BuilderTab(self.model, self)
        self.review_tab = ReviewTab(self.model, self)
        self.tabs.addTab(self.plan_tab, "Plan")
        self.tabs.addTab(self.inputs_tab, "Inputs")
        self.tabs.addTab(self.profile_tab, "Bathymetry Profile")
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
        self.slope_toggle = QCheckBox("Slope panel")
        self.slope_toggle.setChecked(bool(QSettings().value(
            "SubseaCableTools/BurialPlanner/slope_panel_visible", False,
            type=bool)))
        self.slope_toggle.setToolTip(
            "Show longitudinal (+ve = up-slope), cross (+ve = deeper to "
            "starboard of travel) and absolute slope under the depth "
            "profile. Cross/absolute need cross-offset samples — resample "
            "the profile after configuring bathymetry.")
        self.slope_toggle.toggled.connect(self._slope_panel_toggled)
        profile_status_row.addWidget(self.slope_toggle)
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
        self.profile.kpDoubleClicked.connect(self._on_profile_double_clicked)
        self.profile.eventMoveRequested.connect(self._on_profile_event_moved)
        self.profile.set_slope_visible(self.slope_toggle.isChecked())
        profile_layout.addWidget(self.profile, 1)
        self.splitter.addWidget(profile_pane)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        # The tabs' size hints could otherwise squash the profile pane on
        # refresh; the user's chosen split is restored across sessions.
        profile_pane.setMinimumHeight(150)
        splitter_state = QSettings().value(
            "SubseaCableTools/BurialPlanner/dock_splitter_state")
        if splitter_state is not None:
            try:
                self.splitter.restoreState(splitter_state)
            except Exception:
                pass
        self.splitter.splitterMoved.connect(self._save_dock_splitter_state)
        outer.addWidget(self.splitter, 1)
        self.setWidget(container)

        self.model.planChanged.connect(self._refresh_strip)
        self.model.planChanged.connect(self._refresh_profile)
        self.model.planChanged.connect(self.clear_exclusion_preview)
        self.model.eventsChanged.connect(self._refresh_profile_events)
        self.model.sectionsChanged.connect(self._refresh_profile_sections)
        self.model.inputsChanged.connect(self._refresh_profile)
        self.topLevelChanged.connect(self._top_level_changed)

        self.refresh_plans()

    # -- store lifecycle ------------------------------------------------------
    def _open_store_with_recovery(self, path: str,
                                  create_if_missing: bool = True):
        """Open the project store without silently replacing lost plans."""
        self._store_recovery_note = ""
        try:
            store = BurialStore(path)
            if not create_if_missing and not store.exists():
                raise ValueError(
                    "The saved plan file is missing or is not a recognised "
                    "Burial Planner GeoPackage.")
            store.migrate()
            return store, path, None
        except Exception as first_error:
            fallback = default_project_gpkg_path()
            same_path = (os.path.normcase(os.path.abspath(fallback)) ==
                         os.path.normcase(os.path.abspath(path)))
            if not same_path:
                try:
                    store = BurialStore(fallback)
                    if not store.exists():
                        raise ValueError("No existing fallback plan file.")
                    store.migrate()
                    set_project_gpkg_path(fallback)
                    self._store_recovery_note = (
                        "The plan file saved in this QGIS project could not "
                        f"be opened:\n{path}\n\nOpened the existing project-"
                        f"side plan file instead:\n{fallback}")
                    return store, fallback, None
                except Exception:
                    pass
            return BurialStore(path), path, str(first_error)

    @staticmethod
    def _open_existing_store(path: str):
        """Validate before migration so arbitrary GeoPackages stay untouched."""
        store = BurialStore(path)
        if not store.exists():
            raise ValueError(
                "This file does not contain a Burial Planner plan registry. "
                "Choose the GeoPackage originally selected in the Burial "
                "Planner, not an exported sections/events layer file.")
        store.migrate()
        return store

    def _switch_store(self, store: BurialStore, path: str) -> bool:
        """Switch the plan list to an already validated store."""
        if self._task is not None:
            QMessageBox.warning(
                self, "Burial Planner",
                "An exclusion analysis is still running. Stop it and wait "
                "for it to finish before changing the plan file.")
            return False
        self._cancel_profile_refresh(silent=True)
        self.store = store
        self.store_ready = True
        set_project_gpkg_path(path)
        self.model.store = store
        self.model.close_plan()
        self.refresh_plans()
        return True

    def workbench_store(self, plan_hint: Optional[Dict] = None
                        ) -> Optional[WorkbenchStore]:
        """Resolve the Workbench registry after project/profile relocation.

        Prefer a registry containing the plan's exact RPL UUID, then a unique
        name+revision match, then the normal project/default Workbench path.
        """
        try:
            plan = plan_hint or getattr(getattr(self, "model", None), "plan", {})
            configured = wb_store_module.project_gpkg_path()
            discovered = wb_project_layers.discover_gpkg_path()
            default = wb_store_module.default_project_gpkg_path()
            snapshot = str(plan.get("rpl_gpkg_path") or "").split("|")[0]
            project_file = QgsProject.instance().fileName()
            folders = [
                os.path.dirname(project_file) if project_file else "",
                os.path.dirname(getattr(getattr(self, "store", None),
                                        "gpkg_path", "")),
            ]
            candidates = [discovered, configured, default, snapshot]
            for original in (configured, snapshot):
                if not original:
                    continue
                basename = os.path.basename(original)
                candidates.extend(os.path.join(folder, basename)
                                  for folder in folders if folder)

            wanted_id = str(plan.get("rpl_id") or "")
            wanted_name = str(plan.get("rpl_name") or "").strip().casefold()
            wanted_revision = str(plan.get("rpl_revision") or "").strip().casefold()
            valid = []
            seen = set()
            discovered_norm = (os.path.normcase(os.path.abspath(discovered))
                               if discovered else "")
            for path in candidates:
                if not path:
                    continue
                normal = os.path.normcase(os.path.abspath(path))
                if normal in seen:
                    continue
                seen.add(normal)
                store = WorkbenchStore(path)
                if not store.exists():
                    continue
                try:
                    rpls = store.list_rpls()
                except Exception:
                    continue
                score = 5 if normal == discovered_norm else 0
                if wanted_id and any(str(row.get("rpl_id") or "") == wanted_id
                                     for row in rpls):
                    score += 100
                elif wanted_name:
                    matches = [
                        row for row in rpls
                        if str(row.get("name") or "").strip().casefold() == wanted_name
                        and (not wanted_revision or
                             str(row.get("rev_label") or "").strip().casefold()
                             == wanted_revision)
                    ]
                    if len(matches) == 1:
                        score += 50
                valid.append((score, store))
            if not valid:
                return None
            _score, chosen = max(valid, key=lambda item: item[0])
            if configured != chosen.gpkg_path:
                wb_store_module.set_project_gpkg_path(chosen.gpkg_path)
            return chosen
        except Exception:
            return None

    def _on_store_error(self, message: str) -> None:
        QMessageBox.warning(self, "Burial Planner", message)

    # -- plan strip -----------------------------------------------------------
    def _build_plan_strip(self) -> QHBoxLayout:
        strip = QHBoxLayout()
        strip.addWidget(QLabel("Plan:"))
        self.plan_combo = QComboBox()
        self.plan_combo.setPlaceholderText(
            "No plans in this file — use Plan file… to open an existing file")
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
        self.gpkg_name = QLabel("")
        strip.addWidget(self.gpkg_name)
        self.gpkg_button = QPushButton("Plan file…")
        self.gpkg_button.setToolTip(self.store.gpkg_path)
        file_menu = QMenu(self.gpkg_button)
        file_menu.addAction("Open existing plans…", self._pick_gpkg)
        file_menu.addAction("Create new plan file…", self._new_gpkg)
        self.gpkg_button.setMenu(file_menu)
        strip.addWidget(self.gpkg_button)
        return strip

    def refresh_plans(self, select_id: str = "") -> None:
        try:
            plans = self.store.list_plans() if self.store_ready else []
        except Exception as exc:
            plans = []
            self.store_ready = False
            QMessageBox.warning(
                self, "Burial Planner",
                "The selected plan file opened, but its plan registry could "
                f"not be read:\n{exc}")
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
            plan_hint = self.store.get_plan(plan_id) or {}
            self.model.workbench_store = self.workbench_store(plan_hint)
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
        plan_count = self.plan_combo.count()
        state = ("unavailable" if not self.store_ready else
                 f"{plan_count} plan{'s' if plan_count != 1 else ''}")
        self.gpkg_name.setText(
            f"{os.path.basename(self.store.gpkg_path)} ({state})")
        self.gpkg_name.setToolTip(self.store.gpkg_path)
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
        """Open the plan registry contained in an existing GeoPackage."""
        start = (self.store.gpkg_path if os.path.exists(self.store.gpkg_path)
                 else os.path.dirname(self.store.gpkg_path))
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open existing Burial Planner plans", start,
            "GeoPackage (*.gpkg)")
        if not path:
            return
        try:
            store = self._open_existing_store(path)
        except Exception as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"Could not open this plan file:\n{exc}")
            return
        self._switch_store(store, path)

    def _new_gpkg(self) -> None:
        """Create a fresh registry without overwriting an existing file."""
        start = os.path.dirname(self.store.gpkg_path) or self.store.gpkg_path
        path, _filter = QFileDialog.getSaveFileName(
            self, "Create new Burial Planner plan file", start,
            "GeoPackage (*.gpkg)")
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        if os.path.exists(path):
            QMessageBox.warning(
                self, "Burial Planner",
                "That file already exists. Use Plan file → Open existing "
                "plans to open it, or choose a new filename.")
            return
        try:
            store = BurialStore(path)
            store.migrate()
        except Exception as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"Could not create the plan file:\n{exc}")
            return
        self._switch_store(store, path)

    # -- analysis orchestration -----------------------------------------------
    def request_analysis(self) -> None:
        self._start_analysis(generate=False)

    def request_generation(self, fresh: bool = False,
                           keep_client: bool = True) -> None:
        """Generate the plan; ``fresh`` discards user-made events and section
        curation and rebuilds purely from the Exclusion stack (recorded as one
        change-log entry, so it can be rolled back)."""
        self._start_analysis(generate=True, fresh=fresh,
                             keep_client=keep_client)

    def cancel_analysis(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _start_analysis(self, generate: bool, fresh: bool = False,
                        keep_client: bool = True) -> None:
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
        needs_profile = any(
            int(rule.get("enabled") or 0)
            and (rule.get("kind") or "") == "threshold_profile"
            for rule in self.model.rules)
        if needs_profile and self.model.profile_state() != "current":
            QMessageBox.warning(
                self, "Burial Planner",
                "Depth/slope exclusions require a current stored bathymetry "
                "profile. Open Bathymetry Profile, review the sampling "
                "settings, then click Apply & rebuild profile.")
            self.tabs.setCurrentWidget(self.profile_tab)
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
        # Reuse the persisted plan profile for threshold rules when it is
        # current and at least as dense as the analysis step.
        depth_samples = None
        cross_profile = None
        depth_step_m = self.model.resolve_profile_step_m(params)
        stored = self.model.bathy_profile
        if (stored is not None and stored.kps
                and self.model.profile_state() == "current"):
            if stored.step_m <= params.coarse_step_m + 1e-9:
                depth_samples = stored.samples()
                depth_step_m = stored.step_m
            # Snapshot for cross/absolute slope criteria (plain lists so the
            # worker never shares live PlanProfile state).
            cross_profile = {
                "kps": list(stored.kps),
                "depths": list(stored.depths),
                "port": list(stored.port_depths),
                "stbd": list(stored.stbd_depths),
                "cross_offset_m": stored.cross_offset_m,
            }
        work, warnings = analysis_task.build_work(
            self.model.route, self.model.distance, self.model.plan,
            self.model.rules, self.model.inputs, self.model.depth_config(),
            params, self.model.acq_cache, self.model.current_rpl_fingerprint(),
            depth_samples=depth_samples, depth_step_m=depth_step_m,
            cross_profile=cross_profile)
        if warnings:
            self.builder_tab.analysis_message("  ·  ".join(warnings))
        self._generate_after_analysis = generate
        self._generate_fresh = generate and fresh
        self._fresh_keep_client = keep_client
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
        try:
            self.builder_tab.analysis_message("Applying generation results…")
            self._apply_analysis_results(task)
        except Exception as exc:
            # Without this the failure would be swallowed by the QgsTask
            # completion guard and Generate would appear to do nothing.
            self.builder_tab.analysis_finished(f"Generation failed: {exc}")
            QMessageBox.warning(
                self, "Burial Planner",
                "The analysis finished but its results could not be applied:\n"
                f"{exc}\n\nDetails are in the QGIS message log (Burial Planner).")
            raise

    def _apply_analysis_results(self,
                                task: analysis_task.BurialAnalysisTask) -> None:
        params = self.model.gen_params()
        acquisitions: List[generation.RuleAcquisition] = []
        for result in task.results:
            if not result.error and result.cache_key:
                self.model.acq_cache[result.cache_key] = (result.footprint,
                                                          result.nodata)
            acquisitions.append(generation.RuleAcquisition(
                result.rule_row, result.footprint, result.nodata, result.error))

        resolved, influence, nodata, warnings = generation.resolve_stack(
            params, acquisitions, depth_at=self.model.depth_at_kp)
        verdicts = resolved.per_method.get(params.method, [])
        # Per-rule bars show the resolved footprint — extension buffers
        # included — so what fires on screen is what excludes in the plan.
        rule_hits: Dict[str, List] = {
            rule_id: [(iv.start_km, iv.end_km) for iv in intervals]
            for rule_id, intervals in resolved.rule_hits.items()}
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

        # Full generation on the acquired (and refined) intervals. A fresh
        # run rebuilds purely from the Exclusion stack: user-made events and
        # section curation are dropped from the inputs (the prior state is
        # snapshotted in the change log by apply_generation, so it remains
        # rollback-able from Review & Export).
        generation_id = schema.new_id()
        existing_events = self.model.events
        previous_sections = self.model.sections
        if self._generate_fresh:
            existing_events = generation.fresh_existing_events(
                existing_events, keep_client=self._fresh_keep_client)
            previous_sections = None
        proposal = [e for e in existing_events
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
            existing_events=existing_events,
            position_fn=position_fn, depth_fn=depth_fn,
            previous_sections=previous_sections,
            proposal_events=proposal or None,
            plan_id=self.model.plan_id, generation_id=generation_id,
            depth_at=self.model.depth_at_kp)

        fingerprints = {str(r.rule_row.get("rule_id")): r.cache_key
                        for r in task.results}
        if self.model.apply_generation(output, params,
                                       [dict(r.rule_row) for r in task.results],
                                       fingerprints, generation_id):
            self.builder_tab.show_diff(output.summary, output.proposal_diff)
            status = ("Fresh regeneration complete — previous state is in "
                      "the change log (Review & Export)."
                      if self._generate_fresh else "Generation complete.")
            if output.warnings:
                status += "  " + "  ·  ".join(output.warnings[:3])
            self.builder_tab.analysis_finished(status)
            self._refresh_profile()
        else:
            self.builder_tab.analysis_finished("Generation could not be saved.")

    # -- profile pane ---------------------------------------------------------
    def _refresh_profile(self) -> None:
        """Show the persisted plan profile; sample only when none exists.

        Sample-once contract: the stored samples are reused (and marked
        stale when route/bathymetry/scope/cross offset change) — nothing
        resamples without the user clicking Resample profile, except the
        automatic first build for a plan that has never been sampled.
        """
        plan = self.model.plan
        self._cancel_profile_refresh(silent=True)
        self._profile_generation += 1
        if not plan or self.model.route is None:
            self.profile.clear()
            self.profile_status.setText(
                self.model.route_error or "Select a plan route to display its profile.")
            return
        params = self.model.gen_params()
        scope = params.scope
        self.profile.set_scope(scope.start_km, scope.end_km)
        # Replaced with the stored profile's actual resolution when it is
        # displayed. This fallback covers the empty/loading state.
        self.profile.set_slope_window_m(
            self.model.resolve_profile_step_m(params))
        self.profile.set_profile([])
        self.profile.set_slope_series([], [], [])
        self._refresh_profile_overlays()
        self._refresh_profile_events()
        self._refresh_profile_sections()
        config = self.model.depth_config()
        if not config.is_configured():
            self.profile_status.setText(
                "No manual bathymetry configured. Choose a raster or contour source on Inputs.")
            return
        if scope.length_km <= 0:
            self.profile_status.setText("Set a non-zero scope to display the bathymetry profile.")
            return
        stored = self.model.bathy_profile
        if stored is not None and stored.kps:
            self._display_stored_profile(
                stored, params, stale=self.model.profile_state() != "current")
            return
        self.profile_status.setText(
            "No stored bathymetry profile — configure and rebuild it on the "
            "Bathymetry Profile tab.")
        self.profile_tab.refresh()

    def _display_stored_profile(self, profile: profile_data.PlanProfile,
                                params: generation.GenParams,
                                stale: bool) -> None:
        self.profile.set_profile(profile.series())
        self.profile.set_slope_window_m(profile.step_m)
        self._set_slope_series(profile, params)
        date = (profile.sampled_utc or "")[:16].replace("T", " ")
        text = (f"Plan profile — {profile.sample_count:,} stations at "
                f"{profile.step_m:g} m")
        text += f", local slope over {2.0 * profile.step_m:g} m"
        if profile.cross_offset_m > 0:
            text += f", cross ±{profile.cross_offset_m:g} m"
        if date:
            text += f", sampled {date} UTC"
        if stale:
            text += ("  —  STALE: route, bathymetry, scope or cross offset "
                     "changed since sampling. Click Resample profile.")
            self.profile_status.setStyleSheet("color: #b36b00; font-weight: 600;")
        else:
            self.profile_status.setStyleSheet("")
        self.profile_status.setText(text)
        self.profile_tab.refresh()

    def _set_slope_series(self, profile: profile_data.PlanProfile,
                          params: generation.GenParams) -> None:
        # Local terrain slope follows the persisted bathymetry profile. Rules
        # with an explicit slope_window_m intentionally evaluate over that
        # requested vehicle footprint; Auto rules use this local series scale.
        half_km = max(float(profile.step_m), 1.0) / 1000.0
        long_series, cross_series, abs_series = profile.slope_series(
            half_km, params.direction)
        self.profile.set_slope_series(long_series, cross_series, abs_series)

    def _save_dock_splitter_state(self, *_args) -> None:
        QSettings().setValue(
            "SubseaCableTools/BurialPlanner/dock_splitter_state",
            self.splitter.saveState())

    def _slope_panel_toggled(self, checked: bool) -> None:
        QSettings().setValue(
            "SubseaCableTools/BurialPlanner/slope_panel_visible", bool(checked))
        self.profile.set_slope_visible(bool(checked))

    def _resample_profile(self) -> None:
        self._cancel_profile_refresh(silent=True)
        self._profile_generation += 1
        self._start_profile_sampling()

    def request_profile_resample(self) -> None:
        """Public workflow action used by the Bathymetry Profile tab."""
        self._resample_profile()

    def _start_profile_sampling(self) -> None:
        """One background sampling pass over the scope (+ one-step margin)."""
        if not self.model.plan or self.model.route is None:
            return
        config = self.model.depth_config()
        if not config.is_configured():
            return
        params = self.model.gen_params()
        scope = params.scope
        generation_id = self._profile_generation
        # Step follows the data: manual override, else the smallest
        # configured raster cell size (finer stations only re-read the same
        # cells). Clamped to the analysis step and a ~500k-station ceiling.
        step_m = self.model.resolve_profile_step_m(params)
        margin_km = max(params.coarse_step_m, 1.0) / 1000.0
        start_kp = max(0.0, scope.start_km - margin_km)
        end_kp = min(self.model.route.total_length_km,
                     scope.end_km + margin_km)

        # DepthSnapshot only clones providers/feature sources here.  Contour
        # iteration, indexing and all route sampling happen inside QgsTask.
        snapshot = analysis_task.DepthSnapshot(config, QgsProject.instance())
        task = analysis_task.ProfileSamplingTask(
            self.model.route, snapshot, start_kp, end_kp, step_m,
            lambda finished, token=generation_id: self._profile_finished(finished, token),
            distance=self.model.distance,
            cross_offset_m=self.model.resolve_cross_offset_m(params))
        self._profile_task = task
        task.progressMessage.connect(
            lambda message, active=task: self._profile_message(active, message))
        task.progressChanged.connect(
            lambda pct, active=task: self._profile_progress_changed(active, pct))
        self.profile_progress.setValue(0)
        self.profile_progress.setVisible(True)
        self.profile_cancel.setVisible(True)
        self.profile_status.setStyleSheet("")
        self.profile_status.setText("Sampling plan profile…")
        self.profile_tab.set_runtime_status("Sampling plan profile…")
        QgsApplication.taskManager().addTask(task)

    def _profile_message(self, task: analysis_task.ProfileSamplingTask,
                         message: str) -> None:
        if self._profile_task is task:
            self.profile_status.setText(message)
            self.profile_tab.set_runtime_status(message)

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
                self.profile_tab.set_runtime_status("Profile refresh stopping…")
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
            self.profile_status.setText(
                "Profile sampling cancelled — stored samples unchanged.")
            self.profile_tab.refresh()
            return
        if task.error:
            self.profile_status.setText(f"Profile sampling failed: {task.error}")
            self.profile_tab.set_runtime_status(
                f"Profile sampling failed: {task.error}")
            return
        if not task.series:
            # Nothing sampled — do not overwrite any stored profile with an
            # empty one; the user fixes the bathymetry config and resamples.
            self.profile_status.setText(
                "Bathymetry sources have no coverage within the selected "
                "scope. Check the bathymetry configuration on Inputs, then "
                "rebuild on Bathymetry Profile.")
            self.profile_tab.refresh()
            return
        params = self.model.gen_params()
        profile = profile_data.PlanProfile(
            step_m=task.step_m,
            cross_offset_m=task.cross_offset_m,
            scope_start_kp=params.scope.start_km,
            scope_end_kp=params.scope.end_km,
            route_fingerprint=self.model.current_rpl_fingerprint(),
            depth_fingerprint=self.model.depth_fingerprint(),
            sampled_utc=schema.utc_now_iso(),
            kps=task.kps, depths=task.depths,
            port_depths=task.port_depths, stbd_depths=task.stbd_depths)
        self.model.save_profile(profile)
        self._display_stored_profile(profile, params, stale=False)

    def _refresh_profile_overlays(self) -> None:
        self.profile.set_overlays(self.model.context)

    def _refresh_profile_events(self) -> None:
        self.profile.set_events(
            self.model.events, self.model.method,
            editable=self.profile_drag_toggle.isChecked())

    def _refresh_profile_sections(self) -> None:
        self.profile.set_sections(self.model.sections)

    def _profile_drag_toggled(self, enabled: bool) -> None:
        QSettings().setValue(
            "SubseaCableTools/BurialPlanner/profile_drag_enabled", bool(enabled))
        self.profile_drag_toggle.setStyleSheet(
            "color: #b36b00; font-weight: 600;" if enabled else "")
        self._refresh_profile_events()

    def _on_profile_event_moved(self, event_id: str, new_kp: float) -> None:
        # Same justification contract as table edits: optional reason,
        # Cancel aborts (and snaps the dragged line back).
        text, ok = QInputDialog.getText(
            self, "Move event",
            f"Move to KP {schema.format_kp(new_kp)} — reason (optional):")
        if not ok:
            self.profile.revert_event_line(event_id)
            return
        try:
            self.model.move_event(event_id, round(new_kp, 3),
                                  text.strip() or "profile drag")
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))
            self.profile.revert_event_line(event_id)

    def _on_profile_double_clicked(self, kp: float) -> None:
        """Double-click on the profile primes the Plan Builder add-event KP."""
        if not self.model.plan:
            return
        self.builder_tab.set_add_kp(kp)
        self.tabs.setCurrentWidget(self.builder_tab)
        self.goto_kp(kp)

    # -- KP picking -----------------------------------------------------------
    def pick_kp_on_map(self, callback, prompt: str = "") -> bool:
        """One-shot map tool: snap a canvas click to the route, deliver its KP.

        Restores the previously active map tool on pick, right-click or Esc.
        """
        if self.canvas is None:
            return False
        if self.model.route is None:
            QMessageBox.warning(self, "Burial Planner",
                                self.model.route_error
                                or "Set the plan's route on the Inputs tab first.")
            return False
        from .kp_pick_tool import KpPickTool

        previous = self.canvas.mapTool()

        def restore() -> None:
            self._pick_tool = None
            try:
                if self.canvas.mapTool() is tool:
                    if previous is not None:
                        self.canvas.setMapTool(previous)
                    else:
                        self.canvas.unsetMapTool(tool)
            except (AttributeError, RuntimeError):
                pass

        tool = KpPickTool(self.canvas, self.model.route, callback, restore)
        self._pick_tool = tool
        self.canvas.setMapTool(tool)
        if prompt and self.iface is not None:
            try:
                from ..qgis_compat import MESSAGE_INFO

                self.iface.messageBar().pushMessage(
                    "Burial Planner", prompt, MESSAGE_INFO, 4)
            except Exception:
                pass
        return True

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

    def set_exclusion_preview(self, spans) -> None:
        """Draw KP ranges as temporary highlight bands over the route.

        ``spans``: list of ``(start_kp, end_kp, QColor)``. Bands are canvas
        artefacts only (never saved); pass an empty list to clear. Used by
        the Exclusions tab to preview Exclusion Areas before a plan is built.
        """
        self.clear_exclusion_preview()
        if not spans or self.canvas is None or self.model.route is None:
            return
        try:
            from qgis.core import QgsCoordinateReferenceSystem

            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                self.canvas.mapSettings().destinationCrs(),
                QgsProject.instance())
        except Exception:
            transform = None
        for start_kp, end_kp, color in spans:
            geom = self.model.route.extract_segment(float(start_kp),
                                                    float(end_kp))
            if geom is None or geom.isEmpty():
                continue
            if transform is not None:
                try:
                    geom = type(geom)(geom)
                    geom.transform(transform)
                except Exception:
                    pass
            band = QgsRubberBand(self.canvas, GEOMETRY_LINE)
            band.setColor(color)
            band.setWidth(6)
            band.setToGeometry(geom, None)
            band.show()
            self._exclusion_bands.append(band)

    def clear_exclusion_preview(self) -> None:
        bands = self._exclusion_bands
        self._exclusion_bands = []
        for band in bands:
            _remove_canvas_item(band)

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
        saved_path = project_gpkg_path()
        path = saved_path or default_project_gpkg_path()
        if path != self.store.gpkg_path:
            store, path, error = self._open_store_with_recovery(
                path, create_if_missing=not bool(saved_path))
            if not error:
                self.store = store
                self.store_ready = True
                self.model.store = store
                self.model.workbench_store = self.workbench_store()
                self.model.close_plan()
            else:
                self.store_ready = False
                QMessageBox.warning(
                    self, "Burial Planner",
                    "Could not reopen the saved plan file. Use Plan file → "
                    f"Open existing plans to locate it.\n\n{error}")
        else:
            # The Workbench may have been created or changed after this dock
            # opened. Always refresh the read-only route/revision handle.
            self.model.workbench_store = self.workbench_store()
        self.refresh_plans(self.model.plan_id)

    def shutdown(self) -> None:
        """Transient artefacts only — never deletes data or registry rows."""
        try:
            self.cancel_analysis()
        except (AttributeError, RuntimeError):
            pass
        try:
            self._cancel_profile_refresh(silent=True)
        except (AttributeError, RuntimeError):
            pass
        pick_tool = self._pick_tool
        self._pick_tool = None
        if pick_tool is not None and self.canvas is not None:
            try:
                self.canvas.unsetMapTool(pick_tool)
            except (AttributeError, RuntimeError):
                pass
        items = (self._marker, self._band) + tuple(self._exclusion_bands)
        self._marker = None
        self._band = None
        self._exclusion_bands = []
        for item in items:
            _remove_canvas_item(item)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
