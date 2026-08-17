# -*- coding: utf-8 -*-
"""Simple table/schematic overview pages for systems and cable segments."""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import schema
from .rpl_summary import rpl_summary
from .system_schematic import SegmentSchematicWidget, SystemSchematicWidget
from .system_topology import TopologyGraph


class SystemOverviewPanel(QWidget):
    importSegmentRequested = pyqtSignal(str)
    addNodeRequested = pyqtSignal(str)
    connectRequested = pyqtSignal(str)
    componentActivated = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._system_id = ""
        self._schematic_args = None
        layout = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(self.title)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.views = QTabWidget()
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Cable segment", "Latest RPL", "Assemblies", "RPL sections",
            "Route length", "Cable length", "Cable type", "Status",
        ])
        _configure_table(self.table)
        self.table.setToolTip("Double-click a cable segment to open its overview.")
        self.table.cellDoubleClicked.connect(self._segment_activated)
        self.views.addTab(self.table, "Table")
        self.schematic = SystemSchematicWidget()
        self.schematic.componentActivated.connect(self.componentActivated)
        self.views.addTab(self.schematic, "Schematic")
        self.views.currentChanged.connect(self._view_changed)
        layout.addWidget(self.views, 1)

        self.guidance = QLabel()
        self.guidance.setWordWrap(True)
        self.guidance.setTextFormat(Qt.TextFormat.RichText)
        self.guidance.setStyleSheet(
            "QLabel { background:#f3f6f8; border:1px solid #d7dde2; padding:10px; }")
        layout.addWidget(self.guidance)
        actions = QHBoxLayout()
        for label, signal in (
            ("Import cable segment...", self.importSegmentRequested),
            ("Add BU / node...", self.addNodeRequested),
            ("Connect endpoints...", self.connectRequested),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, s=signal: s.emit(self._system_id))
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

    def load_system(self, store, system_id: str) -> None:
        self._system_id = system_id or ""
        self.table.setRowCount(0)
        if store is None or not system_id:
            self.title.setText("Cable system")
            self.summary.setText("No cable system selected.")
            self.guidance.clear()
            self._schematic_args = None
            self.schematic.set_system(None, "")
            return
        systems = store.list_systems()
        routes_all = store.list_routes()
        rpls_all = store.list_rpls()
        fits_all = store.list_fits()
        assessments_all = store.list_assessments()
        system = next((row for row in systems
                       if row.get("system_id") == system_id), {})
        routes = [row for row in routes_all
                  if row.get("system_id") == system_id]
        graph = TopologyGraph.from_store(store)
        members = [c for c in graph.components.values()
                   if c.get("system_id") == system_id]
        nodes = [c for c in members if c.get("kind") == "node"]
        route_ids = {route.get("route_id") for route in routes}
        revisions = [rpl for rpl in rpls_all if rpl.get("route_id") in route_ids]
        latest_by_route = {}
        for rpl in revisions:
            route_id = rpl.get("route_id") or ""
            current = latest_by_route.get(route_id)
            if current is None or _revision_key(rpl) >= _revision_key(current):
                latest_by_route[route_id] = rpl
        latest = list(latest_by_route.values())
        issued = sum(1 for r in latest if r.get("status") == schema.STATUS_ISSUED)
        member_ids = {c.get("component_id") for c in members}
        open_count = len([p for p in graph.open_ports()
                          if p.get("component_id") in member_ids])
        latest_ids = {rpl.get("rpl_id") for rpl in latest}
        fits = sum(1 for row in fits_all if row.get("rpl_id") in latest_ids)
        assessments = sum(1 for row in assessments_all if row.get("rpl_id") in latest_ids)

        summaries = {}
        makeups_by_route = {}
        for makeup in store.list_makeups():
            route_id = makeup.get("route_id") or ""
            current = makeups_by_route.get(route_id)
            if current is None or _revision_key(makeup) >= _revision_key(current):
                makeups_by_route[route_id] = makeup
        makeup_counts_by_id = {}
        for item in store.read_table(schema.TABLE_MAKEUP_ITEM):
            if item.get("kind") == "assembly":
                makeup_id = item.get("makeup_id") or ""
                makeup_counts_by_id[makeup_id] = makeup_counts_by_id.get(makeup_id, 0) + 1
        makeup_counts = {}
        for route in routes:
            route_id = route.get("route_id") or ""
            rpl = latest_by_route.get(route_id)
            summaries[route_id] = rpl_summary(store, rpl)
            makeup = makeups_by_route.get(route_id) or {}
            makeup_counts[route_id] = makeup_counts_by_id.get(
                makeup.get("makeup_id") or "", 0)
        route_lengths = [summary.route_length_km for summary in summaries.values()
                         if summary.route_length_km is not None]
        cable_lengths = [summary.cable_length_km for summary in summaries.values()
                         if summary.cable_length_km is not None]

        name = system.get("name") or "Cable system"
        self.title.setText(name)
        length_bits = []
        if route_lengths:
            length_bits.append(f"{sum(route_lengths):.3f} km route")
        if cable_lengths:
            length_bits.append(f"{sum(cable_lengths):.3f} km cable")
        suffix = (" · " + " · ".join(length_bits)) if length_bits else ""
        self.summary.setText(
            f"{_count(len(routes), 'cable segment')} · {_count(len(nodes), 'node')} · "
            f"{_count(len(revisions), 'RPL revision')} · "
            f"{_count(open_count, 'open endpoint')}{suffix}")
        self._populate_segments(routes, latest_by_route, summaries, makeup_counts)

        core_checks = [
            (bool(routes), "Add at least one cable segment"),
            (len(latest) == len(routes) and bool(routes),
             "Import an RPL revision for every cable segment"),
            (open_count == 0 and bool(members), "Review and connect all intended endpoints"),
        ]
        first_pending = next((text for done, text in core_checks if not done), None)
        if first_pending is None:
            first_pending = (
                "Review outputs and issue the approved RPL revisions"
                if issued != len(latest) else "System setup is complete")
        rows = "".join(
            f"<div style='margin:2px 0'>{'✓' if done else '○'} {text}</div>"
            for done, text in core_checks)
        rows += (
            f"<div style='margin-top:6px'>Optional as required: "
            f"{_count(fits, 'assembly fit')}; {_count(assessments, 'assessment')}.</div>"
            f"<div>{_count(issued, 'issued latest revision')}.</div>")
        self.guidance.setText(f"<b>Next suggested action:</b> {first_pending}<br><br>{rows}")
        self._schematic_args = (store, system_id, graph, rpls_all)
        self._render_schematic_if_visible()

    def _view_changed(self, _index):
        self._render_schematic_if_visible()

    def _render_schematic_if_visible(self):
        if self.views.currentWidget() is not self.schematic or self._schematic_args is None:
            return
        store, system_id, graph, rpls = self._schematic_args
        self._schematic_args = None
        self.schematic.set_system(store, system_id, graph=graph, rpls=rpls)

    def _populate_segments(self, routes, latest_by_route, summaries, makeup_counts):
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(routes))
            for row_index, route in enumerate(sorted(
                    routes, key=lambda row: (row.get("name") or "").lower())):
                route_id = route.get("route_id") or ""
                rpl = latest_by_route.get(route_id) or {}
                summary = summaries.get(route_id)
                values = [
                    route.get("name") or "Cable segment",
                    rpl.get("rev_label") or ("Not imported" if not rpl else "Unlabelled"),
                    str(makeup_counts.get(route_id, 0)),
                    str(summary.section_count) if rpl else "",
                    _km(summary.route_length_km) if rpl else "",
                    _km(summary.cable_length_km) if rpl else "",
                    summary.cable_type if rpl else "",
                    rpl.get("status") or ("Missing RPL" if not rpl else schema.STATUS_DRAFT),
                ]
                _set_row(self.table, row_index, values, route_id)
        finally:
            self.table.setUpdatesEnabled(True)

    def _segment_activated(self, row, _column):
        item = self.table.item(row, 0)
        route_id = item.data(Qt.ItemDataRole.UserRole) if item else ""
        if route_id:
            self.componentActivated.emit("route", str(route_id))


class SegmentOverviewPanel(QWidget):
    openRevisionRequested = pyqtSignal(str)
    importRevisionRequested = pyqtSignal(str)
    extractAssemblyRequested = pyqtSignal(str)
    fitAssemblyRequested = pyqtSignal(str)
    assessmentRequested = pyqtSignal(str)
    topologyRequested = pyqtSignal(str)
    addAssemblyRequested = pyqtSignal(str)
    createAssemblyRequested = pyqtSignal(str)
    removeMakeupItemRequested = pyqtSignal(str, str)
    openAssemblyRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._route_id = ""
        self._latest_rpl_id = ""
        self._schematic_args = None
        self._makeup_total_m = 0.0
        self._makeup_placement_count = 0
        layout = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(self.title)
        self.endpoint_summary = QLabel()
        self.endpoint_summary.setWordWrap(True)
        layout.addWidget(self.endpoint_summary)

        self.views = QTabWidget()
        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_tables = QTabWidget()
        makeup_page = QWidget()
        makeup_layout = QVBoxLayout(makeup_page)
        makeup_layout.setContentsMargins(0, 0, 0, 0)
        self.makeup_summary = QLabel()
        self.makeup_summary.setWordWrap(True)
        makeup_layout.addWidget(self.makeup_summary)
        self.makeup_table = QTableWidget(0, 6)
        self.makeup_table.setHorizontalHeaderLabels([
            "Type", "Assembly / joint", "Length", "Cable type", "Direction", "Notes",
        ])
        _configure_table(self.makeup_table)
        self.makeup_table.itemSelectionChanged.connect(self._makeup_selection_changed)
        self.makeup_table.cellDoubleClicked.connect(self._makeup_activated)
        makeup_layout.addWidget(self.makeup_table)
        makeup_actions = QHBoxLayout()
        self.add_assembly_btn = QPushButton("Add existing assembly...")
        self.add_assembly_btn.clicked.connect(
            lambda: self.addAssemblyRequested.emit(self._route_id))
        self.create_assembly_btn = QPushButton("Create assembly...")
        self.create_assembly_btn.clicked.connect(
            lambda: self.createAssemblyRequested.emit(self._route_id))
        self.remove_makeup_btn = QPushButton("Remove from make-up")
        self.remove_makeup_btn.clicked.connect(self._remove_makeup_item)
        self.remove_makeup_btn.setEnabled(False)
        makeup_actions.addWidget(self.add_assembly_btn)
        makeup_actions.addWidget(self.create_assembly_btn)
        makeup_actions.addWidget(self.remove_makeup_btn)
        makeup_actions.addStretch()
        makeup_layout.addLayout(makeup_actions)
        self.detail_tables.addTab(makeup_page, "Cable make-up")
        self.positions_table = QTableWidget(0, 7)
        self.positions_table.setHorizontalHeaderLabels([
            "Position", "KP", "Cable distance", "Latitude", "Longitude",
            "Depth", "Event",
        ])
        _configure_table(self.positions_table)
        self.positions_table.setToolTip("Double-click a position to open the latest RPL revision.")
        self.positions_table.cellDoubleClicked.connect(self._latest_activated)
        self.detail_tables.addTab(self.positions_table, "Positions")
        self.sections_table = QTableWidget(0, 9)
        self.sections_table.setHorizontalHeaderLabels([
            "From event", "To event", "Start KP", "End KP", "Route length",
            "Cable length", "Slack", "Cable type", "Legs",
        ])
        _configure_table(self.sections_table)
        self.sections_table.setToolTip("Double-click an RPL section to open the latest revision.")
        self.sections_table.cellDoubleClicked.connect(self._latest_activated)
        self.detail_tables.addTab(self.sections_table, "RPL sections")
        self.revisions = QTreeWidget()
        self.revisions.setHeaderLabels(["RPL revision", "Kind", "Status"])
        self.revisions.itemDoubleClicked.connect(self._revision_activated)
        self.detail_tables.addTab(self.revisions, "RPL revisions")
        table_layout.addWidget(self.detail_tables)
        self.views.addTab(table_page, "Table")
        self.schematic = SegmentSchematicWidget()
        self.schematic.componentActivated.connect(self._schematic_activated)
        self.views.addTab(self.schematic, "Schematic")
        self.views.currentChanged.connect(self._view_changed)
        layout.addWidget(self.views, 1)

        self.guidance = QLabel()
        self.guidance.setWordWrap(True)
        self.guidance.setStyleSheet(
            "QLabel { background:#f3f6f8; border:1px solid #d7dde2; padding:10px; }")
        layout.addWidget(self.guidance)
        actions = QHBoxLayout()
        buttons = [
            ("Open latest RPL", lambda: self.openRevisionRequested.emit(self._latest_rpl_id)),
            ("Import revision...", lambda: self.importRevisionRequested.emit(self._route_id)),
            ("Extract assembly from RPL...",
             lambda: self.extractAssemblyRequested.emit(self._latest_rpl_id)),
            ("Fit assembly...", lambda: self.fitAssemblyRequested.emit(self._latest_rpl_id)),
            ("New assessment...", lambda: self.assessmentRequested.emit(self._latest_rpl_id)),
            ("Cable-system topology", lambda: self.topologyRequested.emit(self._route_id)),
        ]
        self._needs_revision_buttons = []
        for label, slot in buttons:
            button = QPushButton(label)
            button.clicked.connect(slot)
            actions.addWidget(button)
            if label not in ("Import revision...", "Cable-system topology"):
                self._needs_revision_buttons.append(button)
        actions.addStretch()
        layout.addLayout(actions)

    def load_segment(self, store, route_id: str) -> None:
        self._route_id = route_id or ""
        route = store.get_route(route_id) if store and route_id else None
        self.revisions.clear()
        self.makeup_table.setRowCount(0)
        self.makeup_summary.clear()
        self._makeup_total_m = 0.0
        self._makeup_placement_count = 0
        self.positions_table.setRowCount(0)
        self.sections_table.setRowCount(0)
        if not route:
            self.title.setText("Cable segment")
            self.endpoint_summary.setText("No cable segment selected.")
            self.guidance.clear()
            self._schematic_args = None
            self.schematic.set_segment(None, None, "")
            return
        self.title.setText(route.get("name") or "Cable segment")
        self._populate_makeup(store, route_id)
        self._schematic_args = (store, route_id, route.get("name") or "Cable segment")
        self._render_schematic_if_visible()
        rows = [row for row in store.list_rpls() if row.get("route_id") == route_id]
        rows.sort(key=_revision_key)
        latest = rows[-1] if rows else None
        self._latest_rpl_id = latest.get("rpl_id") if latest else ""
        for row in reversed(rows):
            item = QTreeWidgetItem([
                row.get("rev_label") or "Unlabelled",
                (row.get("kind") or "").replace("_", " "),
                row.get("status") or schema.STATUS_DRAFT,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, row.get("rpl_id") or "")
            self.revisions.addTopLevelItem(item)
        for button in self._needs_revision_buttons:
            button.setEnabled(bool(latest))
        if latest is None:
            self.endpoint_summary.setText("No RPL revision has been imported yet.")
            self.guidance.setText("Next suggested action: import the first RPL revision.")
            return
        summary = rpl_summary(store, latest)
        if summary.cable_length_km is not None and self._makeup_total_m > 0.0:
            delta_m = self._makeup_total_m - summary.cable_length_km * 1000.0
            if abs(delta_m) <= 1.0:
                coverage = "Make-up matches the latest RPL cable length."
            elif delta_m < 0.0:
                coverage = f"Make-up is {_length_m(-delta_m)} short of the latest RPL."
            else:
                coverage = f"Make-up exceeds the latest RPL by {_length_m(delta_m)}."
            self.makeup_summary.setText(
                self.makeup_summary.text() + f" · {coverage}")
        start = _endpoint_text(summary, "A")
        end = _endpoint_text(summary, "B")
        self.endpoint_summary.setText(
            f"{start}\n{end}\n{_count(summary.section_count, 'event-to-event RPL section')}"
            f" · {_km(summary.route_length_km)} route · {_km(summary.cable_length_km)} cable")
        self._populate_positions(summary)
        self._populate_sections(summary)
        fits = store.list_fits(rpl_id=latest.get("rpl_id"))
        assessments = store.list_assessments(latest.get("rpl_id"))
        if self._makeup_placement_count == 0:
            next_action = "Add the first assembly to define this segment's cable make-up."
        elif latest.get("status") == schema.STATUS_ISSUED:
            next_action = "Revision issued; create a new revision for further changes."
        elif not fits and not assessments:
            next_action = (
                "Review the RPL sections. Then fit an assembly or create an assessment "
                "when either output is required.")
        elif not fits:
            next_action = "Review the assessment; fit an assembly if this segment requires one."
        elif not assessments:
            next_action = "Review the assembly fit; create an assessment if one is required."
        else:
            next_action = "Review current outputs and issue the approved revision."
        self.guidance.setText(f"Next suggested action: {next_action}")

    def _populate_makeup(self, store, route_id):
        header, items = store.current_makeup(route_id)
        assemblies = {row.get("assembly_id"): row for row in store.list_assemblies()}
        sections_by_assembly = {}
        for section in store.read_table(schema.TABLE_ASSEMBLY_ITEM):
            sections_by_assembly.setdefault(
                section.get("assembly_id") or "", []).append(section)
        placement_count = sum(1 for item in items if item.get("kind") == "assembly")
        self._makeup_placement_count = placement_count
        joint_count = sum(1 for item in items if item.get("kind") == "joint")
        total_m = 0.0
        rows = []
        for item in items:
            kind = item.get("kind") or "assembly"
            if kind == "assembly":
                assembly = assemblies.get(item.get("assembly_id")) or {}
                section_rows = sections_by_assembly.get(
                    item.get("assembly_id") or "", [])
                full_length = float(assembly.get("total_cable_len_m") or 0.0)
                start_m = item.get("use_start_m")
                end_m = item.get("use_end_m")
                used_length = full_length
                if start_m is not None or end_m is not None:
                    used_length = max(
                        0.0, float(end_m if end_m is not None else full_length)
                        - float(start_m or 0.0))
                total_m += used_length
                cable_types = []
                for section in section_rows:
                    value = str(section.get("cable_type") or "").strip()
                    if value and value not in cable_types:
                        cable_types.append(value)
                values = [
                    "Assembly", assembly.get("name") or item.get("name") or "Assembly",
                    _length_m(used_length), " / ".join(cable_types),
                    "A → B" if int(item.get("direction") or 1) >= 0 else "B → A",
                    item.get("notes") or "",
                ]
            else:
                values = [
                    "Joint", item.get("name") or "Joint", "", "", "",
                    item.get("notes") or "",
                ]
            rows.append((item, values))
        self._makeup_total_m = total_m
        self.makeup_table.setUpdatesEnabled(False)
        try:
            self.makeup_table.setRowCount(len(rows))
            for row_index, (item, values) in enumerate(rows):
                _set_row(
                    self.makeup_table, row_index, values,
                    item.get("makeup_item_id") or "")
                first = self.makeup_table.item(row_index, 0)
                if first is not None and item.get("assembly_id"):
                    first.setData(int(Qt.ItemDataRole.UserRole) + 1,
                                  item.get("assembly_id"))
        finally:
            self.makeup_table.setUpdatesEnabled(True)
        if header is None:
            self.makeup_summary.setText(
                "No cable make-up yet. Add one or more assemblies in installation order.")
        else:
            review = f" · {header.get('notes')}" if header.get("notes") else ""
            self.makeup_summary.setText(
                f"{placement_count} assembl{'y' if placement_count == 1 else 'ies'} · "
                f"{joint_count} joint{'s' if joint_count != 1 else ''} · "
                f"{_length_m(total_m)}{review}")

    def _selected_makeup_item_id(self):
        row = self.makeup_table.currentRow()
        item = self.makeup_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _makeup_selection_changed(self):
        self.remove_makeup_btn.setEnabled(bool(self._selected_makeup_item_id()))

    def _remove_makeup_item(self):
        item_id = self._selected_makeup_item_id()
        if item_id:
            self.removeMakeupItemRequested.emit(self._route_id, item_id)

    def _makeup_activated(self, row, _column):
        item = self.makeup_table.item(row, 0)
        if item and item.text() == "Assembly":
            assembly_id = item.data(int(Qt.ItemDataRole.UserRole) + 1)
            if assembly_id:
                self.openAssemblyRequested.emit(str(assembly_id))

    def _schematic_activated(self, kind, subject_id):
        if kind == "assembly" and subject_id:
            self.openAssemblyRequested.emit(subject_id)

    def _view_changed(self, _index):
        self._render_schematic_if_visible()

    def _render_schematic_if_visible(self):
        if self.views.currentWidget() is not self.schematic or self._schematic_args is None:
            return
        args = self._schematic_args
        self._schematic_args = None
        self.schematic.set_makeup(*args)

    def _populate_positions(self, summary):
        self.positions_table.setUpdatesEnabled(False)
        try:
            self.positions_table.setRowCount(len(summary.positions))
            for row, point in enumerate(summary.positions):
                _set_row(self.positions_table, row, [
                    point.pos, _km(point.kp_km), _km(point.cable_km),
                    _number(point.latitude, 6), _number(point.longitude, 6),
                    _number(point.depth_m, 1, " m"), point.event,
                ])
        finally:
            self.positions_table.setUpdatesEnabled(True)

    def _populate_sections(self, summary):
        self.sections_table.setUpdatesEnabled(False)
        try:
            self.sections_table.setRowCount(len(summary.sections))
            for row, section in enumerate(summary.sections):
                slack = None
                if section.route_length_km and section.cable_length_km is not None:
                    slack = (section.cable_length_km / section.route_length_km - 1.0) * 100.0
                _set_row(self.sections_table, row, [
                    section.start_event or "(segment start)",
                    section.end_event or "(segment end)",
                    _km(section.start_kp_km), _km(section.end_kp_km),
                    _km(section.route_length_km), _km(section.cable_length_km),
                    _number(slack, 3, "%"), section.cable_type,
                    section.leg_count,
                ])
        finally:
            self.sections_table.setUpdatesEnabled(True)

    def _latest_activated(self, _row, _column):
        if self._latest_rpl_id:
            self.openRevisionRequested.emit(self._latest_rpl_id)

    def _revision_activated(self, item, _column):
        rpl_id = item.data(0, Qt.ItemDataRole.UserRole)
        if rpl_id:
            self.openRevisionRequested.emit(rpl_id)


def _configure_table(table):
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)


def _set_row(table, row, values, user_data=""):
    for column, value in enumerate(values):
        item = QTableWidgetItem("" if value is None else str(value))
        if column == 0 and user_data:
            item.setData(Qt.ItemDataRole.UserRole, user_data)
        table.setItem(row, column, item)


def _endpoint_text(summary, role: str) -> str:
    start = role == "A"
    bits = ["Start (A)" if start else "End (B)"]
    kp = summary.start_kp_km if start else summary.end_kp_km
    pos = summary.start_pos if start else summary.end_pos
    event = summary.start_event if start else summary.end_event
    if kp is not None:
        bits.append(f"KP {kp:.3f}")
    if pos not in (None, ""):
        bits.append(f"Pos {pos}")
    if event:
        bits.append(f"“{event}”")
    return " · ".join(bits)


def _km(value) -> str:
    return "" if value is None else f"{float(value):.3f} km"


def _number(value, decimals, suffix="") -> str:
    return "" if value is None else f"{float(value):.{decimals}f}{suffix}"


def _length_m(value) -> str:
    value = float(value or 0.0)
    return f"{value / 1000.0:.3f} km" if value >= 1000.0 else f"{value:.1f} m"


def _endpoints(summary) -> str:
    start = summary.start_event or "Start (A)"
    end = summary.end_event or "End (B)"
    return f"{start} → {end}"


def _count(value: int, singular: str) -> str:
    return f"{value} {singular}" + ("" if value == 1 else "s")


def _revision_key(row):
    return row.get("created_utc") or "", row.get("name") or ""
