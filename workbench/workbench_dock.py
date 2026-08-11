# -*- coding: utf-8 -*-
"""Cable Route Workbench unified dock.

The workbench is system-first: Systems contain segments, segments contain RPL
revisions, and fits/assessments live under the RPL they analyse. Assemblies
remain a flat library because the same physical build can be fitted to many
RPL revisions.
"""

from __future__ import annotations

from typing import Dict, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject, QgsVectorLayer

from .assembly_manager_dock import AssemblyManagerPanel
from .assessment_panel import AssessmentPanel
from .rpl_manager_dock import RplManagerPanel
from . import schema

KIND_ASSEMBLY = "assembly"
KIND_RPL = "rpl"
KIND_FIT = "fit"
KIND_ASSESSMENT = "assessment"
KIND_GROUP = "group"
KIND_SYSTEM = "system"
KIND_ROUTE = "route"

GROUP_UNASSIGNED_SEGMENTS = "unassigned_segments"
GROUP_LIBRARY = "library"

AMBER = QBrush(QColor(200, 120, 0))


class WorkbenchDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Cable Route Workbench", parent)
        self.iface = iface
        self._project_layer_sync_muted = False
        self._teardown_muted = False
        self._pending_removed_workbench_layers = set()
        self.setObjectName("CableRouteWorkbenchDock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )

        container = QWidget()
        outer = QVBoxLayout(container)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Workbench", "Detail"])
        self.tree.setColumnWidth(0, 240)
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        policy = getattr(Qt, "CustomContextMenu", None)
        if policy is None:
            policy = Qt.ContextMenuPolicy.CustomContextMenu
        self.tree.setContextMenuPolicy(policy)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        left_layout.addWidget(self.tree)

        buttons = QHBoxLayout()
        new_btn = QPushButton("New")
        new_menu = QMenu(new_btn)
        new_menu.addAction("New system...", self._new_system)
        new_menu.addAction("New segment...", self._new_route)
        new_menu.addAction("Import RPL...", self._import_rpl)
        new_menu.addAction("Add RPL from layers...", self._register_rpl)
        new_menu.addAction("New assembly...", self._new_assembly)
        new_menu.addAction("New assessment...", self._new_assessment)
        new_btn.setMenu(new_menu)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_tree)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete_selected)
        buttons.addWidget(new_btn)
        buttons.addWidget(refresh_btn)
        buttons.addWidget(delete_btn)
        left_layout.addLayout(buttons)
        splitter.addWidget(left)

        self.stack = QStackedWidget()
        self.placeholder = QLabel(
            "Select a system, segment, RPL revision, assembly, fit, or assessment in the tree."
        )
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.placeholder)

        self.rpl_panel = RplManagerPanel(iface, embedded=True)
        self.stack.addWidget(self.rpl_panel)
        self.assembly_panel = AssemblyManagerPanel(iface, embedded=True)
        self.stack.addWidget(self.assembly_panel)
        self.assessment_panel = AssessmentPanel(iface, embedded=True)
        self.stack.addWidget(self.assessment_panel)

        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 900])
        self.setWidget(container)

        self.rpl_panel.rpls_changed.connect(self.refresh_tree)
        self.rpl_panel.fits_changed.connect(self.refresh_tree)
        self.rpl_panel.extract_assembly_requested.connect(self._extract_assembly_from_rpl)
        self.assembly_panel.assemblies_changed.connect(self.refresh_tree)
        self.assembly_panel.assembly_saved.connect(lambda _id: self._refresh_labels())
        self.assessment_panel.assessments_changed.connect(self.refresh_tree)
        self._connect_project_layer_sync()

        self.refresh_tree()

    # ------------------------------------------------------ layer sync --
    def _project_sync_signal_slots(self):
        project = QgsProject.instance()
        pairs = [
            (project, "aboutToBeCleared", self._on_project_teardown_starts),
            (project, "cleared", self._on_project_teardown_done),
            (project, "layerWillBeRemoved", self._on_project_layers_will_be_removed),
            (project, "layersWillBeRemoved", self._on_project_layers_will_be_removed),
            (project, "layersRemoved", self._on_project_layers_removed),
        ]
        if self.iface is not None:
            pairs.append((self.iface, "projectRead", self._on_project_switched))
            pairs.append((self.iface, "newProjectCreated", self._on_project_switched))
        return pairs

    def _connect_project_layer_sync(self):
        for obj, signal_name, slot in self._project_sync_signal_slots():
            try:
                getattr(obj, signal_name).connect(slot)
            except Exception:
                pass

    def _disconnect_project_layer_sync(self):
        for obj, signal_name, slot in self._project_sync_signal_slots():
            try:
                getattr(obj, signal_name).disconnect(slot)
            except Exception:
                pass

    def _on_project_teardown_starts(self, *_args):
        # The project is being cleared (close / open another project): every
        # layer goes away, and that must NOT be treated as the user deleting
        # workbench layers.
        self._teardown_muted = True
        self._pending_removed_workbench_layers.clear()

    def _on_project_teardown_done(self, *_args):
        self._teardown_muted = False
        self._pending_removed_workbench_layers.clear()

    def _on_project_switched(self, *_args):
        # A different project is now active: drop any stale state and rebuild
        # the tree from that project's workbench store.
        self._teardown_muted = False
        self._pending_removed_workbench_layers.clear()
        self.refresh_tree()

    @staticmethod
    def _is_project_teardown(args) -> bool:
        """True when this removal covers every layer in the project.

        ``QgsProject.clear()`` removes all layers in one batch; a user delete
        never does. Belt-and-braces for QGIS builds without the
        ``aboutToBeCleared`` signal.
        """
        project = QgsProject.instance()
        all_ids = set(project.mapLayers().keys())
        if not all_ids:
            return False
        removed_ids = set()
        for item in _flatten_signal_args(args):
            if isinstance(item, str):
                removed_ids.add(item)
            elif hasattr(item, "id"):
                try:
                    removed_ids.add(item.id())
                except Exception:
                    pass
        return all_ids <= removed_ids

    def _on_project_layers_will_be_removed(self, *args):
        if self._project_layer_sync_muted or self._teardown_muted:
            return
        if self._is_project_teardown(args):
            self._teardown_muted = True
            self._pending_removed_workbench_layers.clear()
            return
        store = self._store()
        if store is None or not store.exists():
            return
        for layer_name in _workbench_layer_names_from_signal(args, store.gpkg_path):
            self._pending_removed_workbench_layers.add(layer_name)

    def _on_project_layers_removed(self, *_args):
        if self._teardown_muted:
            # Teardown flagged from the will-be-removed heuristic; the project
            # signals (cleared/projectRead) reset the flag too, but do it here
            # so a lone removeAllMapLayers() cannot leave the sync muted.
            self._teardown_muted = False
            self._pending_removed_workbench_layers.clear()
            return
        if self._project_layer_sync_muted:
            self._pending_removed_workbench_layers.clear()
            return
        if not self._pending_removed_workbench_layers:
            return
        layer_names = set(self._pending_removed_workbench_layers)
        self._pending_removed_workbench_layers.clear()
        self._delete_registry_for_removed_project_layers(layer_names)

    def _delete_registry_for_removed_project_layers(self, layer_names):
        store = self._store()
        if store is None or not store.exists():
            return
        removed_rpl_ids = set()
        removed_fit_ids = set()
        removed_assessment_ids = set()
        sibling_layers = set()

        rpls = store.list_rpls()
        for rpl in rpls:
            if rpl.get("points_layer") in layer_names or rpl.get("lines_layer") in layer_names:
                if rpl.get("status") == schema.STATUS_ISSUED:
                    # Issued revisions are read-only records: removing their
                    # layers from the project must never destroy the registry
                    # entry. The layers stay in the gpkg and can be re-added.
                    continue
                rpl_id = rpl.get("rpl_id") or ""
                sibling_layers.update(self._rpl_project_layer_names(store, rpl_id))
                removed_rpl_ids.add(rpl_id)

        for fit in store.list_fits():
            fit_id = fit.get("fit_id") or ""
            if fit_id in removed_fit_ids:
                continue
            names = set(self._fit_project_layer_names(store, fit))
            if names.intersection(layer_names):
                sibling_layers.update(names)
                removed_fit_ids.add(fit_id)

        for assessment in store.list_assessments():
            assessment_id = assessment.get("assessment_id") or ""
            if assessment.get("ranges_layer") in layer_names:
                sibling_layers.add(assessment.get("ranges_layer"))
                removed_assessment_ids.add(assessment_id)

        for rpl_id in removed_rpl_ids:
            store.delete_rpl(rpl_id)
        for fit_id in removed_fit_ids:
            store.delete_fit(fit_id)
        for assessment_id in removed_assessment_ids:
            store.delete_assessment(assessment_id)

        sibling_layers.difference_update(layer_names)
        if sibling_layers:
            self._remove_project_layers(sibling_layers)
        self._clear_deleted_panel_state(removed_rpl_ids, removed_fit_ids, removed_assessment_ids)
        self.refresh_tree()

    def _remove_project_layers(self, layer_names):
        store = self._store()
        if store is None:
            return
        layer_ids = _project_layer_ids_for_names(store.gpkg_path, layer_names)
        if not layer_ids:
            return
        self._project_layer_sync_muted = True
        try:
            QgsProject.instance().removeMapLayers(layer_ids)
        finally:
            self._project_layer_sync_muted = False

    def _clear_deleted_panel_state(self, removed_rpl_ids, removed_fit_ids=None, removed_assessment_ids=None):
        removed_fit_ids = removed_fit_ids or set()
        removed_assessment_ids = removed_assessment_ids or set()
        current_id = self.rpl_panel.current_rpl.get("rpl_id") if self.rpl_panel.current_rpl else None
        if current_id in removed_rpl_ids:
            if self.rpl_panel.edit_btn.isChecked():
                self.rpl_panel.edit_btn.setChecked(False)
            self.rpl_panel.current_rpl = None
            self.rpl_panel.model = None
            self.rpl_panel.sync = None
            self.rpl_panel._refresh_tables()
            self.stack.setCurrentWidget(self.placeholder)
        current_fit_id = (
            self.assembly_panel._fit_row.get("fit_id")
            if self.assembly_panel._fit_row else None
        )
        if current_fit_id in removed_fit_ids:
            self.assembly_panel.set_fit_context(None)
            if self.stack.currentWidget() is self.assembly_panel:
                self.stack.setCurrentWidget(self.placeholder)
        current_assessment_id = (
            self.assessment_panel.assessment.get("assessment_id")
            if self.assessment_panel.assessment else None
        )
        if current_assessment_id in removed_assessment_ids:
            self.assessment_panel.assessment = None
            self.assessment_panel.rpl_id = None
            if self.stack.currentWidget() is self.assessment_panel:
                self.stack.setCurrentWidget(self.placeholder)

    def _rpl_project_layer_names(self, store, rpl_id: str):
        rpl = store.get_rpl(rpl_id) or {}
        names = [rpl.get("points_layer"), rpl.get("lines_layer")]
        for fit in store.list_fits(rpl_id=rpl_id):
            names.extend(self._fit_project_layer_names(store, fit))
        for assessment in store.list_assessments(rpl_id):
            names.append(assessment.get("ranges_layer"))
        return [name for name in names if name]

    def _assembly_project_layer_names(self, store, assembly_id: str):
        names = []
        for fit in store.list_fits(assembly_id=assembly_id):
            names.extend(self._fit_project_layer_names(store, fit))
        return [name for name in names if name]

    def _fit_project_layer_names(self, store, fit):
        assembly, _items = store.get_assembly(fit.get("assembly_id") or "")
        rpl = store.get_rpl(fit.get("rpl_id") or "")
        if not assembly or not rpl:
            return []
        fit_name = f"{assembly.get('name')}_{rpl.get('name')}"
        return [
            schema.fit_bodies_layer_name(fit_name),
            schema.fit_sections_layer_name(fit_name),
        ]

    # ------------------------------------------------------------- tree --
    def _store(self):
        self.rpl_panel._open_store()
        store = self.rpl_panel.store
        if store is not None and store.exists():
            store.migrate()
        return store

    def refresh_tree(self):
        current = self._current_ref()
        self.tree.blockSignals(True)
        self.tree.clear()

        store = self._store()
        library_root = QTreeWidgetItem(["Assembly Library", ""])
        library_root.setData(0, Qt.ItemDataRole.UserRole, (KIND_GROUP, GROUP_LIBRARY))

        if store is not None and store.exists():
            assemblies = {r.get("assembly_id"): r for r in store.list_assemblies()}
            fits = store.list_fits()
            assessments = store.list_assessments()
            routes = store.list_routes()
            systems = {r.get("system_id"): r for r in store.list_systems()}
            segment_counts: Dict[str, int] = {}
            for route in routes:
                system_id = route.get("system_id") or ""
                segment_counts[system_id] = segment_counts.get(system_id, 0) + 1
            system_nodes: Dict[str, QTreeWidgetItem] = {}

            for system in sorted(systems.values(), key=lambda r: r.get("name") or ""):
                system_id = system.get("system_id") or ""
                count = segment_counts.get(system_id, 0)
                detail = f"{count} segment" + ("" if count == 1 else "s")
                item = QTreeWidgetItem([system.get("name") or "System", detail])
                item.setData(0, Qt.ItemDataRole.UserRole, (KIND_SYSTEM, system_id))
                self.tree.addTopLevelItem(item)
                item.setExpanded(True)
                system_nodes[system_id] = item

            unassigned_item = None
            for route in routes:
                system_id = route.get("system_id") or ""
                parent = system_nodes.get(system_id)
                if parent is None:
                    if unassigned_item is None:
                        count = segment_counts.get("", 0)
                        detail = f"{count} segment" + ("" if count == 1 else "s")
                        unassigned_item = QTreeWidgetItem(["Unassigned segments", detail])
                        unassigned_item.setData(
                            0, Qt.ItemDataRole.UserRole,
                            (KIND_GROUP, GROUP_UNASSIGNED_SEGMENTS),
                        )
                        self.tree.addTopLevelItem(unassigned_item)
                        unassigned_item.setExpanded(True)
                    parent = unassigned_item
                self._add_route_item(parent, store, route, assemblies, fits, assessments)

            for assembly in assemblies.values():
                total_km = (assembly.get("total_cable_len_m") or 0.0) / 1000.0
                detail = (
                    f"{assembly.get('kind') or 'cable'} - {total_km:.2f} km - "
                    f"{assembly.get('status') or schema.STATUS_DRAFT}"
                )
                item = QTreeWidgetItem([assembly.get("name") or "?", detail])
                item.setData(0, Qt.ItemDataRole.UserRole,
                             (KIND_ASSEMBLY, assembly.get("assembly_id")))
                library_root.addChild(item)

        self.tree.addTopLevelItem(library_root)
        library_root.setExpanded(True)
        self.tree.blockSignals(False)

        self.rpl_panel.refresh_rpl_list()
        self.assembly_panel.refresh_assembly_list()
        if current is not None:
            self._select_ref(current)

    def _add_route_item(self, parent, store, route, assemblies, fits, assessments):
        revisions = store.revisions_of_route(route.get("route_id") or "")
        detail = f"{len(revisions)} revision" + ("" if len(revisions) == 1 else "s")
        route_item = QTreeWidgetItem([route.get("name") or "Segment", detail])
        route_item.setData(0, Qt.ItemDataRole.UserRole,
                           (KIND_ROUTE, route.get("route_id")))
        parent.addChild(route_item)

        for rpl in revisions:
            status = rpl.get("status") or schema.STATUS_DRAFT
            kind = (rpl.get("kind") or "").replace("_", " ")
            if status == schema.STATUS_ISSUED:
                status = "issued [locked]"
            rpl_item = QTreeWidgetItem([rpl.get("name") or "?", f"{kind} - {status}"])
            rpl_item.setData(0, Qt.ItemDataRole.UserRole, (KIND_RPL, rpl.get("rpl_id")))
            route_item.addChild(rpl_item)

            for fit in fits:
                if fit.get("rpl_id") != rpl.get("rpl_id"):
                    continue
                assembly = assemblies.get(fit.get("assembly_id"), {})
                fit_item = QTreeWidgetItem([
                    assembly.get("name") or "Assembly fit",
                    f"anchor KP {float(fit.get('anchor_kp_km') or 0.0):.3f}",
                ])
                fit_item.setData(0, Qt.ItemDataRole.UserRole,
                                 (KIND_FIT, fit.get("fit_id")))
                rpl_item.addChild(fit_item)

            for assessment in assessments:
                if assessment.get("rpl_id") != rpl.get("rpl_id"):
                    continue
                status = assessment.get("status") or "not run"
                a_item = QTreeWidgetItem([assessment.get("name") or "Assessment", status])
                a_item.setData(0, Qt.ItemDataRole.UserRole,
                               (KIND_ASSESSMENT, assessment.get("assessment_id")))
                if status in ("", "not run", "stale"):
                    a_item.setForeground(1, AMBER)
                rpl_item.addChild(a_item)

    def _refresh_labels(self):
        self.refresh_tree()

    def _current_ref(self):
        item = self.tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _select_ref(self, ref):
        def walk(item):
            if item.data(0, Qt.ItemDataRole.UserRole) == ref:
                return item
            for i in range(item.childCount()):
                found = walk(item.child(i))
                if found is not None:
                    return found
            return None

        for i in range(self.tree.topLevelItemCount()):
            found = walk(self.tree.topLevelItem(i))
            if found is not None:
                self.tree.setCurrentItem(found)
                return

    # -------------------------------------------------------- selection --
    def _on_tree_selection(self, current, _previous=None):
        ref = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        if not ref or ref[0] == KIND_GROUP or ref[0] == KIND_SYSTEM:
            self.stack.setCurrentWidget(self.placeholder)
            return
        kind, entity_id = ref[0], ref[1]

        if kind == KIND_ROUTE:
            store = self._store()
            latest = store.latest_revision(entity_id) if store else None
            if latest:
                self.rpl_panel.select_rpl(latest.get("rpl_id") or "")
                self.stack.setCurrentWidget(self.rpl_panel)
            else:
                self.stack.setCurrentWidget(self.placeholder)
        elif kind == KIND_RPL:
            self.rpl_panel.select_rpl(entity_id)
            self.stack.setCurrentWidget(self.rpl_panel)
        elif kind == KIND_ASSEMBLY:
            self.assembly_panel.set_fit_context(None)
            self.assembly_panel.select_assembly(entity_id)
            self.stack.setCurrentWidget(self.assembly_panel)
        elif kind == KIND_FIT:
            store = self._store()
            fit_row = next(
                (f for f in store.list_fits() if f.get("fit_id") == entity_id), None
            ) if store else None
            if fit_row is not None:
                self.assembly_panel.select_assembly(fit_row.get("assembly_id") or "")
                self.assembly_panel.set_fit_context(fit_row)
                self.rpl_panel.select_rpl(fit_row.get("rpl_id") or "")
            self.stack.setCurrentWidget(self.assembly_panel)
        elif kind == KIND_ASSESSMENT:
            store = self._store()
            row = store.get_assessment(entity_id) if store else None
            if row is not None:
                self.assessment_panel.load_assessment(store, row)
                self.stack.setCurrentWidget(self.assessment_panel)

    # ----------------------------------------------------- context menu --
    def _show_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        ref = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        menu = QMenu(self)
        if not ref:
            menu.addAction("New system...", self._new_system)
            menu.addAction("New segment...", self._new_route)
            menu.addAction("Import RPL...", self._import_rpl)
        elif ref[0] == KIND_GROUP and ref[1] == GROUP_UNASSIGNED_SEGMENTS:
            menu.addAction("New system...", self._new_system)
            menu.addAction("New segment...", self._new_route)
            menu.addAction("Import RPL...", self._import_rpl)
            menu.addAction("Add RPL from layers...", self._register_rpl)
        elif ref[0] == KIND_GROUP and ref[1] == GROUP_LIBRARY:
            menu.addAction("New assembly...", self._new_assembly)
        elif ref[0] == KIND_SYSTEM:
            menu.addAction("New segment...", self._new_route)
            menu.addAction("Delete system", self._delete_selected)
        elif ref[0] == KIND_ROUTE:
            menu.addAction("New RPL revision...", self._new_rpl_revision)
            menu.addAction("Import RPL...", self._import_rpl)
            menu.addAction("Add RPL from layers...", self._register_rpl)
            menu.addAction("Fit assembly...", self._fit_selected_rpl)
            menu.addAction("New assessment...", self._new_assessment)
            self._add_assign_system_menu(menu)
            menu.addAction("Rename segment...", self._rename_route)
            menu.addAction("Delete segment", self._delete_selected)
        elif ref[0] == KIND_RPL:
            store = self._store()
            rpl = store.get_rpl(ref[1]) if store else None
            menu.addAction("New revision...", self._new_rpl_revision)
            label = "Reopen" if rpl and rpl.get("status") == schema.STATUS_ISSUED else "Mark issued"
            menu.addAction(label, self._mark_issued)
            menu.addAction("Fit assembly...", self._fit_selected_rpl)
            menu.addAction("New assessment...", self._new_assessment)
            compare = menu.addAction("Compare with...")
            compare.setEnabled(False)
            menu.addAction("Delete RPL", self._delete_selected)
        elif ref[0] == KIND_ASSEMBLY:
            assembly = self._assembly_row(ref[1])
            menu.addAction("New revision...", self._new_assembly_revision)
            label = "Reopen" if assembly and assembly.get("status") == schema.STATUS_ISSUED else "Mark issued"
            menu.addAction(label, self._mark_issued)
            menu.addAction("Duplicate", self._duplicate_assembly)
            menu.addAction("Export JSON", self._export_assembly_json)
            menu.addAction("Delete assembly", self._delete_selected)
        elif ref[0] in (KIND_FIT, KIND_ASSESSMENT):
            menu.addAction("Open", lambda: self._on_tree_selection(self.tree.currentItem()))
            menu.addAction("Delete", self._delete_selected)
        _exec_menu(menu, self.tree.viewport().mapToGlobal(pos))

    def _add_assign_system_menu(self, menu):
        store = self._store()
        submenu = menu.addMenu("Move segment to system")
        submenu.addAction("(ungrouped)", lambda: self._assign_route_to_system(""))
        if store and store.exists():
            for system in sorted(store.list_systems(), key=lambda r: r.get("name") or ""):
                submenu.addAction(
                    system.get("name") or "System",
                    lambda _checked=False, sid=system.get("system_id"): self._assign_route_to_system(sid or ""),
                )

    # ---------------------------------------------------------- actions --
    def _new_route(self):
        store = self._store()
        if store is None:
            return
        store.migrate()
        system_id = self._selected_system_id()
        name, ok = QInputDialog.getText(self, "New segment", "Segment name:")
        if not ok or not name.strip():
            return
        route_id = store.create_route(name.strip())
        if system_id is not None:
            store.assign_route_to_system(route_id, system_id)
        self.refresh_tree()
        self._select_ref((KIND_ROUTE, route_id))

    def _new_system(self):
        store = self._store()
        if store is None:
            return
        store.migrate()
        name, ok = QInputDialog.getText(self, "New system", "System name:")
        if not ok or not name.strip():
            return
        system_id = store.create_system(name.strip())
        self.refresh_tree()
        self._select_ref((KIND_SYSTEM, system_id))

    def _new_assembly(self):
        self.stack.setCurrentWidget(self.assembly_panel)
        self.assembly_panel._new_assembly()
        self.refresh_tree()

    def _extract_assembly_from_rpl(self, rpl_id: str):
        if not self.assembly_panel.extract_from_rpl_id(rpl_id):
            return
        assembly = self.assembly_panel.assembly
        fit_id = None
        if assembly is not None:
            self.rpl_panel.select_rpl(rpl_id)
            fit_id = self.rpl_panel.fit_assembly_to_current(assembly.assembly_id)
        self.refresh_tree()
        if fit_id is not None:
            self._select_ref((KIND_FIT, fit_id))
        elif assembly is not None:
            self._select_ref((KIND_ASSEMBLY, assembly.assembly_id))
            self.stack.setCurrentWidget(self.assembly_panel)

    def _import_rpl(self):
        """Open the guided Import RPL wizard from the workbench tree."""
        self.stack.setCurrentWidget(self.rpl_panel)
        self.rpl_panel._run_import_wizard()
        self.refresh_tree()

    def _register_rpl(self):
        self.stack.setCurrentWidget(self.rpl_panel)
        self.rpl_panel._run_register_algorithm(self._register_rpl_defaults())
        self.refresh_tree()

    def _register_rpl_defaults(self):
        store = self._store()
        route_id = self._selected_route_id()
        if store is None or not route_id:
            return {}
        route = store.get_route(route_id)
        if not route:
            return {}
        return {
            "ROUTE_NAME": route.get("name") or "",
            "REV_LABEL": schema.next_rev_label(store.revisions_of_route(route_id)),
        }

    def _new_rpl_revision(self):
        store = self._store()
        rpl_id = self._selected_rpl_id()
        if store is None or rpl_id is None:
            QMessageBox.information(self, "New RPL revision", "Select an RPL or segment first.")
            return
        if self.rpl_panel.sync is not None and self.rpl_panel.sync.is_dirty():
            QMessageBox.information(
                self, "New RPL revision",
                "Save or discard the open RPL edits before creating a revision.")
            return
        rpl = store.get_rpl(rpl_id)
        route_id = rpl.get("route_id") if rpl else ""
        default = schema.next_rev_label(store.revisions_of_route(route_id)) if route_id else "Rev 1"
        label, ok = QInputDialog.getText(self, "New RPL revision", "Revision label:", text=default)
        if not ok or not label.strip():
            return
        try:
            new_id = store.new_rpl_revision(rpl_id, label.strip())
        except Exception as exc:
            QMessageBox.warning(self, "New RPL revision", str(exc))
            return
        self.refresh_tree()
        self._select_ref((KIND_RPL, new_id))
        self.rpl_panel.select_rpl(new_id)
        self.rpl_panel._refresh_stored_fits()

    def _new_assembly_revision(self):
        store = self._store()
        assembly_id = self._selected_assembly_id()
        if store is None or assembly_id is None:
            QMessageBox.information(self, "New assembly revision", "Select an assembly first.")
            return
        assembly = self._assembly_row(assembly_id)
        base = (assembly or {}).get("name") or "Assembly"
        default = schema.next_rev_label([assembly] if assembly else [])
        label, ok = QInputDialog.getText(self, "New assembly revision", "Revision label:", text=default)
        if not ok or not label.strip():
            return
        try:
            new_id = store.new_assembly_revision(assembly_id, label.strip())
        except Exception as exc:
            QMessageBox.warning(self, "New assembly revision", f"{base}: {exc}")
            return
        self.refresh_tree()
        self._select_ref((KIND_ASSEMBLY, new_id))

    def _new_assessment(self):
        rpl_id = self._selected_rpl_id()
        if rpl_id is None:
            QMessageBox.information(
                self, "New assessment",
                "Select a segment, RPL, fit, or assessment first.")
            return
        store = self._store()
        if store is None:
            return
        self.assessment_panel.new_assessment(store, rpl_id)
        self.stack.setCurrentWidget(self.assessment_panel)
        self.refresh_tree()

    def _fit_selected_rpl(self):
        rpl_id = self._selected_rpl_id()
        if rpl_id is None:
            QMessageBox.information(self, "Fit assembly", "Select a segment or RPL first.")
            return
        self.rpl_panel.select_rpl(rpl_id)
        self.stack.setCurrentWidget(self.rpl_panel)
        self.rpl_panel._fit_assembly()
        self.refresh_tree()

    def _mark_issued(self):
        store = self._store()
        ref = self._current_ref()
        if store is None or not ref:
            return
        kind, entity_id = ref[0], ref[1]
        if kind == KIND_ROUTE:
            latest = store.latest_revision(entity_id)
            kind, entity_id = KIND_RPL, latest.get("rpl_id") if latest else ""
        if kind == KIND_RPL:
            if self.rpl_panel.sync is not None and self.rpl_panel.sync.is_dirty():
                QMessageBox.information(self, "Issue RPL", "Save or discard open RPL edits first.")
                return
            row = store.get_rpl(entity_id)
            if not row:
                return
            if row.get("status") == schema.STATUS_ISSUED:
                store.reopen_rpl(entity_id)
            else:
                answer = QMessageBox.question(
                    self, "Issue RPL",
                    f"Mark '{row.get('name')}' issued and read-only?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
                store.issue_rpl(entity_id)
        elif kind == KIND_ASSEMBLY:
            row = self._assembly_row(entity_id)
            if not row:
                return
            if row.get("status") == schema.STATUS_ISSUED:
                store.reopen_assembly(entity_id)
            else:
                answer = QMessageBox.question(
                    self, "Issue assembly",
                    f"Mark '{row.get('name')}' issued and read-only?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
                store.issue_assembly(entity_id)
        self.refresh_tree()
        self._select_ref((kind, entity_id))

    def _assign_route_to_system(self, system_id: str):
        store = self._store()
        route_id = self._selected_route_id()
        if store is None or route_id is None:
            return
        store.assign_route_to_system(route_id, system_id)
        self.refresh_tree()
        self._select_ref((KIND_ROUTE, route_id))

    def _rename_route(self):
        store = self._store()
        route_id = self._selected_route_id()
        if store is None or route_id is None:
            return
        route = store.get_route(route_id)
        if not route:
            return
        name, ok = QInputDialog.getText(
            self, "Rename segment", "Segment name:", text=route.get("name") or "")
        if not ok or not name.strip():
            return
        route["name"] = name.strip()
        store.save_route(route)
        self.refresh_tree()
        self._select_ref((KIND_ROUTE, route_id))

    def _duplicate_assembly(self):
        assembly_id = self._selected_assembly_id()
        if assembly_id is None:
            return
        self.assembly_panel.select_assembly(assembly_id)
        self.assembly_panel._duplicate_assembly()
        self.refresh_tree()

    def _export_assembly_json(self):
        assembly_id = self._selected_assembly_id()
        if assembly_id is None:
            return
        self.assembly_panel.select_assembly(assembly_id)
        self.assembly_panel._export_catenary_clipboard()

    def _delete_selected(self):
        ref = self._current_ref()
        if not ref:
            return
        kind, entity_id = ref[0], ref[1]
        store = self._store()
        if store is None:
            return
        try:
            if kind == KIND_ROUTE:
                route = store.get_route(entity_id)
                answer = QMessageBox.question(
                    self, "Delete segment",
                    f"Delete segment '{(route or {}).get('name')}'?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
                store.delete_route(entity_id)
            elif kind == KIND_SYSTEM:
                system = next((s for s in store.list_systems()
                               if s.get("system_id") == entity_id), {})
                answer = QMessageBox.question(
                    self, "Delete system",
                    f"Delete system '{system.get('name') or 'System'}'?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
                store.delete_system(entity_id)
            elif kind == KIND_RPL:
                row = store.get_rpl(entity_id)
                if row and row.get("status") == schema.STATUS_ISSUED and not self._confirm_delete_issued("RPL"):
                    return
                answer = QMessageBox.question(
                    self, "Delete RPL",
                    f"Remove '{(row or {}).get('name')}' from the workbench and QGIS layer panel?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
                layer_names = self._rpl_project_layer_names(store, entity_id)
                fit_ids = {f.get("fit_id") for f in store.list_fits(rpl_id=entity_id)}
                assessment_ids = {a.get("assessment_id") for a in store.list_assessments(entity_id)}
                store.delete_rpl(entity_id)
                self._remove_project_layers(layer_names)
                self._clear_deleted_panel_state({entity_id}, fit_ids, assessment_ids)
            elif kind == KIND_ASSEMBLY:
                row = self._assembly_row(entity_id)
                if row and row.get("status") == schema.STATUS_ISSUED and not self._confirm_delete_issued("assembly"):
                    return
                answer = QMessageBox.question(
                    self, "Delete assembly",
                    f"Delete assembly '{(row or {}).get('name')}' and its fit layers?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
                layer_names = self._assembly_project_layer_names(store, entity_id)
                fit_ids = {f.get("fit_id") for f in store.list_fits(assembly_id=entity_id)}
                store.delete_assembly(entity_id)
                self._remove_project_layers(layer_names)
                self._clear_deleted_panel_state(set(), fit_ids, set())
            elif kind == KIND_FIT:
                fit = next((f for f in store.list_fits() if f.get("fit_id") == entity_id), None)
                layer_names = self._fit_project_layer_names(store, fit or {})
                store.delete_fit(entity_id)
                self._remove_project_layers(layer_names)
                self._clear_deleted_panel_state(set(), {entity_id}, set())
            elif kind == KIND_ASSESSMENT:
                assessment = store.get_assessment(entity_id) or {}
                layer_names = [assessment.get("ranges_layer")]
                store.delete_assessment(entity_id)
                self._remove_project_layers(layer_names)
                self._clear_deleted_panel_state(set(), set(), {entity_id})
        except Exception as exc:
            QMessageBox.warning(self, "Delete", str(exc))
            return
        self.refresh_tree()

    def _confirm_delete_issued(self, label: str) -> bool:
        answer = QMessageBox.question(
            self, f"Delete issued {label}",
            f"This {label} is issued. Delete it anyway?")
        return answer == QMessageBox.StandardButton.Yes

    # ---------------------------------------------------------- helpers --
    def _selected_rpl_id(self) -> Optional[str]:
        ref = self._current_ref()
        store = self._store()
        if not ref or store is None:
            return None
        kind, entity_id = ref[0], ref[1]
        if kind == KIND_RPL:
            return entity_id
        if kind == KIND_ROUTE:
            latest = store.latest_revision(entity_id)
            return latest.get("rpl_id") if latest else None
        if kind == KIND_FIT:
            fit = next((f for f in store.list_fits() if f.get("fit_id") == entity_id), None)
            return fit.get("rpl_id") if fit else None
        if kind == KIND_ASSESSMENT:
            row = store.get_assessment(entity_id)
            return row.get("rpl_id") if row else None
        return None

    def _selected_system_id(self) -> Optional[str]:
        ref = self._current_ref()
        store = self._store()
        if not ref or store is None:
            return None
        if ref[0] == KIND_SYSTEM:
            return ref[1]
        if ref[0] == KIND_GROUP and ref[1] == GROUP_UNASSIGNED_SEGMENTS:
            return ""
        route_id = self._selected_route_id()
        route = store.get_route(route_id) if route_id else None
        return route.get("system_id") if route else None

    def _selected_route_id(self) -> Optional[str]:
        ref = self._current_ref()
        store = self._store()
        if not ref or store is None:
            return None
        if ref[0] == KIND_ROUTE:
            return ref[1]
        rpl_id = self._selected_rpl_id()
        rpl = store.get_rpl(rpl_id) if rpl_id else None
        return rpl.get("route_id") if rpl else None

    def _selected_assembly_id(self) -> Optional[str]:
        ref = self._current_ref()
        if not ref:
            return None
        if ref[0] == KIND_ASSEMBLY:
            return ref[1]
        return None

    def _assembly_row(self, assembly_id: str) -> Optional[Dict]:
        store = self._store()
        if store is None:
            return None
        header, _items = store.get_assembly(assembly_id)
        return header

    def closeEvent(self, event):
        self.rpl_panel.closeEvent(event)
        super().closeEvent(event)

    def shutdown(self):
        """Detach from project signals before the plugin unloads the dock."""
        self._disconnect_project_layer_sync()


def _exec_menu(menu: QMenu, global_pos) -> None:
    exec_fn = getattr(menu, "exec", None) or getattr(menu, "exec_")
    exec_fn(global_pos)


def _workbench_layer_names_from_signal(args, gpkg_path: str):
    names = []
    project = QgsProject.instance()
    for item in _flatten_signal_args(args):
        layer = item if isinstance(item, QgsVectorLayer) else None
        if layer is None and isinstance(item, str):
            layer = project.mapLayer(item)
        if layer is None:
            continue
        name = _layer_name_from_source(layer.source(), gpkg_path)
        if name:
            names.append(name)
    return names


def _flatten_signal_args(args):
    for arg in args:
        if isinstance(arg, str):
            yield arg
        elif isinstance(arg, (list, tuple, set)):
            for item in arg:
                yield item
        else:
            yield arg


def _project_layer_ids_for_names(gpkg_path: str, layer_names) -> list:
    wanted = {name for name in (layer_names or []) if name}
    if not wanted:
        return []
    ids = []
    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        name = _layer_name_from_source(layer.source(), gpkg_path)
        if name in wanted:
            ids.append(layer.id())
    return ids


def _layer_name_from_source(source: str, gpkg_path: str) -> Optional[str]:
    from .project_layers import layer_name_from_source

    return layer_name_from_source(source, gpkg_path)
