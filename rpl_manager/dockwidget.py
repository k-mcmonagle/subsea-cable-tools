from __future__ import annotations

import os
import uuid
from typing import Dict, List, Optional, Tuple

try:
    import sip  # type: ignore
except Exception:  # pragma: no cover
    sip = None

from qgis.PyQt.QtCore import Qt, QPointF, QVariant, QSettings, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsMapLayer,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from qgis.gui import QgsRubberBand, QgsVertexMarker

from .assembly_sld_view import _AssemblySldView


# NOTE: Assembly SLD view moved to rpl_manager/assembly_sld_view.py


class RplManagerDockWidget(QDockWidget):
    """RPL Manager dock widget.

    This is a UI-first wrapper around the managed RPL workflow:
    - Select/import an RPL (points + lines)
    - Convert to managed GeoPackage (nodes + segments with stable IDs)
    - View an alternating point/line table
    - View a cable assembly-focused table (bodies)
    - Bridge to existing SLD + Depth Profile tools
    """

    def __init__(self, iface, parent=None):
        super().__init__("RPL Manager", parent)
        self.iface = iface
        self.setObjectName("RplManagerDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        self._settings = QSettings("subsea_cable_tools", "RplManager")
        self._project_signals_hooked: bool = False
        self._project_refresh_timer = QTimer(self)
        self._project_refresh_timer.setSingleShot(True)
        self._project_refresh_timer.setInterval(300)
        self._project_refresh_timer.timeout.connect(self._refresh_available_managed_rpls)

        self._managed_nodes_layer_id: Optional[str] = None
        self._managed_segs_layer_id: Optional[str] = None

        self._gpkg_path: str = ""
        self._prefix: str = ""
        self._assembly_layer_id: Optional[str] = None

        self._updating_assembly_table: bool = False

        # Map highlight helpers for the RPL Table tab.
        self._rpl_table_marker: Optional[QgsVertexMarker] = None
        self._rpl_table_rubber: Optional[QgsRubberBand] = None

        self.tab_widget = QTabWidget()
        self.setWidget(self.tab_widget)

        self._build_setup_tab()
        self._build_rpl_table_tab()
        self._build_cable_assembly_tab()
        self._build_assembly_sld_tab()
        self._build_kp_sld_tab()
        self._build_depth_profile_tab()

        self.populate_layer_combos()

        # Keep the managed list up to date as layers come/go.
        try:
            self._hook_project_signals()
        except Exception:
            pass

        # Ensure signals are disconnected on teardown (prevents callbacks into deleted Qt objects).
        try:
            self.destroyed.connect(self._unhook_project_signals)
        except Exception:
            pass

        # Populate list and restore the previously active managed RPL (if possible).
        try:
            self._refresh_available_managed_rpls()
            self._restore_active_managed_rpl()
        except Exception:
            pass

    # -----------------
    # Tabs
    # -----------------

    def _build_setup_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # -----------------
        # Managed RPL selection (clean/simple)
        # -----------------
        managed_box = QGroupBox("Managed RPL")
        managed_layout = QVBoxLayout(managed_box)

        managed_help = QLabel(
            "Pick an available Managed RPL to work with.\n"
            "The list is detected from the current QGIS project and recent GeoPackages."
        )
        managed_help.setWordWrap(True)
        managed_layout.addWidget(managed_help)

        self.managed_rpl_list = QListWidget()
        self.managed_rpl_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.managed_rpl_list.itemSelectionChanged.connect(self._on_managed_rpl_selection_changed)
        self.managed_rpl_list.itemDoubleClicked.connect(lambda _it: self._activate_selected_managed_rpl())
        managed_layout.addWidget(self.managed_rpl_list, 1)

        btn_row = QHBoxLayout()
        self.managed_browse_btn = QPushButton("Add GeoPackage…")
        self.managed_browse_btn.setToolTip("Browse to a managed RPL GeoPackage (.gpkg) and add it to the list")
        self.managed_browse_btn.clicked.connect(self._browse_add_managed_gpkg)
        btn_row.addWidget(self.managed_browse_btn)

        self.managed_refresh_btn = QPushButton("Refresh")
        self.managed_refresh_btn.clicked.connect(self._refresh_available_managed_rpls)
        btn_row.addWidget(self.managed_refresh_btn)

        btn_row.addStretch(1)

        self.managed_activate_btn = QPushButton("Set Active")
        self.managed_activate_btn.setEnabled(False)
        self.managed_activate_btn.clicked.connect(self._activate_selected_managed_rpl)
        btn_row.addWidget(self.managed_activate_btn)

        managed_layout.addLayout(btn_row)

        self.managed_stats_label = QLabel("")
        self.managed_stats_label.setWordWrap(True)
        managed_layout.addWidget(self.managed_stats_label)

        layout.addWidget(managed_box)

        # -----------------
        # 2) Create a managed GeoPackage from selected layers
        # -----------------
        self.show_create_btn = QPushButton("Create new managed RPL…")
        self.show_create_btn.setToolTip("Show/hide the create-from-layers section")
        self.show_create_btn.clicked.connect(self._toggle_create_section)
        layout.addWidget(self.show_create_btn)

        create_box = QGroupBox("Create managed RPL from layers")
        self.create_box = create_box
        create_layout = QVBoxLayout(create_box)
        create_form = QFormLayout()
        create_layout.addLayout(create_form)

        self.src_points_combo = QComboBox()
        create_form.addRow(QLabel("Source RPL points layer:"), self.src_points_combo)

        self.src_lines_combo = QComboBox()
        create_form.addRow(QLabel("Source RPL lines layer:"), self.src_lines_combo)

        self.output_gpkg_edit = QLineEdit()
        self.output_gpkg_edit.setPlaceholderText("Choose an output .gpkg file")
        out_gpkg_row = QHBoxLayout()
        out_gpkg_row.addWidget(self.output_gpkg_edit, 1)
        self.browse_gpkg_btn = QPushButton("Browse…")
        self.browse_gpkg_btn.clicked.connect(self._browse_output_gpkg)
        out_gpkg_row.addWidget(self.browse_gpkg_btn)
        out_gpkg_widget = QWidget()
        out_gpkg_widget.setLayout(out_gpkg_row)
        create_form.addRow(QLabel("Output GeoPackage:"), out_gpkg_widget)

        self.prefix_edit = QLineEdit("RPL")
        self.prefix_edit.setPlaceholderText("Leave as 'RPL' to auto-name from SourceFile")
        create_form.addRow(QLabel("Layer name prefix:"), self.prefix_edit)

        self.group_edit = QLineEdit("Managed RPL")
        create_form.addRow(QLabel("Project group:"), self.group_edit)

        create_btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh layers")
        self.refresh_btn.clicked.connect(self.populate_layer_combos)
        create_btn_row.addWidget(self.refresh_btn)

        self.convert_btn = QPushButton("Create / Update managed RPL")
        self.convert_btn.clicked.connect(self._run_conversion)
        create_btn_row.addWidget(self.convert_btn)
        create_layout.addLayout(create_btn_row)

        layout.addWidget(create_box)
        create_box.setVisible(False)

        # Internals (we still use these combos as internal state, but we don't require the user
        # to select them when working from a GeoPackage)
        managed_row = QHBoxLayout()
        self.managed_nodes_combo = QComboBox()
        self.managed_nodes_combo.currentIndexChanged.connect(self._on_managed_selection_changed)
        managed_row.addWidget(QLabel("Nodes:"))
        managed_row.addWidget(self.managed_nodes_combo, 1)

        self.managed_segs_combo = QComboBox()
        self.managed_segs_combo.currentIndexChanged.connect(self._on_managed_selection_changed)
        managed_row.addWidget(QLabel("Segments:"))
        managed_row.addWidget(self.managed_segs_combo, 1)

        # Internal state/debug selectors: hide by default to reduce confusion.
        managed_row_widget = QWidget()
        managed_row_widget.setLayout(managed_row)
        managed_row_widget.setVisible(False)
        layout.addWidget(managed_row_widget)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.tab_widget.addTab(tab, "1) Setup")

    def _build_rpl_table_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.rpl_table = QTableWidget()
        self.rpl_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rpl_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        try:
            self.rpl_table.setToolTip("Tip: single-click a row to highlight it; double-click to pan the map to it")
        except Exception:
            pass
        try:
            self.rpl_table.cellDoubleClicked.connect(self._on_rpl_table_cell_double_clicked)
        except Exception:
            pass
        try:
            self.rpl_table.itemSelectionChanged.connect(self._on_rpl_table_selection_changed)
        except Exception:
            pass
        layout.addWidget(self.rpl_table)

        self.tab_widget.addTab(tab, "2) RPL Table")

    def _qt_isdeleted(self, obj) -> bool:
        try:
            return bool(sip is not None and hasattr(sip, "isdeleted") and sip.isdeleted(obj))
        except Exception:
            return False

    def _clear_rpl_table_highlight(self):
        marker = getattr(self, "_rpl_table_marker", None)
        if marker is not None:
            try:
                if not self._qt_isdeleted(marker):
                    try:
                        marker.hide()
                    except Exception:
                        pass
                    try:
                        marker.deleteLater()
                    except Exception:
                        pass
            except Exception:
                pass
            self._rpl_table_marker = None

        rubber = getattr(self, "_rpl_table_rubber", None)
        if rubber is not None:
            try:
                if not self._qt_isdeleted(rubber):
                    try:
                        rubber.hide()
                    except Exception:
                        pass
                    try:
                        rubber.reset(QgsWkbTypes.LineGeometry)
                    except Exception:
                        pass
                    try:
                        rubber.deleteLater()
                    except Exception:
                        pass
            except Exception:
                pass
            self._rpl_table_rubber = None

        def _highlight_features_on_canvas(self, layer: QgsVectorLayer, features: List[QgsFeature]):
            """Best-effort highlight of one or more features on the map canvas.

            Uses the same marker/rubberband as the RPL table to keep behavior consistent.
            """

            if not features:
                return

            canvas = self.iface.mapCanvas() if self.iface and hasattr(self.iface, "mapCanvas") else None
            if canvas is None:
                return

            # Find first non-empty geometry for point fallback.
            first_geom = None
            for f in features:
                try:
                    g = f.geometry() if f is not None else None
                except Exception:
                    g = None
                if g is not None and not g.isEmpty():
                    first_geom = g
                    break
            if first_geom is None:
                return

            self._clear_rpl_table_highlight()

            try:
                if layer.geometryType() == QgsWkbTypes.PointGeometry:
                    marker = QgsVertexMarker(canvas)
                    marker.setColor(QColor(255, 215, 0))  # gold
                    marker.setIconType(QgsVertexMarker.ICON_CROSS)
                    marker.setIconSize(14)
                    marker.setPenWidth(3)

                    pt = first_geom.asPoint() if first_geom.isMultipart() is False else first_geom.asMultiPoint()[0]
                    try:
                        dest_crs = canvas.mapSettings().destinationCrs()
                        if layer.crs() != dest_crs:
                            tr = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
                            pt = tr.transform(pt)
                    except Exception:
                        pass

                    marker.setCenter(pt)
                    try:
                        canvas.scene().addItem(marker)
                    except Exception:
                        pass
                    self._rpl_table_marker = marker
                    canvas.refresh()
                    return

                rubber = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
                rubber.setColor(QColor(255, 215, 0))
                rubber.setWidth(4)
                rubber.setLineStyle(Qt.SolidLine)
                added_any = False
                for f in features:
                    try:
                        geom = f.geometry() if f is not None else None
                        if geom is None or geom.isEmpty():
                            continue
                        # addGeometry works for multi-part and avoids needing to union
                        try:
                            rubber.addGeometry(geom, layer)
                        except Exception:
                            rubber.addGeometry(QgsGeometry(geom), layer)
                        added_any = True
                    except Exception:
                        continue
                if not added_any:
                    return
                try:
                    rubber.show()
                except Exception:
                    pass
                self._rpl_table_rubber = rubber
                canvas.refresh()
            except Exception:
                pass

        def _pan_canvas_to_geometry(self, layer: QgsVectorLayer, geom: QgsGeometry):
            canvas = self.iface.mapCanvas() if self.iface and hasattr(self.iface, "mapCanvas") else None
            if canvas is None:
                return
            if geom is None or geom.isEmpty():
                return
            try:
                center = geom.boundingBox().center()
                try:
                    dest_crs = canvas.mapSettings().destinationCrs()
                    if layer.crs() != dest_crs:
                        tr = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
                        center = tr.transform(center)
                except Exception:
                    pass
                canvas.setCenter(center)
                canvas.refresh()
            except Exception:
                pass

    def _rpl_table_row_target(self, row: int) -> Optional[Dict[str, object]]:
        try:
            if row < 0:
                return None
            item0 = self.rpl_table.item(row, 0)
            if item0 is None:
                return None
            meta = item0.data(Qt.UserRole)
            return meta if isinstance(meta, dict) else None
        except Exception:
            return None

    def _highlight_feature_on_canvas(self, layer: QgsVectorLayer, feature: QgsFeature):
        canvas = self.iface.mapCanvas() if self.iface and hasattr(self.iface, "mapCanvas") else None
        if canvas is None:
            return

        geom = feature.geometry() if feature is not None else None
        if geom is None or geom.isEmpty():
            return

        self._clear_rpl_table_highlight()

        try:
            if layer.geometryType() == QgsWkbTypes.PointGeometry:
                marker = QgsVertexMarker(canvas)
                marker.setColor(QColor(255, 215, 0))  # gold
                marker.setIconType(QgsVertexMarker.ICON_CROSS)
                marker.setIconSize(14)
                marker.setPenWidth(3)

                pt = geom.asPoint() if geom.isMultipart() is False else geom.asMultiPoint()[0]
                try:
                    dest_crs = canvas.mapSettings().destinationCrs()
                    if layer.crs() != dest_crs:
                        tr = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
                        pt = tr.transform(pt)
                except Exception:
                    pass

                marker.setCenter(pt)
                try:
                    canvas.scene().addItem(marker)
                except Exception:
                    pass
                self._rpl_table_marker = marker
                canvas.refresh()
                return

            rubber = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
            rubber.setColor(QColor(255, 215, 0))
            rubber.setWidth(4)
            rubber.setLineStyle(Qt.SolidLine)
            try:
                rubber.setToGeometry(QgsGeometry(geom), layer)
            except Exception:
                # Best effort fallback: try direct geometry
                try:
                    rubber.setToGeometry(geom, layer)
                except Exception:
                    return
            try:
                rubber.show()
            except Exception:
                pass
            self._rpl_table_rubber = rubber
            canvas.refresh()
        except Exception:
            # Never break the UI for a highlight failure.
            pass

    def _pan_canvas_to_feature(self, layer: QgsVectorLayer, feature: QgsFeature):
        canvas = self.iface.mapCanvas() if self.iface and hasattr(self.iface, "mapCanvas") else None
        if canvas is None:
            return
        try:
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                return
            center = geom.boundingBox().center()
            try:
                dest_crs = canvas.mapSettings().destinationCrs()
                if layer.crs() != dest_crs:
                    tr = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
                    center = tr.transform(center)
            except Exception:
                pass
            canvas.setCenter(center)
            canvas.refresh()
        except Exception:
            pass

    def _on_rpl_table_selection_changed(self):
        try:
            row = self.rpl_table.currentRow()
        except Exception:
            return
        meta = self._rpl_table_row_target(row)
        if not meta:
            self._clear_rpl_table_highlight()
            return

        layer_id = str(meta.get("layer_id") or "")
        fid = meta.get("fid")
        if not layer_id:
            return
        try:
            fid_int = int(fid)
        except Exception:
            return

        layer = QgsProject.instance().mapLayer(layer_id)
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            return

        try:
            req = QgsFeatureRequest().setFilterFid(fid_int)
            feat = next(layer.getFeatures(req), None)
        except Exception:
            feat = None
        if feat is None:
            return
        self._highlight_feature_on_canvas(layer, feat)

    def _on_rpl_table_cell_double_clicked(self, row: int, _column: int):
        meta = self._rpl_table_row_target(row)
        if not meta:
            return

        layer_id = str(meta.get("layer_id") or "")
        fid = meta.get("fid")
        if not layer_id:
            return
        try:
            fid_int = int(fid)
        except Exception:
            return

        layer = QgsProject.instance().mapLayer(layer_id)
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            return

        try:
            req = QgsFeatureRequest().setFilterFid(fid_int)
            feat = next(layer.getFeatures(req), None)
        except Exception:
            feat = None
        if feat is None:
            return

        # Keep highlight in sync and pan the map to the selected row feature.
        self._highlight_feature_on_canvas(layer, feat)
        self._pan_canvas_to_feature(layer, feat)

    def _build_cable_assembly_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Body detection keywords (comma-separated):"))
        self.body_keywords_edit = QLineEdit("repeater, joint, transition")
        top_row.addWidget(self.body_keywords_edit, 1)
        self.detect_bodies_btn = QPushButton("Auto-build assembly")
        self.detect_bodies_btn.setToolTip("Build an initial assembly table from managed nodes/segments")
        self.detect_bodies_btn.clicked.connect(self._autobuild_assembly_from_rpl)
        top_row.addWidget(self.detect_bodies_btn)
        layout.addLayout(top_row)

        btn_row = QHBoxLayout()
        self.add_body_btn = QPushButton("Add body")
        self.add_body_btn.clicked.connect(self._add_assembly_body_row)
        btn_row.addWidget(self.add_body_btn)

        self.add_seg_btn = QPushButton("Add segment")
        self.add_seg_btn.clicked.connect(self._add_assembly_segment_row)
        btn_row.addWidget(self.add_seg_btn)

        self.del_row_btn = QPushButton("Delete row")
        self.del_row_btn.clicked.connect(self._delete_selected_assembly_rows)
        btn_row.addWidget(self.del_row_btn)

        self.up_btn = QPushButton("Move up")
        self.up_btn.clicked.connect(lambda: self._move_selected_assembly_row(-1))
        btn_row.addWidget(self.up_btn)

        self.down_btn = QPushButton("Move down")
        self.down_btn.clicked.connect(lambda: self._move_selected_assembly_row(+1))
        btn_row.addWidget(self.down_btn)

        self.save_assembly_btn = QPushButton("Save to GeoPackage")
        self.save_assembly_btn.clicked.connect(self._save_assembly_to_gpkg)
        btn_row.addWidget(self.save_assembly_btn)

        layout.addLayout(btn_row)

        self.cable_table = QTableWidget()
        self.cable_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.cable_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        try:
            self.cable_table.setToolTip("Tip: single-click a row to highlight it; double-click to pan the map to it")
        except Exception:
            pass
        try:
            self.cable_table.cellDoubleClicked.connect(self._on_cable_table_cell_double_clicked)
        except Exception:
            pass
        try:
            self.cable_table.itemSelectionChanged.connect(self._on_cable_table_selection_changed)
        except Exception:
            pass
        layout.addWidget(self.cable_table)

        # Keep the SLD tab in sync with assembly edits.
        try:
            self.cable_table.itemChanged.connect(self._on_assembly_table_changed)
        except Exception:
            pass

        self.tab_widget.addTab(tab, "3) Cable Assembly")

    def _cable_table_row_value(self, row: int, col_name: str) -> str:
        if row < 0:
            return ""
        name_norm = (col_name or "").strip().lower()
        if not name_norm:
            return ""
        try:
            for c in range(self.cable_table.columnCount()):
                hi = self.cable_table.horizontalHeaderItem(c)
                if hi is None:
                    continue
                if (hi.text() or "").strip().lower() == name_norm:
                    it = self.cable_table.item(row, c)
                    return (it.text() or "").strip() if it else ""
        except Exception:
            return ""
        return ""

    def _managed_node_feature_by_node_id(self, node_id: str) -> Optional[QgsFeature]:
        nodes, _segs = self._managed_layers()
        if not nodes or not isinstance(nodes, QgsVectorLayer) or not nodes.isValid():
            return None
        nid = (node_id or "").strip()
        if not nid:
            return None
        idx = nodes.fields().lookupField("node_id")
        if idx < 0:
            return None
        try:
            for f in nodes.getFeatures():
                try:
                    if str(f[idx]) == nid:
                        return f
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _managed_segment_features_for_span(self, from_node_id: str, to_node_id: str) -> List[QgsFeature]:
        """Return managed segment features whose seq spans from_node_id..to_node_id.

        This mirrors the span logic used in `_autobuild_assembly_from_rpl`.
        """

        nodes, segs = self._managed_layers()
        if not nodes or not segs:
            return []
        if not isinstance(nodes, QgsVectorLayer) or not isinstance(segs, QgsVectorLayer):
            return []
        if not nodes.isValid() or not segs.isValid():
            return []

        a_id = (from_node_id or "").strip()
        b_id = (to_node_id or "").strip()
        if not a_id or not b_id:
            return []

        n_seq_idx = nodes.fields().lookupField("seq")
        n_id_idx = nodes.fields().lookupField("node_id")
        if n_id_idx < 0:
            return []

        def safe_int(v):
            try:
                return int(v)
            except Exception:
                return 10**18

        node_feats = list(nodes.getFeatures())
        node_feats.sort(key=lambda f: safe_int(f[n_seq_idx]) if n_seq_idx >= 0 else int(f.id()))

        node_seq_by_id: Dict[str, int] = {}
        for idx, f in enumerate(node_feats):
            try:
                nid = f[n_id_idx]
            except Exception:
                nid = None
            if nid in (None, ""):
                continue
            nid_s = str(nid)
            seq_val = None
            if n_seq_idx >= 0:
                try:
                    seq_val = int(f[n_seq_idx])
                except Exception:
                    seq_val = None
            if seq_val is None:
                seq_val = idx + 1
            node_seq_by_id[nid_s] = seq_val

        a = node_seq_by_id.get(a_id)
        b = node_seq_by_id.get(b_id)
        if a is None or b is None:
            return []

        lo = min(a, b)
        hi = max(a, b)

        s_seq_idx = segs.fields().lookupField("seq")
        seg_feats = list(segs.getFeatures())

        def safe_seq(f):
            if s_seq_idx >= 0:
                try:
                    return int(f[s_seq_idx])
                except Exception:
                    return 10**18
            return int(f.id())

        seg_feats.sort(key=safe_seq)

        out: List[QgsFeature] = []
        for sf in seg_feats:
            sseq = safe_seq(sf)
            if sseq < lo:
                continue
            if sseq > hi - 1:
                break
            out.append(sf)
        return out

    def _on_cable_table_selection_changed(self):
        if getattr(self, "_updating_assembly_table", False):
            return
        try:
            row = self.cable_table.currentRow()
        except Exception:
            return
        if row < 0:
            self._clear_rpl_table_highlight()
            return

        rt = self._cable_table_row_value(row, "row_type").upper()
        if rt == "BODY":
            node_id = self._cable_table_row_value(row, "node_id")
            feat = self._managed_node_feature_by_node_id(node_id)
            nodes, _segs = self._managed_layers()
            if feat is not None and nodes is not None:
                self._highlight_features_on_canvas(nodes, [feat])
            return

        if rt == "SEGMENT":
            from_nid = self._cable_table_row_value(row, "from_node_id")
            to_nid = self._cable_table_row_value(row, "to_node_id")
            feats = self._managed_segment_features_for_span(from_nid, to_nid)
            _nodes, segs = self._managed_layers()
            if feats and segs is not None:
                self._highlight_features_on_canvas(segs, feats)
            return

        # Unknown row type
        self._clear_rpl_table_highlight()

    def _on_cable_table_cell_double_clicked(self, row: int, _column: int):
        if getattr(self, "_updating_assembly_table", False):
            return

        rt = self._cable_table_row_value(row, "row_type").upper()
        if rt == "BODY":
            node_id = self._cable_table_row_value(row, "node_id")
            feat = self._managed_node_feature_by_node_id(node_id)
            nodes, _segs = self._managed_layers()
            if feat is None or nodes is None:
                return
            self._highlight_features_on_canvas(nodes, [feat])
            self._pan_canvas_to_feature(nodes, feat)
            return

        if rt == "SEGMENT":
            from_nid = self._cable_table_row_value(row, "from_node_id")
            to_nid = self._cable_table_row_value(row, "to_node_id")
            feats = self._managed_segment_features_for_span(from_nid, to_nid)
            _nodes, segs = self._managed_layers()
            if not feats or segs is None:
                return
            self._highlight_features_on_canvas(segs, feats)
            # Pan to combined geometry center
            try:
                geoms = [f.geometry() for f in feats if f is not None and f.geometry() is not None and not f.geometry().isEmpty()]
                if not geoms:
                    return
                bbox = geoms[0].boundingBox()
                for g in geoms[1:]:
                    try:
                        bbox.combineExtentWith(g.boundingBox())
                    except Exception:
                        pass
                self._pan_canvas_to_geometry(segs, QgsGeometry.fromRect(bbox))
            except Exception:
                # fallback: pan to first feature
                try:
                    self._pan_canvas_to_feature(segs, feats[0])
                except Exception:
                    pass
            return

    def _build_assembly_sld_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Layout:"))
        self.sld_layout_combo = QComboBox()
        self.sld_layout_combo.addItem("Wrapped", "wrap")
        self.sld_layout_combo.addItem("Single line", "single")
        self.sld_layout_combo.currentIndexChanged.connect(self._on_sld_layout_changed)
        top_row.addWidget(self.sld_layout_combo)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.assembly_sld_view = _AssemblySldView(tab)
        layout.addWidget(self.assembly_sld_view, 1)

        self.tab_widget.addTab(tab, "4) Assembly SLD")

    def _build_kp_sld_tab(self):
        """Embed the existing SLD tool inside the RPL Manager.

        Implementation detail: we host the existing `StraightLineDiagramDockWidget`
        as a child widget (title bar hidden) so we can reuse its mature UI
        without duplicating logic.
        """

        tab = QWidget()
        layout = QVBoxLayout(tab)

        self._kp_sld_dock = None
        try:
            from qgis.PyQt.QtWidgets import QDockWidget
            from ..sld_dockwidget import StraightLineDiagramDockWidget

            dock = StraightLineDiagramDockWidget(self.iface, parent=tab)
            try:
                dock.setTitleBarWidget(QWidget())
            except Exception:
                pass
            try:
                dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
            except Exception:
                pass

            layout.addWidget(dock, 1)
            self._kp_sld_dock = dock

            # Try to auto-wire the managed layers into the SLD selection.
            try:
                self._sync_embedded_kp_sld_selection()
            except Exception:
                pass

        except Exception as e:
            msg = (
                "SLD could not be embedded. This tool requires optional plotting dependencies "
                "(e.g. matplotlib).\n\n"
                f"Details: {e}"
            )
            lbl = QLabel(msg)
            lbl.setWordWrap(True)
            layout.addWidget(lbl)

        self.tab_widget.addTab(tab, "5) SLD")

    def _sync_embedded_kp_sld_selection(self):
        """Attempt to set SLD inputs to the currently selected managed layers."""

        dock = getattr(self, "_kp_sld_dock", None)
        if dock is None:
            return

        # Refresh layer combos so managed layers appear.
        try:
            dock.populate_layer_combos()
        except Exception:
            return

        _nodes, segs = self._managed_layers()
        if not segs:
            return

        # Prefer managed segments as the route line.
        try:
            for i in range(getattr(dock, "rpl_line_combo").count()):
                if getattr(dock, "rpl_line_combo").itemData(i) == segs.id():
                    getattr(dock, "rpl_line_combo").setCurrentIndex(i)
                    break
        except Exception:
            pass

        # If we have a managed GPKG prefix, prefer a matching KP ranges table.
        if self._gpkg_path and self._prefix:
            preferred_name = f"{self._prefix}_kp_ranges"
            try:
                for i in range(getattr(dock, "kp_ranges_combo").count()):
                    lid = getattr(dock, "kp_ranges_combo").itemData(i)
                    lyr = QgsProject.instance().mapLayer(lid) if lid else None
                    if not isinstance(lyr, QgsVectorLayer):
                        continue
                    if (lyr.name() or "") == preferred_name:
                        getattr(dock, "kp_ranges_combo").setCurrentIndex(i)
                        break
            except Exception:
                pass

    def _on_sld_layout_changed(self):
        if not hasattr(self, "assembly_sld_view") or not hasattr(self, "sld_layout_combo"):
            return
        mode = self.sld_layout_combo.currentData() or "wrap"
        try:
            self.assembly_sld_view.set_layout_mode(str(mode))
        except Exception:
            pass

    def _assembly_table_rows(self) -> List[Dict[str, object]]:
        cols = self._assembly_columns()
        out: List[Dict[str, object]] = []
        for r in range(self.cable_table.rowCount()):
            row: Dict[str, object] = {}
            for c, col in enumerate(cols):
                item = self.cable_table.item(r, c)
                row[col] = (item.text() if item else "")
            out.append(row)
        return out

    def _refresh_sld_from_assembly_table(self):
        if not hasattr(self, "assembly_sld_view"):
            return
        try:
            self.assembly_sld_view.set_rows(self._assembly_table_rows())
        except Exception:
            # Avoid crashing the dock if a drawing edge-case occurs
            pass

    def _on_assembly_table_changed(self, _item):
        if self._updating_assembly_table:
            return
        self._refresh_sld_from_assembly_table()

    def _build_depth_profile_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        layout.addWidget(QLabel(
            "This tab will host a depth profile linked to the managed route.\n"
            "For now it links to the existing Depth Profile tool."
        ))

        self.open_depth_btn = QPushButton("Open Depth Profile")
        self.open_depth_btn.clicked.connect(self._open_depth_profile)
        layout.addWidget(self.open_depth_btn)
        layout.addStretch(1)

        self.tab_widget.addTab(tab, "6) Depth Profile")

    # -----------------
    # Layer selection
    # -----------------

    def populate_layer_combos(self):
        project = QgsProject.instance()
        layers = list(project.mapLayers().values())

        def is_vec(layer: QgsMapLayer) -> bool:
            return isinstance(layer, QgsVectorLayer) and layer.isValid()

        def is_point(layer: QgsMapLayer) -> bool:
            return is_vec(layer) and layer.geometryType() == QgsWkbTypes.PointGeometry

        def is_line(layer: QgsMapLayer) -> bool:
            return is_vec(layer) and layer.geometryType() == QgsWkbTypes.LineGeometry

        # Source layers
        self._populate_combo(self.src_points_combo, [l for l in layers if is_point(l)])
        self._populate_combo(self.src_lines_combo, [l for l in layers if is_line(l)])

        # Managed layers (heuristic by field presence)
        managed_nodes = []
        managed_segs = []
        for layer in layers:
            if not is_vec(layer):
                continue
            names = set(f.name() for f in layer.fields())
            if layer.geometryType() == QgsWkbTypes.PointGeometry and {"node_id", "seq"}.issubset(names):
                managed_nodes.append(layer)
            if layer.geometryType() == QgsWkbTypes.LineGeometry and {"seg_id", "from_node_id", "to_node_id"}.issubset(names):
                managed_segs.append(layer)

        self._populate_combo(self.managed_nodes_combo, managed_nodes, preferred_id=self._managed_nodes_layer_id)
        self._populate_combo(self.managed_segs_combo, managed_segs, preferred_id=self._managed_segs_layer_id)

        # Assembly tables (no geometry) in project
        assembly_tables = []
        for layer in layers:
            if not is_vec(layer):
                continue
            if layer.wkbType() != QgsWkbTypes.NoGeometry:
                continue
            names = set(f.name() for f in layer.fields())
            if {"row_type", "seq"}.issubset(names) and ("assembly_row_id" in names):
                assembly_tables.append(layer)

        # If we can find an assembly table that matches prefix, remember it.
        if self._prefix:
            for lyr in assembly_tables:
                if lyr.name() == f"{self._prefix}_assembly":
                    self._assembly_layer_id = lyr.id()
                    break

        self._update_status()

        # Also refresh the managed list so it reflects newly loaded/removed layers.
        self._schedule_project_refresh()

    # -----------------
    # Setup tab: Managed RPL list
    # -----------------

    def _toggle_create_section(self):
        box = getattr(self, "create_box", None)
        if box is None:
            return
        try:
            if sip is not None and hasattr(sip, "isdeleted") and sip.isdeleted(box):
                return
            box.setVisible(not box.isVisible())
        except RuntimeError:
            # Qt widget may already be deleted during plugin reload/teardown.
            return
        except Exception:
            pass

    def _hook_project_signals(self):
        proj = QgsProject.instance()
        if self._project_signals_hooked:
            return
        try:
            proj.layersAdded.connect(self._on_project_layers_added)
            proj.layersRemoved.connect(self._on_project_layers_removed)
            self._project_signals_hooked = True
        except Exception:
            # Best-effort; if connection fails, keep app stable.
            self._project_signals_hooked = False

    def _unhook_project_signals(self, *_args):
        """Disconnect project-level signals.

        This is critical when the dockwidget is closed/reloaded: QGIS project signals
        can otherwise call back into Python closures that reference deleted Qt objects.
        """

        if not getattr(self, "_project_signals_hooked", False):
            return
        try:
            proj = QgsProject.instance()
            try:
                proj.layersAdded.disconnect(self._on_project_layers_added)
            except Exception:
                pass
            try:
                proj.layersRemoved.disconnect(self._on_project_layers_removed)
            except Exception:
                pass
        finally:
            self._project_signals_hooked = False

    def closeEvent(self, event):
        try:
            self._unhook_project_signals()
        except Exception:
            pass
        try:
            self._clear_rpl_table_highlight()
        except Exception:
            pass
        super().closeEvent(event)

    def _schedule_project_refresh(self):
        try:
            t = getattr(self, "_project_refresh_timer", None)
            if t is None:
                return
            if sip is not None and hasattr(sip, "isdeleted") and sip.isdeleted(t):
                return
            self._project_refresh_timer.start()
        except RuntimeError:
            # Timer may already be deleted during teardown.
            pass
        except Exception:
            pass

    def _on_project_layers_added(self, *_layers):
        self._schedule_project_refresh()

    def _on_project_layers_removed(self, *_layer_ids):
        self._schedule_project_refresh()

    def _get_recent_gpkgs(self) -> List[str]:
        v = self._settings.value("recent_gpkgs")
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [s.strip() for s in v.split("|") if s.strip()]
        return []

    def _set_recent_gpkgs(self, paths: List[str]):
        cleaned: List[str] = []
        seen = set()
        for p in paths:
            s = (p or "").strip()
            if not s:
                continue
            n = self._normalize_path(s)
            if n in seen:
                continue
            seen.add(n)
            cleaned.append(s)
        # Keep it short
        cleaned = cleaned[:10]
        try:
            self._settings.setValue("recent_gpkgs", cleaned)
        except Exception:
            # Fallback to string join
            try:
                self._settings.setValue("recent_gpkgs", "|".join(cleaned))
            except Exception:
                pass

    def _remember_recent_gpkg(self, gpkg: str):
        if not gpkg:
            return
        rec = [gpkg] + self._get_recent_gpkgs()
        self._set_recent_gpkgs(rec)

    def _save_active_managed_rpl(self, gpkg: str, prefix: str):
        try:
            self._settings.setValue("active_gpkg_path", (gpkg or "").strip())
            self._settings.setValue("active_prefix", (prefix or "").strip())
        except Exception:
            pass

    def _restore_active_managed_rpl(self):
        gpkg = str(self._settings.value("active_gpkg_path") or "").strip()
        prefix = str(self._settings.value("active_prefix") or "").strip()
        if not gpkg or not prefix:
            return
        if not os.path.exists(gpkg):
            return
        # Do not spam UI: activate quietly.
        self._activate_managed_rpl(gpkg, prefix, silent=True)

    def _detect_managed_rpls_in_project(self) -> Dict[Tuple[str, str], Dict[str, object]]:
        """Return mapping (gpkg_path, prefix) -> metadata for managed RPL sets loaded in the project."""

        out: Dict[Tuple[str, str], Dict[str, object]] = {}
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
                continue

            name = (lyr.name() or "").strip()
            src_path, _src_layer = self._parse_ogr_source(lyr.source() or "")
            if not src_path or not str(src_path).lower().endswith(".gpkg"):
                continue

            fields = set(f.name() for f in lyr.fields())

            if lyr.geometryType() == QgsWkbTypes.PointGeometry and name.endswith("_nodes") and {"node_id", "seq"}.issubset(fields):
                prefix = name[: -len("_nodes")]
                key = (src_path, prefix)
                out.setdefault(key, {"gpkg": src_path, "prefix": prefix})
                out[key]["nodes_layer_id"] = lyr.id()

            if lyr.geometryType() == QgsWkbTypes.LineGeometry and name.endswith("_segments") and {"seg_id", "from_node_id", "to_node_id"}.issubset(fields):
                prefix = name[: -len("_segments")]
                key = (src_path, prefix)
                out.setdefault(key, {"gpkg": src_path, "prefix": prefix})
                out[key]["segs_layer_id"] = lyr.id()

            if lyr.wkbType() == QgsWkbTypes.NoGeometry and name.endswith("_assembly") and {"row_type", "seq", "assembly_row_id"}.issubset(fields):
                prefix = name[: -len("_assembly")]
                key = (src_path, prefix)
                out.setdefault(key, {"gpkg": src_path, "prefix": prefix})
                out[key]["assembly_layer_id"] = lyr.id()

        # keep only valid sets (nodes+segments)
        valid: Dict[Tuple[str, str], Dict[str, object]] = {}
        for k, meta in out.items():
            if meta.get("nodes_layer_id") and meta.get("segs_layer_id"):
                valid[k] = meta
        return valid

    def _detect_valid_prefixes_in_gpkg(self, gpkg: str) -> Dict[str, Dict[str, bool]]:
        names = self._list_gpkg_layer_names(gpkg)
        prefixes = self._detect_managed_prefixes(names)
        return {p: meta for p, meta in prefixes.items() if meta.get("nodes") and meta.get("segments")}

    def _refresh_available_managed_rpls(self):
        if not hasattr(self, "managed_rpl_list"):
            return

        # Remember selection if possible
        prev_key = None
        try:
            it = self.managed_rpl_list.currentItem()
            if isinstance(it, QListWidgetItem):
                prev_key = it.data(Qt.UserRole)
        except Exception:
            prev_key = None

        self.managed_rpl_list.blockSignals(True)
        self.managed_rpl_list.clear()

        project_sets = self._detect_managed_rpls_in_project()

        # Include active + recent gpkg even if not currently loaded
        active_gpkg = str(self._settings.value("active_gpkg_path") or "").strip()
        recents = self._get_recent_gpkgs()
        candidates = []
        if active_gpkg:
            candidates.append(active_gpkg)
        candidates.extend(recents)

        # Create a lookup of existing keys for de-dupe
        seen = set((self._normalize_path(g), p) for (g, p) in project_sets.keys())

        # Add loaded project sets first
        for (_gpkg, _prefix), meta in sorted(project_sets.items(), key=lambda kv: (os.path.basename(kv[0][0]).lower(), kv[0][1].lower())):
            gpkg = str(meta.get("gpkg") or "")
            prefix = str(meta.get("prefix") or "")
            disp = f"{os.path.basename(gpkg)} :: {prefix}"
            item = QListWidgetItem(disp)
            item.setData(Qt.UserRole, {"gpkg": gpkg, "prefix": prefix})
            self.managed_rpl_list.addItem(item)

        # Add valid managed sets from recent gpkg files (not necessarily loaded)
        for gpkg in candidates:
            if not gpkg or not os.path.exists(gpkg):
                continue
            try:
                valids = self._detect_valid_prefixes_in_gpkg(gpkg)
            except Exception:
                continue
            for prefix in sorted(valids.keys()):
                key_n = (self._normalize_path(gpkg), prefix)
                if key_n in seen:
                    continue
                seen.add(key_n)
                disp = f"{os.path.basename(gpkg)} :: {prefix}"
                item = QListWidgetItem(disp)
                item.setData(Qt.UserRole, {"gpkg": gpkg, "prefix": prefix})
                self.managed_rpl_list.addItem(item)

        self.managed_rpl_list.blockSignals(False)

        # Restore selection
        if prev_key is not None:
            try:
                for i in range(self.managed_rpl_list.count()):
                    it = self.managed_rpl_list.item(i)
                    if it and it.data(Qt.UserRole) == prev_key:
                        self.managed_rpl_list.setCurrentRow(i)
                        break
            except Exception:
                pass

        self._on_managed_rpl_selection_changed()

    def _on_managed_rpl_selection_changed(self):
        it = self.managed_rpl_list.currentItem() if hasattr(self, "managed_rpl_list") else None
        if not isinstance(it, QListWidgetItem):
            try:
                self.managed_activate_btn.setEnabled(False)
                self.managed_stats_label.setText("")
            except Exception:
                pass
            return

        ref = it.data(Qt.UserRole) or {}
        gpkg = str(ref.get("gpkg") or "")
        prefix = str(ref.get("prefix") or "")
        self.managed_activate_btn.setEnabled(bool(gpkg and prefix))

        # Stats: if loaded, use layer counts; otherwise show path/prefix.
        nodes = self._find_loaded_gpkg_layer(gpkg, f"{prefix}_nodes")
        segs = self._find_loaded_gpkg_layer(gpkg, f"{prefix}_segments")
        assembly = self._find_loaded_gpkg_layer(gpkg, f"{prefix}_assembly")
        try:
            if nodes and segs:
                n = nodes.featureCount()
                s = segs.featureCount()
                a = assembly.featureCount() if assembly and assembly.isValid() else 0
                self.managed_stats_label.setText(
                    f"GeoPackage: {gpkg}\nPrefix: {prefix}\nFeatures: nodes={n}, segments={s}, assembly={a if assembly else 'n/a'}"
                )
            else:
                self.managed_stats_label.setText(f"GeoPackage: {gpkg}\nPrefix: {prefix}\n(Managed layers not currently loaded in the project)")
        except Exception:
            self.managed_stats_label.setText(f"GeoPackage: {gpkg}\nPrefix: {prefix}")

    def _browse_add_managed_gpkg(self):
        gpkg, _ = QFileDialog.getOpenFileName(self, "Select Managed RPL GeoPackage", "", "GeoPackage (*.gpkg)")
        if not gpkg:
            return

        if not os.path.exists(gpkg):
            QMessageBox.warning(self, "RPL Manager", "Selected GeoPackage does not exist.")
            return

        valids = self._detect_valid_prefixes_in_gpkg(gpkg)
        if not valids:
            QMessageBox.warning(
                self,
                "RPL Manager",
                "This GeoPackage does not look like a managed RPL.\n\nExpected tables like '<prefix>_nodes' and '<prefix>_segments'.",
            )
            return

        self._remember_recent_gpkg(gpkg)
        self._refresh_available_managed_rpls()

        # Auto-select + activate the first valid prefix.
        chosen_prefix = sorted(valids.keys())[0]
        # Prefer any prefix with assembly present.
        for p, meta in valids.items():
            if meta.get("assembly"):
                chosen_prefix = p
                break

        # Select in list
        try:
            for i in range(self.managed_rpl_list.count()):
                it = self.managed_rpl_list.item(i)
                ref = it.data(Qt.UserRole) if it else None
                if isinstance(ref, dict) and self._normalize_path(str(ref.get("gpkg") or "")) == self._normalize_path(gpkg) and str(ref.get("prefix") or "") == chosen_prefix:
                    self.managed_rpl_list.setCurrentRow(i)
                    break
        except Exception:
            pass

        self._activate_managed_rpl(gpkg, chosen_prefix, silent=False)

    def _activate_selected_managed_rpl(self):
        it = self.managed_rpl_list.currentItem() if hasattr(self, "managed_rpl_list") else None
        if not isinstance(it, QListWidgetItem):
            return
        ref = it.data(Qt.UserRole) or {}
        gpkg = str(ref.get("gpkg") or "")
        prefix = str(ref.get("prefix") or "")
        if not gpkg or not prefix:
            return
        self._activate_managed_rpl(gpkg, prefix, silent=False)

    def _activate_managed_rpl(self, gpkg: str, prefix: str, silent: bool = False):
        gpkg = (gpkg or "").strip()
        prefix = (prefix or "").strip()
        if not gpkg or not prefix:
            return
        if not os.path.exists(gpkg):
            if not silent:
                QMessageBox.warning(self, "RPL Manager", "Managed GeoPackage path is missing.")
            return

        # Validate prefix before loading
        try:
            valids = self._detect_valid_prefixes_in_gpkg(gpkg)
            if prefix not in valids:
                if not silent:
                    QMessageBox.warning(self, "RPL Manager", "Selected managed RPL is no longer valid (missing nodes/segments tables).")
                return
        except Exception:
            if not silent:
                QMessageBox.warning(self, "RPL Manager", "Could not read GeoPackage to validate managed tables.")
            return

        group_name = (self.group_edit.text() or "Managed RPL").strip() if hasattr(self, "group_edit") else "Managed RPL"
        nodes_layer = self._load_or_get_gpkg_layer(gpkg, f"{prefix}_nodes", group_name)
        segs_layer = self._load_or_get_gpkg_layer(gpkg, f"{prefix}_segments", group_name)
        assembly_layer = self._load_or_get_gpkg_layer(gpkg, f"{prefix}_assembly", group_name)

        if not nodes_layer or not segs_layer:
            if not silent:
                QMessageBox.warning(self, "RPL Manager", "Could not load managed nodes/segments layers from this GeoPackage.")
            return

        self._gpkg_path = gpkg
        self._prefix = prefix
        self._managed_nodes_layer_id = nodes_layer.id()
        self._managed_segs_layer_id = segs_layer.id()
        self._assembly_layer_id = assembly_layer.id() if assembly_layer else None

        # Persist active selection
        self._remember_recent_gpkg(gpkg)
        self._save_active_managed_rpl(gpkg, prefix)

        # Refresh dependent UI
        self.populate_layer_combos()
        self._refresh_rpl_table_view()
        self._load_or_init_assembly_table()

        # Provide a simple status line
        try:
            n_count = nodes_layer.featureCount()
            s_count = segs_layer.featureCount()
            a_count = assembly_layer.featureCount() if assembly_layer and assembly_layer.isValid() else 0
            self.status_label.setText(
                f"Active Managed RPL: {os.path.basename(gpkg)} :: {prefix}  (nodes={n_count}, segments={s_count}, assembly={a_count if assembly_layer else 'n/a'})"
            )
        except Exception:
            self.status_label.setText(f"Active Managed RPL: {os.path.basename(gpkg)} :: {prefix}")

        if not silent:
            self.iface.messageBar().pushMessage(
                "RPL Manager",
                f"Active Managed RPL set to '{prefix}'",
                level=Qgis.Success,
                duration=3,
            )

        # Update list stats
        try:
            self._refresh_available_managed_rpls()
        except Exception:
            pass

    def _populate_combo(self, combo: QComboBox, layers: List[QgsVectorLayer], preferred_id: Optional[str] = None):
        prev_id = preferred_id
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("— select —", None)
        for lyr in layers:
            combo.addItem(lyr.name(), lyr.id())
        # Restore selection while still blocking signals to avoid partial-update callbacks
        if prev_id:
            for i in range(combo.count()):
                if combo.itemData(i) == prev_id:
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)

    def _on_managed_selection_changed(self):
        self._managed_nodes_layer_id = self.managed_nodes_combo.currentData()
        self._managed_segs_layer_id = self.managed_segs_combo.currentData()
        self._update_status()
        self._refresh_rpl_table_view()
        self._load_or_init_assembly_table()

    def _managed_layers(self) -> Tuple[Optional[QgsVectorLayer], Optional[QgsVectorLayer]]:
        project = QgsProject.instance()
        nodes = project.mapLayer(self._managed_nodes_layer_id) if self._managed_nodes_layer_id else None
        segs = project.mapLayer(self._managed_segs_layer_id) if self._managed_segs_layer_id else None
        return nodes if isinstance(nodes, QgsVectorLayer) else None, segs if isinstance(segs, QgsVectorLayer) else None

    # -----------------
    # Actions
    # -----------------

    def _browse_output_gpkg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save GeoPackage", "", "GeoPackage (*.gpkg)")
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.output_gpkg_edit.setText(path)
            self._gpkg_path = path

    @staticmethod
    def _normalize_path(p: str) -> str:
        try:
            p2 = (p or "").strip().strip('"').strip("'")
            # QGIS sources often use forward slashes
            p2 = p2.replace("/", os.sep)
            p2 = os.path.abspath(p2)
            return os.path.normcase(os.path.normpath(p2))
        except Exception:
            return (p or "").strip()

    @staticmethod
    def _parse_ogr_source(source: str) -> Tuple[str, str]:
        """Return (path, layername) from common OGR source strings."""
        src = (source or "").strip()
        if not src:
            return "", ""
        parts = src.split("|")
        path_part = parts[0].strip().strip('"').strip("'")
        layername = ""
        for part in parts[1:]:
            p = part.strip()
            if p.lower().startswith("layername="):
                layername = p.split("=", 1)[1].strip()
                break
        return path_part, layername

    def _find_loaded_gpkg_layer(self, gpkg: str, layer_name: str) -> Optional[QgsVectorLayer]:
        gpkg_n = self._normalize_path(gpkg)
        target = (layer_name or "").strip()
        if not gpkg_n or not target:
            return None

        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
                continue
            if (lyr.name() or "").strip() != target:
                continue
            src_path, src_layer = self._parse_ogr_source(lyr.source() or "")
            if not src_path:
                continue
            if self._normalize_path(src_path) != gpkg_n:
                continue
            # If we can parse a layername, enforce it; else accept based on name+path.
            if src_layer and src_layer.strip() != target:
                continue
            return lyr
        return None

    def _load_or_get_gpkg_layer(self, gpkg: str, layer_name: str, group_name: str) -> Optional[QgsVectorLayer]:
        # Reuse already-loaded layer if present (avoid duplicates on repeated "Use this GeoPackage")
        existing = self._find_loaded_gpkg_layer(gpkg, layer_name)
        if existing is not None:
            return existing

        uri = f"{gpkg}|layername={layer_name}"
        lyr = QgsVectorLayer(uri, layer_name, "ogr")
        if not lyr.isValid():
            return None

        if group_name:
            root = QgsProject.instance().layerTreeRoot()
            group = root.findGroup(group_name) or root.addGroup(group_name)
            QgsProject.instance().addMapLayer(lyr, False)
            group.addLayer(lyr)
        else:
            QgsProject.instance().addMapLayer(lyr)
        return lyr

    def _list_gpkg_layer_names(self, gpkg: str) -> List[str]:
        # Prefer osgeo.ogr (stable across QGIS versions)
        try:
            from osgeo import ogr  # type: ignore

            ds = ogr.Open(gpkg, 0)
            if ds is None:
                return []
            names: List[str] = []
            for i in range(ds.GetLayerCount()):
                layer = ds.GetLayerByIndex(i)
                if layer is None:
                    continue
                nm = layer.GetName()
                if nm:
                    names.append(str(nm))
            return names
        except Exception:
            return []

    def _detect_managed_prefixes(self, layer_names: List[str]) -> Dict[str, Dict[str, bool]]:
        prefixes: Dict[str, Dict[str, bool]] = {}
        for nm in layer_names:
            if nm.endswith("_nodes"):
                p = nm[: -len("_nodes")]
                prefixes.setdefault(p, {"nodes": False, "segments": False, "assembly": False})
                prefixes[p]["nodes"] = True
            elif nm.endswith("_segments"):
                p = nm[: -len("_segments")]
                prefixes.setdefault(p, {"nodes": False, "segments": False, "assembly": False})
                prefixes[p]["segments"] = True
            elif nm.endswith("_assembly"):
                p = nm[: -len("_assembly")]
                prefixes.setdefault(p, {"nodes": False, "segments": False, "assembly": False})
                prefixes[p]["assembly"] = True
        return {p: meta for p, meta in prefixes.items() if any(meta.values())}

    def _run_conversion(self):
        points_id = self.src_points_combo.currentData()
        lines_id = self.src_lines_combo.currentData()
        out_gpkg = (self.output_gpkg_edit.text() or "").strip()
        prefix = (self.prefix_edit.text() or "").strip()
        group_name = (self.group_edit.text() or "").strip()

        if not points_id:
            QMessageBox.warning(self, "RPL Manager", "Select a source RPL points layer.")
            return
        if not out_gpkg:
            QMessageBox.warning(self, "RPL Manager", "Choose an output GeoPackage path.")
            return

        # Use QGIS Processing programmatically.
        try:
            import processing  # type: ignore
            from qgis.core import QgsProcessingFeedback

            params = {
                "INPUT_POINTS": QgsProject.instance().mapLayer(points_id),
                "INPUT_LINES": QgsProject.instance().mapLayer(lines_id) if lines_id else None,
                "OUTPUT_GPKG": out_gpkg,
                "OUTPUT_PREFIX": prefix or "RPL",
                "OUTPUT_GROUP": group_name or "Managed RPL",
                "RPL_ID": "",
                "ORDER_MODE": 0,
                "OUTPUT_MODE": 0,
                "REBUILD_SEGMENTS": True,
            }

            feedback = QgsProcessingFeedback()
            result = processing.run("subsea_cable_processing:convertimportedrpltomanagedgpkg", params, feedback=feedback)

            # QGIS can return QgsProcessingOutputMultipleLayers as a dict OR a list depending on version/context.
            raw_out_layers = (result or {}).get("OUTPUT_LAYERS")
            nodes_id: Optional[str] = None
            segs_id: Optional[str] = None
            detected_prefix: Optional[str] = None

            def _try_set_prefix_from_layer_name(layer_name: str):
                nonlocal detected_prefix
                if layer_name.endswith("_nodes"):
                    detected_prefix = layer_name[: -len("_nodes")]
                elif layer_name.endswith("_segments"):
                    detected_prefix = layer_name[: -len("_segments")]

            # Case 1: dict {layer_name: layer_id}
            if isinstance(raw_out_layers, dict):
                for name, lid in raw_out_layers.items():
                    if not isinstance(name, str):
                        continue
                    if isinstance(lid, str):
                        if name.endswith("_nodes"):
                            nodes_id = lid
                            _try_set_prefix_from_layer_name(name)
                        if name.endswith("_segments"):
                            segs_id = lid
                            _try_set_prefix_from_layer_name(name)

            # Case 2: list of layer ids or sources
            elif isinstance(raw_out_layers, (list, tuple)):
                for entry in raw_out_layers:
                    if not isinstance(entry, str):
                        continue
                    lyr = QgsProject.instance().mapLayer(entry)
                    if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
                        nm = lyr.name()
                        if nm.endswith("_nodes"):
                            nodes_id = lyr.id()
                            _try_set_prefix_from_layer_name(nm)
                        if nm.endswith("_segments"):
                            segs_id = lyr.id()
                            _try_set_prefix_from_layer_name(nm)

            # Persist gpkg + prefix (prefer actual produced prefix if the algorithm inferred one)
            self._gpkg_path = out_gpkg
            self._prefix = detected_prefix or (prefix or "RPL")

            # Fallback: if IDs weren't returned (or returned in an unexpected shape), load from gpkg.
            if not nodes_id or not segs_id:
                # If user left prefix default, the algorithm may have inferred a different one.
                # Scan the gpkg for any managed prefixes and pick the most complete match.
                layer_names = self._list_gpkg_layer_names(out_gpkg)
                prefixes = self._detect_managed_prefixes(layer_names)
                chosen_prefix = None
                if self._prefix and self._prefix in prefixes and prefixes[self._prefix].get("nodes") and prefixes[self._prefix].get("segments"):
                    chosen_prefix = self._prefix
                else:
                    for p, meta in prefixes.items():
                        if meta.get("nodes") and meta.get("segments"):
                            chosen_prefix = p
                            break
                if chosen_prefix:
                    self._prefix = chosen_prefix
                    group_name_eff = group_name or "Managed RPL"
                    nodes_layer = self._load_or_get_gpkg_layer(out_gpkg, f"{self._prefix}_nodes", group_name_eff)
                    segs_layer = self._load_or_get_gpkg_layer(out_gpkg, f"{self._prefix}_segments", group_name_eff)
                    if nodes_layer and segs_layer:
                        nodes_id = nodes_layer.id()
                        segs_id = segs_layer.id()

            self._managed_nodes_layer_id = nodes_id
            self._managed_segs_layer_id = segs_id
            self.populate_layer_combos()

            # Make the newly created managed RPL available + active.
            try:
                self._remember_recent_gpkg(out_gpkg)
                self._refresh_available_managed_rpls()
                self._activate_managed_rpl(out_gpkg, self._prefix, silent=True)
            except Exception:
                pass

            # Ensure/refresh assembly table after conversion
            self._load_or_init_assembly_table()

            if nodes_id and segs_id:
                self.iface.messageBar().pushMessage("RPL Manager", "Managed RPL created.", level=Qgis.Success, duration=4)
            else:
                self.iface.messageBar().pushMessage("RPL Manager", "Conversion completed, but could not auto-select outputs.", level=Qgis.Warning, duration=6)

        except Exception as e:
            QMessageBox.critical(self, "RPL Manager", f"Conversion failed:\n\n{e}")

    def _open_sld(self):
        # Delegate to existing tool if available.
        try:
            if hasattr(self.iface, "mainWindow") and self.iface.mainWindow():
                pass
            # Trigger the plugin action if present
            # (the plugin class owns the dock widget creation; safest is to ask user to open via toolbar).
            QMessageBox.information(
                self,
                "RPL Manager",
                "Open the existing SLD tool from the Subsea Cable Tools toolbar/menu.\n\n"
                "Next phase: the RPL Manager will embed the SLD view and auto-wire the managed layers.",
            )
        except Exception:
            pass

    def _open_depth_profile(self):
        try:
            QMessageBox.information(
                self,
                "RPL Manager",
                "Open the existing Depth Profile tool from the Subsea Cable Tools toolbar/menu.\n\n"
                "Next phase: the RPL Manager will drive the profile from the managed route.",
            )
        except Exception:
            pass

    # -----------------
    # Views
    # -----------------

    def _update_status(self):
        nodes, segs = self._managed_layers()
        if not nodes or not segs:
            self.status_label.setText(
                "Select a Managed RPL above (or create a new one).\n"
                "Once active, the other tabs will populate automatically."
            )
            return

        try:
            self.status_label.setText(
                f"Managed layers selected: {nodes.name()} (nodes={nodes.featureCount()}), "
                f"{segs.name()} (segments={segs.featureCount()})."
            )
        except Exception:
            self.status_label.setText(
                f"Managed layers selected: {nodes.name()} (nodes), {segs.name()} (segments)."
            )

    def _refresh_rpl_table_view(self):
        nodes, segs = self._managed_layers()
        if not nodes or not segs:
            self.rpl_table.setRowCount(0)
            self.rpl_table.setColumnCount(0)
            try:
                self._clear_rpl_table_highlight()
            except Exception:
                pass
            return

        # Pull features ordered by seq
        n_seq_idx = nodes.fields().lookupField("seq")
        s_seq_idx = segs.fields().lookupField("seq")

        node_feats = list(nodes.getFeatures())
        seg_feats = list(segs.getFeatures())

        def safe_int(v):
            try:
                return int(v)
            except Exception:
                return 10**18

        node_feats.sort(key=lambda f: safe_int(f[n_seq_idx]) if n_seq_idx >= 0 else int(f.id()))
        seg_feats.sort(key=lambda f: safe_int(f[s_seq_idx]) if s_seq_idx >= 0 else int(f.id()))

        # Alternating rows: node, segment, node, segment, ... , node
        columns = [
            "RowType",
            "seq",
            "PosNo",
            "Event",
            "DistCumulative",
            "CableDistCumulative",
            "ApproxDepth",
            "Remarks",
            "Slack",
            "CableType",
            "CableCode",
        ]

        rows: List[Dict[str, object]] = []
        row_targets: List[Optional[Dict[str, object]]] = []
        for i, n in enumerate(node_feats):
            rows.append({
                "RowType": "POINT",
                "seq": n[n_seq_idx] if n_seq_idx >= 0 else i + 1,
                "PosNo": n["PosNo"] if nodes.fields().lookupField("PosNo") >= 0 else None,
                "Event": n["Event"] if nodes.fields().lookupField("Event") >= 0 else None,
                "DistCumulative": n["DistCumulative"] if nodes.fields().lookupField("DistCumulative") >= 0 else None,
                "CableDistCumulative": n["CableDistCumulative"] if nodes.fields().lookupField("CableDistCumulative") >= 0 else None,
                "ApproxDepth": n["ApproxDepth"] if nodes.fields().lookupField("ApproxDepth") >= 0 else None,
                "Remarks": n["Remarks"] if nodes.fields().lookupField("Remarks") >= 0 else None,
                "Slack": None,
                "CableType": None,
                "CableCode": None,
            })
            row_targets.append({"row_type": "POINT", "layer_id": nodes.id(), "fid": int(n.id())})
            if i < len(seg_feats):
                s = seg_feats[i]
                rows.append({
                    "RowType": "LINE",
                    "seq": s[s_seq_idx] if s_seq_idx >= 0 else i + 1,
                    "PosNo": "",
                    "Event": "",
                    "DistCumulative": "",
                    "CableDistCumulative": "",
                    "ApproxDepth": "",
                    "Remarks": "",
                    "Slack": s["Slack"] if segs.fields().lookupField("Slack") >= 0 else None,
                    "CableType": s["CableType"] if segs.fields().lookupField("CableType") >= 0 else None,
                    "CableCode": s["CableCode"] if segs.fields().lookupField("CableCode") >= 0 else None,
                })
                row_targets.append({"row_type": "LINE", "layer_id": segs.id(), "fid": int(s.id())})

        self.rpl_table.setColumnCount(len(columns))
        self.rpl_table.setHorizontalHeaderLabels(columns)
        self.rpl_table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            for c, col in enumerate(columns):
                v = row.get(col)
                item = QTableWidgetItem("" if v is None else str(v))
                if c == 0:
                    try:
                        item.setData(Qt.UserRole, row_targets[r] if r < len(row_targets) else None)
                    except Exception:
                        pass
                self.rpl_table.setItem(r, c, item)

        self.rpl_table.resizeColumnsToContents()

    def _refresh_cable_assembly_view(self):
        # Back-compat: keep method name used elsewhere.
        self._load_or_init_assembly_table()

    # -----------------
    # Assembly table
    # -----------------

    def _assembly_layer_name(self) -> str:
        prefix = (self._prefix or self.prefix_edit.text() or "RPL").strip()
        if not prefix:
            prefix = "RPL"
        return f"{prefix}_assembly"

    def _get_or_load_assembly_layer(self) -> Optional[QgsVectorLayer]:
        if self._assembly_layer_id:
            lyr = QgsProject.instance().mapLayer(self._assembly_layer_id)
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
                return lyr

        gpkg = (self._gpkg_path or self.output_gpkg_edit.text() or "").strip()
        if not gpkg:
            return None

        name = self._assembly_layer_name()

        # Reuse already-loaded layer if present
        existing = self._find_loaded_gpkg_layer(gpkg, name)
        if existing is not None:
            self._assembly_layer_id = existing.id()
            return existing

        uri = f"{gpkg}|layername={name}"
        lyr = QgsVectorLayer(uri, name, "ogr")
        if lyr.isValid():
            # Load into project, ideally in same group
            group_name = (self.group_edit.text() or "Managed RPL").strip()
            if group_name:
                root = QgsProject.instance().layerTreeRoot()
                group = root.findGroup(group_name) or root.addGroup(group_name)
                QgsProject.instance().addMapLayer(lyr, False)
                group.addLayer(lyr)
            else:
                QgsProject.instance().addMapLayer(lyr)
            self._assembly_layer_id = lyr.id()
            return lyr

        return None

    def _create_empty_assembly_memory_layer(self, layer_name: str) -> QgsVectorLayer:
        lyr = QgsVectorLayer("None", layer_name, "memory")
        dp = lyr.dataProvider()
        fields = QgsFields()
        fields.append(QgsField("assembly_row_id", QVariant.String))
        fields.append(QgsField("seq", QVariant.Int))
        fields.append(QgsField("row_type", QVariant.String))  # BODY / SEGMENT
        fields.append(QgsField("label", QVariant.String))
        fields.append(QgsField("node_id", QVariant.String))
        fields.append(QgsField("from_node_id", QVariant.String))
        fields.append(QgsField("to_node_id", QVariant.String))
        fields.append(QgsField("cable_len", QVariant.Double))
        fields.append(QgsField("cable_type", QVariant.String))
        fields.append(QgsField("cable_code", QVariant.String))
        fields.append(QgsField("slack", QVariant.Double))
        fields.append(QgsField("length_mode", QVariant.String))
        fields.append(QgsField("notes", QVariant.String))
        fields.append(QgsField("source_ref", QVariant.String))
        dp.addAttributes(list(fields))
        lyr.updateFields()
        return lyr

    def _load_or_init_assembly_table(self):
        # Prefer loading existing assembly layer from gpkg if present.
        existing = self._get_or_load_assembly_layer()
        if existing is not None:
            self._populate_assembly_table_from_layer(existing)
            return

        # If we have managed layers selected, build a draft in UI (not persisted yet).
        self._autobuild_assembly_from_rpl(persist=False)

    def _populate_assembly_table_from_layer(self, layer: QgsVectorLayer):
        seq_idx = layer.fields().lookupField("seq")
        feats = list(layer.getFeatures())

        def safe_int(v):
            try:
                return int(v)
            except Exception:
                return 10**18

        feats.sort(key=lambda f: safe_int(f[seq_idx]) if seq_idx >= 0 else int(f.id()))

        columns = [
            "seq",
            "row_type",
            "label",
            "node_id",
            "from_node_id",
            "to_node_id",
            "cable_len",
            "cable_type",
            "cable_code",
            "slack",
            "length_mode",
            "notes",
        ]

        self._updating_assembly_table = True
        try:
            self.cable_table.blockSignals(True)
            self.cable_table.setColumnCount(len(columns))
            self.cable_table.setHorizontalHeaderLabels(columns)
            self.cable_table.setRowCount(len(feats))

            for r, f in enumerate(feats):
                for c, col in enumerate(columns):
                    val = None
                    try:
                        if layer.fields().lookupField(col) >= 0:
                            val = f[col]
                    except Exception:
                        val = None
                    item = QTableWidgetItem("" if val is None else str(val))
                    self.cable_table.setItem(r, c, item)

            self.cable_table.resizeColumnsToContents()
        finally:
            try:
                self.cable_table.blockSignals(False)
            except Exception:
                pass
            self._updating_assembly_table = False
        self._refresh_sld_from_assembly_table()

    def _autobuild_assembly_from_rpl(self, persist: bool = False):
        nodes, segs = self._managed_layers()
        if not nodes or not segs:
            self.cable_table.setRowCount(0)
            self.cable_table.setColumnCount(0)
            return

        keywords = [k.strip().lower() for k in (self.body_keywords_edit.text() or "").split(",") if k.strip()]

        n_seq_idx = nodes.fields().lookupField("seq")
        node_id_idx = nodes.fields().lookupField("node_id")
        event_idx = nodes.fields().lookupField("Event")
        pos_idx = nodes.fields().lookupField("PosNo")
        n_cable_cum_idx = nodes.fields().lookupField("CableDistCumulative")

        node_feats = list(nodes.getFeatures())

        def safe_int(v):
            try:
                return int(v)
            except Exception:
                return 10**18

        node_feats.sort(key=lambda f: safe_int(f[n_seq_idx]) if n_seq_idx >= 0 else int(f.id()))

        # Build quick lookup maps for span calculations
        node_seq_by_id: Dict[str, int] = {}
        node_cable_cum_by_id: Dict[str, Optional[float]] = {}
        for idx, f in enumerate(node_feats):
            nid = None
            if node_id_idx >= 0:
                nid = f[node_id_idx]
            if nid is None:
                continue
            nid_s = str(nid)
            seq_val = None
            if n_seq_idx >= 0:
                try:
                    seq_val = int(f[n_seq_idx])
                except Exception:
                    seq_val = None
            if seq_val is None:
                seq_val = idx + 1
            node_seq_by_id[nid_s] = seq_val

            cum_val: Optional[float] = None
            if n_cable_cum_idx >= 0:
                try:
                    v = f[n_cable_cum_idx]
                    cum_val = float(v) if v not in (None, "") else None
                except Exception:
                    cum_val = None
            node_cable_cum_by_id[nid_s] = cum_val

        s_seq_idx = segs.fields().lookupField("seq")
        s_from_idx = segs.fields().lookupField("from_node_id")
        s_to_idx = segs.fields().lookupField("to_node_id")
        s_type_idx = segs.fields().lookupField("CableType")
        s_code_idx = segs.fields().lookupField("CableCode")
        s_slack_idx = segs.fields().lookupField("Slack")
        s_cable_between_idx = segs.fields().lookupField("CableDistBetweenPos")

        seg_feats = list(segs.getFeatures())

        def safe_seq(f):
            if s_seq_idx >= 0:
                try:
                    return int(f[s_seq_idx])
                except Exception:
                    return 10**18
            return int(f.id())

        seg_feats.sort(key=safe_seq)

        def _span_segments(from_nid: Optional[str], to_nid: Optional[str]) -> List[QgsFeature]:
            if not from_nid or not to_nid:
                return []
            a = node_seq_by_id.get(from_nid)
            b = node_seq_by_id.get(to_nid)
            if a is None or b is None:
                return []
            lo = min(a, b)
            hi = max(a, b)
            # Managed segment seq is expected to correspond to the start node seq.
            # Span should include segments whose seq is in [lo, hi-1].
            out: List[QgsFeature] = []
            for sf in seg_feats:
                sseq = safe_seq(sf)
                if sseq < lo:
                    continue
                if sseq > hi - 1:
                    break
                out.append(sf)
            return out

        def _span_stats(from_nid: Optional[str], to_nid: Optional[str]) -> Dict[str, object]:
            stats: Dict[str, object] = {
                "cable_len": "",
                "cable_type": "",
                "cable_code": "",
                "slack": "",
                "notes": "",
            }
            if not from_nid or not to_nid:
                return stats

            span = _span_segments(from_nid, to_nid)
            if not span:
                stats["notes"] = "No matching managed segments for span"
                return stats

            # Cable type/code consistency check across the span
            types = set()
            codes = set()
            slack_sum: float = 0.0
            slack_any = False
            cable_between_sum: float = 0.0
            cable_between_any = False

            for sf in span:
                if s_type_idx >= 0:
                    v = sf[s_type_idx]
                    if v not in (None, ""):
                        types.add(str(v))
                if s_code_idx >= 0:
                    v = sf[s_code_idx]
                    if v not in (None, ""):
                        codes.add(str(v))
                if s_slack_idx >= 0:
                    try:
                        v = sf[s_slack_idx]
                        if v not in (None, ""):
                            slack_sum += float(v)
                            slack_any = True
                    except Exception:
                        pass
                if s_cable_between_idx >= 0:
                    try:
                        v = sf[s_cable_between_idx]
                        if v not in (None, ""):
                            cable_between_sum += float(v)
                            cable_between_any = True
                    except Exception:
                        pass

            if len(types) == 1:
                stats["cable_type"] = next(iter(types))
            elif len(types) > 1:
                stats["cable_type"] = "<MIXED>"
                notes = str(stats.get("notes") or "")
                stats["notes"] = notes + "CableType varies across span; "

            if len(codes) == 1:
                stats["cable_code"] = next(iter(codes))
            elif len(codes) > 1:
                stats["cable_code"] = "<MIXED>"
                notes = str(stats.get("notes") or "")
                stats["notes"] = notes + "CableCode varies across span; "

            if slack_any:
                stats["slack"] = str(slack_sum)

            # Prefer explicit segment cable distances if present; fall back to endpoint cumulative difference.
            if cable_between_any:
                notes = str(stats.get("notes") or "")
                stats["notes"] = notes + f"CableDistBetweenPos sum={cable_between_sum}; "
                stats["cable_len"] = str(cable_between_sum)
            else:
                a = node_cable_cum_by_id.get(from_nid)
                b = node_cable_cum_by_id.get(to_nid)
                if a is not None and b is not None:
                    notes = str(stats.get("notes") or "")
                    delta = abs(b - a)
                    stats["notes"] = notes + f"CableDistCumulative Δ={delta}; "
                    stats["cable_len"] = str(delta)
                else:
                    notes = str(stats.get("notes") or "")
                    stats["notes"] = notes + "No cable distance fields available; "

            # Trim trailing separators
            stats["notes"] = str(stats.get("notes") or "").strip()
            if stats["notes"].endswith(";"):
                stats["notes"] = stats["notes"].rstrip(";").strip()
            return stats

        # Detect bodies
        body_nodes: List[QgsFeature] = []
        for idx, f in enumerate(node_feats):
            ev = ""
            if event_idx >= 0:
                try:
                    ev = str(f[event_idx] or "")
                except Exception:
                    ev = ""
            ev_l = ev.lower()
            is_body = any(k in ev_l for k in keywords) if keywords else False

            # Always include first and last as bodies
            if idx == 0 or idx == len(node_feats) - 1:
                is_body = True

            if is_body:
                body_nodes.append(f)

        if len(body_nodes) < 2:
            # Fall back: treat all nodes as bodies (better than empty)
            body_nodes = node_feats

        # Build assembly rows: BODY, SEGMENT, BODY, ...
        rows: List[Dict[str, object]] = []
        for i, body in enumerate(body_nodes):
            node_id = body[node_id_idx] if node_id_idx >= 0 else None
            posno = body[pos_idx] if pos_idx >= 0 else None
            ev = body[event_idx] if event_idx >= 0 else ""
            label = str(ev) if ev else (f"Pos {posno}" if posno is not None else "Body")
            rows.append({
                "seq": len(rows) + 1,
                "row_type": "BODY",
                "label": label,
                "node_id": node_id,
                "from_node_id": "",
                "to_node_id": "",
                "cable_len": "",
                "cable_type": "",
                "cable_code": "",
                "slack": "",
                "length_mode": "",
                "notes": "",
            })

            if i < len(body_nodes) - 1:
                next_body = body_nodes[i + 1]
                from_node_id = node_id
                to_node_id = next_body[node_id_idx] if node_id_idx >= 0 else None
                stats = _span_stats(str(from_node_id) if from_node_id not in (None, "") else None, str(to_node_id) if to_node_id not in (None, "") else None)
                rows.append({
                    "seq": len(rows) + 1,
                    "row_type": "SEGMENT",
                    "label": "",
                    "node_id": "",
                    "from_node_id": from_node_id,
                    "to_node_id": to_node_id,
                    "cable_len": stats.get("cable_len", ""),
                    "cable_type": stats.get("cable_type", ""),
                    "cable_code": stats.get("cable_code", ""),
                    "slack": stats.get("slack", ""),
                    "length_mode": "SLACK_LOCKED",
                    "notes": stats.get("notes", ""),
                })

        # Populate UI table
        columns = [
            "seq",
            "row_type",
            "label",
            "node_id",
            "from_node_id",
            "to_node_id",
            "cable_len",
            "cable_type",
            "cable_code",
            "slack",
            "length_mode",
            "notes",
        ]
        self._updating_assembly_table = True
        try:
            self.cable_table.blockSignals(True)
            self.cable_table.setColumnCount(len(columns))
            self.cable_table.setHorizontalHeaderLabels(columns)
            self.cable_table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, col in enumerate(columns):
                    self.cable_table.setItem(r, c, QTableWidgetItem("" if row.get(col) is None else str(row.get(col))))
            self.cable_table.resizeColumnsToContents()
        finally:
            try:
                self.cable_table.blockSignals(False)
            except Exception:
                pass
            self._updating_assembly_table = False

        self._refresh_sld_from_assembly_table()

        if persist:
            self._save_assembly_to_gpkg()

    def _selected_rows(self) -> List[int]:
        rows = sorted({idx.row() for idx in self.cable_table.selectionModel().selectedRows()})
        return rows

    def _add_assembly_body_row(self):
        r = self.cable_table.rowCount()
        self.cable_table.insertRow(r)
        defaults = {
            "seq": r + 1,
            "row_type": "BODY",
            "label": "",
            "node_id": "",
            "from_node_id": "",
            "to_node_id": "",
            "cable_len": "",
            "cable_type": "",
            "cable_code": "",
            "slack": "",
            "length_mode": "",
            "notes": "",
        }
        for c, col in enumerate(self._assembly_columns()):
            self.cable_table.setItem(r, c, QTableWidgetItem(str(defaults.get(col, ""))))
        self._refresh_sld_from_assembly_table()

    def _add_assembly_segment_row(self):
        r = self.cable_table.rowCount()
        self.cable_table.insertRow(r)
        defaults = {
            "seq": r + 1,
            "row_type": "SEGMENT",
            "label": "",
            "node_id": "",
            "from_node_id": "",
            "to_node_id": "",
            "cable_len": "",
            "cable_type": "",
            "cable_code": "",
            "slack": "",
            "length_mode": "SLACK_LOCKED",
            "notes": "",
        }
        for c, col in enumerate(self._assembly_columns()):
            self.cable_table.setItem(r, c, QTableWidgetItem(str(defaults.get(col, ""))))
        self._refresh_sld_from_assembly_table()

    def _delete_selected_assembly_rows(self):
        rows = self._selected_rows()
        if not rows:
            return
        for r in reversed(rows):
            self.cable_table.removeRow(r)
        self._renumber_assembly_seq()
        self._refresh_sld_from_assembly_table()
        # Make it explicit: assembly edits do NOT delete/modify RPL layers.
        try:
            self.iface.messageBar().pushMessage(
                "RPL Manager",
                "Removed rows from assembly table only (RPL unchanged).",
                level=Qgis.Info,
                duration=4,
            )
        except Exception:
            pass

    def _move_selected_assembly_row(self, delta: int):
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        r = rows[0]
        new_r = r + delta
        if new_r < 0 or new_r >= self.cable_table.rowCount():
            return

        cols = self._assembly_columns()
        # swap row contents
        for c in range(len(cols)):
            a = self.cable_table.item(r, c)
            b = self.cable_table.item(new_r, c)
            a_text = a.text() if a else ""
            b_text = b.text() if b else ""
            self.cable_table.setItem(r, c, QTableWidgetItem(b_text))
            self.cable_table.setItem(new_r, c, QTableWidgetItem(a_text))

        self._renumber_assembly_seq()
        self.cable_table.selectRow(new_r)
        self._refresh_sld_from_assembly_table()

    def _assembly_columns(self) -> List[str]:
        return [
            "seq",
            "row_type",
            "label",
            "node_id",
            "from_node_id",
            "to_node_id",
            "cable_len",
            "cable_type",
            "cable_code",
            "slack",
            "length_mode",
            "notes",
        ]

    def _renumber_assembly_seq(self):
        seq_col = 0
        for r in range(self.cable_table.rowCount()):
            self.cable_table.setItem(r, seq_col, QTableWidgetItem(str(r + 1)))

    def _save_assembly_to_gpkg(self):
        gpkg = (self._gpkg_path or self.output_gpkg_edit.text() or "").strip()
        if not gpkg:
            QMessageBox.warning(self, "RPL Manager", "Choose a Managed GeoPackage path in Setup tab first.")
            return

        layer_name = self._assembly_layer_name()
        mem = self._create_empty_assembly_memory_layer(layer_name)
        dp = mem.dataProvider()

        cols = self._assembly_columns()
        for r in range(self.cable_table.rowCount()):
            feat = QgsFeature(mem.fields())
            feat.setAttributes([None] * len(mem.fields()))

            # assembly_row_id
            feat.setAttribute("assembly_row_id", str(uuid.uuid4()))
            # seq + columns
            for col in cols:
                item = self.cable_table.item(r, cols.index(col))
                text = item.text() if item else ""
                if col == "seq":
                    try:
                        feat.setAttribute("seq", int(text))
                    except Exception:
                        feat.setAttribute("seq", r + 1)
                elif col in {"slack", "cable_len"}:
                    try:
                        feat.setAttribute(col, float(text) if text else None)
                    except Exception:
                        feat.setAttribute(col, None)
                else:
                    feat.setAttribute(col, text)

            # source_ref
            feat.setAttribute("source_ref", os.path.basename(gpkg))
            dp.addFeature(feat)

        mem.updateExtents()

        save_opts = QgsVectorFileWriter.SaveVectorOptions()
        save_opts.driverName = "GPKG"
        save_opts.fileEncoding = "UTF-8"
        save_opts.layerName = layer_name
        save_opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer if os.path.exists(gpkg) else QgsVectorFileWriter.CreateOrOverwriteFile

        err, _, _, err_msg = QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsProject.instance().transformContext(), save_opts)
        if err != QgsVectorFileWriter.NoError:
            QMessageBox.critical(self, "RPL Manager", f"Failed to write assembly table to GeoPackage:\n\n{err_msg}")
            return

        # Load/refresh layer
        uri = f"{gpkg}|layername={layer_name}"
        lyr = QgsVectorLayer(uri, layer_name, "ogr")
        if lyr.isValid():
            group_name = (self.group_edit.text() or "Managed RPL").strip()
            if group_name:
                root = QgsProject.instance().layerTreeRoot()
                group = root.findGroup(group_name) or root.addGroup(group_name)
                QgsProject.instance().addMapLayer(lyr, False)
                group.addLayer(lyr)
            else:
                QgsProject.instance().addMapLayer(lyr)
            self._assembly_layer_id = lyr.id()
            self.iface.messageBar().pushMessage("RPL Manager", "Assembly table saved.", level=Qgis.Success, duration=3)
        else:
            self.iface.messageBar().pushMessage("RPL Manager", "Assembly saved, but layer could not be loaded.", level=Qgis.Warning, duration=6)
            return
