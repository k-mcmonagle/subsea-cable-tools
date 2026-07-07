# -*- coding: utf-8 -*-
"""Cable Route Workbench — the unified dock.

One entry point for the whole workbench: an entity tree on the left
(Assemblies and RPLs, with fits shown as children under both — an assembly
lists the RPLs it is fitted onto, an RPL lists its fitted assembly), and a
detail panel on the right that switches with the selection:

- an RPL          -> the RPL panel (tables, map editing, systems, fit action)
- an assembly     -> the assembly panel (SLD + items)
- a fit           -> the assembly panel with the KP axis driven by that fit
                     (cable distance along the bottom, route KP along the top)

The two panels are the full former RPL Manager / Assembly Manager, embedded
with their own browser columns hidden; all editing logic lives in them.
"""

from __future__ import annotations

from typing import Dict, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .assembly_manager_dock import AssemblyManagerPanel
from .assessment_panel import AssessmentPanel
from .rpl_manager_dock import RplManagerPanel

KIND_ASSEMBLY = "assembly"
KIND_RPL = "rpl"
KIND_FIT = "fit"
KIND_ASSESSMENT = "assessment"
KIND_GROUP = "group"


class WorkbenchDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Cable Route Workbench", parent)
        self.iface = iface
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

        # ---- left: entity tree
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Workbench", "Detail"])
        self.tree.setColumnWidth(0, 200)
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        left_layout.addWidget(self.tree)

        buttons = QHBoxLayout()
        new_btn = QPushButton("New ▾")
        new_menu = QMenu(new_btn)
        new_menu.addAction("Assembly…", self._new_assembly)
        new_menu.addAction("Register RPL…", self._register_rpl)
        new_menu.addAction("Assessment…", self._new_assessment)
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

        # ---- right: stacked detail panels
        self.stack = QStackedWidget()
        self.placeholder = QLabel(
            "Select an assembly, an RPL, or a fit in the tree.\n\n"
            "New ▾ creates an assembly or registers an imported RPL pair.")
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
        splitter.setSizes([260, 900])
        self.setWidget(container)

        # keep the tree in sync with panel-driven changes
        self.rpl_panel.rpls_changed.connect(self.refresh_tree)
        self.rpl_panel.fits_changed.connect(self.refresh_tree)
        self.rpl_panel.extract_assembly_requested.connect(self._extract_assembly_from_rpl)
        self.assembly_panel.assemblies_changed.connect(self.refresh_tree)
        self.assembly_panel.assembly_saved.connect(lambda _id: self._refresh_labels())
        self.assessment_panel.assessments_changed.connect(self.refresh_tree)

        self.refresh_tree()

    # ------------------------------------------------------------- tree --
    def _store(self):
        # both panels share the same gpkg; the RPL panel owns the freshest handle
        self.rpl_panel._open_store()
        return self.rpl_panel.store

    def refresh_tree(self):
        current = self._current_ref()
        self.tree.blockSignals(True)
        self.tree.clear()

        store = self._store()
        assemblies_root = QTreeWidgetItem(["Assemblies", ""])
        assemblies_root.setData(0, Qt.ItemDataRole.UserRole, (KIND_GROUP, "assemblies"))
        rpls_root = QTreeWidgetItem(["RPLs", ""])
        rpls_root.setData(0, Qt.ItemDataRole.UserRole, (KIND_GROUP, "rpls"))
        self.tree.addTopLevelItem(assemblies_root)
        self.tree.addTopLevelItem(rpls_root)

        if store is not None and store.exists():
            assemblies = {r.get("assembly_id"): r for r in store.list_assemblies()}
            rpls = {r.get("rpl_id"): r for r in store.list_rpls()}
            fits = store.list_fits()

            for assembly_id, row in assemblies.items():
                total_km = (row.get("total_cable_len_m") or 0.0) / 1000.0
                item = QTreeWidgetItem(
                    [row.get("name") or "?", f"{row.get('kind')}, {total_km:.2f} km"])
                item.setData(0, Qt.ItemDataRole.UserRole, (KIND_ASSEMBLY, assembly_id))
                for fit in fits:
                    if fit.get("assembly_id") == assembly_id:
                        rpl = rpls.get(fit.get("rpl_id"), {})
                        fit_item = QTreeWidgetItem(
                            [f"⇘ fitted on {rpl.get('name') or '?'}",
                             f"anchor KP {float(fit.get('anchor_kp_km') or 0):.3f}"])
                        fit_item.setData(0, Qt.ItemDataRole.UserRole,
                                         (KIND_FIT, fit.get("fit_id")))
                        item.addChild(fit_item)
                assemblies_root.addChild(item)

            assessments = store.list_assessments()
            for rpl_id, row in rpls.items():
                item = QTreeWidgetItem([row.get("name") or "?", row.get("kind") or ""])
                item.setData(0, Qt.ItemDataRole.UserRole, (KIND_RPL, rpl_id))
                for fit in fits:
                    if fit.get("rpl_id") == rpl_id:
                        assembly = assemblies.get(fit.get("assembly_id"), {})
                        fit_item = QTreeWidgetItem(
                            [f"⇘ assembly {assembly.get('name') or '?'}",
                             f"anchor KP {float(fit.get('anchor_kp_km') or 0):.3f}"])
                        fit_item.setData(0, Qt.ItemDataRole.UserRole,
                                         (KIND_FIT, fit.get("fit_id")))
                        item.addChild(fit_item)
                for assessment in assessments:
                    if assessment.get("rpl_id") == rpl_id:
                        status = assessment.get("status") or "not run"
                        a_item = QTreeWidgetItem(
                            [f"▤ {assessment.get('name') or '?'}", status])
                        a_item.setData(0, Qt.ItemDataRole.UserRole,
                                       (KIND_ASSESSMENT, assessment.get("assessment_id")))
                        item.addChild(a_item)
                rpls_root.addChild(item)

        assemblies_root.setExpanded(True)
        rpls_root.setExpanded(True)
        self.tree.blockSignals(False)

        # keep the panels' hidden lists current, then restore selection
        self.rpl_panel.refresh_rpl_list()
        self.assembly_panel.refresh_assembly_list()
        if current is not None:
            self._select_ref(current)

    def _refresh_labels(self):
        """Light refresh of tree labels after in-place assembly edits."""
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
        if not ref or ref[0] == KIND_GROUP:
            self.stack.setCurrentWidget(self.placeholder)
            return
        kind, entity_id = ref[0], ref[1]

        if kind == KIND_RPL:
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
                # keep the RPL panel pointed at the fitted route so map
                # actions (zoom, edit) relate to the same fit
                self.rpl_panel.select_rpl(fit_row.get("rpl_id") or "")
            self.stack.setCurrentWidget(self.assembly_panel)
        elif kind == KIND_ASSESSMENT:
            store = self._store()
            row = store.get_assessment(entity_id) if store else None
            if row is not None:
                self.assessment_panel.load_assessment(store, row)
                self.stack.setCurrentWidget(self.assessment_panel)

    # ---------------------------------------------------------- actions --
    def _new_assembly(self):
        self.stack.setCurrentWidget(self.assembly_panel)
        self.assembly_panel._new_assembly()
        self.refresh_tree()

    def _extract_assembly_from_rpl(self, rpl_id: str):
        """'Create assembly…' on an RPL: extract, then fit it straight back on.

        The fit anchors at the route start (the assembly came from this RPL,
        so cable distance 0 is the first position), giving body-landing and
        section layers plus the dual cable/KP axis immediately.
        """
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

    def _register_rpl(self):
        self.stack.setCurrentWidget(self.rpl_panel)
        self.rpl_panel._run_register_algorithm()
        self.refresh_tree()

    def _new_assessment(self):
        """Create a new assessment for the RPL implied by the current selection."""
        rpl_id = self._selected_rpl_id()
        if rpl_id is None:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "New assessment",
                "Select an RPL (or one of its items) first, then New ▾ → Assessment.")
            return
        store = self._store()
        if store is None:
            return
        self.assessment_panel.new_assessment(store, rpl_id)
        self.stack.setCurrentWidget(self.assessment_panel)
        self.refresh_tree()

    def _selected_rpl_id(self) -> Optional[str]:
        """The RPL id for the current selection (an RPL, or a fit/assessment child)."""
        ref = self._current_ref()
        store = self._store()
        if not ref or store is None:
            return None
        kind, entity_id = ref[0], ref[1]
        if kind == KIND_RPL:
            return entity_id
        if kind == KIND_FIT:
            fit = next((f for f in store.list_fits() if f.get("fit_id") == entity_id), None)
            return fit.get("rpl_id") if fit else None
        if kind == KIND_ASSESSMENT:
            row = store.get_assessment(entity_id)
            return row.get("rpl_id") if row else None
        return None

    def _delete_selected(self):
        ref = self._current_ref()
        if not ref:
            return
        kind, entity_id = ref[0], ref[1]
        store = self._store()
        if kind == KIND_RPL:
            self.rpl_panel.select_rpl(entity_id)
            self.rpl_panel._delete_current()
        elif kind == KIND_ASSEMBLY:
            self.assembly_panel.select_assembly(entity_id)
            self.assembly_panel._delete_assembly()
        elif kind == KIND_FIT and store is not None:
            store.delete_fit(entity_id)
        elif kind == KIND_ASSESSMENT and store is not None:
            store.delete_assessment(entity_id)
        self.refresh_tree()

    def closeEvent(self, event):
        self.rpl_panel.closeEvent(event)
        super().closeEvent(event)
