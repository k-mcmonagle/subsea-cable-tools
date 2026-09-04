# -*- coding: utf-8 -*-
"""Dialogs for building cable-system topology: nodes, connections, suggestions.

Shared by the System overview (schematic + endpoints table) and the RPL
editor's Cable systems tab so both surfaces behave identically.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QSpinBox, QVBoxLayout,
)

from ..qgis_compat import DIALOG_ACCEPTED, qt_exec
from . import schema
from .rpl_summary import rpl_summary
from .system_topology import (
    NODE_TYPES, TopologyGraph, apply_proposal, default_port_labels,
    describe_proposal, endpoint_event, endpoint_label, is_segment_component,
    suggest_connections,
)


def open_endpoint_choices(store, graph: TopologyGraph, system_id: str = "",
                          exclude_component: str = "",
                          same_system_only: bool = True) -> List[Dict]:
    """Open ports as ``{"port_id", "component_id", "label", "in_system"}`` rows."""
    latest: Dict[str, Dict] = {}
    for rpl in store.list_rpls():
        route_id = rpl.get("route_id") or ""
        current = latest.get(route_id)
        if current is None or schema.revision_sort_key(rpl) >= schema.revision_sort_key(current):
            latest[route_id] = rpl
    out = []
    for port in graph.open_ports():
        component = graph.components.get(port.get("component_id")) or {}
        if not component or component.get("component_id") == exclude_component:
            continue
        in_system = (component.get("system_id") or "") == (system_id or "")
        if same_system_only and system_id and not in_system:
            continue
        summary = None
        if is_segment_component(component):
            summary = rpl_summary(store, latest.get(component.get("subject_id") or ""))
        out.append({
            "port_id": port["port_id"],
            "component_id": component.get("component_id") or "",
            "label": endpoint_label(component, port, summary, with_component=True),
            "in_system": in_system,
        })
    out.sort(key=lambda row: (not row["in_system"], row["label"].lower()))
    return out


def next_node_name(store, node_type: str, system_id: str = "") -> str:
    """``BU-1``, ``BMH-2`` … continuing the numbering used in the system."""
    prefix = {"bu": "BU", "bmh": "BMH", "joint": "JT"}.get(node_type or "", "Node")
    import re

    highest = 0
    for component in store.list_components():
        if component.get("kind") != "node":
            continue
        if system_id and (component.get("system_id") or "") not in ("", system_id):
            continue
        match = re.match(rf"^{re.escape(prefix)}\s*-?\s*(\d+)", str(component.get("name") or ""),
                         re.IGNORECASE)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}-{highest + 1}"


class NodeDialog(QDialog):
    """Create a BU / BMH / joint node, optionally connected to one endpoint."""

    def __init__(self, store, system_id: str = "", node_type: str = "bu",
                 connect_port_id: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add cable-system node")
        self._store = store
        self._system_id = system_id or ""
        self._graph = TopologyGraph.from_store(store)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.type_combo = QComboBox()
        for code, label in NODE_TYPES:
            self.type_combo.addItem(label, code)
        index = max(0, self.type_combo.findData(node_type or "bu"))
        self.type_combo.setCurrentIndex(index)
        form.addRow("Node type:", self.type_combo)
        self.name_edit = QLineEdit()
        form.addRow("Name:", self.name_edit)
        self.branch_spin = QSpinBox()
        self.branch_spin.setRange(1, 12)
        self.branch_spin.setValue(2)
        self.branch_spin.setToolTip(
            "Branch ports on the BU (plus one trunk). More ports can be added later.")
        self.branch_label = QLabel("Branches:")
        form.addRow(self.branch_label, self.branch_spin)
        self.port_summary = QLabel()
        self.port_summary.setStyleSheet("color:#52606d;")
        form.addRow("Ports:", self.port_summary)
        self.connect_combo = QComboBox()
        self.connect_combo.addItem("(leave unconnected)", "")
        choices = open_endpoint_choices(store, self._graph, self._system_id,
                                        same_system_only=False)
        for choice in choices:
            text = choice["label"] if choice["in_system"] else choice["label"] + "  (other system)"
            self.connect_combo.addItem(text, choice["port_id"])
        if connect_port_id:
            found = self.connect_combo.findData(connect_port_id)
            if found >= 0:
                self.connect_combo.setCurrentIndex(found)
        form.addRow("Connect to endpoint:", self.connect_combo)
        layout.addLayout(form)
        hint = QLabel(
            "The node joins the selected endpoint on its first open port "
            "(a BU's trunk). Connect the remaining ports from the schematic: "
            "click an open endpoint, then the port.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#52606d;")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.type_combo.currentIndexChanged.connect(self._type_changed)
        self.branch_spin.valueChanged.connect(self._refresh_ports)
        self._type_changed()
        self.resize(460, self.sizeHint().height())

    def node_type(self) -> str:
        return str(self.type_combo.currentData() or "other")

    def _type_changed(self, *_args):
        node_type = self.node_type()
        is_bu = node_type == "bu"
        self.branch_spin.setVisible(is_bu)
        self.branch_label.setVisible(is_bu)
        auto = next_node_name(self._store, node_type, self._system_id)
        if not self.name_edit.text().strip() or getattr(self, "_auto_name", "") == self.name_edit.text():
            self.name_edit.setText(auto)
            self._auto_name = auto
        self._refresh_ports()

    def _refresh_ports(self, *_args):
        labels = default_port_labels(self.node_type(), self.branch_spin.value())
        self.port_summary.setText(", ".join(labels))

    def port_labels(self) -> List[str]:
        return default_port_labels(self.node_type(), self.branch_spin.value())

    def connect_port_id(self) -> str:
        return str(self.connect_combo.currentData() or "")

    def accept(self):
        if not self.name_edit.text().strip():
            self.name_edit.setFocus()
            return
        super().accept()

    def create(self) -> Optional[str]:
        """Write the node (and the optional connection); returns component id."""
        component_id = self._store.save_component({
            "component_id": schema.new_id(), "kind": "node",
            "name": self.name_edit.text().strip(), "node_type": self.node_type(),
            "system_id": self._system_id,
        }, port_labels=self.port_labels())
        target = self.connect_port_id()
        if target:
            graph = TopologyGraph.from_store(self._store)
            node_ports = graph.open_ports(component_id)
            if node_ports:
                self._store.connect_ports(target, node_ports[0]["port_id"])
        return component_id


class ConnectEndpointsDialog(QDialog):
    """Pick two open endpoints side by side; the second list follows the first."""

    def __init__(self, store, system_id: str = "", first_port_id: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect endpoints")
        self._store = store
        self._system_id = system_id or ""
        self._graph = TopologyGraph.from_store(store)
        self._choices = open_endpoint_choices(store, self._graph, self._system_id,
                                              same_system_only=False)
        layout = QVBoxLayout(self)
        self.include_other = QCheckBox("Include open endpoints from other systems")
        self.include_other.setChecked(not self._system_id or not any(
            c["in_system"] for c in self._choices))
        layout.addWidget(self.include_other)
        lists = QHBoxLayout()
        first_box = QVBoxLayout()
        first_box.addWidget(QLabel("First endpoint"))
        self.first_list = QListWidget()
        first_box.addWidget(self.first_list)
        second_box = QVBoxLayout()
        second_box.addWidget(QLabel("Connect to"))
        self.second_list = QListWidget()
        second_box.addWidget(self.second_list)
        lists.addLayout(first_box)
        lists.addLayout(second_box)
        layout.addLayout(lists, 1)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            "QLabel { background:#f3f6f8; border:1px solid #d7dde2; padding:6px; }")
        layout.addWidget(self.preview)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.include_other.toggled.connect(self._fill_first)
        self.first_list.currentItemChanged.connect(self._fill_second)
        self.second_list.currentItemChanged.connect(self._update_preview)
        self._fill_first()
        if first_port_id:
            self.select_first(first_port_id)
        self.resize(760, 420)

    def _visible_choices(self) -> List[Dict]:
        if self.include_other.isChecked() or not self._system_id:
            return list(self._choices)
        return [c for c in self._choices if c["in_system"]]

    def _fill_first(self, *_args):
        current = self.first_port_id()
        self.first_list.clear()
        for choice in self._visible_choices():
            item = QListWidgetItem(choice["label"] + ("" if choice["in_system"] else "  (other system)"))
            item.setData(Qt.ItemDataRole.UserRole, choice["port_id"])
            item.setData(int(Qt.ItemDataRole.UserRole) + 1, choice["component_id"])
            self.first_list.addItem(item)
        if current:
            self.select_first(current)
        elif self.first_list.count():
            self.first_list.setCurrentRow(0)
        self._fill_second()

    def select_first(self, port_id: str):
        for row in range(self.first_list.count()):
            item = self.first_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == port_id:
                self.first_list.setCurrentRow(row)
                return

    def _fill_second(self, *_args):
        current = self.second_port_id()
        first = self.first_list.currentItem()
        first_component = (first.data(int(Qt.ItemDataRole.UserRole) + 1) if first else "")
        first_port = first.data(Qt.ItemDataRole.UserRole) if first else ""
        self.second_list.clear()
        for choice in self._visible_choices():
            if choice["port_id"] == first_port or choice["component_id"] == first_component:
                continue
            item = QListWidgetItem(choice["label"] + ("" if choice["in_system"] else "  (other system)"))
            item.setData(Qt.ItemDataRole.UserRole, choice["port_id"])
            self.second_list.addItem(item)
        restored = False
        for row in range(self.second_list.count()):
            if self.second_list.item(row).data(Qt.ItemDataRole.UserRole) == current:
                self.second_list.setCurrentRow(row)
                restored = True
                break
        if not restored and self.second_list.count():
            self.second_list.setCurrentRow(0)
        self._update_preview()

    def _update_preview(self, *_args):
        first, second = self.first_list.currentItem(), self.second_list.currentItem()
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if first is None or second is None:
            self.preview.setText(
                "Fewer than two open endpoints are available."
                if self.first_list.count() < 2 else "Select an endpoint on each side.")
            if ok_button is not None:
                ok_button.setEnabled(False)
            return
        self.preview.setText(f"{first.text()}\n⟷ {second.text()}")
        if ok_button is not None:
            ok_button.setEnabled(True)

    def first_port_id(self) -> str:
        item = self.first_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def second_port_id(self) -> str:
        item = self.second_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""


class SuggestConnectionsDialog(QDialog):
    """Tick the proposed nodes/connections derived from RPL endpoint events."""

    def __init__(self, store, system_id: str, classify=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Suggest connections from RPL events")
        self._store = store
        self._system_id = system_id or ""
        self._graph = TopologyGraph.from_store(store)
        self._proposals = build_proposals(store, self._graph, self._system_id, classify)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Open segment endpoints whose RPL event names match are proposed as one "
            "node (a BU or BMH per the event rules) or a direct connection. Untick "
            "anything that is wrong; nothing is written until you press Apply.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.list = QListWidget()
        for proposal in self._proposals:
            item = QListWidgetItem(describe_proposal(proposal, self._graph))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)
        if not self._proposals:
            empty = QListWidgetItem(
                "No matching endpoint events found. Open segment endpoints need an "
                "Event at the first/last RPL position (e.g. “BU-1” on both segments).")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(empty)
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        apply_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if apply_button is not None:
            apply_button.setText("Apply")
            apply_button.setEnabled(bool(self._proposals))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(640, 360)

    def proposals(self) -> List[Dict]:
        return list(self._proposals)

    def selected_proposals(self) -> List[Dict]:
        out = []
        for row, proposal in enumerate(self._proposals):
            item = self.list.item(row)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(proposal)
        return out

    def apply(self) -> int:
        count = 0
        for proposal in self.selected_proposals():
            count += len(apply_proposal(self._store, proposal, self._system_id))
        return count


def build_proposals(store, graph: TopologyGraph, system_id: str, classify=None) -> List[Dict]:
    """Collect endpoint events for the system's open segment ports and suggest."""
    latest: Dict[str, Dict] = {}
    for rpl in store.list_rpls():
        route_id = rpl.get("route_id") or ""
        current = latest.get(route_id)
        if current is None or schema.revision_sort_key(rpl) >= schema.revision_sort_key(current):
            latest[route_id] = rpl
    events: Dict[str, str] = {}
    for port in graph.open_ports():
        component = graph.components.get(port.get("component_id")) or {}
        if not is_segment_component(component):
            continue
        if system_id and (component.get("system_id") or "") != system_id:
            continue
        summary = rpl_summary(store, latest.get(component.get("subject_id") or ""))
        event = endpoint_event(summary, port)
        if event:
            events[port["port_id"]] = event
    if classify is None:
        from .assembly_model import EventClassifier

        try:
            classify = EventClassifier(store.list_event_rules()).classify
        except Exception:
            classify = EventClassifier.with_defaults().classify
    return suggest_connections(graph, events, classify, system_id or None)


def run_dialog(dialog) -> bool:
    return qt_exec(dialog) == DIALOG_ACCEPTED
