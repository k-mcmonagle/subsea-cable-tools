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

import dataclasses
import math
import os
from typing import Dict, List, Optional

from qgis.core import (
    QgsApplication,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)
from qgis.gui import QgsVertexMarker, QgsRubberBand
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import QEvent, QSettings, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolButton,
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
    TOOLBUTTON_POPUP_MODE_INSTANT,
    WINDOW_HINT_CLOSE,
    WINDOW_HINT_CUSTOMIZE,
    WINDOW_HINT_MIN_MAX,
    WINDOW_HINT_TITLE,
    WINDOW_TYPE_WINDOW,
)
from ..workbench import project_layers as wb_project_layers
from ..workbench import store as wb_store_module
from ..workbench.store import WorkbenchStore
from . import (analysis_task, footprint, generation, map_layers, path_data,
               profile_data, schema)
from . import tools as tools_mod
from . import ui_helpers
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
from .tabs.paths_tab import PathsTab
from .tabs.profile_tab import ProfileTab
from .tabs.review_tab import ReviewTab
from .tabs.risk_tab import RiskTab
from .tabs.rules_tab import RulesTab
from .tabs.tools_tab import ToolsTab

_VERTICAL = getattr(Qt, "Orientation", Qt).Vertical

# Qt5 exposes QEvent enum members flat; Qt6 scopes them under QEvent.Type.
_EVENT_SCOPE = getattr(QEvent, "Type", QEvent)
_EVENT_MOUSE_MOVE = getattr(_EVENT_SCOPE, "MouseMove")
_EVENT_LEAVE = getattr(_EVENT_SCOPE, "Leave")


def _mouse_event_pos(event):
    """Widget-local mouse position for Qt5 (pos) and Qt6 (position)."""
    position = getattr(event, "position", None)
    if position is not None:
        return position().toPoint()
    return event.pos()

_FLOATING_GEOMETRY_KEY = "SubseaCableTools/BurialPlanner/floating_geometry"
_FLOATING_MODE_KEY = "SubseaCableTools/BurialPlanner/floating"


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
        self._task_plan_id = ""
        self._profile_task: Optional[analysis_task.ProfileSamplingTask] = None
        self._profile_generation = 0
        self._generate_after_analysis = False
        self._generate_fresh = False
        self._fresh_keep_client = True
        self._marker = None
        self._band = None
        self._footprint_band = None
        self._vessel_band = None
        self._footprint_cache: Dict[str, object] = {}  # tool_id -> QgsGeometry
        # path_id -> (tool path points, barge points) parsed from the
        # persisted WKT; vessel_id -> outline QgsGeometry.
        self._path_points_cache: Dict[str, object] = {}
        self._vessel_outline_cache: Dict[str, object] = {}
        # path_id -> (lon scale, scaled QgsGeometry) for cursor snapping.
        self._path_snap_cache: Dict[str, object] = {}
        self._exclusion_bands: List = []
        self._pick_tool = None
        self._cursor_outline_enabled = False
        self._cursor_outline_pos = None
        self._cursor_outline_timer = None
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
        self.tools_tab = ToolsTab(self.model, self)
        self.profile_tab = ProfileTab(self.model, self)
        self.rules_tab = RulesTab(self.model, self)
        self.risk_tab = RiskTab(self.model, self)
        self.paths_tab = PathsTab(self.model, self)
        self.builder_tab = BuilderTab(self.model, self)
        self.review_tab = ReviewTab(self.model, self)
        self.tabs.addTab(self.plan_tab, "Plan")
        self.tabs.addTab(self.inputs_tab, "Inputs")
        self.tabs.addTab(self.tools_tab, "Burial Tools")
        self.tabs.addTab(self.profile_tab, "Bathymetry Profile")
        self.tabs.addTab(self.rules_tab, "Exclusions")
        self.tabs.addTab(self.risk_tab, "Risk Profile")
        self.tabs.addTab(self.paths_tab, "Installation Paths")
        self.tabs.addTab(self.builder_tab, "Plan Builder")
        self.tabs.addTab(self.review_tab, "Review && Export")
        # Workflow badges: these tabs gain a " ⚠" suffix (and a tooltip)
        # when a prerequisite the workflow depends on is missing or stale.
        self._tab_titles = {
            self.inputs_tab: "Inputs",
            self.profile_tab: "Bathymetry Profile",
        }
        self.splitter.addWidget(self.tabs)

        profile_pane = QWidget()
        profile_layout = QVBoxLayout(profile_pane)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_status_row = QHBoxLayout()
        self.profile_status = QLabel("Bathymetry profile")
        profile_status_row.addWidget(self.profile_status, 1)
        self.goto_kp_edit = QLineEdit()
        self.goto_kp_edit.setPlaceholderText("Go to KP…")
        self.goto_kp_edit.setMaximumWidth(90)
        self.goto_kp_edit.setToolTip(
            "Type a KP (km) and press Enter to centre the map and the "
            "profile crosshair there.")
        self.goto_kp_edit.returnPressed.connect(self._goto_kp_entered)
        profile_status_row.addWidget(self.goto_kp_edit)
        # The three profile view toggles live in one compact menu button so
        # the row survives narrow (screen-share) dock widths.
        self.view_button = QToolButton()
        self.view_button.setText("View ▾")
        self.view_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        self.view_button.setToolTip(
            "Profile view options: slope panel, event dragging, tool "
            "outline, image export.")
        view_menu = QMenu(self.view_button)
        view_menu.setToolTipsVisible(True)
        self.slope_toggle = view_menu.addAction("Slope panel")
        self.slope_toggle.setCheckable(True)
        self.slope_toggle.setChecked(bool(QSettings().value(
            "SubseaCableTools/BurialPlanner/slope_panel_visible", False,
            type=bool)))
        self.slope_toggle.setToolTip(
            "Show longitudinal (+ve = up-slope), cross (+ve = deeper to "
            "starboard of travel) and absolute slope under the depth "
            "profile. Cross/absolute need cross-offset samples — resample "
            "the profile after configuring bathymetry.")
        self.slope_toggle.toggled.connect(self._slope_panel_toggled)
        self.profile_drag_toggle = view_menu.addAction("Allow event dragging")
        self.profile_drag_toggle.setCheckable(True)
        self.profile_drag_toggle.setChecked(bool(QSettings().value(
            "SubseaCableTools/BurialPlanner/profile_drag_enabled", False,
            type=bool)))
        self.profile_drag_toggle.setToolTip(
            "When enabled, unlocked burial start/end lines (PLDN/PLUP, "
            "TRENCH_START/TRENCH_END…) can be dragged on the profile. "
            "A confirmation shows the exact KP (editable) with an optional "
            "reason; moves can be undone with Ctrl+Z in Plan Builder.")
        self.profile_drag_toggle.toggled.connect(self._profile_drag_toggled)
        self.footprint_toggle = view_menu.addAction("Tool outline")
        self.footprint_toggle.setCheckable(True)
        self.footprint_toggle.setChecked(bool(QSettings().value(
            "SubseaCableTools/BurialPlanner/tool_footprint_visible", False,
            type=bool)))
        self.footprint_toggle.setToolTip(
            "Show the effective burial tool's footprint on the map, to "
            "scale, at the hovered/selected profile KP — instant scale "
            "context for seabed features. Uses the section's tool (or the "
            "plan default); the tool needs a DXF footprint registered on "
            "the Burial Tools tab. When Installation Paths are generated "
            "the outline rides the tool path, and the selected vessel's "
            "outline (if imported) rides the barge track at the tow point.")
        self.footprint_toggle.toggled.connect(self._footprint_toggled)
        self.cursor_outline_toggle = view_menu.addAction(
            "Outline follows map cursor")
        self.cursor_outline_toggle.setCheckable(True)
        self.cursor_outline_toggle.setChecked(False)  # deliberate default
        self.cursor_outline_toggle.setToolTip(
            "While enabled, the burial tool outline is drawn on the map "
            "snapped to the generated tool path (or the RPL when no path "
            "exists yet) at the point closest to the mouse cursor, with "
            "the vessel outline at the matching barge-track tow point. "
            "Purely an overlay — the active map tool keeps working. Off "
            "by default each session.")
        self.cursor_outline_toggle.toggled.connect(
            self._cursor_outline_toggled)
        view_menu.addSeparator()
        view_menu.addAction("Export profile image…",
                            self._export_profile_image)
        self.view_button.setMenu(view_menu)
        self._style_view_button(self.profile_drag_toggle.isChecked())
        profile_status_row.addWidget(self.view_button)
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
        self.profile.link_kp_plot(self.paths_tab.dcc_plot)
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
        self.model.planChanged.connect(self._clear_footprint_cache)
        self.model.toolsChanged.connect(self._clear_footprint_cache)
        self.model.pathsChanged.connect(self._clear_footprint_cache)
        self.model.vesselsChanged.connect(self._clear_footprint_cache)
        self.model.eventsChanged.connect(self._refresh_profile_events)
        self.model.sectionsChanged.connect(self._refresh_profile_sections)
        self.model.inputsChanged.connect(self._refresh_profile)
        self.model.inputsChanged.connect(self._refresh_tab_badges)
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
        if getattr(self.paths_tab, "is_running", lambda: False)():
            QMessageBox.information(
                self, "Burial Planner",
                "Installation path generation is still running. Stop it "
                "and wait for it to finish before changing the plan file.")
            return False
        self._cancel_profile_refresh(silent=True)
        try:
            self.store.close()  # checkpoint + release the old SQL handle
        except Exception:
            pass
        self.store = store
        self.store_ready = True
        set_project_gpkg_path(path)
        self.model.store = store
        self.model.refresh_tools()  # the registry is per GeoPackage
        self.model.refresh_layback_profiles()
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
        # One compact menu instead of four buttons so the strip fits a
        # narrow (screen-share) dock width.
        self.plan_actions_button = ui_helpers.menu_tool_button(
            "Plan ▾",
            (("New…", self._new_plan),
             ("Duplicate…", self._duplicate_plan),
             ("Rename…", self._rename_plan),
             None,
             ("Delete…", self._delete_plan)),
            tooltip="Create, duplicate, rename or delete plans in this file.")
        strip.addWidget(self.plan_actions_button)
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
            sections_layer, _events_layer = map_layers.ensure_plan_layers(
                QgsProject.instance(), self.store.gpkg_path, self.model.plan)
            if sections_layer is None \
                    and (self.model.sections or self.model.events) \
                    and self.model.route is not None:
                # The plan has data but its spatial tables were never
                # written (e.g. a fresh duplicate) — build them now instead
                # of showing an empty map until the first edit.
                self.model.refresh_layers(immediate=True)
            if self.model.path_result:
                # Path WKT is authoritative in the registry; rebuildable
                # spatial layers may not yet exist after a file move/copy.
                self.model.refresh_path_layers()
            # The map follows the selector: show this plan's layers, hide
            # the other plans' (still in the project, just unchecked).
            map_layers.set_active_plan_layers(QgsProject.instance(),
                                              self.model.plan)
        elif not plan_id:
            self.model.close_plan()
        self._refresh_strip()

    def _refresh_strip(self) -> None:
        plan = self.model.plan
        method_text = schema.METHOD_LABELS.get(
            schema.normalise_method(plan.get("method") or ""), "")
        self.plan_combo.setToolTip(
            f"Method: {method_text}" if method_text else "")
        status = plan.get("status") or ""
        self.status_badge.setText(status)
        self.status_badge.setStyleSheet(ui_helpers.badge_style(status))
        self.status_badge.setToolTip(
            ("Plan status. " + f"Method: {method_text}.") if method_text
            else "Plan status.")
        self.status_badge.setVisible(bool(status and plan))
        self._refresh_tab_badges()
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

    def _refresh_tab_badges(self) -> None:
        """Flag workflow prerequisites on the tab titles themselves."""
        def set_badge(widget, warn: bool, tip: str) -> None:
            index = self.tabs.indexOf(widget)
            if index < 0:
                return
            title = self._tab_titles.get(widget) or \
                self.tabs.tabText(index).replace(" ⚠", "")
            self.tabs.setTabText(index, title + (" ⚠" if warn else ""))
            self.tabs.setTabToolTip(index, tip if warn else "")

        plan = self.model.plan
        if not plan:
            set_badge(self.inputs_tab, False, "")
            set_badge(self.profile_tab, False, "")
            return
        route_missing = self.model.route is None
        scope_zero = self.model.gen_params().scope.length_km <= 1e-9
        bathy_missing = not self.model.depth_config().is_configured()
        problems = []
        if route_missing:
            problems.append("no route set")
        if scope_zero:
            problems.append("scope not set")
        if bathy_missing:
            problems.append("no bathymetry source")
        set_badge(self.inputs_tab, bool(problems),
                  ("This plan still needs: " + ", ".join(problems) + ".")
                  if problems else "")
        state = self.model.profile_state() if not problems else "missing"
        warn_profile = not problems and state != "current"
        tip = ("The stored bathymetry profile is stale — rebuild it here "
               "before generating." if state == "stale"
               else "No stored bathymetry profile — build it here.")
        set_badge(self.profile_tab, warn_profile, tip)

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
        if self._profile_task is not None:
            # The route frame and profile store are shared state; running
            # both tasks at once risked racing their lazy indexes and
            # swapping the profile mid-analysis.
            self.builder_tab.analysis_message(
                "Wait for the profile sampling to finish (or stop it) "
                "before running the analysis.")
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
        # Results are applied against the parameters the work was built
        # with; re-reading the model after the run silently mixed old
        # intervals with new scope/method/sliver settings when the user
        # edited them mid-run.
        self._task_params = params
        self._task_plan_id = self.model.plan_id
        self._task = analysis_task.BurialAnalysisTask(work, self._analysis_finished)
        self._task.progressMessage.connect(self.rules_tab.set_progress)
        self._task.progressMessage.connect(self.builder_tab.analysis_message)
        self._task.progressChanged.connect(self.builder_tab.analysis_progress)
        self.builder_tab.analysis_started()
        self.rules_tab.analysis_started()
        QgsApplication.taskManager().addTask(self._task)

    def _analysis_finished(self, task: analysis_task.BurialAnalysisTask) -> None:
        self._task = None
        try:
            self.rules_tab.analysis_finished()
        except Exception:
            pass  # must never block resetting the builder progress below
        if getattr(self, "_task_plan_id", "") != self.model.plan_id:
            # The user switched plans (or plan files) while the analysis
            # ran — the results were built from the other plan's rules and
            # scope and must never be applied to this one.
            self.builder_tab.analysis_finished(
                "Analysis discarded — a different plan is now open. Run it "
                "again with its plan selected.")
            return
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
        # The parameters snapshotted when the work was built — not a fresh
        # read that could have changed while the task ran.
        params = getattr(self, "_task_params", None) or self.model.gen_params()
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
            # User-resolved no-data ranges (skip or burial) are not II —
            # same subtraction as generation.generate().
            self.model.context = generation.ResolutionContext(
                excluded=[v for v in verdicts if v.status == "excluded"],
                screening=[v for v in verdicts if v.status == "risk"],
                influence=list(influence),
                insufficient=generation.unresolved_insufficient(
                    params, nodata),
                rule_hits={rule_id: list(intervals) for rule_id, intervals
                           in resolved.rule_hits.items()})
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
            # Dismissed Insufficient Information ranges are curation too:
            # a fresh run rebuilds purely from the Exclusion stack, so any
            # dismissed no-data range reappears as II.
            params = dataclasses.replace(params, dismissed_insufficient=[])
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
            depth_at=self.model.depth_at_kp,
            # Reuse the fire-bar resolution — resolving the identical stack
            # twice per Generate was the largest main-thread duplicate.
            resolution=(resolved, influence, nodata, warnings))

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
            self.profile_status.setStyleSheet(ui_helpers.status_style("warn"))
        else:
            self.profile_status.setStyleSheet("")
        self.profile_status.setText(text)
        self.profile_tab.refresh()
        self._refresh_tab_badges()

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
        if self._task is not None:
            self.profile_status.setText(
                "Wait for the running analysis to finish (or stop it) "
                "before resampling the profile.")
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

        # Fingerprints captured before sampling starts: if a bathymetry
        # file is replaced on disk mid-run (no model signal fires), the
        # stored profile must not claim currency against data its samples
        # never came from.
        self._profile_fingerprints = (self.model.current_rpl_fingerprint(),
                                      self.model.depth_fingerprint())
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
        route_fp, depth_fp = getattr(
            self, "_profile_fingerprints",
            (self.model.current_rpl_fingerprint(),
             self.model.depth_fingerprint()))
        profile = profile_data.PlanProfile(
            step_m=task.step_m,
            cross_offset_m=task.cross_offset_m,
            scope_start_kp=params.scope.start_km,
            scope_end_kp=params.scope.end_km,
            route_fingerprint=route_fp,
            depth_fingerprint=depth_fp,
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
        self._style_view_button(enabled)
        self._refresh_profile_events()

    def _style_view_button(self, drag_enabled: bool) -> None:
        """Amber View button = event dragging is live (edit mode warning)."""
        self.view_button.setStyleSheet(
            f"color: {ui_helpers.color('warn')}; font-weight: 600;"
            if drag_enabled else "")

    def _goto_kp_entered(self) -> None:
        text = (self.goto_kp_edit.text() or "").lower().replace("kp", "")
        text = text.replace(",", ".").strip()
        try:
            kp = float(text)
        except ValueError:
            return
        self.goto_kp(kp)

    def _export_profile_image(self) -> None:
        """Save the profile pane (as displayed) to a PNG file."""
        pixmap = self.profile.grab()
        if pixmap.isNull():
            QMessageBox.warning(self, "Burial Planner",
                                "The profile pane could not be captured.")
            return
        name = schema.sanitize_slug(
            (self.model.plan.get("name") or "profile")) + "_profile.png"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export profile image", name, "PNG (*.png)")
        if not path:
            return
        if not pixmap.save(path, "PNG"):
            QMessageBox.warning(self, "Burial Planner",
                                f"Could not write the image:\n{path}")

    def _on_profile_event_moved(self, event_id: str, new_kp: float) -> None:
        # Same confirmation as table KP edits: the dialog shows the exact
        # (editable) KP plus an optional reason; Cancel snaps the line back.
        if not self.builder_tab.confirm_move_event(event_id,
                                                   round(new_kp, 3)):
            self.profile.revert_event_line(event_id)

    def _on_profile_double_clicked(self, kp: float) -> None:
        """Double-click on the profile primes the Plan Builder add-event KP."""
        if not self.model.plan:
            return
        self.builder_tab.set_add_kp(kp)
        self.tabs.setCurrentWidget(self.builder_tab)
        self.goto_kp(kp)

    # -- KP picking -----------------------------------------------------------
    def pick_kp_on_map(self, callback, prompt: str = "",
                       on_finished=None) -> bool:
        """One-shot map tool: snap a canvas click to the route, deliver its KP.

        Restores the previously active map tool on pick, right-click or Esc.
        ``on_finished`` (if given) runs when the pick ends for any reason —
        picked, cancelled or the user switching tools — after the previous
        map tool is restored (modal callers re-show themselves with it).
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
            if on_finished is not None:
                try:
                    on_finished()
                except Exception:
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

    def pick_route_offset_on_map(self, callback, prompt: str = "",
                                 on_finished=None) -> bool:
        """Multi-shot map tool delivering (KP, signed DCC) per click.

        Used by Installation Paths to place path adjustments: each click
        reports the nearest route KP and the click's cross-course offset
        (positive = port of travel). Right-click or Esc ends the picking
        and restores the previously active map tool.
        """
        if self.canvas is None:
            return False
        if self.model.route is None:
            QMessageBox.warning(self, "Burial Planner",
                                self.model.route_error
                                or "Set the plan's route on the Inputs tab first.")
            return False
        from .kp_pick_tool import RouteOffsetPickTool

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
            if on_finished is not None:
                try:
                    on_finished()
                except Exception:
                    pass

        tool = RouteOffsetPickTool(self.canvas, self.model.route, callback,
                                   direction=self.model.direction,
                                   on_finished=restore)
        self._pick_tool = tool
        self.canvas.setMapTool(tool)
        if prompt and self.iface is not None:
            try:
                from ..qgis_compat import MESSAGE_INFO

                self.iface.messageBar().pushMessage(
                    "Burial Planner", prompt, MESSAGE_INFO, 6)
            except Exception:
                pass
        return True

    # -- map sync -------------------------------------------------------------
    def _canvas_transform(self):
        """WGS84 → canvas transform, cached per destination CRS.

        Rebuilding a QgsCoordinateTransform on every hover was a hidden
        PROJ lookup per mouse move.
        """
        from qgis.core import QgsCoordinateReferenceSystem

        dest = self.canvas.mapSettings().destinationCrs()
        cached = getattr(self, "_canvas_transform_cache", None)
        if cached is not None and cached[0] == dest.authid():
            return cached[1]
        transform = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem("EPSG:4326"), dest,
            QgsProject.instance())
        self._canvas_transform_cache = (dest.authid(), transform)
        return transform

    def _canvas_point(self, kp: float):
        if self.model.route is None or self.canvas is None:
            return None
        point = self.model.route.point_at_kp(kp, clamp=True)
        if point is None:
            return None
        try:
            return self._canvas_transform().transform(point)
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
        self._update_footprint(kp)

    def highlight_kp(self, kp: float) -> None:
        point = self._canvas_point(kp)
        if point is None:
            return
        marker = self._ensure_marker()
        marker.setCenter(point)
        marker.show()
        self._update_footprint(kp)

    # -- tool footprint overlay ----------------------------------------------
    def _footprint_toggled(self, checked: bool) -> None:
        QSettings().setValue(
            "SubseaCableTools/BurialPlanner/tool_footprint_visible",
            bool(checked))
        if not checked:
            self._hide_footprint()

    @staticmethod
    def _hide_band(band) -> None:
        if band is not None and not _sip_isdeleted(band):
            try:
                band.hide()
            except (AttributeError, RuntimeError):
                pass

    def _hide_footprint(self) -> None:
        self._hide_band(self._footprint_band)
        self._hide_band(self._vessel_band)

    def _clear_footprint_cache(self) -> None:
        self._footprint_cache = {}
        self._path_points_cache = {}
        self._path_snap_cache = {}
        self._vessel_outline_cache = {}
        self._hide_footprint()

    def _ensure_outline_band(self, attr: str, geom_type, color: QColor):
        band = getattr(self, attr)
        if band is None or _sip_isdeleted(band) \
                or getattr(band, "_bp_geom_type", None) != geom_type:
            if band is not None:
                _remove_canvas_item(band)
            band = QgsRubberBand(self.canvas, geom_type)
            band.setStrokeColor(color)
            band.setFillColor(QColor(color.red(), color.green(),
                                     color.blue(), 60))
            band.setWidth(2)
            band._bp_geom_type = geom_type
            setattr(self, attr, band)
        return band

    def _ensure_footprint_band(self, geom_type):
        return self._ensure_outline_band("_footprint_band", geom_type,
                                         QColor(31, 119, 180))

    def _path_display_points(self):
        """(tool path points, barge points) parsed from the saved result."""
        result = self.model.path_result or {}
        path_id = str(result.get("path_id") or "")
        if not path_id or not result.get("tool_path_wkt"):
            return [], []
        cached = self._path_points_cache.get(path_id)
        if cached is None:
            cached = (path_data.parse_linestring_wkt(
                          result.get("tool_path_wkt") or ""),
                      path_data.parse_linestring_wkt(
                          result.get("barge_track_wkt") or ""))
            self._path_points_cache = {path_id: cached}
        return cached

    def _nearest_path_index(self, points, kp: float) -> Optional[int]:
        if len(points) < 2:
            return None
        anchor = self.model.route.point_at_kp(float(kp), clamp=True)
        if anchor is None:
            return None
        ax, ay = float(anchor.x()), float(anchor.y())
        # Longitude compression: adequate for nearest-vertex picking at the
        # sub-kilometre offsets an installation path can reach.
        scale = max(0.05, math.cos(math.radians(min(89.0, abs(ay)))))
        return min(range(len(points)), key=lambda i: (
            ((points[i][0] - ax) * scale) ** 2 + (points[i][1] - ay) ** 2))

    @staticmethod
    def _polyline_pose(points, index: int):
        """(anchor, before, after) WGS84 triplet at a polyline vertex."""
        anchor = QgsPointXY(points[index][0], points[index][1])
        before = QgsPointXY(*points[max(0, index - 1)])
        after = QgsPointXY(*points[min(len(points) - 1, index + 1)])
        return anchor, before, after

    def _place_body_outline(self, outline, pose, dest_crs):
        try:
            geom, _heading = footprint.place_outline_at(
                outline, *pose, target_crs=dest_crs)
        except Exception:
            return None
        return None if geom is None or geom.isEmpty() else geom

    def _update_footprint(self, kp: float) -> None:
        """Draw the effective tool (and towing vessel) outline at kp.

        A saved installation path takes precedence: the tool outline rides
        the generated tool path and the vessel outline rides the barge track
        at the matching tow point.  Without a path the tool outline falls
        back to the RPL, as before.
        """
        if not self.footprint_toggle.isChecked() or self.canvas is None \
                or self.model.route is None:
            return
        tool_points, barge_points = self._path_display_points()
        anchor_index = self._nearest_path_index(tool_points, kp)
        tool_pose = barge_pose = None
        if anchor_index is not None:
            tool_pose = self._polyline_pose(tool_points, anchor_index)
            if anchor_index < len(barge_points):
                barge_pose = self._polyline_pose(barge_points, anchor_index)
        self._draw_outlines(kp, tool_pose, barge_pose)

    def _draw_outlines(self, kp, tool_pose, barge_pose) -> None:
        """Draw the tool (and vessel) outline bands at the given poses.

        ``tool_pose``/``barge_pose`` are WGS84 ``(anchor, before, after)``
        triplets or ``None``; without a tool pose the tool outline falls
        back to the RPL at ``kp``.  Shared by the profile-hover overlay and
        the follow-cursor overlay.
        """
        if self.canvas is None:
            return
        from ..qgis_compat import GEOMETRY_LINE, GEOMETRY_POLYGON

        dest_crs = self.canvas.mapSettings().destinationCrs()
        tool = None
        if kp is not None:
            tool = tools_mod.tool_at_kp(self.model.sections, self.model.plan,
                                        self.model.tools, kp)
        tool_id = str((tool or {}).get("tool_id") or "")
        wkt = str((tool or {}).get("footprint_wkt") or "")
        tool_shown = False
        if tool_id and wkt:
            outline = self._footprint_cache.get(tool_id)
            if outline is None:
                outline = QgsGeometry.fromWkt(wkt)
                self._footprint_cache[tool_id] = outline
            if outline is not None and not outline.isNull() \
                    and not outline.isEmpty():
                geom = None
                if tool_pose is not None:
                    geom = self._place_body_outline(outline, tool_pose,
                                                    dest_crs)
                if geom is None and kp is not None \
                        and self.model.route is not None:
                    try:
                        geom, _heading = footprint.place_outline(
                            outline, self.model.route, kp,
                            target_crs=dest_crs)
                    except Exception:
                        geom = None
                if geom is not None and not geom.isEmpty():
                    geom_type = (GEOMETRY_LINE
                                 if outline.type() == GEOMETRY_LINE
                                 else GEOMETRY_POLYGON)
                    band = self._ensure_footprint_band(geom_type)
                    band.setToGeometry(geom, None)
                    band.show()
                    tool_shown = True
        if not tool_shown:
            self._hide_band(self._footprint_band)

        vessel_shown = False
        if barge_pose is not None:
            vessel = self.model.vessel(str(
                self.model.path_config().get("vessel_id") or ""))
            vessel_id = str((vessel or {}).get("vessel_id") or "")
            vessel_wkt = str((vessel or {}).get("footprint_wkt") or "")
            if vessel_id and vessel_wkt:
                outline = self._vessel_outline_cache.get(vessel_id)
                if outline is None:
                    outline = QgsGeometry.fromWkt(vessel_wkt)
                    self._vessel_outline_cache[vessel_id] = outline
                if outline is not None and not outline.isNull() \
                        and not outline.isEmpty():
                    geom = self._place_body_outline(outline, barge_pose,
                                                    dest_crs)
                    if geom is not None:
                        geom_type = (GEOMETRY_LINE
                                     if outline.type() == GEOMETRY_LINE
                                     else GEOMETRY_POLYGON)
                        band = self._ensure_outline_band(
                            "_vessel_band", geom_type, QColor(122, 61, 184))
                        band.setToGeometry(geom, None)
                        band.show()
                        vessel_shown = True
        if not vessel_shown:
            self._hide_band(self._vessel_band)

    # -- follow-cursor outline overlay ----------------------------------------
    def _cursor_outline_toggled(self, checked: bool) -> None:
        """Attach/detach the passive canvas mouse watcher.

        An event filter on the canvas viewport (never a map tool) so the
        user's active tool — pan, identify, a pick tool — keeps working.
        Nothing is installed while the toggle is off.
        """
        self._cursor_outline_enabled = bool(checked)
        if self.canvas is None:
            return
        viewport = self.canvas.viewport()
        if checked:
            if viewport is not None:
                viewport.installEventFilter(self)
        else:
            if viewport is not None:
                try:
                    viewport.removeEventFilter(self)
                except (AttributeError, RuntimeError):
                    pass
            if self._cursor_outline_timer is not None:
                self._cursor_outline_timer.stop()
            self._cursor_outline_pos = None
            self._hide_footprint()

    def eventFilter(self, obj, event):
        if self._cursor_outline_enabled and self.canvas is not None \
                and not _sip_isdeleted(self.canvas) \
                and obj is self.canvas.viewport():
            try:
                etype = event.type()
                if etype == _EVENT_MOUSE_MOVE:
                    self._cursor_outline_pos = _mouse_event_pos(event)
                    timer = self._cursor_outline_timer
                    if timer is None:
                        timer = QTimer(self)
                        timer.setSingleShot(True)
                        timer.setInterval(30)  # coalesce like profile hover
                        timer.timeout.connect(self._cursor_outline_tick)
                        self._cursor_outline_timer = timer
                    if not timer.isActive():
                        timer.start()
                elif etype == _EVENT_LEAVE:
                    self._cursor_outline_pos = None
                    self._hide_footprint()
            except (AttributeError, RuntimeError):
                pass
        return super().eventFilter(obj, event)

    def _canvas_to_wgs84(self, map_point):
        """Canvas-CRS point → WGS84, cached per destination CRS."""
        from qgis.core import QgsCoordinateReferenceSystem

        dest = self.canvas.mapSettings().destinationCrs()
        if dest.authid() == "EPSG:4326":
            return QgsPointXY(map_point)
        cached = getattr(self, "_canvas_inverse_cache", None)
        if cached is None or cached[0] != dest.authid():
            transform = QgsCoordinateTransform(
                dest, QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance())
            cached = (dest.authid(), transform)
            self._canvas_inverse_cache = cached
        try:
            return cached[1].transform(map_point)
        except Exception:
            return None

    def _snap_to_tool_path(self, wgs_point, points):
        """Nearest tool-path segment: ``(index, fraction)`` or ``None``.

        Snapping runs on a longitude-compressed copy of the path held in a
        ``QgsGeometry`` so ``closestSegmentWithContext`` (C++) does the
        per-mouse-move work; adequate for the sub-kilometre offsets an
        installation path can reach (same convention as
        :meth:`_nearest_path_index`).
        """
        if len(points) < 2:
            return None
        result = self.model.path_result or {}
        path_id = str(result.get("path_id") or "")
        entry = self._path_snap_cache.get(path_id) if path_id else None
        if entry is None:
            mean_lat = sum(p[1] for p in points) / len(points)
            scale = max(0.05, math.cos(math.radians(min(89.0,
                                                        abs(mean_lat)))))
            geom = QgsGeometry.fromPolylineXY(
                [QgsPointXY(p[0] * scale, p[1]) for p in points])
            entry = (scale, geom)
            self._path_snap_cache = {path_id: entry} if path_id else {}
        scale, geom = entry
        probe = QgsPointXY(float(wgs_point.x()) * scale,
                           float(wgs_point.y()))
        try:
            sqr_dist, min_point, after_vertex, _side = \
                geom.closestSegmentWithContext(probe)
        except Exception:
            return None
        if sqr_dist < 0 or after_vertex <= 0:
            return None
        index = min(int(after_vertex) - 1, len(points) - 2)
        ax, ay = points[index][0] * scale, points[index][1]
        bx, by = points[index + 1][0] * scale, points[index + 1][1]
        span = math.hypot(bx - ax, by - ay)
        fraction = 0.0 if span <= 0.0 else min(1.0, max(0.0, math.hypot(
            float(min_point.x()) - ax, float(min_point.y()) - ay) / span))
        return index, fraction

    @staticmethod
    def _segment_pose(points, index: int, fraction: float):
        """WGS84 ``(anchor, before, after)`` interpolated along a segment."""
        a, b = points[index], points[index + 1]
        anchor = QgsPointXY(a[0] + fraction * (b[0] - a[0]),
                            a[1] + fraction * (b[1] - a[1]))
        return anchor, QgsPointXY(a[0], a[1]), QgsPointXY(b[0], b[1])

    def _cursor_outline_tick(self) -> None:
        if not self._cursor_outline_enabled or self.canvas is None \
                or _sip_isdeleted(self.canvas):
            return
        pos = self._cursor_outline_pos
        if pos is None:
            return
        try:
            map_point = self.canvas.getCoordinateTransform() \
                .toMapCoordinates(pos.x(), pos.y())
        except (AttributeError, RuntimeError):
            return
        wgs = self._canvas_to_wgs84(map_point)
        if wgs is None:
            return
        tool_points, barge_points = self._path_display_points()
        tool_pose = barge_pose = kp = None
        snap = self._snap_to_tool_path(wgs, tool_points)
        if snap is not None:
            index, fraction = snap
            tool_pose = self._segment_pose(tool_points, index, fraction)
            if index + 1 < len(barge_points):
                barge_pose = self._segment_pose(barge_points, index,
                                                fraction)
            probe = tool_pose[0]
        else:
            probe = wgs
        if self.model.route is not None:
            try:
                hit = self.model.route.kp_at_point(probe)
                if hit is not None and hit.snapped_xy is not None \
                        and math.isfinite(hit.dcc_m):
                    kp = float(hit.kp_km)
            except Exception:
                kp = None
        if tool_pose is None and kp is None:
            self._hide_footprint()
            return
        self._draw_outlines(kp, tool_pose, barge_pose)

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
    def apply_saved_window_mode(self) -> None:
        """Open as a floating window by default.

        The dock honours the user's last choice: re-docking it and closing
        makes the next open docked again; floating (the default) restores
        the saved monitor/size via ``_top_level_changed``.
        """
        if bool(QSettings().value(_FLOATING_MODE_KEY, True, type=bool)) \
                and not self.isFloating():
            self.setFloating(True)

    def _top_level_changed(self, floating: bool) -> None:
        if floating:
            self.setWindowFlags(WINDOW_TYPE_WINDOW | WINDOW_HINT_CUSTOMIZE
                                | WINDOW_HINT_TITLE | WINDOW_HINT_MIN_MAX
                                | WINDOW_HINT_CLOSE)
            self.show()
            # Reopen on the same monitor at the same size (second-screen
            # workflows keep their window placement across sessions).
            geometry = QSettings().value(_FLOATING_GEOMETRY_KEY)
            if geometry is not None:
                try:
                    self.restoreGeometry(geometry)
                except Exception:
                    pass
            else:
                self.resize(1100, 750)

    def _save_window_state(self) -> None:
        try:
            settings = QSettings()
            settings.setValue(_FLOATING_MODE_KEY, self.isFloating())
            if self.isFloating():
                settings.setValue(_FLOATING_GEOMETRY_KEY, self.saveGeometry())
        except Exception:
            pass

    def refresh(self) -> None:
        """Re-read the current project's store on every open."""
        saved_path = project_gpkg_path()
        path = saved_path or default_project_gpkg_path()
        if path != self.store.gpkg_path:
            store, path, error = self._open_store_with_recovery(
                path, create_if_missing=not bool(saved_path))
            if not error:
                try:
                    self.store.close()
                except Exception:
                    pass
                self.store = store
                self.store_ready = True
                self.model.store = store
                self.model.workbench_store = self.workbench_store()
                self.model.refresh_tools()  # the registry is per GeoPackage
                self.model.refresh_layback_profiles()
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
        self._save_window_state()  # unload may bypass closeEvent
        try:
            self.cancel_analysis()
        except (AttributeError, RuntimeError):
            pass
        try:
            self.paths_tab.shutdown()
        except (AttributeError, RuntimeError):
            pass
        try:
            self.store.close()  # checkpoint WAL + release the SQL handle
        except Exception:
            pass
        try:
            self._cancel_profile_refresh(silent=True)
        except (AttributeError, RuntimeError):
            pass
        try:
            self.risk_tab.shutdown()
        except (AttributeError, RuntimeError):
            pass
        pick_tool = self._pick_tool
        self._pick_tool = None
        if pick_tool is not None and self.canvas is not None:
            try:
                self.canvas.unsetMapTool(pick_tool)
            except (AttributeError, RuntimeError):
                pass
        # Follow-cursor overlay: detach the viewport watcher and stop the
        # coalescing timer before the canvas items are removed.
        self._cursor_outline_enabled = False
        self._cursor_outline_pos = None
        if self._cursor_outline_timer is not None:
            try:
                self._cursor_outline_timer.stop()
            except (AttributeError, RuntimeError):
                pass
            self._cursor_outline_timer = None
        if self.canvas is not None and not _sip_isdeleted(self.canvas):
            try:
                self.canvas.viewport().removeEventFilter(self)
            except (AttributeError, RuntimeError):
                pass
        items = (self._marker, self._band, self._footprint_band,
                 self._vessel_band) + tuple(self._exclusion_bands)
        self._marker = None
        self._band = None
        self._footprint_band = None
        self._vessel_band = None
        self._exclusion_bands = []
        for item in items:
            _remove_canvas_item(item)

    def closeEvent(self, event) -> None:
        self.shutdown()  # saves the window state first
        super().closeEvent(event)
