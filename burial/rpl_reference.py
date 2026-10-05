"""Which RPL an imported file's KPs refer to, and translation onto the plan.

The Cable Workbench is the RPL register: every revision of every route is
there. Imports (plan tables, events CSV) pick the RPL their KPs were
quoted against — the plan's own RPL by default — and KPs on any other RPL
are translated to the plan's route by seabed position (the same geometry
mapping the Ground Model and BAS imports use).
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import QgsProject
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QWidget

REGISTER = "__register__"


def rpl_choices(model) -> List[Tuple[str, str]]:
    """``[(label, rpl_id)]``: the plan's RPL first, then same-route
    revisions, then every other Workbench RPL."""
    from .rereference_qgis import rpl_label
    store = getattr(model, "workbench_store", None)
    plan_rpl = str(getattr(model, "resolved_rpl_id", "") or model.plan.get("rpl_id") or "")
    label = model.plan.get("rpl_name") or "route"
    if model.plan.get("rpl_revision"):
        label += f" — {model.plan.get('rpl_revision')}"
    out = [(f"This plan's RPL ({label})", "")]
    if store is None:
        return out
    try:
        rows = list(store.list_rpls())
    except Exception:
        rows = []
    current = next((r for r in rows if str(r.get("rpl_id") or "") == plan_rpl), None)
    route_id = (current or {}).get("route_id") or ""
    rows = [r for r in rows if str(r.get("rpl_id") or "") != plan_rpl]
    rows.sort(key=lambda r: (0 if route_id and r.get("route_id") == route_id else 1,
                             (r.get("name") or "").lower(), r.get("rev_label") or ""))
    out += [(rpl_label(r), str(r.get("rpl_id") or "")) for r in rows]
    return out


def kp_transform(model, rpl_id: str):
    """``(map_range, kp_map)`` translating KPs on ``rpl_id`` to the plan route.

    ``map_range(start, end) -> (start, end, flags)``. ``rpl_id`` "" (the
    plan's own RPL) returns ``(None, None)``. Raises ValueError.
    """
    if not rpl_id:
        return None, None
    from .rereference_qgis import geometry_map, rpl_label, rpl_route
    store = getattr(model, "workbench_store", None)
    if store is None or model.route is None:
        raise ValueError("The plan route and the Cable Workbench are needed to "
                         "translate KPs from another RPL.")
    rpl = store.get_rpl(rpl_id)
    if rpl is None:
        raise ValueError("The selected RPL is no longer in the Workbench.")
    src_route, _distance, _positions = rpl_route(store, rpl, QgsProject.instance(),
                                                 model.kp_mode())
    kp_map = geometry_map(src_route, model.route, source_label=rpl_label(rpl),
                          target_label=model.plan.get("rpl_name") or "plan route")
    return kp_map.map_range, kp_map


def plan_rpl_label(model) -> str:
    """Readable name of the plan's own route reference."""
    label = model.plan.get("rpl_name") or "route"
    if model.plan.get("rpl_revision"):
        label += f" — {model.plan.get('rpl_revision')}"
    if not str(getattr(model, "resolved_rpl_id", "") or ""):
        label += " (plan route, not a registered RPL)"
    return label


def table_reference(model, picker_rpl_id: str) -> Tuple[str, str, Optional[float]]:
    """``(rpl_id, label, start_kp)`` to store for KP data quoted on the picked RPL.

    "This plan's RPL" is stored as the plan's resolved RPL id, never as
    "", so the data stays tied to the RPL it was quoted against when the
    plan later moves to another revision. "" is stored only when the plan
    route is not a registered RPL. ``start_kp`` is that RPL's start KP now,
    so a later renumbering of its start can be corrected for.
    """
    if picker_rpl_id:
        from .rereference_qgis import rpl_label, rpl_route
        store = getattr(model, "workbench_store", None)
        rpl = store.get_rpl(picker_rpl_id) if store is not None else None
        if rpl is None:
            return picker_rpl_id, picker_rpl_id, None
        try:
            route, _distance, _positions = rpl_route(
                store, rpl, QgsProject.instance(), model.kp_mode())
            start = float(route.start_kp_km)
        except ValueError:
            start = None
        return picker_rpl_id, rpl_label(rpl), start
    route = getattr(model, "route", None)
    start = float(route.start_kp_km) if route is not None else None
    return (str(getattr(model, "resolved_rpl_id", "") or ""),
            plan_rpl_label(model), start)


def table_kp_map(model, config: Dict, cache: Optional[Dict] = None):
    """``(map_range or None, notes)`` placing a KP table's ranges on the plan.

    ``map_range(start, end) -> (start, end, flags)``; ``None`` means the
    table's KPs are already plan KPs. A config with no recorded reference is
    read on the plan's RPL and a note asks for it to be confirmed. When the
    reference RPL's start KP has changed since the table was referenced,
    the quoted KPs are shifted by that change first. Raises ValueError when
    the reference RPL cannot be translated (e.g. deleted from the Workbench).
    """
    from . import kp_table
    from .kp_rereference import KpMap
    notes: List[str] = []
    if kp_table.KP_REF_KEY not in config:
        notes.append("its KP reference RPL is not recorded — KPs are read "
                     "as quoted on this plan's RPL; edit it to confirm")
        return None, notes
    ref = str(config.get(kp_table.KP_REF_KEY) or "")
    label = kp_table.reference_text(config)
    plan_rpl = str(getattr(model, "resolved_rpl_id", "") or "")
    if not ref or ref == plan_rpl:
        kp_map, start_now = None, getattr(model.route, "start_kp_km", None)
    else:
        key = ("rpl", ref)
        if cache is not None and key in cache:
            kp_map, start_now = cache[key]
        else:
            from .rereference_qgis import geometry_map, rpl_label, rpl_route
            store = getattr(model, "workbench_store", None)
            if store is None or model.route is None:
                raise ValueError("the plan route and the Cable Workbench are "
                                 "needed to translate KPs from another RPL")
            rpl = store.get_rpl(ref)
            if rpl is None:
                raise ValueError(f"{label} is no longer in the Workbench — "
                                 "re-register it or choose the table's RPL again")
            src_route, _distance, _positions = rpl_route(
                store, rpl, QgsProject.instance(), model.kp_mode())
            kp_map = geometry_map(
                src_route, model.route, source_label=rpl_label(rpl),
                target_label=model.plan.get("rpl_name") or "plan route")
            start_now = float(src_route.start_kp_km)
            if cache is not None:
                cache[key] = (kp_map, start_now)
        notes.append(f"KPs translated from {label} to this plan's route by "
                     "seabed position")
    shift = 0.0
    recorded = config.get(kp_table.KP_REF_START_KEY)
    if recorded is not None and start_now is not None:
        shift = float(start_now) - float(recorded)
    if abs(shift) <= 5e-7:
        return (kp_map.map_range if kp_map is not None else None), notes
    notes.append(f"{label} now starts at KP {float(start_now):.3f} (was "
                 f"{float(recorded):.3f} when the table was referenced); "
                 f"quoted KPs shifted by {shift * 1000.0:+.1f} m")
    if kp_map is None:
        return KpMap.shift(shift).map_range, notes

    def map_range(start, end, _m=kp_map, _d=shift):
        return _m.map_range(float(start) + _d, float(end) + _d)
    return map_range, notes


def make_table_picker(model, config: Dict, parent=None) -> "RplReferencePicker":
    """A picker showing the config's stored reference.

    A stored RPL that is no longer offered (deleted from the Workbench)
    stays selected as an explicit entry instead of silently falling back
    to the plan's RPL.
    """
    from . import kp_table
    picker = RplReferencePicker(model, parent)
    if kp_table.KP_REF_KEY not in config:
        return picker
    ref = str(config.get(kp_table.KP_REF_KEY) or "")
    plan_rpl = str(getattr(model, "resolved_rpl_id", "") or "")
    if not ref or ref == plan_rpl:
        return picker
    index = picker.combo.findData(ref)
    if index < 0:
        picker.combo.insertItem(
            picker.combo.count() - 1,
            f"{kp_table.reference_text(config)} (not in the Workbench)", ref)
        index = picker.combo.findData(ref)
    picker.combo.setCurrentIndex(index)
    picker._last = ref
    return picker


def rpl_event_rows(model, rpl_id: str):
    """``(rows, label)``: every position of a registered RPL as
    :class:`rpl_plan_import.RplRow`, placed on the plan route by seabed
    position (so another revision or route translates correctly).

    ``rpl_id`` "" is the plan's own RPL. Raises ValueError.
    """
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                           QgsPointXY)
    from ..workbench.rpl_layer_io import _attr
    from .rereference_qgis import rpl_label
    from .rpl_plan_import import RplRow
    store = getattr(model, "workbench_store", None)
    if store is None:
        raise ValueError("Open the Cable Workbench: its RPL register holds the RPLs "
                         "to import from.")
    target = rpl_id or str(getattr(model, "resolved_rpl_id", "") or "")
    if not target:
        raise ValueError("This plan's route is not an RPL in the Workbench register. "
                         "Choose a registered RPL.")
    if model.route is None:
        raise ValueError("The plan has no route to place the RPL's events on.")
    rpl = store.get_rpl(target)
    if rpl is None:
        raise ValueError("The selected RPL is no longer in the Workbench.")
    layer = store.open_layer(rpl.get("points_layer") or "")
    if layer is None or not layer.isValid():
        raise ValueError(f"The positions of '{rpl_label(rpl)}' could not be opened.")
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    xform = None
    if layer.crs() != wgs84:
        xform = QgsCoordinateTransform(layer.crs(), wgs84, QgsProject.instance())

    def number(value, kind):
        try:
            return kind(value) if value is not None and str(value).strip() != "" else None
        except (TypeError, ValueError):
            return None

    raw = []
    for order, feat in enumerate(layer.getFeatures()):
        geom = feat.geometry()
        point = None
        if geom is not None and not geom.isEmpty():
            point = QgsPointXY(geom.asPoint())
            if xform is not None:
                try:
                    point = xform.transform(point)
                except Exception:
                    point = None
        seq = number(_attr(feat, "SeqNo"), int)
        raw.append((seq if seq is not None else order, order, feat, point))
    raw.sort(key=lambda r: (r[0], r[1]))
    rows = []
    for index, (_seq, _order, feat, point) in enumerate(raw):
        row = RplRow(seq=index, pos_no=number(_attr(feat, "PosNo"), int),
                     event=str(_attr(feat, "Event") or "").strip(),
                     remarks=str(_attr(feat, "Remarks") or "").strip(),
                     stated_kp=number(_attr(feat, "DistCumulative"), float))
        if point is not None:
            hit = model.route.kp_at_point(point)
            if hit is not None and hit.feature_index >= 0:
                row.kp = round(float(hit.kp_km), 6)
                row.offset_m = float(hit.dcc_m)
        rows.append(row)
    if not rows:
        raise ValueError(f"'{rpl_label(rpl)}' has no positions.")
    # Segment k (SeqNo order) joins positions k and k + 1: its protection
    # method rides on row k.
    lines = store.open_layer(rpl.get("lines_layer") or "")
    if lines is not None and lines.isValid() and "ProtectionMethod" in lines.fields().names():
        segments = []
        for order, feat in enumerate(lines.getFeatures()):
            seq = number(_attr(feat, "SeqNo"), int)
            segments.append((seq if seq is not None else order, order,
                             str(_attr(feat, "ProtectionMethod") or "").strip()))
        segments.sort(key=lambda s: (s[0], s[1]))
        for index, (_seq, _order, value) in enumerate(segments[:len(rows) - 1]):
            rows[index].protection = value
    return rows, rpl_label(rpl)


class RplReferencePicker(QWidget):
    """"KPs referenced to: [RPL ▾]" with a *Register another RPL…* entry."""

    changed = pyqtSignal()

    def __init__(self, model, parent=None, label: str = "KPs referenced to:"):
        super().__init__(parent)
        self.model = model
        self._cache: Dict[str, Tuple] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(label))
        self.combo = QComboBox()
        self.combo.setToolTip(
            "The RPL the file's KPs were quoted against. KPs on another RPL "
            "(an older revision, or a different route) are translated to this "
            "plan's route by seabed position; stretches where the routes "
            "diverge are flagged.")
        layout.addWidget(self.combo, 1)
        self.reload()
        self.combo.activated.connect(self._activated)

    def reload(self, select: str = "") -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        for text, rpl_id in rpl_choices(self.model):
            self.combo.addItem(text, rpl_id)
        self.combo.addItem("Register another RPL…", REGISTER)
        index = self.combo.findData(select) if select else 0
        self.combo.setCurrentIndex(max(0, index))
        self.combo.blockSignals(False)
        self._last = self.combo.currentData() or ""

    def _activated(self, _index: int) -> None:
        if self.combo.currentData() == REGISTER:
            new_id = self._register()
            self.reload(new_id or self._last)
        self._last = self.combo.currentData() or ""
        self.changed.emit()

    def _register(self) -> str:
        store = getattr(self.model, "workbench_store", None)
        if store is None:
            QMessageBox.information(self, "Register RPL",
                                    "Open the Cable Workbench first to create its store.")
            return ""
        try:
            from ..qgis_compat import qt_exec
            from ..workbench.rpl_import_wizard import RplImportWizard
            wizard = RplImportWizard(store, None, parent=self)
            imported: List[str] = []
            wizard.imported.connect(imported.append)
            qt_exec(wizard)
            return imported[-1] if imported else ""
        except Exception as exc:
            QMessageBox.warning(self, "Register RPL",
                                f"Could not open the RPL import wizard:\n{exc}")
            return ""

    def rpl_id(self) -> str:
        return self.combo.currentData() or ""

    def label(self) -> str:
        return self.combo.currentText()

    def transform(self) -> Tuple[Optional[Callable], object]:
        """``(map_range, kp_map)`` or ``(None, None)``; cached per RPL."""
        rpl_id = self.rpl_id()
        if rpl_id not in self._cache:
            self._cache[rpl_id] = kp_transform(self.model, rpl_id)
        return self._cache[rpl_id]
