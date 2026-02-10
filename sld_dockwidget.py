import json

from qgis.PyQt.QtCore import Qt, pyqtSignal, QSortFilterProxyModel
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
    QFileDialog,
)

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsCoordinateTransform,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsDistanceArea,
    QgsField,
    QgsFields,
    QgsFeature,
    QgsVectorFileWriter,
)

from qgis.PyQt.QtCore import QVariant

try:
    # Per-user persistence fallback (works across sessions)
    from qgis.PyQt.QtCore import QSettings
except Exception:  # pragma: no cover
    QSettings = None  # type: ignore
from qgis.gui import QgsVertexMarker

try:  # Safe sip import for deleted checks
    import sip  # type: ignore

    _sip_isdeleted = sip.isdeleted
except Exception:  # pragma: no cover

    def _sip_isdeleted(_obj):
        return False


from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from .kp_range_utils import extract_line_segment


class _EventFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._search = ""
        self._event_filter = "All"

    @staticmethod
    def _try_float(v):
        if v is None:
            return None
        # Prefer numeric roles if provided
        if isinstance(v, (int, float)):
            return float(v)
        try:
            s = str(v).strip()
        except Exception:
            return None
        if not s:
            return None
        # Common formatting: commas as thousand separators
        s = s.replace(",", "")
        try:
            return float(s)
        except Exception:
            return None

    def lessThan(self, left, right):
        """Numeric-aware sorting.

        If both sides can be interpreted as numbers, compare numerically.
        Otherwise fall back to string comparison.
        """
        model = self.sourceModel()
        if model is None:
            return super().lessThan(left, right)

        try:
            # Prefer explicit numeric role if present; else use DisplayRole.
            left_num = model.data(left, Qt.UserRole + 20)
            right_num = model.data(right, Qt.UserRole + 20)

            # Special-case KP column: it already stores numeric KP at UserRole+1.
            if left.column() == 0:
                left_num = model.data(left, Qt.UserRole + 1)
                right_num = model.data(right, Qt.UserRole + 1)

            lf = self._try_float(left_num)
            rf = self._try_float(right_num)
            if lf is None or rf is None:
                # Fallback: try parsing from displayed text
                lf = self._try_float(model.data(left, Qt.DisplayRole))
                rf = self._try_float(model.data(right, Qt.DisplayRole))
            if lf is not None and rf is not None:
                return lf < rf
        except Exception:
            pass

        try:
            ls = str(model.data(left, Qt.DisplayRole) or "")
            rs = str(model.data(right, Qt.DisplayRole) or "")
            return ls < rs
        except Exception:
            return super().lessThan(left, right)

    def set_search(self, text: str):
        self._search = (text or "").strip().lower()
        self.invalidateFilter()

    def set_event_filter(self, value: str):
        self._event_filter = value or "All"
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent):
        model = self.sourceModel()
        if model is None:
            return True

        if self._event_filter and self._event_filter != "All":
            try:
                # Event filter should work even if the Event field is not displayed.
                ev_item = model.item(source_row, 0)
                ev = str(ev_item.data(Qt.UserRole + 10) or "")
            except Exception:
                ev = ""
            if ev != self._event_filter:
                return False

        if not self._search:
            return True

        cols = model.columnCount(source_parent)
        for c in range(cols):
            idx = model.index(source_row, c, source_parent)
            val = str(model.data(idx) or "").lower()
            if self._search in val:
                return True
        return False


class SLDEventsDockWidget(QDockWidget):
    """Dockable window that lists SLD events and allows search/filter + navigation."""

    eventActivated = pyqtSignal(float, object)  # kp_km, map_point (QgsPointXY or None)
    dockClosed = pyqtSignal()
    columnsChanged = pyqtSignal(list)

    def __init__(self, iface, parent=None):
        super().__init__("SLD Events", parent)
        self.iface = iface
        self.setObjectName("SLDEventsDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        w = QWidget()
        self.setWidget(w)
        layout = QVBoxLayout(w)

        self._available_fields = []
        self._selected_fields = []
        self._event_field_name = None
        self._rows = []

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Events"), 1)
        self.columns_btn = QPushButton("Columns...")
        self.columns_btn.clicked.connect(self._configure_columns)
        top_row.addWidget(self.columns_btn)
        layout.addLayout(top_row)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search events...")
        layout.addWidget(self.search_edit)

        self.event_filter_combo = QComboBox()
        self.event_filter_combo.addItem("All")
        layout.addWidget(self.event_filter_combo)

        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)

        self._updating_header = False

        self.model = QStandardItemModel(0, 1, self)
        self.model.setHorizontalHeaderLabels(["KP (km)"])
        self.proxy = _EventFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setDynamicSortFilter(True)
        self.table.setModel(self.proxy)
        self.table.sortByColumn(0, Qt.AscendingOrder)

        try:
            header = self.table.horizontalHeader()
            header.setSectionsMovable(True)
            header.sectionMoved.connect(self._on_header_moved)
        except Exception:
            pass

        self.search_edit.textChanged.connect(self.proxy.set_search)
        self.event_filter_combo.currentTextChanged.connect(self.proxy.set_event_filter)
        self.table.doubleClicked.connect(self._on_double_clicked)

    def closeEvent(self, event):
        try:
            self.dockClosed.emit()
        except Exception:
            pass
        super().closeEvent(event)

    def set_events(self, rows, distinct_events=None):
        """Rows: list of dicts with keys kp_km,map_pt,attrs (dict of field->value), event_value (optional)"""
        self._rows = rows or []
        self._rebuild_model(distinct_events=distinct_events)

    def set_schema(self, available_fields, selected_fields, event_field_name=None):
        self._available_fields = list(available_fields or [])
        # De-dup while preserving order
        seen = set()
        clean_sel = []
        for f in (selected_fields or []):
            if f and f in self._available_fields and f not in seen:
                seen.add(f)
                clean_sel.append(f)
        self._selected_fields = clean_sel
        self._event_field_name = event_field_name if event_field_name in self._available_fields else None
        self._rebuild_model()

    def get_selected_fields(self):
        return list(self._selected_fields)

    def _rebuild_model(self, distinct_events=None):
        self._updating_header = True
        headers = ["KP (km)"] + list(self._selected_fields)
        self.model.clear()
        self.model.setColumnCount(len(headers))
        self.model.setHorizontalHeaderLabels(headers)

        # Populate filter values
        if self._event_field_name:
            self.event_filter_combo.setVisible(True)
            if distinct_events is None:
                distinct_events = set()
                for r in self._rows:
                    v = r.get("event_value")
                    if v:
                        distinct_events.add(str(v))
        else:
            self.event_filter_combo.setVisible(False)
            distinct_events = None

        if self.event_filter_combo.isVisible():
            try:
                self.event_filter_combo.blockSignals(True)
                current = self.event_filter_combo.currentText() or "All"
                self.event_filter_combo.clear()
                self.event_filter_combo.addItem("All")
                if distinct_events:
                    vals = list(distinct_events)
                    vals.sort()
                    if len(vals) > 500:
                        vals = vals[:500]
                    for v in vals:
                        self.event_filter_combo.addItem(v)
                idx = self.event_filter_combo.findText(current)
                if idx >= 0:
                    self.event_filter_combo.setCurrentIndex(idx)
            finally:
                try:
                    self.event_filter_combo.blockSignals(False)
                except Exception:
                    pass

        for r in self._rows:
            kp_km = float(r.get("kp_km") or 0.0)
            attrs = r.get("attrs") or {}
            items = [QStandardItem(f"{kp_km:,.3f}")]
            for f in self._selected_fields:
                txt = str(attrs.get(f, ""))
                it = QStandardItem(txt)
                # Provide a numeric sort key when possible (proxy reads UserRole+20)
                try:
                    num = _EventFilterProxy._try_float(txt)
                    if num is not None:
                        it.setData(num, Qt.UserRole + 20)
                except Exception:
                    pass
                items.append(it)

            # Navigation/filter payload on the KP item
            try:
                items[0].setData(kp_km, Qt.UserRole + 1)
                items[0].setData(r.get("map_pt"), Qt.UserRole + 2)
                items[0].setData(str(r.get("event_value") or ""), Qt.UserRole + 10)
                items[0].setData(kp_km, Qt.UserRole + 20)
            except Exception:
                pass
            self.model.appendRow(items)

        # Keep KP fixed at visual column 0
        try:
            header = self.table.horizontalHeader()
            if header.visualIndex(0) != 0:
                header.moveSection(header.visualIndex(0), 0)
        except Exception:
            pass

        self._updating_header = False

        try:
            self.table.resizeColumnsToContents()
        except Exception:
            pass

    def _configure_columns(self):
        if not self._available_fields:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Select Event Fields")
        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.addWidget(QLabel("Choose which fields to show in the events table:"))

        lst = QListWidget()
        lst.setSelectionMode(QAbstractItemView.NoSelection)
        for f in self._available_fields:
            it = QListWidgetItem(f)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if f in self._selected_fields else Qt.Unchecked)
            lst.addItem(it)
        dlg_layout.addWidget(lst, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        dlg_layout.addWidget(buttons)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)

        if dlg.exec_() != QDialog.Accepted:
            return

        selected = []
        for i in range(lst.count()):
            it = lst.item(i)
            if it.checkState() == Qt.Checked:
                selected.append(it.text())
        self._selected_fields = selected
        self._rebuild_model()
        try:
            self.columnsChanged.emit(list(self._selected_fields))
        except Exception:
            pass

    def _on_header_moved(self, logicalIndex, oldVisualIndex, newVisualIndex):
        # Persist the *visual* order (excluding KP) when the user drags columns.
        if self._updating_header:
            return

        try:
            header = self.table.horizontalHeader()

            # Force KP (logical 0) back to the first visual position
            if header.visualIndex(0) != 0:
                self._updating_header = True
                header.moveSection(header.visualIndex(0), 0)
                self._updating_header = False

            new_fields = []
            for visual in range(1, header.count()):
                logical = header.logicalIndex(visual)
                name = str(self.model.headerData(logical, Qt.Horizontal) or "")
                if name and name != "KP (km)":
                    new_fields.append(name)

            # Only accept if it matches the same set of fields
            if set(new_fields) == set(self._selected_fields):
                self._selected_fields = new_fields
                try:
                    self.columnsChanged.emit(list(self._selected_fields))
                except Exception:
                    pass
        except Exception:
            pass

    def select_nearest_kp(self, kp_km: float):
        """Select nearest KP row (based on rendered numeric KP in model)."""
        try:
            # Source rows are in model order; scan quickly.
            best_src_row = None
            best_d = None
            for src_row in range(self.model.rowCount()):
                it = self.model.item(src_row, 0)
                v = it.data(Qt.UserRole + 1)
                if v is None:
                    continue
                d = abs(float(v) - float(kp_km))
                if best_d is None or d < best_d:
                    best_d = d
                    best_src_row = src_row
            if best_src_row is None:
                return
            src_index = self.model.index(best_src_row, 0)
            proxy_index = self.proxy.mapFromSource(src_index)
            if not proxy_index.isValid():
                return
            self.table.selectRow(proxy_index.row())
        except Exception:
            pass

    def _on_double_clicked(self, proxy_index):
        if not proxy_index.isValid():
            return
        src = self.proxy.mapToSource(proxy_index)
        if not src.isValid():
            return
        try:
            item = self.model.item(src.row(), 0)
            kp_km = float(item.data(Qt.UserRole + 1))
            map_pt = item.data(Qt.UserRole + 2)
        except Exception:
            return
        try:
            self.eventActivated.emit(kp_km, map_pt)
        except Exception:
            pass


class StraightLineDiagramDockWidget(QDockWidget):
    """Straight Line Diagram (SLD) dock widget.

    MVP:
      - User selects RPL line layer and RPL points (events) layer.
      - SLD tab shows a KP-axis bar with event ticks.
      - Hover moves a synced map marker + crosshair.
      - Right-click jumps map to KP.
      - Pan/zoom via Matplotlib navigation toolbar.
    """

    _DEFAULT_EVENT_KP_FIELDS = [
        "CableDistCumulative",
        "DistCumulative",
        "KP",
        "kp",
        "Kp",
        "Chainage",
        "chainage",
        "DCC",
        "dcc",
    ]

    _PROJECT_GROUP = "subsea_cable_tools"
    _PROJECT_KEY = "sld_state_v1"
    _SETTINGS_KEY = "subsea_cable_tools/sld/state_v1"

    _DEFAULT_RANGE_START_FIELDS = ["start_kp", "StartKP", "Start_KP", "kp_from", "KPFrom", "KP_FROM"]
    _DEFAULT_RANGE_END_FIELDS = ["end_kp", "EndKP", "End_KP", "kp_to", "KPTo", "KP_TO"]
    _DEFAULT_RANGE_LABEL_FIELDS = ["label", "Label", "name", "Name"]
    _DEFAULT_RANGE_CATEGORY_FIELDS = ["category", "Category", "type", "Type"]
    _DEFAULT_RANGE_ENABLED_FIELDS = ["enabled", "Enabled", "active", "Active"]
    _DEFAULT_RANGE_REF_LINE_FIELDS = ["ref_line", "RefLine", "refline", "route", "Route"]

    def __init__(self, iface, parent=None):
        super().__init__("Straight Line Diagram", parent)
        self.iface = iface
        self.setObjectName("StraightLineDiagramDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        self.distance_area = QgsDistanceArea()
        self.marker = None

        self.merged_geometry = None
        self.line_parts = None
        self.line_length_m = 0.0

        self._motion_cid = None
        self._right_click_cid = None
        self._crosshair = None

        self._event_kps = []
        self._project_read_connected = False

        self._events_rows = []
        self._events_dock = None
        self._events_columns_by_points_key = {}
        self._events_event_field_name = None

        self._ranges_rows = []
        self._ranges_dock = None
        self._ranges_fields_by_table_key = {}
        self._ranges_overlay_layer_id = None
        self._ranges_layer = None
        self._ranges_layer_connections = []
        self._ranges_spans = []

        self._state_loaded = False
        self._populating_combos = False
        self._last_line_key = None
        self._last_points_key = None
        self._last_ranges_key = None

        self.tab_widget = QTabWidget()
        self.setWidget(self.tab_widget)

        # --- Selection tab ---
        self.selection_tab = QWidget()
        selection_layout = QVBoxLayout(self.selection_tab)

        selection_layout.addWidget(QLabel("RPL Line Layer:"))
        self.rpl_line_combo = QComboBox()
        selection_layout.addWidget(self.rpl_line_combo)

        try:
            self.rpl_line_combo.currentIndexChanged.connect(self._on_layer_selection_changed)
        except Exception:
            pass

        selection_layout.addWidget(QLabel("RPL Points (Events) Layer:"))
        self.rpl_points_combo = QComboBox()
        selection_layout.addWidget(self.rpl_points_combo)

        try:
            self.rpl_points_combo.currentIndexChanged.connect(self._on_layer_selection_changed)
        except Exception:
            pass

        # --- KP ranges (table) ---
        selection_layout.addWidget(QLabel("KP Ranges (Table) Layer:"))
        self.kp_ranges_combo = QComboBox()
        selection_layout.addWidget(self.kp_ranges_combo)
        try:
            self.kp_ranges_combo.currentIndexChanged.connect(self._on_layer_selection_changed)
        except Exception:
            pass

        ranges_map_row = QHBoxLayout()
        self.show_ranges_on_map_chk = QCheckBox("Show ranges on map")
        ranges_map_row.addWidget(self.show_ranges_on_map_chk, 1)
        self.save_ranges_btn = QPushButton("Save map ranges...")
        self.save_ranges_btn.clicked.connect(self._save_ranges_overlay_as)
        ranges_map_row.addWidget(self.save_ranges_btn)
        selection_layout.addLayout(ranges_map_row)
        try:
            self.show_ranges_on_map_chk.toggled.connect(self._on_ranges_visibility_changed)
        except Exception:
            pass

        self.new_ranges_table_btn = QPushButton("New ranges table")
        self.new_ranges_table_btn.clicked.connect(self._create_new_ranges_table)
        selection_layout.addWidget(self.new_ranges_table_btn)

        # Field mapping row
        mapping_row = QHBoxLayout()
        mapping_row.addWidget(QLabel("Start:"))
        self.range_start_field_combo = QComboBox()
        mapping_row.addWidget(self.range_start_field_combo)
        mapping_row.addWidget(QLabel("End:"))
        self.range_end_field_combo = QComboBox()
        mapping_row.addWidget(self.range_end_field_combo)
        selection_layout.addLayout(mapping_row)

        mapping_row2 = QHBoxLayout()
        mapping_row2.addWidget(QLabel("Label:"))
        self.range_label_field_combo = QComboBox()
        mapping_row2.addWidget(self.range_label_field_combo)
        mapping_row2.addWidget(QLabel("Category:"))
        self.range_category_field_combo = QComboBox()
        mapping_row2.addWidget(self.range_category_field_combo)
        selection_layout.addLayout(mapping_row2)

        mapping_row3 = QHBoxLayout()
        mapping_row3.addWidget(QLabel("Enabled:"))
        self.range_enabled_field_combo = QComboBox()
        mapping_row3.addWidget(self.range_enabled_field_combo)
        mapping_row3.addWidget(QLabel("Ref line:"))
        self.range_ref_line_field_combo = QComboBox()
        mapping_row3.addWidget(self.range_ref_line_field_combo)
        selection_layout.addLayout(mapping_row3)

        self.ranges_btn = QPushButton("Ranges")
        self.ranges_btn.clicked.connect(self.show_ranges_window)
        selection_layout.addWidget(self.ranges_btn)

        # Persist mapping changes
        for combo in [
            self.range_start_field_combo,
            self.range_end_field_combo,
            self.range_label_field_combo,
            self.range_category_field_combo,
            self.range_enabled_field_combo,
            self.range_ref_line_field_combo,
        ]:
            try:
                combo.currentIndexChanged.connect(self._on_ranges_mapping_changed)
            except Exception:
                pass

        buttons_row = QHBoxLayout()
        self.refresh_layers_btn = QPushButton("Refresh")
        self.refresh_layers_btn.clicked.connect(self.populate_layer_combos)
        buttons_row.addWidget(self.refresh_layers_btn)

        self.draw_btn = QPushButton("Draw SLD")
        self.draw_btn.clicked.connect(self.draw_sld)
        buttons_row.addWidget(self.draw_btn)

        self.events_btn = QPushButton("Events")
        self.events_btn.clicked.connect(self.show_events_window)
        buttons_row.addWidget(self.events_btn)
        selection_layout.addLayout(buttons_row)
        selection_layout.addStretch(1)

        self.tab_widget.addTab(self.selection_tab, "Selection")

        # --- SLD tab ---
        self.sld_tab = QWidget()
        self._sld_layout = QVBoxLayout(self.sld_tab)
        self.figure = None
        self.canvas = None
        self.toolbar = None
        self._ensure_matplotlib_widgets()
        self.tab_widget.addTab(self.sld_tab, "SLD")

        self._load_persisted_state()
        self.populate_layer_combos()
        try:
            self.iface.projectRead.connect(self.populate_layer_combos)
            self._project_read_connected = True
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_matplotlib_widgets()
        self.populate_layer_combos()
        # If we were disconnected on close, reconnect when shown again
        if not self._project_read_connected:
            try:
                self.iface.projectRead.connect(self.populate_layer_combos)
                self._project_read_connected = True
            except Exception:
                pass

    def _layer_persist_key(self, layer: QgsVectorLayer):
        """Build a stable-ish key for a layer across sessions."""
        if not layer:
            return None
        try:
            provider = layer.providerType() or ""
        except Exception:
            provider = ""
        try:
            source = layer.source() or ""
        except Exception:
            source = ""
        try:
            name = layer.name() or ""
        except Exception:
            name = ""
        return f"{provider}|{source}|{name}"

    def _load_persisted_state(self):
        if self._state_loaded:
            return
        self._state_loaded = True

        json_str = ""
        try:
            project = QgsProject.instance()
            json_str, ok = project.readEntry(self._PROJECT_GROUP, self._PROJECT_KEY, "")
            if not ok:
                json_str = ""
        except Exception:
            json_str = ""

        if not json_str:
            try:
                if QSettings is not None:
                    json_str = QSettings().value(self._SETTINGS_KEY, "")
            except Exception:
                json_str = ""

        if not json_str:
            return

        try:
            state = json.loads(json_str)
        except Exception:
            return

        self._last_line_key = state.get("last_line")
        self._last_points_key = state.get("last_points")
        self._last_ranges_key = state.get("last_ranges")

        m = state.get("points_columns")
        if isinstance(m, dict):
            self._events_columns_by_points_key = m

        m2 = state.get("ranges_fields")
        if isinstance(m2, dict):
            self._ranges_fields_by_table_key = m2

        try:
            show_map = bool(state.get("show_ranges_on_map", False))
            self.show_ranges_on_map_chk.setChecked(show_map)
        except Exception:
            pass

    def _save_persisted_state(self):
        state = {
            "last_line": self._last_line_key,
            "last_points": self._last_points_key,
            "last_ranges": self._last_ranges_key,
            "points_columns": self._events_columns_by_points_key,
            "ranges_fields": self._ranges_fields_by_table_key,
            "show_ranges_on_map": bool(self.show_ranges_on_map_chk.isChecked()),
        }

        try:
            json_str = json.dumps(state)
        except Exception:
            return

        try:
            project = QgsProject.instance()
            project.writeEntry(self._PROJECT_GROUP, self._PROJECT_KEY, json_str)
        except Exception:
            pass

        try:
            if QSettings is not None:
                QSettings().setValue(self._SETTINGS_KEY, json_str)
        except Exception:
            pass

    def _on_layer_selection_changed(self, *_args):
        if getattr(self, "_populating_combos", False):
            return

        line_layer = self._get_selected_line_layer()
        points_layer = self._get_selected_points_layer()
        ranges_layer = self._get_selected_ranges_layer()
        self._last_line_key = self._layer_persist_key(line_layer) if line_layer else None
        self._last_points_key = self._layer_persist_key(points_layer) if points_layer else None
        self._last_ranges_key = self._layer_persist_key(ranges_layer) if ranges_layer else None
        self._save_persisted_state()

        # Prevent stale derived overlay when selection changes.
        try:
            self._remove_ranges_overlay_layer()
        except Exception:
            pass

        # Always keep mapping combos in sync with the selected ranges table
        try:
            self._apply_ranges_schema_from_selected_table_layer()
        except Exception:
            pass

        if self._events_dock and not _sip_isdeleted(self._events_dock):
            try:
                self._apply_events_schema_from_selected_points_layer()
                self._refresh_events_dock()
            except Exception:
                pass

        if self._ranges_dock and not _sip_isdeleted(self._ranges_dock):
            try:
                self._apply_ranges_schema_from_selected_table_layer()
                self._refresh_ranges_dock()
            except Exception:
                pass

        # Rewire layer-change listeners for selected ranges layer
        try:
            self._connect_ranges_layer_signals()
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            self._on_layer_selection_changed()
        except Exception:
            pass
        self.cleanup_plot_and_marker()
        self.cleanup_matplotlib_resources_on_close()
        self._close_events_window()
        self._close_ranges_window()
        self._disconnect_ranges_layer_signals()
        self._remove_ranges_overlay_layer()
        try:
            self.iface.projectRead.disconnect(self.populate_layer_combos)
            self._project_read_connected = False
        except Exception:
            pass
        super().closeEvent(event)

    def show_events_window(self):
        if self._events_dock and not _sip_isdeleted(self._events_dock):
            try:
                self._events_dock.show()
                self._events_dock.raise_()
                self._events_dock.activateWindow()
            except Exception:
                pass
            return

        try:
            dock = SLDEventsDockWidget(self.iface, parent=self.iface.mainWindow())
        except Exception:
            dock = SLDEventsDockWidget(self.iface, parent=None)

        self._events_dock = dock
        try:
            dock.eventActivated.connect(self._on_event_activated)
            dock.dockClosed.connect(self._on_events_dock_closed)
            dock.columnsChanged.connect(self._on_events_columns_changed)
        except Exception:
            pass

        try:
            self.iface.addDockWidget(Qt.RightDockWidgetArea, dock)
        except Exception:
            # Fallback: show as floating window
            try:
                dock.setFloating(True)
            except Exception:
                pass

        self._apply_events_schema_from_selected_points_layer()
        self._refresh_events_dock()
        try:
            dock.show()
        except Exception:
            pass

    def _on_events_dock_closed(self):
        # User closed the events dock; remove and delete it safely.
        self._close_events_window()

    def _on_events_columns_changed(self, fields):
        # Persist per points-layer key so the selection is remembered across sessions.
        points_layer = self._get_selected_points_layer()
        if not points_layer:
            return
        try:
            key = self._layer_persist_key(points_layer)
            if key:
                self._events_columns_by_points_key[key] = list(fields or [])
        except Exception:
            pass
        self._save_persisted_state()

    def _close_events_window(self):
        dock = self._events_dock
        if not dock or _sip_isdeleted(dock):
            self._events_dock = None
            return
        try:
            dock.blockSignals(True)
        except Exception:
            pass
        try:
            self.iface.removeDockWidget(dock)
        except Exception:
            pass
        try:
            dock.deleteLater()
        except Exception:
            pass
        self._events_dock = None

    # -----------------------
    # KP ranges: UI + data
    # -----------------------

    def show_ranges_window(self):
        if self._ranges_dock and not _sip_isdeleted(self._ranges_dock):
            try:
                self._ranges_dock.show()
                self._ranges_dock.raise_()
                self._ranges_dock.activateWindow()
            except Exception:
                pass
            return

        try:
            dock = _KPRangeDockWidget(self, self.iface, parent=self.iface.mainWindow())
        except Exception:
            dock = _KPRangeDockWidget(self, self.iface, parent=None)

        self._ranges_dock = dock
        try:
            dock.rangesChanged.connect(self._on_ranges_changed_from_ui)
            dock.dockClosed.connect(self._on_ranges_dock_closed)
        except Exception:
            pass

        try:
            self.iface.addDockWidget(Qt.RightDockWidgetArea, dock)
        except Exception:
            try:
                dock.setFloating(True)
            except Exception:
                pass

        self._apply_ranges_schema_from_selected_table_layer()
        self._refresh_ranges_dock()
        try:
            dock.show()
        except Exception:
            pass

    def _on_ranges_dock_closed(self):
        self._close_ranges_window()

    def _close_ranges_window(self):
        dock = self._ranges_dock
        if not dock or _sip_isdeleted(dock):
            self._ranges_dock = None
            return
        try:
            dock.blockSignals(True)
        except Exception:
            pass
        try:
            self.iface.removeDockWidget(dock)
        except Exception:
            pass
        try:
            dock.deleteLater()
        except Exception:
            pass
        self._ranges_dock = None

    def _on_ranges_changed_from_ui(self):
        # Table changed; rebuild rows + redraw map overlay if enabled.
        try:
            self._rebuild_ranges_rows()
        except Exception:
            pass
        try:
            if self.show_ranges_on_map_chk.isChecked():
                self._update_ranges_overlay_layer()
        except Exception:
            pass
        try:
            # Redraw SLD spans (if the SLD has been drawn)
            self._redraw_ranges_spans_only()
        except Exception:
            pass

    def _on_ranges_visibility_changed(self, *_args):
        self._save_persisted_state()
        if self.show_ranges_on_map_chk.isChecked():
            try:
                self._update_ranges_overlay_layer()
            except Exception:
                pass
        else:
            self._remove_ranges_overlay_layer()

    def _on_ranges_mapping_changed(self, *_args):
        # Save mapping for this table layer
        ranges_layer = self._get_selected_ranges_layer()
        if not ranges_layer:
            return
        table_key = self._layer_persist_key(ranges_layer)
        if not table_key:
            return

        start_f = self.range_start_field_combo.currentData() or None
        end_f = self.range_end_field_combo.currentData() or None
        label_f = self.range_label_field_combo.currentData() or ""
        cat_f = self.range_category_field_combo.currentData() or ""
        enabled_f = self.range_enabled_field_combo.currentData() or ""
        ref_f = self.range_ref_line_field_combo.currentData() or None

        self._ranges_fields_by_table_key[table_key] = {
            "start": start_f,
            "end": end_f,
            "label": label_f,
            "category": cat_f,
            "enabled": enabled_f,
            "ref_line": ref_f,
        }
        self._save_persisted_state()
        try:
            self._rebuild_ranges_rows()
        except Exception:
            pass
        try:
            self._redraw_ranges_spans_only()
        except Exception:
            pass
        try:
            if self.show_ranges_on_map_chk.isChecked():
                self._update_ranges_overlay_layer()
        except Exception:
            pass

    def _get_selected_ranges_layer(self):
        layer_id = getattr(self, "kp_ranges_combo", None).currentData() if getattr(self, "kp_ranges_combo", None) else None
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if not layer or not isinstance(layer, QgsVectorLayer):
            return None
        # No-geometry table layers
        try:
            if layer.wkbType() == QgsWkbTypes.NoGeometry:
                return layer
        except Exception:
            pass
        try:
            if layer.geometryType() == QgsWkbTypes.NullGeometry:
                return layer
        except Exception:
            pass
        return None

    def _disconnect_ranges_layer_signals(self):
        for (sig, fn) in list(self._ranges_layer_connections or []):
            try:
                sig.disconnect(fn)
            except Exception:
                pass
        self._ranges_layer_connections = []
        self._ranges_layer = None

    def _connect_ranges_layer_signals(self):
        # Reconnect if selection changed.
        layer = self._get_selected_ranges_layer()
        if layer == self._ranges_layer:
            return
        self._disconnect_ranges_layer_signals()
        self._ranges_layer = layer
        if not layer:
            return

        # Keep derived output in sync with edits.
        def _on_any_change(*_a):
            self._on_ranges_changed_from_ui()

        try:
            layer.committedFeaturesAdded.connect(_on_any_change)
            self._ranges_layer_connections.append((layer.committedFeaturesAdded, _on_any_change))
        except Exception:
            pass
        try:
            layer.committedFeaturesRemoved.connect(_on_any_change)
            self._ranges_layer_connections.append((layer.committedFeaturesRemoved, _on_any_change))
        except Exception:
            pass
        try:
            layer.committedAttributeValuesChanges.connect(_on_any_change)
            self._ranges_layer_connections.append((layer.committedAttributeValuesChanges, _on_any_change))
        except Exception:
            pass
        try:
            layer.featureAdded.connect(_on_any_change)
            self._ranges_layer_connections.append((layer.featureAdded, _on_any_change))
        except Exception:
            pass
        try:
            layer.featureDeleted.connect(_on_any_change)
            self._ranges_layer_connections.append((layer.featureDeleted, _on_any_change))
        except Exception:
            pass
        try:
            layer.attributeValueChanged.connect(_on_any_change)
            self._ranges_layer_connections.append((layer.attributeValueChanged, _on_any_change))
        except Exception:
            pass

    def _pick_default_field(self, names, candidates):
        name_set = {n for n in names}
        for c in candidates:
            if c in name_set:
                return c
        return None

    def _apply_ranges_schema_from_selected_table_layer(self):
        ranges_layer = self._get_selected_ranges_layer()
        if not ranges_layer:
            for combo in [
                self.range_start_field_combo,
                self.range_end_field_combo,
                self.range_label_field_combo,
                self.range_category_field_combo,
                self.range_enabled_field_combo,
                self.range_ref_line_field_combo,
            ]:
                try:
                    combo.clear()
                except Exception:
                    pass
            return

        try:
            self._ensure_required_ranges_fields(ranges_layer)
        except Exception:
            pass

        names = [f.name() for f in ranges_layer.fields()]
        table_key = self._layer_persist_key(ranges_layer)
        saved = self._ranges_fields_by_table_key.get(table_key) if table_key else None

        def _fill(combo: QComboBox, allow_empty: bool):
            try:
                combo.blockSignals(True)
                combo.clear()
                if allow_empty:
                    combo.addItem("(none)", "")
                for n in names:
                    combo.addItem(n, n)
            finally:
                try:
                    combo.blockSignals(False)
                except Exception:
                    pass

        _fill(self.range_start_field_combo, allow_empty=False)
        _fill(self.range_end_field_combo, allow_empty=False)
        _fill(self.range_label_field_combo, allow_empty=True)
        _fill(self.range_category_field_combo, allow_empty=True)
        _fill(self.range_enabled_field_combo, allow_empty=True)
        _fill(self.range_ref_line_field_combo, allow_empty=False)

        start_f = None
        end_f = None
        label_f = ""
        cat_f = ""
        enabled_f = ""
        ref_f = None

        if isinstance(saved, dict):
            start_f = saved.get("start") or None
            end_f = saved.get("end") or None
            label_f = saved.get("label") or ""
            cat_f = saved.get("category") or ""
            enabled_f = saved.get("enabled") or ""
            ref_f = saved.get("ref_line") or None

        start_f = start_f if start_f in names else self._pick_default_field(names, self._DEFAULT_RANGE_START_FIELDS)
        end_f = end_f if end_f in names else self._pick_default_field(names, self._DEFAULT_RANGE_END_FIELDS)
        label_f = label_f if label_f in names else (self._pick_default_field(names, self._DEFAULT_RANGE_LABEL_FIELDS) or "")
        cat_f = cat_f if cat_f in names else (self._pick_default_field(names, self._DEFAULT_RANGE_CATEGORY_FIELDS) or "")
        enabled_f = enabled_f if enabled_f in names else (self._pick_default_field(names, self._DEFAULT_RANGE_ENABLED_FIELDS) or "")
        ref_f = ref_f if ref_f in names else self._pick_default_field(names, self._DEFAULT_RANGE_REF_LINE_FIELDS)

        def _set_current(combo: QComboBox, field_name: str):
            try:
                idx = combo.findData(field_name)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            except Exception:
                pass

        if start_f:
            _set_current(self.range_start_field_combo, start_f)
        if end_f:
            _set_current(self.range_end_field_combo, end_f)
        _set_current(self.range_label_field_combo, label_f)
        _set_current(self.range_category_field_combo, cat_f)
        _set_current(self.range_enabled_field_combo, enabled_f)
        if ref_f:
            _set_current(self.range_ref_line_field_combo, ref_f)

        if table_key:
            self._ranges_fields_by_table_key[table_key] = {
                "start": start_f,
                "end": end_f,
                "label": label_f,
                "category": cat_f,
                "enabled": enabled_f,
                "ref_line": ref_f,
            }
            self._save_persisted_state()

        # Keep dock in sync
        if self._ranges_dock and not _sip_isdeleted(self._ranges_dock):
            try:
                self._ranges_dock.set_schema(
                    start_field=start_f,
                    end_field=end_f,
                    label_field=label_f,
                    category_field=cat_f,
                    enabled_field=enabled_f,
                    ref_line_field=ref_f,
                )
            except Exception:
                pass

        # Also rebuild our internal rows
        self._rebuild_ranges_rows()

    def _ranges_field_map(self):
        ranges_layer = self._get_selected_ranges_layer()
        if not ranges_layer:
            return None
        table_key = self._layer_persist_key(ranges_layer)
        saved = self._ranges_fields_by_table_key.get(table_key) if table_key else None
        if isinstance(saved, dict):
            return saved
        return None

    def _ensure_required_ranges_fields(self, layer: QgsVectorLayer):
        # Ensure there is a ref_line field for traceability (required by spec).
        existing = {f.name() for f in layer.fields()}
        if any(n in existing for n in self._DEFAULT_RANGE_REF_LINE_FIELDS):
            return
        try:
            if not layer.isEditable():
                layer.startEditing()
        except Exception:
            pass
        try:
            pr = layer.dataProvider()
            pr.addAttributes([QgsField("ref_line", QVariant.String)])
            layer.updateFields()
        except Exception:
            pass

    def _create_new_ranges_table(self):
        # Create an editable no-geometry memory layer with required fields.
        try:
            layer = QgsVectorLayer("None", "KP Ranges", "memory")
        except Exception:
            QMessageBox.critical(self, "Error", "Could not create memory table layer.")
            return

        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("start_kp", QVariant.Double),
            QgsField("end_kp", QVariant.Double),
            QgsField("label", QVariant.String),
            QgsField("category", QVariant.String),
            QgsField("enabled", QVariant.Int),
            QgsField("ref_line", QVariant.String),
        ])
        layer.updateFields()
        QgsProject.instance().addMapLayer(layer)

        # Refresh combos so the new layer appears, then select it
        try:
            self.populate_layer_combos()
        except Exception:
            pass

        try:
            for i in range(self.kp_ranges_combo.count()):
                if self.kp_ranges_combo.itemData(i) == layer.id():
                    self.kp_ranges_combo.setCurrentIndex(i)
                    break
        except Exception:
            pass

        try:
            self._apply_ranges_schema_from_selected_table_layer()
        except Exception:
            pass

    def _rebuild_ranges_rows(self):
        self._ranges_rows = []
        ranges_layer = self._get_selected_ranges_layer()
        line_layer = self._get_selected_line_layer()
        if not ranges_layer or not line_layer:
            self._refresh_ranges_dock()
            return

        self._ensure_required_ranges_fields(ranges_layer)

        fmap = self._ranges_field_map() or {}
        start_f = fmap.get("start")
        end_f = fmap.get("end")
        label_f = fmap.get("label") or ""
        cat_f = fmap.get("category") or ""
        enabled_f = fmap.get("enabled") or ""
        ref_f = fmap.get("ref_line")

        if not start_f or not end_f or not ref_f:
            self._refresh_ranges_dock()
            return

        line_key = self._layer_persist_key(line_layer) or ""
        max_kp = (self.line_length_m / 1000.0) if self.line_length_m else None

        for feat in ranges_layer.getFeatures():
            try:
                ref_val = str(feat[ref_f] or "")
            except Exception:
                ref_val = ""
            if not ref_val or ref_val != line_key:
                continue

            try:
                s = float(feat[start_f])
                e = float(feat[end_f])
            except Exception:
                continue
            if s > e:
                s, e = e, s
            if max_kp is not None:
                if s < 0 or e < 0 or s > max_kp or e > max_kp:
                    # skip out of bounds
                    continue

            label = ""
            category = ""
            enabled = True
            if label_f:
                try:
                    label = str(feat[label_f] or "")
                except Exception:
                    label = ""
            if cat_f:
                try:
                    category = str(feat[cat_f] or "")
                except Exception:
                    category = ""
            if enabled_f:
                try:
                    v = feat[enabled_f]
                    if v is None:
                        enabled = True
                    elif isinstance(v, bool):
                        enabled = bool(v)
                    else:
                        enabled = str(v).strip().lower() not in ("0", "false", "no", "off", "")
                except Exception:
                    enabled = True

            self._ranges_rows.append(
                {
                    "fid": int(feat.id()),
                    "start_kp": float(s),
                    "end_kp": float(e),
                    "label": label,
                    "category": category,
                    "enabled": bool(enabled),
                    "ref_line": ref_val,
                }
            )

        self._ranges_rows.sort(key=lambda r: r.get("start_kp", 0.0))
        self._refresh_ranges_dock()

    def _refresh_ranges_dock(self):
        if not self._ranges_dock or _sip_isdeleted(self._ranges_dock):
            return
        try:
            self._ranges_dock.set_ranges(self._ranges_rows)
        except Exception:
            pass

    def _remove_ranges_overlay_layer(self):
        if not self._ranges_overlay_layer_id:
            return
        try:
            QgsProject.instance().removeMapLayer(self._ranges_overlay_layer_id)
        except Exception:
            pass
        self._ranges_overlay_layer_id = None

    def _ensure_ranges_overlay_layer(self):
        # Create a memory layer for derived range segments.
        if self._ranges_overlay_layer_id:
            lyr = QgsProject.instance().mapLayer(self._ranges_overlay_layer_id)
            if isinstance(lyr, QgsVectorLayer):
                return lyr

        crs = QgsProject.instance().crs()
        layer = QgsVectorLayer(f"LineString?crs={crs.authid()}", "SLD KP Ranges (derived)", "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("start_kp", QVariant.Double),
            QgsField("end_kp", QVariant.Double),
            QgsField("label", QVariant.String),
            QgsField("category", QVariant.String),
            QgsField("enabled", QVariant.Int),
            QgsField("ref_line", QVariant.String),
        ])
        layer.updateFields()
        QgsProject.instance().addMapLayer(layer)
        self._ranges_overlay_layer_id = layer.id()
        return layer

    def _update_ranges_overlay_layer(self):
        line_layer = self._get_selected_line_layer()
        if not line_layer or not self.merged_geometry or self.line_length_m <= 0:
            return

        overlay = self._ensure_ranges_overlay_layer()
        if not overlay:
            return

        try:
            overlay.dataProvider().truncate()
        except Exception:
            try:
                ids = [f.id() for f in overlay.getFeatures()]
                overlay.dataProvider().deleteFeatures(ids)
            except Exception:
                pass

        feats = []
        for r in self._ranges_rows:
            if not r.get("enabled", True):
                continue
            s = float(r.get("start_kp") or 0.0)
            e = float(r.get("end_kp") or 0.0)
            seg = extract_line_segment(self.merged_geometry, s, e, self.distance_area)
            if not seg or seg.isEmpty():
                continue
            f = QgsFeature(overlay.fields())
            f.setGeometry(seg)
            f.setAttributes([
                s,
                e,
                str(r.get("label") or ""),
                str(r.get("category") or ""),
                1 if r.get("enabled", True) else 0,
                str(r.get("ref_line") or ""),
            ])
            feats.append(f)

        if feats:
            try:
                overlay.dataProvider().addFeatures(feats)
            except Exception:
                pass

        try:
            overlay.updateExtents()
        except Exception:
            pass
        try:
            self.iface.layerTreeView().refreshLayerSymbology(overlay.id())
        except Exception:
            pass
        try:
            self.iface.mapCanvas().refresh()
        except Exception:
            pass

    def _save_ranges_overlay_as(self):
        if not self._ranges_overlay_layer_id:
            try:
                self._update_ranges_overlay_layer()
            except Exception:
                pass
        lyr = QgsProject.instance().mapLayer(self._ranges_overlay_layer_id) if self._ranges_overlay_layer_id else None
        if not isinstance(lyr, QgsVectorLayer):
            QMessageBox.information(self, "Save", "No derived ranges layer to save yet. Turn on 'Show ranges on map' and draw SLD.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Save Ranges Layer", "", "GeoPackage (*.gpkg);;Shapefile (*.shp)")
        if not path:
            return
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG" if path.lower().endswith(".gpkg") else "ESRI Shapefile"
        options.layerName = "kp_ranges" if options.driverName == "GPKG" else ""
        res, err, _newpath, _newlayer = QgsVectorFileWriter.writeAsVectorFormatV3(
            lyr,
            path,
            QgsProject.instance().transformContext(),
            options,
        )
        if res != QgsVectorFileWriter.NoError:
            QMessageBox.critical(self, "Save failed", f"Failed to save layer: {err}")
        else:
            QMessageBox.information(self, "Saved", f"Saved ranges layer to:\n{path}")

    def _refresh_events_dock(self):
        if not self._events_dock or _sip_isdeleted(self._events_dock):
            return
        try:
            distinct_events = {r.get("event_value") for r in self._events_rows if r.get("event_value")}
            self._events_dock.set_events(self._events_rows, distinct_events=distinct_events)
        except Exception:
            pass

    def _apply_events_schema_from_selected_points_layer(self):
        """Apply persisted/default column schema for the currently selected points layer."""
        if not self._events_dock or _sip_isdeleted(self._events_dock):
            return

        points_layer = self._get_selected_points_layer()
        if not points_layer:
            return

        try:
            available_fields = [f.name() for f in points_layer.fields()]
            field_set = set(available_fields)
            event_field = "Event" if "Event" in field_set else None

            kp_field = None
            try:
                kp_field = self._pick_event_kp_field(points_layer)
            except Exception:
                kp_field = None

            points_key = self._layer_persist_key(points_layer)
            sel = self._events_columns_by_points_key.get(points_key) if points_key else None

            if sel:
                sel = [f for f in sel if f in field_set and f != kp_field]

            if not sel:
                preferred = ["PosNo", "Event", "Remarks"]
                sel = [f for f in preferred if f in field_set and f != kp_field]
                if not sel:
                    sel = [f for f in available_fields if f != kp_field][:4]

                if points_key:
                    self._events_columns_by_points_key[points_key] = sel
                    self._save_persisted_state()

            self._events_dock.set_schema(available_fields, sel, event_field_name=event_field)
        except Exception:
            pass

    def _ensure_matplotlib_widgets(self):
        """Ensure Figure/Canvas/Toolbar exist (dockwidgets can be closed+reopened)."""
        if self.figure is not None and self.canvas is not None and self.toolbar is not None:
            return

        self.figure = Figure(figsize=(8, 3))
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, self)

        # Clear previous layout widgets (if any)
        try:
            for i in reversed(range(self._sld_layout.count())):
                item = self._sld_layout.itemAt(i)
                w = item.widget() if item else None
                if w is not None:
                    self._sld_layout.removeWidget(w)
                    w.setParent(None)
        except Exception:
            pass

        self._sld_layout.addWidget(self.toolbar)
        self._sld_layout.addWidget(self.canvas)

    def populate_layer_combos(self):
        self._load_persisted_state()

        prev_line_id = self.rpl_line_combo.currentData()
        prev_points_id = self.rpl_points_combo.currentData()
        prev_ranges_id = self.kp_ranges_combo.currentData() if getattr(self, "kp_ranges_combo", None) else None

        self._populating_combos = True
        try:
            try:
                self.rpl_line_combo.blockSignals(True)
                self.rpl_points_combo.blockSignals(True)
            except Exception:
                pass

            self.rpl_line_combo.clear()
            self.rpl_points_combo.clear()
            self.kp_ranges_combo.clear()

            restore_line_idx = None
            restore_points_idx = None
            restore_ranges_idx = None

            for layer in QgsProject.instance().mapLayers().values():
                if not isinstance(layer, QgsVectorLayer):
                    continue

                if layer.geometryType() == QgsWkbTypes.LineGeometry:
                    idx = self.rpl_line_combo.count()
                    self.rpl_line_combo.addItem(layer.name(), layer.id())
                    if self._last_line_key and self._layer_persist_key(layer) == self._last_line_key:
                        restore_line_idx = idx

                elif layer.geometryType() == QgsWkbTypes.PointGeometry:
                    idx = self.rpl_points_combo.count()
                    self.rpl_points_combo.addItem(layer.name(), layer.id())
                    if self._last_points_key and self._layer_persist_key(layer) == self._last_points_key:
                        restore_points_idx = idx

                else:
                    # no-geometry table layer
                    try:
                        if layer.wkbType() == QgsWkbTypes.NoGeometry or layer.geometryType() == QgsWkbTypes.NullGeometry:
                            idx = self.kp_ranges_combo.count()
                            self.kp_ranges_combo.addItem(layer.name(), layer.id())
                            if self._last_ranges_key and self._layer_persist_key(layer) == self._last_ranges_key:
                                restore_ranges_idx = idx
                    except Exception:
                        pass

            # Restore selection using persisted key; else keep prior selection by id.
            try:
                if restore_line_idx is not None:
                    self.rpl_line_combo.setCurrentIndex(restore_line_idx)
                elif prev_line_id:
                    for i in range(self.rpl_line_combo.count()):
                        if self.rpl_line_combo.itemData(i) == prev_line_id:
                            self.rpl_line_combo.setCurrentIndex(i)
                            break
            except Exception:
                pass

            try:
                if restore_points_idx is not None:
                    self.rpl_points_combo.setCurrentIndex(restore_points_idx)
                elif prev_points_id:
                    for i in range(self.rpl_points_combo.count()):
                        if self.rpl_points_combo.itemData(i) == prev_points_id:
                            self.rpl_points_combo.setCurrentIndex(i)
                            break
            except Exception:
                pass

            try:
                if restore_ranges_idx is not None:
                    self.kp_ranges_combo.setCurrentIndex(restore_ranges_idx)
                elif prev_ranges_id:
                    for i in range(self.kp_ranges_combo.count()):
                        if self.kp_ranges_combo.itemData(i) == prev_ranges_id:
                            self.kp_ranges_combo.setCurrentIndex(i)
                            break
            except Exception:
                pass
        finally:
            try:
                self.rpl_line_combo.blockSignals(False)
                self.rpl_points_combo.blockSignals(False)
            except Exception:
                pass
            self._populating_combos = False

        # After populating, apply ranges schema (mapping combos)
        try:
            self._apply_ranges_schema_from_selected_table_layer()
        except Exception:
            pass

    def _get_selected_line_layer(self):
        layer_id = self.rpl_line_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if not layer or not isinstance(layer, QgsVectorLayer) or layer.geometryType() != QgsWkbTypes.LineGeometry:
            return None
        return layer

    def _get_selected_points_layer(self):
        layer_id = self.rpl_points_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if not layer or not isinstance(layer, QgsVectorLayer) or layer.geometryType() != QgsWkbTypes.PointGeometry:
            return None
        return layer

    def _pick_event_kp_field(self, points_layer: QgsVectorLayer):
        field_names = {f.name() for f in points_layer.fields()}
        for name in self._DEFAULT_EVENT_KP_FIELDS:
            if name in field_names:
                return name
        return None

    def draw_sld(self):
        """Build and draw the SLD plot for the currently selected layers."""
        self._ensure_matplotlib_widgets()
        self.cleanup_plot_and_marker()

        ax = self.figure.add_subplot(111)
        ax.clear()

        line_layer = self._get_selected_line_layer()
        points_layer = self._get_selected_points_layer()
        if not line_layer or not points_layer:
            ax.set_title("Select an RPL line layer and an RPL points layer")
            self.canvas.draw()
            return

        # Persist the current layer selection now we have valid layers
        try:
            self._last_line_key = self._layer_persist_key(line_layer)
            self._last_points_key = self._layer_persist_key(points_layer)
            self._save_persisted_state()
        except Exception:
            pass

        # Set up distance area (matches other tools: assumes project CRS == layer CRS)
        project_crs = QgsProject.instance().crs()
        self.distance_area.setSourceCrs(project_crs, QgsProject.instance().transformContext())
        self.distance_area.setEllipsoid(QgsProject.instance().ellipsoid())

        # Merge all line features into one geometry
        line_features = [f for f in line_layer.getFeatures()]
        if not line_features:
            ax.set_title("RPL line layer has no features")
            self.canvas.draw()
            return
        geometries = [f.geometry() for f in line_features]
        merged = QgsGeometry.unaryUnion(geometries)
        if merged.isEmpty():
            ax.set_title("RPL line geometry is empty after merging")
            self.canvas.draw()
            return

        self.merged_geometry = merged
        self.line_parts = merged.asMultiPolyline() if merged.isMultipart() else [merged.asPolyline()]
        self.line_length_m = float(self.distance_area.measureLength(merged))

        # Rebuild ranges rows now that route length is known
        try:
            self._rebuild_ranges_rows()
        except Exception:
            pass

        # Event KP values
        kp_field = self._pick_event_kp_field(points_layer)
        if not kp_field:
            ax.set_title("No KP field found on points layer")
            try:
                self.iface.messageBar().pushMessage(
                    "SLD",
                    "RPL points layer is missing a known KP field (e.g. DistCumulative / CableDistCumulative / KP).",
                    level=1,
                    duration=6,
                )
            except Exception:
                pass
            self.canvas.draw()
            return

        available_fields = [f.name() for f in points_layer.fields()]
        field_set = set(available_fields)
        event_field = "Event" if "Event" in field_set else None
        self._events_event_field_name = event_field

        project_crs = QgsProject.instance().crs()
        transform = None
        try:
            if points_layer.crs() != project_crs:
                transform = QgsCoordinateTransform(points_layer.crs(), project_crs, QgsProject.instance())
        except Exception:
            transform = None

        raw_kps = []
        raw_rows = []  # kp_raw, attrs(dict), event_value, map_pt
        for feat in points_layer.getFeatures():
            try:
                kp_raw = float(feat[kp_field])
            except Exception:
                continue

            attrs = {}
            for fn in available_fields:
                try:
                    v = feat[fn]
                except Exception:
                    v = None
                attrs[fn] = "" if v is None else str(v)

            ev_val = attrs.get(event_field, "") if event_field else ""

            map_pt = None
            try:
                g = feat.geometry()
                if g and not g.isEmpty():
                    p = g.asPoint()
                    map_pt = QgsPointXY(p.x(), p.y())
                    if transform:
                        map_pt = transform.transform(map_pt)
            except Exception:
                map_pt = None

            raw_kps.append(kp_raw)
            raw_rows.append((kp_raw, attrs, ev_val, map_pt))

        raw_kps.sort()
        self._event_kps = raw_kps

        # Convert event KP units if they look like meters (common when using cumulative distance fields)
        length_km = self.line_length_m / 1000.0 if self.line_length_m else 0.0
        event_kps_km = raw_kps
        unit_note = ""
        if raw_kps and length_km > 0:
            try:
                max_ev = max(raw_kps)
                # Heuristic: if max event is far larger than route length in km but
                # still in the ballpark of route length in meters, treat as meters.
                if max_ev > (length_km * 5.0) and max_ev <= (self.line_length_m * 1.5):
                    event_kps_km = [v / 1000.0 for v in raw_kps]
                    unit_note = " (events: m→km)"
            except Exception:
                pass

        # Build rows for events dock (always in km, matching x-axis)
        treat_as_m = unit_note != ""
        self._events_rows = []
        for kp_raw, attrs, ev_val, map_pt in raw_rows:
            kp_km = (kp_raw / 1000.0) if treat_as_m else kp_raw
            self._events_rows.append(
                {
                    "kp_km": float(kp_km),
                    "map_pt": map_pt,
                    "attrs": attrs,
                    "event_value": ev_val,
                }
            )
        self._events_rows.sort(key=lambda r: r.get("kp_km", 0.0))

        # Prepare default / persisted visible columns for this points layer
        points_key = self._layer_persist_key(points_layer)
        sel = self._events_columns_by_points_key.get(points_key) if points_key else None
        # Validate against available fields
        if sel:
            sel = [f for f in sel if f in field_set and f != kp_field]
        if not sel:
            preferred = ["PosNo", "Event", "Remarks"]
            sel = [f for f in preferred if f in field_set and f != kp_field]
            if not sel:
                # fallback: first few fields excluding KP field
                sel = [f for f in available_fields if f != kp_field][:4]
            if points_key:
                self._events_columns_by_points_key[points_key] = sel
                self._save_persisted_state()

        if self._events_dock and not _sip_isdeleted(self._events_dock):
            try:
                self._events_dock.set_schema(available_fields, sel, event_field_name=event_field)
            except Exception:
                pass
        self._refresh_events_dock()

        # Draw base bar
        ax.hlines(y=0.0, xmin=0.0, xmax=length_km, linewidth=6, color="0.35", zorder=1)

        # Draw ranges as shaded spans behind events
        self._ranges_spans = []
        try:
            for r in self._ranges_rows:
                if not r.get("enabled", True):
                    continue
                s = float(r.get("start_kp") or 0.0)
                e = float(r.get("end_kp") or 0.0)
                if s > e:
                    s, e = e, s
                span = ax.axvspan(s, e, ymin=0.46, ymax=0.54, alpha=0.35, color="tab:blue", zorder=2)
                self._ranges_spans.append(span)
        except Exception:
            pass

        # Draw events as ticks
        if event_kps_km:
            ax.vlines(event_kps_km, ymin=0.12, ymax=0.85, linewidth=1.4, color="tab:red", zorder=3)

        # Crosshair (vertical line)
        self._crosshair = ax.axvline(x=0.0, color="k", linestyle="--", lw=1)

        ax.set_yticks([])
        ax.set_xlabel("KP (km)")
        ax.set_xlim(0.0, max(0.1, length_km))
        ax.set_ylim(-1.0, 1.0)
        ax.set_title(f"SLD | Length: {length_km:,.2f} km | Events: {len(raw_kps):,} | KP field: {kp_field}{unit_note}")
        ax.grid(True, axis="x", alpha=0.25)

        try:
            self.figure.tight_layout()
        except Exception:
            pass

        self.canvas.draw()
        self.tab_widget.setCurrentWidget(self.sld_tab)
        self._connect_canvas_events()

        # Update derived map overlay if enabled
        if self.show_ranges_on_map_chk.isChecked():
            try:
                self._update_ranges_overlay_layer()
            except Exception:
                pass

    def _redraw_ranges_spans_only(self):
        # If we have a current axes, update spans without rebuilding everything.
        if not self.figure or not self.canvas:
            return
        try:
            ax = self.figure.axes[0]
        except Exception:
            return

        try:
            for sp in list(self._ranges_spans or []):
                try:
                    sp.remove()
                except Exception:
                    pass
        except Exception:
            pass
        self._ranges_spans = []

        try:
            for r in self._ranges_rows:
                if not r.get("enabled", True):
                    continue
                s = float(r.get("start_kp") or 0.0)
                e = float(r.get("end_kp") or 0.0)
                if s > e:
                    s, e = e, s
                span = ax.axvspan(s, e, ymin=0.46, ymax=0.54, alpha=0.35, color="tab:blue", zorder=2)
                self._ranges_spans.append(span)
        except Exception:
            pass
        try:
            self.canvas.draw_idle()
        except Exception:
            pass

    def _connect_canvas_events(self):
        if not self.canvas:
            return
        if self._motion_cid is None:
            try:
                self._motion_cid = self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)
            except Exception:
                pass
        if self._right_click_cid is None:
            try:
                self._right_click_cid = self.canvas.mpl_connect("button_press_event", self._on_right_click)
            except Exception:
                pass

    def _disconnect_canvas_events(self):
        if not self.canvas:
            return
        if self._motion_cid is not None:
            try:
                self.canvas.mpl_disconnect(self._motion_cid)
            except Exception:
                pass
            self._motion_cid = None
        if self._right_click_cid is not None:
            try:
                self.canvas.mpl_disconnect(self._right_click_cid)
            except Exception:
                pass
            self._right_click_cid = None

    def _on_mouse_move(self, event):
        if not event.inaxes:
            if self.marker and self.marker.isVisible():
                try:
                    self.marker.hide()
                    self.iface.mapCanvas().refresh()
                except Exception:
                    pass
            if self._crosshair and self._crosshair.get_visible():
                self._crosshair.set_visible(False)
                try:
                    self.canvas.draw_idle()
                except Exception:
                    pass
            return

        if self._crosshair and not self._crosshair.get_visible():
            self._crosshair.set_visible(True)

        kp = event.xdata
        if kp is None:
            return
        if kp < 0:
            kp = 0.0
        max_kp = self.line_length_m / 1000.0 if self.line_length_m else 0.0
        if max_kp and kp > max_kp:
            kp = max_kp

        if self._crosshair:
            self._crosshair.set_xdata([kp, kp])
            try:
                self.canvas.draw_idle()
            except Exception:
                pass

        self._update_map_marker(kp)

        # Keep events window selection in sync (nearest event)
        if self._events_dock and not _sip_isdeleted(self._events_dock):
            try:
                self._events_dock.select_nearest_kp(kp)
            except Exception:
                pass

    def _on_event_activated(self, kp_km: float, map_pt):
        """From events dock: center map and sync SLD crosshair/marker."""
        try:
            kp_km = float(kp_km)
        except Exception:
            return

        if self._crosshair:
            try:
                self._crosshair.set_visible(True)
                self._crosshair.set_xdata([kp_km, kp_km])
                self.canvas.draw_idle()
            except Exception:
                pass

        self._update_map_marker(kp_km)

        if map_pt is not None:
            try:
                canvas = self.iface.mapCanvas()
                canvas.setCenter(map_pt)
                canvas.refresh()
                return
            except Exception:
                pass
        self._zoom_map_to_kp(kp_km)

    def _on_right_click(self, event):
        if event.button != 3:
            return
        if not event.inaxes:
            return
        kp = event.xdata
        if kp is None:
            return
        self._zoom_map_to_kp(kp)

    def _interpolate_point_along_line(self, distance_m: float):
        if not self.line_parts or not self.merged_geometry:
            return None
        if distance_m <= 0:
            first_point = self.line_parts[0][0]
            return QgsGeometry.fromPointXY(first_point)
        if distance_m >= self.line_length_m:
            last_part = self.line_parts[-1]
            last_point = last_part[-1]
            return QgsGeometry.fromPointXY(last_point)

        cumulative = 0.0
        for part in self.line_parts:
            for i in range(len(part) - 1):
                p1, p2 = part[i], part[i + 1]
                seg_len = float(self.distance_area.measureLine(p1, p2))
                if cumulative + seg_len >= distance_m:
                    dist_into = distance_m - cumulative
                    ratio = (dist_into / seg_len) if seg_len > 0 else 0.0
                    x = p1.x() + ratio * (p2.x() - p1.x())
                    y = p1.y() + ratio * (p2.y() - p1.y())
                    return QgsGeometry.fromPointXY(QgsPointXY(x, y))
                cumulative += seg_len
        return None

    def _ensure_marker(self):
        if self.marker and not _sip_isdeleted(self.marker):
            return
        self.marker = QgsVertexMarker(self.iface.mapCanvas())
        self.marker.setColor(Qt.red)
        self.marker.setIconSize(12)
        self.marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.marker.setPenWidth(3)
        try:
            self.iface.mapCanvas().scene().addItem(self.marker)
        except Exception:
            pass

    def _update_map_marker(self, kp_km: float):
        if not self.line_parts or self.line_length_m <= 0:
            return
        dist_m = float(kp_km) * 1000.0
        if dist_m < 0 or dist_m > self.line_length_m:
            return

        point_geom = self._interpolate_point_along_line(dist_m)
        if point_geom is None or point_geom.isEmpty():
            return

        self._ensure_marker()
        if self.marker and not self.marker.isVisible():
            try:
                self.marker.show()
            except Exception:
                pass

        try:
            self.marker.setCenter(point_geom.asPoint())
            self.iface.mapCanvas().refresh()
        except Exception:
            pass

    def _zoom_map_to_kp(self, kp_km: float):
        if not self.line_parts or self.line_length_m <= 0:
            return
        dist_m = float(kp_km) * 1000.0
        point_geom = self._interpolate_point_along_line(dist_m)
        if point_geom is None or point_geom.isEmpty():
            return
        point = point_geom.asPoint()
        canvas = self.iface.mapCanvas()
        try:
            canvas.setCenter(point)
            canvas.refresh()
        except Exception:
            pass

    def cleanup_plot_and_marker(self):
        self._disconnect_canvas_events()

        if self.marker:
            try:
                if not _sip_isdeleted(self.marker):
                    try:
                        self.marker.hide()
                    except Exception:
                        pass
                    try:
                        self.marker.deleteLater()
                    except Exception:
                        pass
            finally:
                self.marker = None

        if getattr(self, "figure", None):
            try:
                self.figure.clear()
            except Exception:
                pass
        self._crosshair = None
        self._event_kps = []
        self._ranges_spans = []
        self.merged_geometry = None
        self.line_parts = None
        self.line_length_m = 0.0

        if getattr(self, "canvas", None):
            try:
                self.canvas.draw()
            except Exception:
                pass

    def cleanup_matplotlib_resources_on_close(self):
        self._disconnect_canvas_events()
        if getattr(self, "figure", None):
            try:
                self.figure.clear()
                import matplotlib.pyplot as plt

                plt.close(self.figure)
            except Exception:
                pass
        self.figure = None
        self.canvas = None
        self.toolbar = None
        self._crosshair = None
        self._close_events_window()


class _KPRangeEditDialog(QDialog):
    def __init__(self, parent=None, *, start_kp=None, end_kp=None, label="", category="", enabled=True):
        super().__init__(parent)
        self.setWindowTitle("KP Range")
        layout = QVBoxLayout(self)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Start KP (km):"))
        self.start_edit = QLineEdit("" if start_kp is None else str(start_kp))
        row1.addWidget(self.start_edit)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("End KP (km):"))
        self.end_edit = QLineEdit("" if end_kp is None else str(end_kp))
        row2.addWidget(self.end_edit)
        layout.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Label:"))
        self.label_edit = QLineEdit(label or "")
        row3.addWidget(self.label_edit)
        layout.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Category:"))
        self.category_edit = QLineEdit(category or "")
        row4.addWidget(self.category_edit)
        layout.addLayout(row4)

        self.enabled_chk = QCheckBox("Enabled")
        self.enabled_chk.setChecked(bool(enabled))
        layout.addWidget(self.enabled_chk)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        def _to_float(txt):
            s = (txt or "").strip().replace(",", "")
            return float(s)

        return {
            "start_kp": _to_float(self.start_edit.text()),
            "end_kp": _to_float(self.end_edit.text()),
            "label": (self.label_edit.text() or "").strip(),
            "category": (self.category_edit.text() or "").strip(),
            "enabled": bool(self.enabled_chk.isChecked()),
        }


class _KPRangeDockWidget(QDockWidget):
    """Dockable editor for KP ranges stored in a table layer."""

    rangesChanged = pyqtSignal()
    dockClosed = pyqtSignal()

    def __init__(self, owner: StraightLineDiagramDockWidget, iface, parent=None):
        super().__init__("KP Ranges", parent)
        self._owner = owner
        self.iface = iface
        self.setObjectName("KPRangesDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        self._ranges = []
        self._schema = {
            "start": None,
            "end": None,
            "label": "",
            "category": "",
            "enabled": "",
            "ref_line": None,
        }

        w = QWidget()
        self.setWidget(w)
        layout = QVBoxLayout(w)

        top = QHBoxLayout()
        top.addWidget(QLabel("Ranges"), 1)
        self.add_btn = QPushButton("Add")
        self.edit_btn = QPushButton("Edit")
        self.dup_btn = QPushButton("Duplicate")
        self.del_btn = QPushButton("Delete")
        top.addWidget(self.add_btn)
        top.addWidget(self.edit_btn)
        top.addWidget(self.dup_btn)
        top.addWidget(self.del_btn)
        layout.addLayout(top)

        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        try:
            self.table.doubleClicked.connect(self._on_double_clicked)
        except Exception:
            pass
        layout.addWidget(self.table, 1)

        self.model = QStandardItemModel(0, 6, self)
        self.model.setHorizontalHeaderLabels(["Start KP", "End KP", "Label", "Category", "Enabled", "Route"])
        self.table.setModel(self.model)

        self.add_btn.clicked.connect(self._add)
        self.edit_btn.clicked.connect(self._edit)
        self.dup_btn.clicked.connect(self._duplicate)
        self.del_btn.clicked.connect(self._delete)

    @staticmethod
    def _try_float(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            try:
                return float(v)
            except Exception:
                return None
        try:
            s = str(v).strip().replace(",", "")
            return float(s) if s else None
        except Exception:
            return None

    def _on_double_clicked(self, index):
        """Center map / sync crosshair for a selected KP range row."""

        if not index or not index.isValid():
            return

        try:
            start_txt = self.model.item(index.row(), 0).text()
            end_txt = self.model.item(index.row(), 1).text()
        except Exception:
            return

        s = self._try_float(start_txt)
        e = self._try_float(end_txt)
        if s is None or e is None:
            return
        if s > e:
            s, e = e, s

        mid = (float(s) + float(e)) / 2.0

        # Prefer centering on the range segment's bbox center when geometry is available.
        map_pt = None
        try:
            owner = self._owner
            if owner and owner.merged_geometry is not None and owner.line_length_m and owner.distance_area:
                seg = extract_line_segment(owner.merged_geometry, float(s), float(e), owner.distance_area)
                if seg and not seg.isEmpty():
                    try:
                        map_pt = seg.boundingBox().center()
                    except Exception:
                        map_pt = None
        except Exception:
            map_pt = None

        try:
            self._owner._on_event_activated(mid, map_pt)
        except Exception:
            try:
                self._owner._zoom_map_to_kp(mid)
            except Exception:
                pass

    def closeEvent(self, event):
        try:
            self.dockClosed.emit()
        except Exception:
            pass
        super().closeEvent(event)

    def set_schema(self, start_field, end_field, label_field, category_field, enabled_field, ref_line_field):
        self._schema = {
            "start": start_field,
            "end": end_field,
            "label": label_field or "",
            "category": category_field or "",
            "enabled": enabled_field or "",
            "ref_line": ref_line_field,
        }

    def set_ranges(self, rows):
        self._ranges = list(rows or [])
        self._rebuild()

    def _rebuild(self):
        self.model.removeRows(0, self.model.rowCount())
        for r in self._ranges:
            s = float(r.get("start_kp") or 0.0)
            e = float(r.get("end_kp") or 0.0)
            items = [
                QStandardItem(f"{s:,.3f}"),
                QStandardItem(f"{e:,.3f}"),
                QStandardItem(str(r.get("label") or "")),
                QStandardItem(str(r.get("category") or "")),
                QStandardItem("Yes" if r.get("enabled", True) else "No"),
                QStandardItem(str(r.get("ref_line") or "")),
            ]
            try:
                fid = int(r.get("fid"))
                for it in items:
                    it.setData(fid, Qt.UserRole + 1)
            except Exception:
                pass
            self.model.appendRow(items)
        try:
            self.table.resizeColumnsToContents()
        except Exception:
            pass

    def _selected_fids(self):
        fids = []
        try:
            for idx in self.table.selectionModel().selectedRows():
                it = self.model.item(idx.row(), 0)
                fid = it.data(Qt.UserRole + 1)
                if fid is not None:
                    fids.append(int(fid))
        except Exception:
            pass
        return fids

    def _context(self):
        owner = self._owner
        ranges_layer = owner._get_selected_ranges_layer()
        line_layer = owner._get_selected_line_layer()
        if not ranges_layer or not line_layer:
            return None
        owner._ensure_required_ranges_fields(ranges_layer)
        fmap = owner._ranges_field_map() or {}
        start_f = fmap.get("start")
        end_f = fmap.get("end")
        ref_f = fmap.get("ref_line")
        if not start_f or not end_f or not ref_f:
            return None
        return {
            "layer": ranges_layer,
            "line_key": owner._layer_persist_key(line_layer) or "",
            "max_kp": (owner.line_length_m / 1000.0) if owner.line_length_m else None,
            "start_f": start_f,
            "end_f": end_f,
            "label_f": fmap.get("label") or "",
            "cat_f": fmap.get("category") or "",
            "enabled_f": fmap.get("enabled") or "",
            "ref_f": ref_f,
        }

    def _ensure_editable(self, layer: QgsVectorLayer) -> bool:
        # Many table sources (e.g. CSV) are not editable.
        if not layer.isValid():
            return False
        if layer.isEditable():
            return True
        caps = 0
        try:
            caps = int(layer.dataProvider().capabilities())
        except Exception:
            caps = 0
        try:
            from qgis.core import QgsVectorDataProvider

            if not (caps & QgsVectorDataProvider.AddFeatures):
                QMessageBox.warning(
                    self,
                    "Not editable",
                    "This ranges table is not editable (common for CSV).\n\n"
                    "Export it to a GeoPackage/SQLite table (Right click layer → Export → Save Features As...), "
                    "or create a new editable ranges table using the 'New ranges table' button in the SLD dock.",
                )
                return False
        except Exception:
            pass

        try:
            return bool(layer.startEditing())
        except Exception:
            return False

    def _add(self):
        ctx = self._context()
        if not ctx:
            QMessageBox.warning(self, "Ranges", "Select an RPL line and a KP ranges table, and map the Start/End/Ref line fields.")
            return

        layer: QgsVectorLayer = ctx["layer"]
        if not self._ensure_editable(layer):
            return

        max_kp = ctx.get("max_kp")
        dlg = _KPRangeEditDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            v = dlg.values()
        except Exception as e:
            QMessageBox.warning(self, "Invalid", f"Invalid values: {e}")
            return

        s = float(v["start_kp"])
        e = float(v["end_kp"])
        if s > e:
            s, e = e, s
        if max_kp is not None and (s < 0 or e < 0 or s > max_kp or e > max_kp):
            QMessageBox.warning(self, "Out of bounds", f"Range must be within 0..{max_kp:,.3f} km")
            return

        feat = QgsFeature(layer.fields())
        feat.setAttribute(ctx["start_f"], s)
        feat.setAttribute(ctx["end_f"], e)
        feat.setAttribute(ctx["ref_f"], ctx["line_key"])
        if ctx.get("label_f"):
            feat.setAttribute(ctx["label_f"], v["label"])
        if ctx.get("cat_f"):
            feat.setAttribute(ctx["cat_f"], v["category"])
        if ctx.get("enabled_f"):
            feat.setAttribute(ctx["enabled_f"], 1 if v["enabled"] else 0)

        ok = False
        try:
            ok = bool(layer.addFeature(feat))
        except Exception:
            ok = False
        if not ok:
            QMessageBox.critical(self, "Error", "Failed to add range.")
            try:
                layer.rollBack()
            except Exception:
                pass
            return

        try:
            layer.commitChanges()
        except Exception:
            pass
        try:
            self.rangesChanged.emit()
        except Exception:
            pass

    def _edit(self):
        ctx = self._context()
        if not ctx:
            QMessageBox.warning(self, "Ranges", "Select an RPL line and a KP ranges table, and map the Start/End/Ref line fields.")
            return
        fids = self._selected_fids()
        if len(fids) != 1:
            QMessageBox.information(self, "Edit", "Select exactly one range row to edit.")
            return

        layer: QgsVectorLayer = ctx["layer"]
        if not self._ensure_editable(layer):
            return

        fid = fids[0]
        feat = None
        try:
            for f in layer.getFeatures():
                if int(f.id()) == fid:
                    feat = f
                    break
        except Exception:
            feat = None
        if feat is None:
            QMessageBox.warning(self, "Edit", "Could not find selected feature.")
            return

        def _getf(name, default=""):
            if not name:
                return default
            try:
                return feat[name]
            except Exception:
                return default

        dlg = _KPRangeEditDialog(
            self,
            start_kp=_getf(ctx["start_f"], None),
            end_kp=_getf(ctx["end_f"], None),
            label=str(_getf(ctx.get("label_f"), "") or ""),
            category=str(_getf(ctx.get("cat_f"), "") or ""),
            enabled=(str(_getf(ctx.get("enabled_f"), "1") or "1").strip().lower() not in ("0", "false", "no", "off", "")),
        )
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            v = dlg.values()
        except Exception as e:
            QMessageBox.warning(self, "Invalid", f"Invalid values: {e}")
            return

        s = float(v["start_kp"])
        e = float(v["end_kp"])
        if s > e:
            s, e = e, s
        max_kp = ctx.get("max_kp")
        if max_kp is not None and (s < 0 or e < 0 or s > max_kp or e > max_kp):
            QMessageBox.warning(self, "Out of bounds", f"Range must be within 0..{max_kp:,.3f} km")
            return

        try:
            layer.changeAttributeValue(fid, layer.fields().indexOf(ctx["start_f"]), s)
            layer.changeAttributeValue(fid, layer.fields().indexOf(ctx["end_f"]), e)
            layer.changeAttributeValue(fid, layer.fields().indexOf(ctx["ref_f"]), ctx["line_key"])
            if ctx.get("label_f"):
                layer.changeAttributeValue(fid, layer.fields().indexOf(ctx["label_f"]), v["label"])
            if ctx.get("cat_f"):
                layer.changeAttributeValue(fid, layer.fields().indexOf(ctx["cat_f"]), v["category"])
            if ctx.get("enabled_f"):
                layer.changeAttributeValue(fid, layer.fields().indexOf(ctx["enabled_f"]), 1 if v["enabled"] else 0)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to edit range: {e}")
            try:
                layer.rollBack()
            except Exception:
                pass
            return

        try:
            layer.commitChanges()
        except Exception:
            pass
        try:
            self.rangesChanged.emit()
        except Exception:
            pass

    def _duplicate(self):
        ctx = self._context()
        if not ctx:
            QMessageBox.warning(self, "Ranges", "Select an RPL line and a KP ranges table, and map the Start/End/Ref line fields.")
            return
        fids = self._selected_fids()
        if len(fids) != 1:
            QMessageBox.information(self, "Duplicate", "Select exactly one range row to duplicate.")
            return
        layer: QgsVectorLayer = ctx["layer"]
        if not self._ensure_editable(layer):
            return
        fid = fids[0]
        feat = None
        try:
            for f in layer.getFeatures():
                if int(f.id()) == fid:
                    feat = f
                    break
        except Exception:
            feat = None
        if feat is None:
            QMessageBox.warning(self, "Duplicate", "Could not find selected feature.")
            return

        new_feat = QgsFeature(layer.fields())
        for name in layer.fields().names():
            try:
                new_feat.setAttribute(name, feat[name])
            except Exception:
                pass
        # Always set route reference to current line key
        new_feat.setAttribute(ctx["ref_f"], ctx["line_key"])

        ok = False
        try:
            ok = bool(layer.addFeature(new_feat))
        except Exception:
            ok = False
        if not ok:
            QMessageBox.critical(self, "Error", "Failed to duplicate range.")
            try:
                layer.rollBack()
            except Exception:
                pass
            return
        try:
            layer.commitChanges()
        except Exception:
            pass
        try:
            self.rangesChanged.emit()
        except Exception:
            pass

    def _delete(self):
        ctx = self._context()
        if not ctx:
            QMessageBox.warning(self, "Ranges", "Select an RPL line and a KP ranges table, and map the Start/End/Ref line fields.")
            return
        fids = self._selected_fids()
        if not fids:
            QMessageBox.information(self, "Delete", "Select one or more range rows to delete.")
            return
        if QMessageBox.question(self, "Delete", f"Delete {len(fids)} range(s)?") != QMessageBox.Yes:
            return

        layer: QgsVectorLayer = ctx["layer"]
        if not self._ensure_editable(layer):
            return
        try:
            for fid in fids:
                layer.deleteFeature(int(fid))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete: {e}")
            try:
                layer.rollBack()
            except Exception:
                pass
            return
        try:
            layer.commitChanges()
        except Exception:
            pass
        try:
            self.rangesChanged.emit()
        except Exception:
            pass

