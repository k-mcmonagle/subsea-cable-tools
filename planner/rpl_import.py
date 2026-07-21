# -*- coding: utf-8 -*-
"""Import Workbench or project RPL segments as planner-owned task drafts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)
from qgis.core import QgsGeometry, QgsProject

from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..qgis_compat import (
    BUTTON_BOX_CANCEL, BUTTON_BOX_OK, DIALOG_ACCEPTED, GEOMETRY_LINE, GEOMETRY_POINT,
    ITEM_FLAG_EDITABLE, LAYER_VECTOR, qt_exec,
)


GROUP_FIELDS = ("CableType", "CableCode", "ProtectionMethod", "LayVessel")
OPERATIONS = ("Lay", "PLGR", "Plough", "ROV", "Recover")
RPL_RULES_SETTING = "subsea_cable_tools/planner/rpl_operation_rules"
DEFAULT_OPERATION_RULES = [
    {"match": "PLGR", "operation": "PLGR"},
    {"match": "PLOUGH", "operation": "Plough"},
    {"match": "ROV", "operation": "ROV"},
    {"match": "JET", "operation": "ROV"},
    {"match": "RECOVER", "operation": "Recover"},
    {"match": "BURIAL", "operation": "Plough"},
]


@dataclass
class RplSource:
    label: str
    kind: str
    line_layer: object
    point_layer: object = None
    rpl_id: str = ""


@dataclass
class SegmentDraft:
    feature_id: str
    geometry: QgsGeometry
    kp_start: float
    kp_end: float
    length_m: float
    attrs: Dict


class RplImportDialog(QDialog):
    def __init__(self, planner_store, resources, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import RPL into Planner")
        self.resize(900, 580)
        self.planner_store = planner_store
        self.resources = list(resources)
        self.sources = _discover_sources(planner_store)
        self.segments: List[SegmentDraft] = []
        self._loading = False
        self._operation_overrides = {}
        self.operation_rules = _load_operation_rules()

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source_combo = QComboBox()
        for source in self.sources:
            self.source_combo.addItem(source.label, source)
        self.start_spin = _kp_spin()
        self.end_spin = _kp_spin()
        self.group_combo = QComboBox()
        self.group_combo.addItem("Group consecutive operational sections", "operational")
        self.group_combo.addItem("One task per RPL segment", "segment")
        self.group_combo.addItem("One task for the whole selected range", "whole")
        self.resource_combo = QComboBox()
        for resource in self.resources:
            self.resource_combo.addItem(resource.get("name") or "Resource",
                                        resource.get("resource_id") or "")
        self.operation_combo = QComboBox()
        for operation in OPERATIONS:
            self.operation_combo.addItem(operation, operation)
        rules_widget = QWidget()
        rules_layout = QHBoxLayout(rules_widget)
        rules_layout.setContentsMargins(0, 0, 0, 0)
        self.use_rules = QCheckBox("Use saved ProtectionMethod rules")
        rules_button = QPushButton("Edit rules…")
        rules_button.clicked.connect(self._edit_operation_rules)
        rules_layout.addWidget(self.use_rules)
        rules_layout.addWidget(rules_button)
        rules_layout.addStretch(1)
        form.addRow("Source:", self.source_combo)
        form.addRow("Start KP:", self.start_spin)
        form.addRow("End KP:", self.end_spin)
        form.addRow("Task grouping:", self.group_combo)
        form.addRow("Resource:", self.resource_combo)
        form.addRow("Operation for all sections:", self.operation_combo)
        form.addRow("Automatic mapping:", rules_widget)
        layout.addLayout(form)

        layout.addWidget(QLabel(
            "Default task speeds by cable type (0 kn creates a manual-duration task):"))
        self.speed_table = QTableWidget(0, 2)
        self.speed_table.setHorizontalHeaderLabels(["Cable type", "Speed (kn)"])
        self.speed_table.setMaximumHeight(150)
        layout.addWidget(self.speed_table)

        layout.addWidget(QLabel("Import preview:"))
        self.preview = QTableWidget(0, 7)
        self.preview.setHorizontalHeaderLabels(
            ["Task", "Operation", "Start KP", "End KP", "Cable type",
             "Length (km)", "Speed (kn)"])
        layout.addWidget(self.preview, 1)
        self.status = QLabel("")
        layout.addWidget(self.status)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self._accept_if_valid)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self.start_spin.valueChanged.connect(self._refresh_preview)
        self.end_spin.valueChanged.connect(self._refresh_preview)
        self.group_combo.currentIndexChanged.connect(self._refresh_preview)
        self.resource_combo.currentIndexChanged.connect(self._refresh_preview)
        self.operation_combo.currentIndexChanged.connect(self._apply_operation_to_all)
        self.use_rules.toggled.connect(self._rules_toggled)
        self.speed_table.itemChanged.connect(self._refresh_preview)
        self._source_changed()

    def _source_changed(self, _index=None):
        source = self.source_combo.currentData()
        self._operation_overrides.clear()
        self.segments = _read_segments(source) if source is not None else []
        self._loading = True
        try:
            if self.segments:
                minimum = min(segment.kp_start for segment in self.segments)
                maximum = max(segment.kp_end for segment in self.segments)
                self.start_spin.setRange(minimum, maximum)
                self.end_spin.setRange(minimum, maximum)
                self.start_spin.setValue(minimum)
                self.end_spin.setValue(maximum)
            self._populate_speeds()
        finally:
            self._loading = False
        self._refresh_preview()

    def _populate_speeds(self):
        cable_types = sorted({_text(_attr_ci(segment.attrs, "CableType")) or "(unspecified)"
                              for segment in self.segments})
        default_speed = self._resource_default_speed()
        self.speed_table.setRowCount(len(cable_types))
        for row, cable_type in enumerate(cable_types):
            type_item = QTableWidgetItem(cable_type)
            type_item.setFlags(type_item.flags() & ~ITEM_FLAG_EDITABLE)
            self.speed_table.setItem(row, 0, type_item)
            self.speed_table.setItem(row, 1, QTableWidgetItem(_number_text(default_speed)))

    def _resource_default_speed(self):
        resource_id = self.resource_combo.currentData()
        resource = next((row for row in self.resources
                         if row.get("resource_id") == resource_id), {})
        return float(resource.get("default_speed_kn") or 0.0)

    def _speed_map(self):
        speeds = {}
        for row in range(self.speed_table.rowCount()):
            cable_type = self.speed_table.item(row, 0).text()
            try:
                speeds[cable_type] = max(0.0, float(self.speed_table.item(row, 1).text()))
            except (AttributeError, TypeError, ValueError):
                speeds[cable_type] = 0.0
        return speeds

    def _groups(self):
        if not self.segments:
            return []
        start, end = sorted((self.start_spin.value(), self.end_spin.value()))
        clipped = [_clip_segment(segment, start, end, self.source_combo.currentData().line_layer.crs())
                   for segment in self.segments
                   if segment.kp_end > start and segment.kp_start < end]
        clipped = [segment for segment in clipped if segment is not None]
        mode = self.group_combo.currentData()
        if mode == "segment":
            return [[segment] for segment in clipped]
        if mode == "whole":
            return [clipped] if clipped else []
        groups = []
        for segment in clipped:
            key = tuple(_text(_attr_ci(segment.attrs, field)) for field in GROUP_FIELDS)
            if groups and groups[-1][0] == key:
                groups[-1][1].append(segment)
            else:
                groups.append((key, [segment]))
        return [items for _key, items in groups]

    def _drafts(self):
        source = self.source_combo.currentData()
        resource_id = self.resource_combo.currentData() or ""
        speeds = self._speed_map()
        mode = self.group_combo.currentData()
        drafts = []
        for index, group in enumerate(self._groups(), start=1):
            geometry = _join_geometries([segment.geometry for segment in group])
            if geometry is None or geometry.isEmpty():
                continue
            cable_types = [_text(_attr_ci(segment.attrs, "CableType")) or "(unspecified)"
                           for segment in group]
            primary_type = cable_types[0]
            total_length = sum(segment.length_m for segment in group)
            duration_hours = 0.0
            duration_valid = True
            for segment, cable_type in zip(group, cable_types):
                speed = speeds.get(cable_type, 0.0)
                if speed <= 0:
                    duration_valid = False
                    continue
                duration_hours += segment.length_m / (speed * 0.514444) / 3600.0
            if mode == "whole" and duration_valid and duration_hours > 0:
                speed_knots = total_length / duration_hours / 3600.0 / 0.514444
                duration_mode = "manual" if len(set(cable_types)) > 1 else "computed"
            else:
                speed_knots = speeds.get(primary_type, 0.0)
                duration_mode = "computed" if speed_knots > 0 else "manual"
                if not duration_valid:
                    duration_hours = 1.0
            kp_start = group[0].kp_start
            kp_end = group[-1].kp_end
            group_key = _group_key(group)
            protection = _text(_attr_ci(group[0].attrs, "ProtectionMethod"))
            default_operation = (
                _rule_operation(protection, self.operation_rules)
                if self.use_rules.isChecked() else
                self.operation_combo.currentData() or "Lay")
            operation = self._operation_overrides.get(
                group_key, default_operation)
            label_bits = [bit for bit in (primary_type, protection)
                          if bit and bit != "(unspecified)"]
            task_name = "%s %s KP %.3f–%.3f" % (
                operation, " / ".join(label_bits) if label_bits else "RPL section",
                kp_start, kp_end)
            drafts.append({
                "name": task_name, "description": "Imported from %s" % source.label,
                "resource_id": resource_id, "speed_knots": speed_knots,
                "duration_hours": duration_hours if duration_hours > 0 else 1.0,
                "duration_mode": duration_mode, "geometry": geometry,
                "source_crs": source.line_layer.crs(),
                "source_kind": "workbench_rpl" if source.kind == "workbench" else "project_rpl",
                "source_ref": {
                    "rpl_id": source.rpl_id, "source_layer": source.line_layer.source(),
                    "source_feature_ids": [segment.feature_id for segment in group],
                    "kp_start": kp_start, "kp_end": kp_end,
                    "operation": operation,
                    "group_fields": {field: _text(_attr_ci(group[0].attrs, field))
                                     for field in GROUP_FIELDS},
                },
                "notes": "Operation: %s; Cable type: %s" % (operation, primary_type),
                "kp_start": kp_start, "kp_end": kp_end,
                "cable_type": primary_type, "length_m": total_length,
                "operation": operation, "group_key": group_key,
            })
        return drafts

    def _refresh_preview(self, *_args):
        if self._loading:
            return
        drafts = self._drafts()
        self.preview.setRowCount(len(drafts))
        for row, draft in enumerate(drafts):
            values = (
                draft["name"], "%.3f" % draft["kp_start"], "%.3f" % draft["kp_end"],
                draft["cable_type"], "%.3f" % (draft["length_m"] / 1000.0),
                _number_text(draft["speed_knots"]),
            )
            self.preview.setItem(row, 0, QTableWidgetItem(values[0]))
            operation_combo = QComboBox()
            for operation in OPERATIONS:
                operation_combo.addItem(operation, operation)
            operation_combo.setCurrentIndex(max(
                0, operation_combo.findData(draft["operation"])))
            operation_combo.currentIndexChanged.connect(
                lambda _index, combo=operation_combo, key=draft["group_key"]:
                self._operation_changed(key, combo.currentData()))
            self.preview.setCellWidget(row, 1, operation_combo)
            for column, value in enumerate(values[1:], start=2):
                self.preview.setItem(row, column, QTableWidgetItem(value))
        self.status.setText("%d source segment(s) → %d task(s)" % (len(self.segments), len(drafts)))

    def _apply_operation_to_all(self, *_args):
        if self._loading:
            return
        operation = self.operation_combo.currentData() or "Lay"
        for group in self._groups():
            self._operation_overrides[_group_key(group)] = operation
        self._refresh_preview()

    def _operation_changed(self, group_key, operation):
        if self._loading:
            return
        self._operation_overrides[group_key] = operation or "Lay"
        # Avoid rebuilding the combo while handling its own signal; only the
        # generated task name needs to change immediately.
        for row, draft in enumerate(self._drafts()):
            if draft["group_key"] == group_key and self.preview.item(row, 0) is not None:
                self.preview.item(row, 0).setText(draft["name"])
                break

    def _rules_toggled(self, _checked):
        if self._loading:
            return
        self._operation_overrides.clear()
        self._refresh_preview()

    def _edit_operation_rules(self):
        dialog = RplOperationRulesDialog(self.operation_rules, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        self.operation_rules = dialog.rules()
        QSettings().setValue(RPL_RULES_SETTING, json.dumps(self.operation_rules))
        if self.use_rules.isChecked():
            self._operation_overrides.clear()
            self._refresh_preview()

    def _accept_if_valid(self):
        if self._drafts():
            self.accept()
        else:
            self.status.setText("The selected range contains no importable line geometry.")

    def task_drafts(self):
        return self._drafts()


class RplOperationRulesDialog(QDialog):
    def __init__(self, rules, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RPL operation mapping rules")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Rules are matched in order against ProtectionMethod (case-insensitive). "
            "Unmatched sections use Lay."))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["ProtectionMethod contains", "Operation"])
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(lambda: self._add_rule({"match": "", "operation": "Lay"}))
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: self.table.removeRow(self.table.currentRow())
                               if self.table.currentRow() >= 0 else None)
        defaults = QPushButton("Restore defaults")
        defaults.clicked.connect(self._restore_defaults)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(defaults)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        for rule in rules:
            self._add_rule(rule)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _add_rule(self, rule):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(rule.get("match") or "")))
        combo = QComboBox()
        for operation in OPERATIONS:
            combo.addItem(operation, operation)
        combo.setCurrentIndex(max(0, combo.findData(rule.get("operation") or "Lay")))
        self.table.setCellWidget(row, 1, combo)

    def _restore_defaults(self):
        self.table.setRowCount(0)
        for rule in DEFAULT_OPERATION_RULES:
            self._add_rule(rule)

    def rules(self):
        rows = []
        for row in range(self.table.rowCount()):
            match = self.table.item(row, 0).text().strip() if self.table.item(row, 0) else ""
            combo = self.table.cellWidget(row, 1)
            if match:
                rows.append({"match": match, "operation": combo.currentData() or "Lay"})
        return rows


def _discover_sources(planner_store) -> List[RplSource]:
    sources = []
    known_sources = set()
    try:
        from ..workbench.store import WorkbenchStore, project_gpkg_path
        path = project_gpkg_path()
        if path:
            store = WorkbenchStore(path)
            if store.exists():
                for row in store.list_rpls():
                    line_layer = store.open_layer(row.get("lines_layer") or "")
                    point_layer = store.open_layer(row.get("points_layer") or "")
                    if line_layer is not None:
                        sources.append(RplSource(
                            "Workbench: %s" % (row.get("name") or "RPL"), "workbench",
                            line_layer, point_layer, row.get("rpl_id") or ""))
                        known_sources.add(line_layer.source())
    except Exception:
        pass
    planner_sources = {
        planner_store.geometry_layer("line").source() if planner_store.geometry_layer("line") else ""
    }
    for layer in QgsProject.instance().mapLayers().values():
        try:
            if (layer.type() == LAYER_VECTOR and layer.geometryType() == GEOMETRY_LINE
                    and layer.source() not in planner_sources
                    and layer.source() not in known_sources):
                sources.append(RplSource(
                    "Project layer: %s" % layer.name(), "project", layer,
                    _matching_point_layer(layer)))
        except Exception:
            continue
    return sources


def _matching_point_layer(line_layer):
    line_name = line_layer.name().lower()
    line_path = line_layer.source().split("|", 1)[0].lower()
    best = None
    best_score = 0
    for candidate in QgsProject.instance().mapLayers().values():
        try:
            if candidate.type() != LAYER_VECTOR or candidate.geometryType() != GEOMETRY_POINT:
                continue
            fields = {field.name().lower() for field in candidate.fields()}
            if "posno" not in fields or "distcumulative" not in fields:
                continue
            score = 1
            if candidate.source().split("|", 1)[0].lower() == line_path:
                score += 2
            point_name = candidate.name().lower()
            if line_name.replace("lines", "points") == point_name:
                score += 4
            if score > best_score:
                best, best_score = candidate, score
        except Exception:
            continue
    return best


def _read_segments(source: Optional[RplSource]) -> List[SegmentDraft]:
    if source is None or source.line_layer is None:
        return []
    line_layer = source.line_layer
    field_lookup = {field.name().lower(): field.name() for field in line_layer.fields()}
    seq_field = _field(field_lookup, "seqno", "seq", "segment_id")
    features = list(line_layer.getFeatures())
    features.sort(key=lambda feature: _float_attr(feature, seq_field, feature.id()))
    point_kp = _point_kp_lookup(source.point_layer)
    distance = make_distance_area(line_layer.crs(), QgsProject.instance().transformContext())
    running_kp = 0.0
    segments = []
    for feature in features:
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        frame = RouteFrame.from_source([geometry], distance)
        attrs = {field.name(): feature[field.name()] for field in line_layer.fields()}
        from_pos = _attr_ci(attrs, "FromPos")
        to_pos = _attr_ci(attrs, "ToPos")
        kp_start = point_kp.get(str(from_pos)) if from_pos is not None else None
        kp_end = point_kp.get(str(to_pos)) if to_pos is not None else None
        if kp_start is None:
            kp_start = _first_float(attrs, "StartKP", "KP_Start", "FromKP", "start_kp")
        if kp_end is None:
            kp_end = _first_float(attrs, "EndKP", "KP_End", "ToKP", "end_kp")
        if kp_start is None or kp_end is None:
            kp_start = running_kp
            kp_end = running_kp + frame.total_length_km
        if kp_end < kp_start:
            kp_start, kp_end = kp_end, kp_start
        running_kp = kp_end
        segments.append(SegmentDraft(
            str(feature.id()), QgsGeometry(geometry), float(kp_start), float(kp_end),
            frame.total_length_m, attrs))
    return segments


def _point_kp_lookup(point_layer):
    if point_layer is None:
        return {}
    fields = {field.name().lower(): field.name() for field in point_layer.fields()}
    pos_field = _field(fields, "posno", "position", "id")
    kp_field = _field(fields, "distcumulative", "kp", "route_kp")
    if not pos_field or not kp_field:
        return {}
    lookup = {}
    for feature in point_layer.getFeatures():
        try:
            lookup[str(feature[pos_field])] = float(feature[kp_field])
        except (TypeError, ValueError):
            continue
    return lookup


def _clip_segment(segment, start, end, crs):
    overlap_start = max(start, segment.kp_start)
    overlap_end = min(end, segment.kp_end)
    if overlap_end <= overlap_start:
        return None
    span = segment.kp_end - segment.kp_start
    if span <= 0:
        return segment
    distance = make_distance_area(crs, QgsProject.instance().transformContext())
    frame = RouteFrame.from_source([segment.geometry], distance)
    start_fraction = (overlap_start - segment.kp_start) / span
    end_fraction = (overlap_end - segment.kp_start) / span
    geometry = frame.extract_segment(
        start_fraction * frame.total_length_km, end_fraction * frame.total_length_km)
    if geometry is None:
        return None
    return SegmentDraft(
        segment.feature_id, geometry, overlap_start, overlap_end,
        frame.total_length_m * (end_fraction - start_fraction), dict(segment.attrs))


def _join_geometries(geometries):
    points = []
    for geometry in geometries:
        parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
        for part in parts:
            if not part:
                continue
            part = list(part)
            if points:
                direct = _point_distance(points[-1], part[0])
                reversed_distance = _point_distance(points[-1], part[-1])
                if reversed_distance < direct:
                    part.reverse()
                if _point_distance(points[-1], part[0]) < 1e-9:
                    part = part[1:]
            points.extend(part)
    return QgsGeometry.fromPolylineXY(points) if len(points) >= 2 else None


def _group_key(group):
    """Stable key for operation overrides while the preview is edited."""
    return tuple(segment.feature_id for segment in group)


def _point_distance(a, b):
    return ((a.x() - b.x()) ** 2 + (a.y() - b.y()) ** 2) ** 0.5


def _kp_spin():
    spin = QDoubleSpinBox()
    spin.setDecimals(4)
    spin.setRange(-1_000_000.0, 1_000_000.0)
    spin.setSuffix(" km")
    return spin


def _field(lookup, *candidates):
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return ""


def _attr_ci(attrs, name):
    target = name.lower()
    return next((value for key, value in attrs.items() if key.lower() == target), None)


def _first_float(attrs, *names):
    for name in names:
        value = _attr_ci(attrs, name)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _float_attr(feature, field, default):
    try:
        return float(feature[field]) if field else float(default)
    except (TypeError, ValueError):
        return float(default)


def _text(value):
    if value is None:
        return ""
    if type(value).__name__ == "QVariant":
        if not value.isValid() or value.isNull():
            return ""
        value = value.value()
    return str(value).strip()


def _number_text(value):
    return ("%.4f" % float(value or 0.0)).rstrip("0").rstrip(".") or "0"


def _load_operation_rules():
    raw = QSettings().value(RPL_RULES_SETTING, "")
    if raw:
        try:
            rows = json.loads(str(raw))
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
        except (TypeError, ValueError):
            pass
    return [dict(row) for row in DEFAULT_OPERATION_RULES]


def _rule_operation(protection_method, rules):
    text = str(protection_method or "").upper()
    for rule in rules:
        match = str(rule.get("match") or "").strip().upper()
        operation = str(rule.get("operation") or "Lay")
        if match and match in text and operation in OPERATIONS:
            return operation
    return "Lay"
