# -*- coding: utf-8 -*-
"""The Route Comments dock: comment set, editable table, and the selected
comment's thread (or details) beside its images.

Layout: the table runs full width on top; below it a header strip (number,
location, quick status and priority, previous / next, follow on the map)
and the review panes, side by side when the dock is wide and stacked when
it is narrow, the image pane shrinking when the comment has no image.

Storage (``store``), route references (``route_ref``), the map display
(``map_layers``), the table/thread widgets (``widgets``) and the image
panel (``pictures``) live elsewhere; this module wires them together.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from typing import Callable, Dict, List, Optional

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
                       QgsProject, QgsRectangle)
from qgis.PyQt.QtCore import (QBuffer, QByteArray, QIODevice, QItemSelection, QItemSelectionModel, QSettings, Qt,
                              QTimer, QUrl)
from qgis.PyQt.QtGui import QDesktopServices, QIcon, QImage, QKeySequence
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QColorDialog, QComboBox, QDockWidget,
                                 QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu,
                                 QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                                 QTableView, QTabWidget, QToolButton, QVBoxLayout, QWidget)

try:
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt5: QShortcut is in QtWidgets
    from qgis.PyQt.QtWidgets import QShortcut

from ..plugin_log import log_exception
from ..qgis_compat import (CONTEXT_MENU_POLICY_CUSTOM, MESSAGEBOX_NO, MESSAGEBOX_YES, MESSAGE_INFO, MESSAGE_SUCCESS,
                           SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_EXTENDED, TOOLBUTTON_POPUP_MODE_INSTANT,
                           WINDOW_HINT_CLOSE, WINDOW_HINT_CUSTOMIZE, WINDOW_HINT_MIN_MAX, WINDOW_HINT_TITLE,
                           WINDOW_TYPE_WINDOW, qt_exec)
from . import map_layers, model
from .dialogs import (SETTINGS, ChoicesDialog, CommentForm, ExportDialog, FieldsDialog, ImportDialog, LabelDialog,
                      NewCommentDialog, default_author, remember_author)
from .pictures import MAX_PIXELS, PicturePanel, ThumbnailCache, image_size, pictures_from_mime
from .route_ref import CaptureRoute, ReferenceChecker, location_without_route
from .store import CommentStore, CommentStoreError, ConflictError, attachments_dir_for, is_register
from .widgets import (COND_COLORS, EDITABLE, ID_ROLE, AdaptiveSplitter, CommentDelegate, CommentProxy,
                      CommentTableModel, ThreadView, readable_selection)

MAX_RECENT = 8
POLL_MS = 4000
FLOATING_KEY = f"{SETTINGS}/floating"
GEOMETRY_KEY = f"{SETTINGS}/window_geometry"
SPLIT_KEY = f"{SETTINGS}/split_table"           # table / review heights
REVIEW_SPLIT_KEY = f"{SETTINGS}/split_review"   # thread / images, side by side and stacked
FOLLOW_KEY = f"{SETTINGS}/follow_map"
WRITE_ONLY = getattr(getattr(QIODevice, "OpenModeFlag", QIODevice), "WriteOnly")


def _theme_icon(name: str) -> QIcon:
    return QgsApplication.getThemeIcon(name)


def default_register_path(project: Optional[QgsProject] = None) -> str:
    """``<project folder>/<project>_comments.gpkg`` (plugin folder when unsaved)."""
    from ..workbench.schema import gpkg_folder_for, sanitize_slug
    project = project or QgsProject.instance()
    path = project.fileName()
    stem = os.path.splitext(os.path.basename(path))[0] if path else (sanitize_slug(project.title()) or "project")
    return os.path.join(gpkg_folder_for(path), f"{stem}_comments.gpkg")


def unused_register_path(path: str) -> str:
    """``path``, or ``<stem>_2.gpkg``, ``_3``… when that file or its attachments
    folder is there already (every unsaved project shares one default name)."""
    stem, ext = os.path.splitext(path)
    candidate, n = path, 1
    while os.path.exists(candidate) or os.path.exists(attachments_dir_for(candidate)):
        n += 1
        candidate = f"{stem}_{n}{ext}"
    return candidate


class RouteCommentsDock(QDockWidget):
    """Route Comments: one register, one comment set at a time."""

    def __init__(self, iface, start_kp_for: Optional[Callable] = None,
                 reference_layer: Optional[Callable] = None, parent=None):
        super().__init__("Route Comments", parent or (iface.mainWindow() if iface else None))
        self.setObjectName("SubseaCableToolsRouteComments")
        self.iface = iface
        self.start_kp_for = start_kp_for
        self.reference_layer = reference_layer or (lambda: None)
        self.store: Optional[CommentStore] = None
        self.set_row: Optional[Dict] = None
        self.fields: List[model.CustomField] = []
        self.comments: List[Dict] = []
        self.by_id: Dict[str, Dict] = {}
        self.replies: Dict[str, List[Dict]] = {}
        self.checker = ReferenceChecker(start_kp_for=start_kp_for)
        self.layers = map_layers.CommentLayers()
        self.highlight = map_layers.Highlight(iface.mapCanvas()) if iface else None
        self.pick_tool = None
        self.current_id: Optional[str] = None
        self._loaded_version = None
        self._data_version = None
        self._dirty = False
        self._syncing = False
        self._connected_layers: List = []
        self._header_key = ""
        self._from_map = False
        self._thread_saving = False
        self.thumb_cache = ThumbnailCache()
        self._build_ui()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_MS)
        self._poll_timer.timeout.connect(self._poll)
        self._header_timer = QTimer(self)
        self._header_timer.setSingleShot(True)
        self._header_timer.setInterval(400)
        self._header_timer.timeout.connect(self._save_header)
        self.topLevelChanged.connect(self._top_level_changed)
        project = QgsProject.instance()
        project.readProject.connect(self._on_project_changed)
        project.cleared.connect(self._on_project_changed)
        self._project_connected = True
        self._on_project_changed()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        row1 = QHBoxLayout()
        self.register_button = QToolButton()
        self.register_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        self.register_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.register_button.setIcon(_theme_icon("/mGeoPackage.svg"))
        self.register_menu = QMenu(self.register_button)
        self.register_menu.aboutToShow.connect(self._fill_register_menu)
        self.register_button.setMenu(self.register_menu)
        row1.addWidget(self.register_button)
        self.set_combo = QComboBox()
        self.set_combo.setToolTip("Comment set")
        self.set_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.set_combo.setMinimumContentsLength(10)
        self.set_combo.currentIndexChanged.connect(self._on_set_changed)
        row1.addWidget(self.set_combo, 1)
        self.set_button = QToolButton()
        self.set_button.setText("Set")
        self.set_button.setToolTip("Comment set: new, rename, delete, custom fields, statuses and priorities")
        self.set_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        set_menu = QMenu(self.set_button)
        self.act_new_set = set_menu.addAction("New set…", self.new_set)
        self.act_rename_set = set_menu.addAction("Rename set…", self.rename_set)
        self.act_delete_set = set_menu.addAction("Delete set…", self.delete_set)
        set_menu.addSeparator()
        self.act_fields = set_menu.addAction("Custom fields…", self.edit_fields)
        self.act_choices = set_menu.addAction("Statuses and priorities…", self.edit_choices)
        self.set_button.setMenu(set_menu)
        row1.addWidget(self.set_button)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search comments, replies, numbers (#12)…")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        row2.addWidget(self.search, 1)
        self.filter_button = QToolButton()
        self.filter_button.setIcon(_theme_icon("/mActionFilter2.svg"))
        self.filter_button.setToolTip("Filter by status, priority, reference condition and type")
        self.filter_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        self.filter_menu = QMenu(self.filter_button)
        self.filter_button.setMenu(self.filter_menu)
        self._filter_actions: Dict[str, Dict[str, object]] = {}
        row2.addWidget(self.filter_button)
        self.map_button = QToolButton()
        self.map_button.setIcon(_theme_icon("/mActionShowAllLayers.svg"))
        self.map_button.setToolTip("Map: show comments, labels, highlight, pick comments on the map, zoom")
        self.map_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        map_menu = QMenu(self.map_button)
        self.act_show_map = map_menu.addAction("Show on map")
        self.act_show_map.setCheckable(True)
        self.act_show_map.setChecked(QSettings().value(f"{SETTINGS}/show_map", True, type=bool))
        self.act_show_map.toggled.connect(self._toggle_map)
        self.act_labels = map_menu.addAction("Labels")
        self.act_labels.setCheckable(True)
        self.act_labels.setChecked(QSettings().value(f"{SETTINGS}/labels", True, type=bool))
        self.act_labels.toggled.connect(self._toggle_labels)
        map_menu.addAction("Label contents…", self.edit_labels)
        map_menu.addAction("Highlight colour…", self.edit_highlight_color)
        map_menu.addSeparator()
        self.act_pick = map_menu.addAction(_theme_icon("/mActionIdentify.svg"), "Pick comments on the map")
        self.act_pick.setCheckable(True)
        self.act_pick.toggled.connect(self._toggle_pick)
        self.act_zoom = map_menu.addAction(_theme_icon("/mActionZoomToSelected.svg"), "Zoom to selected",
                                           self.zoom_to_selected)
        map_menu.addSeparator()
        map_menu.addAction(_theme_icon("/mActionRefresh.svg"), "Check route references now",
                           self.check_references)
        self.map_button.setMenu(map_menu)
        row2.addWidget(self.map_button)
        self.pick_button = QToolButton()
        self.pick_button.setDefaultAction(self.act_pick)
        row2.addWidget(self.pick_button)
        self.io_button = QToolButton()
        self.io_button.setIcon(_theme_icon("/mActionFileSave.svg"))
        self.io_button.setToolTip("Import or export comments (CSV / Excel)")
        self.io_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        io_menu = QMenu(self.io_button)
        self.act_import = io_menu.addAction("Import comments from CSV / Excel…", self.import_comments)
        self.act_export = io_menu.addAction("Export comments…", self.export_comments)
        self.io_button.setMenu(io_menu)
        row2.addWidget(self.io_button)
        outer.addLayout(row2)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.TextFormat.RichText)
        self.banner.linkActivated.connect(self._banner_link)
        self.banner.hide()
        outer.addWidget(self.banner)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.table_model = CommentTableModel(self)
        self.table_model.edit_handler = self._inline_edit
        self.proxy = CommentProxy(self)
        self.proxy.setSourceModel(self.table_model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setItemDelegate(CommentDelegate(self.table_model, self.table))
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        readable_selection(self.table)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().height() + 6)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setStretchLastSection(False)
        header.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        header.customContextMenuRequested.connect(self._header_menu)
        header.sectionMoved.connect(lambda *_a: self._header_timer.start())
        header.sectionResized.connect(lambda *_a: self._header_timer.start())
        header.sortIndicatorChanged.connect(lambda *_a: self._header_timer.start())
        self.table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.selectionModel().selectionChanged.connect(self._on_table_selection)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.setToolTip("Double-click a comment, status, priority, author, date, tags or custom field to "
                              "edit it in place; double-click a number or KP to zoom to it.")
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self._build_review())
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        outer.addWidget(self.splitter, 1)

        self.status = QLabel()
        self.status.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.status)
        self.setWidget(body)
        for keys, slot in (("Alt+Up", lambda: self.step(-1)), ("Alt+Down", lambda: self.step(1)),
                           (QKeySequence.StandardKey.Paste, self.paste_image)):
            # Text boxes keep Ctrl+V for text: they take the key before a shortcut.
            QShortcut(QKeySequence(keys), body, activated=slot, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._restore_splits()
        self._rebuild_filter_menu()
        self._update_enabled()

    def _build_review(self) -> QWidget:
        """The selected comment: a header strip, then its thread (or details)
        beside its images."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(3)
        self.editor_title = QLabel()
        self.editor_title.setTextFormat(Qt.TextFormat.RichText)
        self.editor_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.editor_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(self.editor_title, 1)
        self.quick_status = QComboBox()
        self.quick_status.setToolTip("Status (saved at once)")
        self.quick_status.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.quick_status.activated.connect(lambda _i: self._quick_set("status"))
        self.quick_priority = QComboBox()
        self.quick_priority.setToolTip("Priority (saved at once)")
        self.quick_priority.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.quick_priority.activated.connect(lambda _i: self._quick_set("priority"))
        head.addWidget(self.quick_status)
        head.addWidget(self.quick_priority)
        self.prev_button = QToolButton()
        self.prev_button.setArrowType(Qt.ArrowType.UpArrow)
        self.prev_button.setAutoRaise(True)
        self.prev_button.setToolTip("Previous comment in the table (Alt+Up)")
        self.prev_button.clicked.connect(lambda: self.step(-1))
        self.next_button = QToolButton()
        self.next_button.setArrowType(Qt.ArrowType.DownArrow)
        self.next_button.setAutoRaise(True)
        self.next_button.setToolTip("Next comment in the table (Alt+Down)")
        self.next_button.clicked.connect(lambda: self.step(1))
        self.follow_button = QToolButton()
        self.follow_button.setIcon(_theme_icon("/mActionPanToSelected.svg"))
        self.follow_button.setAutoRaise(True)
        self.follow_button.setCheckable(True)
        self.follow_button.setChecked(QSettings().value(FOLLOW_KEY, False, type=bool))
        self.follow_button.setToolTip("Follow on the map: pan to each comment you select here, when it is "
                                      "out of view")
        self.follow_button.toggled.connect(lambda on: QSettings().setValue(FOLLOW_KEY, bool(on)))
        for button in (self.prev_button, self.next_button, self.follow_button):
            head.addWidget(button)
        layout.addLayout(head)
        self.condition_label = QLabel()
        self.condition_label.setWordWrap(True)
        self.condition_label.hide()
        layout.addWidget(self.condition_label)

        self.review_split = AdaptiveSplitter()
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.review_split.addWidget(self.tabs)
        self.pictures = PicturePanel(self.thumb_cache)
        self.pictures.add_requested.connect(self.add_images)
        self.pictures.paste_requested.connect(self.paste_image)
        self.pictures.files_dropped.connect(self.add_image_files)
        self.pictures.image_dropped.connect(self.add_image_data)
        self.pictures.remove_requested.connect(self.remove_image)
        self.pictures.map_image_changed.connect(self.set_map_image)
        self.pictures.open_requested.connect(self._open_file)
        self.review_split.addWidget(self.pictures)
        layout.addWidget(self.review_split, 1)

        # Thread: the comment and its replies, and the reply box.
        thread = QWidget()
        thread_layout = QVBoxLayout(thread)
        thread_layout.setContentsMargins(2, 4, 2, 2)
        self.thread = ThreadView()
        self.thread.delete_requested.connect(self.delete_reply)
        self.thread.reply_edited.connect(self._save_thread_edit)
        self.thread.comment_edited.connect(lambda text: self._save_thread_edit(None, text))
        self.thread.comment_delete_requested.connect(self.delete_selected)
        thread_layout.addWidget(self.thread, 1)
        composer = QHBoxLayout()
        self.reply_text = QPlainTextEdit()
        self.reply_text.setPlaceholderText("Write a reply… (Ctrl+Enter sends)")
        self.reply_text.setTabChangesFocus(True)
        self.reply_text.setMaximumHeight(self.reply_text.fontMetrics().lineSpacing() * 3 + 14)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(keys), self.reply_text, activated=self.add_reply,
                      context=Qt.ShortcutContext.WidgetShortcut)
        self.reply_button = QPushButton("Send")
        self.reply_button.clicked.connect(lambda: self.add_reply())
        self.close_button = QPushButton("Send && close")
        self.close_button.clicked.connect(self.send_and_toggle)
        send = QVBoxLayout()
        send.setSpacing(2)
        send.addWidget(self.reply_button)
        send.addWidget(self.close_button)
        composer.addWidget(self.reply_text, 1)
        composer.addLayout(send)
        thread_layout.addLayout(composer)
        self.tabs.addTab(thread, "Thread")

        # Details: every field of the comment (status and priority are in the header).
        details = QScrollArea()
        details.setWidgetResizable(True)
        details.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        form_layout = QVBoxLayout(inner)
        form_layout.setContentsMargins(2, 4, 2, 0)
        self.form = CommentForm(inner, text_lines=3, show_state=False)
        self.form.changed.connect(self._on_form_changed)
        form_layout.addWidget(self.form)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip("Save changes to this comment (Ctrl+S)")
        self.save_button.clicked.connect(self.save_current)
        QShortcut(QKeySequence("Ctrl+S"), inner, activated=self.save_current,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(self.revert_current)
        self.delete_button = QPushButton("Delete…")
        self.delete_button.clicked.connect(self.delete_selected)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.revert_button)
        buttons.addStretch(1)
        buttons.addWidget(self.delete_button)
        form_layout.addLayout(buttons)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setStyleSheet("color: palette(mid);")
        form_layout.addWidget(self.info)
        form_layout.addStretch(1)
        details.setWidget(inner)
        self.tabs.addTab(details, "Details")
        self.editor_panel = panel
        return panel

    def _restore_splits(self) -> None:
        sizes = QSettings().value(SPLIT_KEY)
        try:
            sizes = [int(v) for v in (sizes or [])]
        except (TypeError, ValueError):
            sizes = []
        if len(sizes) == 2 and all(v > 0 for v in sizes):
            self.splitter.setSizes(sizes)
        else:
            self.splitter.setSizes([240, 480])
        try:
            state = json.loads(str(QSettings().value(REVIEW_SPLIT_KEY, "") or "{}"))
        except ValueError:
            state = {}
        self.review_split.restore(state)

    def _save_splits(self) -> None:
        sizes = self.splitter.sizes()
        if len(sizes) == 2 and all(v > 0 for v in sizes):
            QSettings().setValue(SPLIT_KEY, sizes)
        QSettings().setValue(REVIEW_SPLIT_KEY, json.dumps(self.review_split.state()))

    # ------------------------------------------------------------------ window
    def apply_saved_window_mode(self) -> None:
        """Float as a normal window (with minimise/maximise) if it was floating last time."""
        if QSettings().value(FLOATING_KEY, False, type=bool) and not self.isFloating():
            self.setFloating(True)

    def _top_level_changed(self, floating: bool) -> None:
        QSettings().setValue(FLOATING_KEY, bool(floating))
        if floating:
            self.setWindowFlags(WINDOW_TYPE_WINDOW | WINDOW_HINT_CUSTOMIZE | WINDOW_HINT_TITLE
                                | WINDOW_HINT_MIN_MAX | WINDOW_HINT_CLOSE)
            self.show()
            geometry = QSettings().value(GEOMETRY_KEY)
            restored = False
            if geometry is not None:
                try:
                    restored = bool(self.restoreGeometry(geometry))
                except TypeError:
                    restored = False
            if not restored:
                self.resize(900, 700)

    def _save_window_state(self) -> None:
        if self.isFloating():
            QSettings().setValue(GEOMETRY_KEY, self.saveGeometry())
        self._save_splits()

    # ------------------------------------------------------------------ register
    def _message(self, text: str, level=MESSAGE_INFO, duration: int = 4) -> None:
        try:
            self.iface.messageBar().pushMessage("Route Comments", text, level=level, duration=duration)
        except Exception:  # noqa: BLE001 - no message bar (tests)
            pass

    def _error(self, title: str, exc: BaseException) -> None:
        QMessageBox.warning(self, f"Route Comments — {title}", str(exc))

    def _recent(self) -> List[str]:
        value = QSettings().value(f"{SETTINGS}/recent", []) or []
        if isinstance(value, str):
            value = [value]
        return [str(p) for p in value if p]

    def _remember(self, path: str) -> None:
        recent = [p for p in self._recent() if os.path.normcase(p) != os.path.normcase(path)]
        QSettings().setValue(f"{SETTINGS}/recent", [path] + recent[: MAX_RECENT - 1])
        QSettings().setValue(f"{SETTINGS}/last_register", path)

    @staticmethod
    def _clear_menu(menu) -> None:
        """Clear ``menu`` and delete its submenus (``clear()`` keeps them)."""
        for sub in menu.findChildren(QMenu):
            if sub.parent() is menu:
                sub.deleteLater()
        menu.clear()

    def _fill_register_menu(self) -> None:
        menu = self.register_menu
        self._clear_menu(menu)
        menu.addAction("New register…", self.new_register)
        menu.addAction("Open register…", lambda: self.open_register())
        recent = [p for p in self._recent()
                  if not self.store or os.path.normcase(p) != os.path.normcase(self.store.path)]
        if recent:
            sub = menu.addMenu("Recent registers")
            for path in recent:
                action = sub.addAction(os.path.basename(path), lambda p=path: self.open_register(p))
                action.setToolTip(path)
                action.setEnabled(os.path.isfile(path))
            sub.setToolTipsVisible(True)
        if self.store is not None:
            menu.addSeparator()
            menu.addAction("Show register folder", self._show_folder)
            menu.addAction("Close register", self.close_register)

    def _show_folder(self) -> None:
        if self.store is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self.store.path)))

    def new_register(self) -> bool:
        suggested = unused_register_path(default_register_path())
        while True:
            # The dialog never confirms an overwrite: a register is never
            # replaced (other projects may use it), picking one is asked below.
            path, _f = QFileDialog.getSaveFileName(self, "New comment register", suggested, "GeoPackage (*.gpkg)",
                                                   options=QFileDialog.Option.DontConfirmOverwrite)
            if not path:
                return False
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            existing = is_register(path)
            if not existing:
                break
            choice = self._ask_existing_register(path)
            if choice == "open":
                break
            if choice != "rename":
                return False
            suggested = unused_register_path(path)
        try:
            store = CommentStore(path).open() if existing else CommentStore.create(path)
        except CommentStoreError as exc:
            self._error("New register", exc)
            return False
        self._use_store(store)
        return True

    def _ask_existing_register(self, path: str) -> str:
        """``"open"``, ``"rename"`` or ``""`` (cancel) for an existing register picked as a new one."""
        box = QMessageBox(QMessageBox.Icon.Question, "New comment register",
                          f"{os.path.basename(path)} is already a comment register, with its own comments. "
                          "A register is never overwritten.", parent=self)
        rename = box.addButton("Choose another name…", QMessageBox.ButtonRole.AcceptRole)
        existing = box.addButton("Open it", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(rename)
        qt_exec(box)
        if box.clickedButton() is rename:
            return "rename"
        return "open" if box.clickedButton() is existing else ""

    def open_register(self, path: Optional[str] = None) -> bool:
        if not path:
            start = os.path.dirname(self.store.path) if self.store else os.path.dirname(default_register_path())
            path, _f = QFileDialog.getOpenFileName(self, "Open comment register", start, "GeoPackage (*.gpkg)")
            if not path:
                return False
        try:
            store = CommentStore(path).open()
        except CommentStoreError as exc:
            self._error("Open register", exc)
            return False
        self._use_store(store)
        return True

    def close_register(self) -> None:
        if not self._confirm_leave():
            return
        self._disconnect_layers()
        self.layers.remove()
        if self.store is not None:
            self.store.close()
        self.store = None
        self.thumb_cache.clear()
        map_layers.set_project_register("", "")
        self._reload_sets()

    def _use_store(self, store: CommentStore, set_id: str = "") -> None:
        if self.store is not None and os.path.normcase(self.store.path) != os.path.normcase(store.path):
            self.store.close()
            self.thumb_cache.clear()
        self.store = store
        self._remember(store.path)
        self.checker.clear()
        self._reload_sets(set_id)

    def _on_project_changed(self, *_args) -> None:
        """Open the register the project names (or none)."""
        self._disconnect_layers()
        path, set_id = map_layers.project_register()
        self.current_id = None
        self._dirty = False
        self.checker.clear()
        self.thumb_cache.clear()
        if path and is_register(path):
            try:
                self._use_store(CommentStore(path).open(), set_id)
                return
            except CommentStoreError:
                log_exception("Route Comments: the project's comment register could not be opened")
        if self.store is not None:
            self.store.close()
        self.store = None
        self._reload_sets()
        if path:
            self._show_banner(f"This project's comment register was not found: {path}. "
                              "<a href='open'>Open register…</a> (the comment layers will be pointed at it)")

    def ensure_register(self) -> bool:
        """A register is open, or the user creates/opens one now."""
        if self.store is not None and self.set_row is not None:
            return True
        if self.store is None:
            box = QMessageBox(QMessageBox.Icon.Question, "Route Comments",
                              "Comments are kept in a comment register (a GeoPackage you choose, with an "
                              "attachments folder beside it). Create a new register or open an existing one?",
                              parent=self)
            new = box.addButton("New register…", QMessageBox.ButtonRole.AcceptRole)
            existing = box.addButton("Open register…", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            qt_exec(box)
            if box.clickedButton() is new:
                self.new_register()
            elif box.clickedButton() is existing:
                self.open_register()
        if self.store is not None and self.set_row is None:
            try:
                self.store.create_set("Comments")
            except CommentStoreError as exc:
                self._error("New set", exc)
                return False
            self._reload_sets()
        return self.store is not None and self.set_row is not None

    # ------------------------------------------------------------------ sets
    def _reload_sets(self, select_id: str = "") -> None:
        current = select_id or (self.set_row or {}).get("set_id") or ""
        if not current:
            current = map_layers.project_register()[1]
        sets = []
        if self.store is not None:
            try:
                sets = self.store.list_sets()
            except CommentStoreError as exc:
                self._error("Comment sets", exc)
        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        for row in sets:
            self.set_combo.addItem(row["name"], row["set_id"])
        index = max(0, self.set_combo.findData(current)) if sets else -1
        self.set_combo.setCurrentIndex(index)
        self.set_combo.blockSignals(False)
        self.register_button.setText(self.store.name if self.store else "No register")
        self.register_button.setToolTip(self.store.path if self.store else
                                        "Choose or create a comment register (a GeoPackage)")
        self._on_set_changed()

    def _on_set_changed(self, *_args) -> None:
        set_id = self.set_combo.currentData()
        self.set_row = self.store.get_set(set_id) if (self.store is not None and set_id) else None
        if self.store is not None:
            map_layers.set_project_register(self.store.path, set_id or "")
        self.fields = self.store.list_fields(set_id) if self.set_row else []
        self.form.set_choices(self.statuses(), self.priorities())
        self.form.set_fields(self.fields)
        self.current_id = None
        self._dirty = False
        self._rebuild_filter_menu()
        self.reload(keep_selection=False)
        self._update_enabled()
        if self.store is None:
            self._show_banner("No comment register is open. <a href='new'>New register…</a> or "
                              "<a href='open'>Open register…</a> Add comments from the KP Mouse Tool's "
                              "right-click menu.")
        else:
            self._show_banner("")

    def statuses(self) -> List[str]:
        return model.status_names(CommentStore.statuses_of(self.set_row))

    def priorities(self) -> List[str]:
        return CommentStore.priorities_of(self.set_row)

    def new_set(self) -> None:
        if self.store is None and not self.ensure_register():
            return
        name, ok = QInputDialog.getText(self, "New comment set", "Name")
        if not ok or not name.strip():
            return
        try:
            row = self.store.create_set(name.strip())
        except CommentStoreError as exc:
            self._error("New set", exc)
            return
        self._reload_sets(row["set_id"])

    def rename_set(self) -> None:
        if self.set_row is None:
            return
        name, ok = QInputDialog.getText(self, "Rename comment set", "Name", text=self.set_row["name"])
        if not ok or not name.strip():
            return
        try:
            self.store.update_set(self.set_row["set_id"], name=name.strip())
        except CommentStoreError as exc:
            self._error("Rename set", exc)
            return
        self._reload_sets(self.set_row["set_id"])

    def delete_set(self) -> None:
        if self.set_row is None:
            return
        answer = QMessageBox.question(
            self, "Delete comment set",
            f"Delete the set '{self.set_row['name']}' with its {len(self.comments)} comment(s), their replies "
            "and images? This cannot be undone.", MESSAGEBOX_YES | MESSAGEBOX_NO, MESSAGEBOX_NO)
        if answer != MESSAGEBOX_YES:
            return
        try:
            self.store.delete_set(self.set_row["set_id"])
        except CommentStoreError as exc:
            self._error("Delete set", exc)
            return
        self.set_row = None
        self._reload_sets()

    def edit_fields(self) -> None:
        if self.set_row is None:
            return
        dialog = FieldsDialog(self, self.set_row["name"], self.fields)
        if not qt_exec(dialog):
            return
        try:
            self.store.save_fields(self.set_row["set_id"], dialog.result_fields())
        except CommentStoreError as exc:
            self._error("Custom fields", exc)
            return
        self._reload_sets(self.set_row["set_id"])

    def edit_choices(self) -> None:
        if self.set_row is None:
            return
        dialog = ChoicesDialog(self, self.set_row["name"], CommentStore.statuses_of(self.set_row), self.priorities())
        if not qt_exec(dialog):
            return
        statuses, priorities = dialog.result_choices()
        try:
            self.store.update_set(self.set_row["set_id"], statuses=statuses, priorities=priorities)
        except CommentStoreError as exc:
            self._error("Statuses and priorities", exc)
            return
        self._reload_sets(self.set_row["set_id"])

    # ------------------------------------------------------------------ data
    def reload(self, keep_selection: bool = True) -> None:
        """Read the set from the register, re-check references, refresh table and map."""
        selected = self.selected_ids() if keep_selection else []
        self.comments, self.replies = [], {}
        if self.store is not None and self.set_row is not None:
            try:
                self.comments = self.store.list_comments(self.set_row["set_id"])
                self.replies = self.store.replies_by_comment(self.set_row["set_id"])
                self._data_version = self.store.data_version()
            except CommentStoreError as exc:
                self._error("Read", exc)
        self.by_id = {c["comment_id"]: c for c in self.comments}
        conditions = self.checker.check_all(self.comments)
        closed = model.closed_statuses(CommentStore.statuses_of(self.set_row))
        self.proxy.blobs = {c["comment_id"]: model.search_blob(c, self.fields, self.replies.get(c["comment_id"], ()))
                            for c in self.comments}
        self.table_model.reset(self.comments, self.fields, conditions, closed, self.statuses(), self.priorities())
        self._configure_header()
        self._refresh_map()
        self._select_ids([cid for cid in selected if cid in self.by_id], scroll=False)
        if self.current_id not in self.by_id:
            self.current_id = None
        self._show_comment(self.current_id if len(self.selected_ids()) == 1 else None)
        self._update_status()

    def check_references(self) -> None:
        self.checker.clear()
        self.reload()
        attention = sum(1 for c in self.comments if self.table_model.condition(c) in model.ATTENTION_CONDITIONS)
        self._message(f"Route references checked: {attention} comment(s) need attention." if attention else
                      "Route references checked: all unchanged.")

    def _refresh_map(self) -> None:
        if not self.act_show_map.isChecked() or self.set_row is None or self.store is None:
            self._disconnect_layers()
            self.layers.remove()
            if self.highlight:
                self.highlight.clear()
            return
        attention = [c["comment_id"] for c in self.comments
                     if self.table_model.condition(c) in model.ATTENTION_CONDITIONS]
        self.layers.show(self.store.path, self.set_row["set_id"], self.set_row["name"], self.table_model.closed,
                         attention, labels_on=self.act_labels.isChecked())
        self._connect_layers()

    def _connect_layers(self) -> None:
        current = self.layers.layers()
        if [id(x) for x in current] == [id(x) for x in self._connected_layers]:
            return
        self._disconnect_layers()
        for layer in current:
            layer.selectionChanged.connect(self._on_layer_selection)
            layer.willBeDeleted.connect(self._disconnect_layers)
        self._connected_layers = current

    def _disconnect_layers(self, *_args) -> None:
        for layer in self._connected_layers:
            try:
                layer.selectionChanged.disconnect(self._on_layer_selection)
                layer.willBeDeleted.disconnect(self._disconnect_layers)
            except (TypeError, RuntimeError):
                pass
        self._connected_layers = []

    def _toggle_map(self, on: bool) -> None:
        QSettings().setValue(f"{SETTINGS}/show_map", bool(on))
        self._refresh_map()

    def _toggle_labels(self, on: bool) -> None:
        QSettings().setValue(f"{SETTINGS}/labels", bool(on))
        self.layers.set_labels(on)

    def edit_labels(self) -> None:
        if self.set_row is None:
            return
        if not self.act_show_map.isChecked():
            self.act_show_map.setChecked(True)
        points = self.layers.find("points")
        dialog = LabelDialog(self, map_layers.label_settings_of(points), points)
        if qt_exec(dialog):
            self.act_labels.setChecked(True)
            map_layers.apply_labels(self.layers.layers(), dialog.result_settings(), True)

    def edit_highlight_color(self) -> None:
        color = QColorDialog.getColor(map_layers.highlight_color(), self, "Highlight colour")
        if color.isValid():
            QSettings().setValue(map_layers.HIGHLIGHT_KEY, color.name())
            self._after_selection(sync_map=False)

    # ------------------------------------------------------------------ table
    def _configure_header(self) -> None:
        keys = self.table_model.column_keys()
        digest = hashlib.sha1("\x1f".join(keys).encode("utf-8")).hexdigest()[:12]
        key = f"{SETTINGS}/header_{digest}"
        if key == self._header_key:
            return
        self._header_key = key
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        try:
            state = QSettings().value(key)
            restored = False
            if state is not None:
                try:
                    restored = bool(header.restoreState(state))
                except (TypeError, ValueError):
                    restored = False
            if not restored:
                self._default_columns()
        finally:
            header.blockSignals(False)
        self.table.sortByColumn(header.sortIndicatorSection(), header.sortIndicatorOrder())

    def _default_columns(self) -> None:
        header = self.table.horizontalHeader()
        for logical, (key, _label, shown) in enumerate(self.table_model.columns):
            header.moveSection(header.visualIndex(logical), logical)
            self.table.setColumnHidden(logical, not shown)
            width = {"ref": 46, "status": 82, "priority": 66, "kp": 104, "text": 230, "author": 80,
                     "date": 82, "condition": 108, "replies": 56}.get(key, 90)
            self.table.setColumnWidth(logical, width)
        header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)

    def _save_header(self) -> None:
        if self._header_key:
            QSettings().setValue(self._header_key, self.table.horizontalHeader().saveState())

    def _header_menu(self, pos) -> None:
        menu = QMenu(self)
        for logical, (_key, label, _shown) in enumerate(self.table_model.columns):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(logical))
            action.toggled.connect(lambda on, c=logical: self._set_column_shown(c, on))
        menu.addSeparator()
        menu.addAction("Reset columns", self._reset_columns)
        qt_exec(menu, self.table.horizontalHeader().mapToGlobal(pos))
        menu.deleteLater()

    def _set_column_shown(self, logical: int, on: bool) -> None:
        self.table.setColumnHidden(logical, not on)
        if on and self.table.columnWidth(logical) < 20:
            self.table.setColumnWidth(logical, 90)
        self._save_header()

    def _reset_columns(self) -> None:
        self._default_columns()
        self._save_header()

    def selected_ids(self) -> List[str]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return [self.proxy.data(index, ID_ROLE) for index in rows]

    def visible_ids(self) -> List[str]:
        return [self.proxy.data(self.proxy.index(row, 0), ID_ROLE) for row in range(self.proxy.rowCount())]

    def show_comment(self, comment_id: str) -> bool:
        """Select a comment (another tool asked); False when it is not in the set shown."""
        if comment_id not in self.by_id:
            self.reload()
        if comment_id not in self.by_id:
            return False
        self._select_ids([comment_id])
        return True

    def _select_ids(self, ids: List[str], scroll: bool = True) -> None:
        selection = QItemSelection()
        first = None
        for cid in ids:
            row = self.table_model.row_of(cid)
            if row < 0:
                continue
            index = self.proxy.mapFromSource(self.table_model.index(row, 0))
            if not index.isValid():
                continue
            selection.select(index, index)
            first = first or index
        selection_model = self.table.selectionModel()
        self._syncing = True
        try:
            selection_model.select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                   | QItemSelectionModel.SelectionFlag.Rows)
            if first is not None:
                selection_model.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
                if scroll:
                    self.table.scrollTo(first)
        finally:
            self._syncing = False
        self._after_selection(sync_map=True)

    def _on_table_selection(self, *_args) -> None:
        if not self._syncing:
            self._after_selection(sync_map=True)

    def _after_selection(self, sync_map: bool) -> None:
        ids = self.selected_ids()
        new_current = ids[0] if len(ids) == 1 else None
        if new_current != self.current_id and not self._confirm_leave():
            return
        if sync_map and self.act_show_map.isChecked():
            self._syncing = True
            try:
                self.layers.select(ids)
            finally:
                self._syncing = False
        if self.highlight is not None:
            self.highlight.show([self.by_id[c].get("wkt") for c in ids[:200] if c in self.by_id])
        if new_current != self.current_id or new_current is None:
            self._show_comment(new_current)
            if new_current is not None and not self._from_map and self.follow_button.isChecked():
                self._follow(new_current)
        self._update_enabled()
        self._update_status()

    def _on_layer_selection(self, *_args) -> None:
        if self._syncing:
            return
        ids = self.layers.selected_ids()
        hidden = self._hidden(ids)
        if hidden:
            self._show_banner(f"{len(hidden)} selected comment(s) are hidden by the search or filters. "
                              "<a href='clear'>Clear filters</a>")
        self._from_map = True
        try:
            self._select_ids(ids)
        finally:
            self._from_map = False

    def _hidden(self, ids: List[str]) -> List[str]:
        """Those of ``ids`` in the set but filtered out of the table."""
        out = []
        for cid in ids:
            row = self.table_model.row_of(cid)
            if row >= 0 and not self.proxy.mapFromSource(self.table_model.index(row, 0)).isValid():
                out.append(cid)
        return out

    def _on_double_click(self, index) -> None:
        key = self.table_model.key(self.proxy.mapToSource(index).column())
        if not self.table_model.is_editable(key):
            self.zoom_to_selected()

    def _inline_edit(self, cid: str, key: str, value) -> Optional[Dict]:
        """Save one cell edited in the table (with the same conflict check as Save)."""
        comment = self.by_id.get(cid)
        if comment is None or self.store is None:
            return None
        if cid == self.current_id and self._dirty and not self._confirm_leave():
            return None
        comment = self.by_id.get(cid, comment)
        if key.startswith("cf:"):
            fld = self.table_model.field_for(key)
            coerced, problem = model.coerce_custom(fld, value)
            if coerced is None and problem:
                QMessageBox.warning(self, "Route Comments", problem)
                return None
            values = model.load_custom(comment)
            if coerced is None:
                values.pop(fld.field_id, None)
            else:
                values[fld.field_id] = coerced
            changes = {"custom_json": model.dump_custom(values)}
        else:
            if key == "text" and not str(value or "").strip():
                QMessageBox.warning(self, "Route Comments", "A comment needs some text.")
                return None
            changes = {EDITABLE[key]: value}
        try:
            saved = self.store.update_comment(cid, changes, expected_version=comment.get("version"))
        except CommentStoreError as exc:
            self._error("Edit", exc)
            QTimer.singleShot(0, self.reload)
            return None
        self.by_id[cid] = saved
        QTimer.singleShot(0, self.reload)   # map, search text, details (not inside the editor's commit)
        return saved

    def _table_menu(self, pos) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        menu = QMenu(self)
        menu.addAction(_theme_icon("/mActionZoomToSelected.svg"), "Zoom to", self.zoom_to_selected)
        status_menu = menu.addMenu("Set status")
        for name in self.statuses():
            status_menu.addAction(name, lambda n=name: self.bulk_update({"status": n}))
        prio_menu = menu.addMenu("Set priority")
        for name in self.priorities():
            prio_menu.addAction(name, lambda n=name: self.bulk_update({"priority": n}))
        menu.addSeparator()
        menu.addAction("Copy comment ID" + ("s" if len(ids) > 1 else ""),
                       lambda: QApplication.clipboard().setText("\n".join(ids)))
        menu.addAction("Delete…", self.delete_selected)
        qt_exec(menu, self.table.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def bulk_update(self, changes: Dict) -> None:
        ids = self.selected_ids()
        if self.current_id in ids and not self._confirm_leave():
            return
        failed = []
        for cid in ids:
            try:
                self.store.update_comment(cid, changes)
            except CommentStoreError as exc:
                failed.append(str(exc))
        self.reload()
        if failed:
            QMessageBox.warning(self, "Route Comments", "\n".join(sorted(set(failed))))

    def zoom_to_selected(self) -> None:
        ids = self.selected_ids()
        if not ids or self.iface is None:
            return
        rect = None
        for cid in ids:
            geom = QgsGeometry.fromWkt(self.by_id.get(cid, {}).get("wkt") or "")
            if geom is None or geom.isNull():
                continue
            box = geom.boundingBox()
            if rect is None:
                rect = QgsRectangle(box)
            else:
                rect.combineExtentWith(box)
        if rect is None:
            return
        canvas = self.iface.mapCanvas()
        box = self._canvas_box(rect)
        if box is None:
            return
        if box.width() <= 0 and box.height() <= 0:
            canvas.setCenter(box.center())
        else:
            box.scale(1.4)
            canvas.setExtent(box)
        canvas.refresh()

    def _canvas_box(self, rect: QgsRectangle) -> Optional[QgsRectangle]:
        """``rect`` (EPSG:4326) in the map canvas CRS, or None."""
        xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                       self.iface.mapCanvas().mapSettings().destinationCrs(), QgsProject.instance())
        try:
            return xform.transformBoundingBox(rect)
        except Exception:  # noqa: BLE001 - outside the map CRS
            return None

    def _follow(self, cid: str) -> None:
        """Bring the comment into view when it is out of (or at the edge of) the map."""
        if self.iface is None:
            return
        geom = QgsGeometry.fromWkt(self.by_id.get(cid, {}).get("wkt") or "")
        if geom is None or geom.isNull():
            return
        box = self._canvas_box(geom.boundingBox())
        if box is None:
            return
        canvas = self.iface.mapCanvas()
        inner = QgsRectangle(canvas.extent())
        inner.scale(0.8)
        if inner.contains(box):
            return
        if box.width() > inner.width() or box.height() > inner.height():
            box.scale(1.4)
            canvas.setExtent(box)
        else:
            canvas.setCenter(box.center())
        canvas.refresh()

    def _current_row(self) -> int:
        """The table row (as sorted and filtered) of the comment shown, or -1."""
        if self.current_id is None or self.table.selectionModel() is None:
            return -1
        index = self.table.selectionModel().currentIndex()
        return index.row() if index.isValid() and self.proxy.data(index, ID_ROLE) == self.current_id else -1

    def step(self, delta: int) -> None:
        """Select the previous / next comment in the table's order."""
        count = self.proxy.rowCount()
        if not count:
            return
        here = self._current_row()
        row = here + delta if here >= 0 else (0 if delta > 0 else count - 1)
        if 0 <= row < count:
            self._select_ids([self.proxy.data(self.proxy.index(row, 0), ID_ROLE)])

    # ------------------------------------------------------------------ filters
    def _rebuild_filter_menu(self) -> None:
        menu = self.filter_menu
        self._clear_menu(menu)
        self._filter_actions = {}

        def group(title: str, key: str, items):
            sub = menu.addMenu(title)
            self._filter_actions[key] = {}
            for value, label in items:
                action = sub.addAction(label)
                action.setCheckable(True)
                action.setChecked(True)
                action.toggled.connect(self._apply_filter)
                self._filter_actions[key][value] = action

        group("Status", "status", [(n.casefold(), n) for n in self.statuses()] + [("", "(other)")])
        group("Priority", "priority", [(n.casefold(), n) for n in self.priorities()] + [("", "(other)")])
        group("Reference", "condition", [(c, model.COND_LABELS[c]) for c in
                                         (model.COND_OK, model.COND_SUPERSEDED, model.COND_CHANGED,
                                          model.COND_UNAVAILABLE, model.COND_NONE)])
        group("Type", "kind", [(k, model.REF_LABELS[k]) for k in model.REF_KINDS])
        menu.addSeparator()
        self.act_hide_closed = menu.addAction("Hide closed")
        self.act_hide_closed.setCheckable(True)
        self.act_hide_closed.toggled.connect(self._apply_filter)
        menu.addAction("Show all", self.clear_filters)
        self._apply_filter()

    def _checked(self, key: str, known: List[str]) -> Optional[set]:
        actions = self._filter_actions.get(key, {})
        if all(a.isChecked() for a in actions.values()):
            return None
        chosen = {value for value, action in actions.items() if action.isChecked() and value}
        if "" in actions and actions[""].isChecked():
            return _OtherSet(chosen, set(known))   # "(other)": values outside the set's choices
        return chosen

    def _apply_filter(self, *_args) -> None:
        flt = model.Filter(text=self.search.text(),
                           statuses=self._checked("status", [n.casefold() for n in self.statuses()]),
                           priorities=self._checked("priority", [n.casefold() for n in self.priorities()]),
                           conditions=self._checked("condition", []), kinds=self._checked("kind", []),
                           hide_closed=getattr(self, "act_hide_closed", None) is not None
                           and self.act_hide_closed.isChecked())
        self.proxy.set_filter(flt)
        active = any(v is not None for v in (flt.statuses, flt.priorities, flt.conditions, flt.kinds)) \
            or flt.hide_closed
        self.filter_button.setText("●" if active else "")
        self.filter_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if active
                                              else Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._update_status()

    def clear_filters(self) -> None:
        for actions in self._filter_actions.values():
            for action in actions.values():
                action.blockSignals(True)
                action.setChecked(True)
                action.blockSignals(False)
        self.act_hide_closed.blockSignals(True)
        self.act_hide_closed.setChecked(False)
        self.act_hide_closed.blockSignals(False)
        self.search.clear()
        self._apply_filter()
        self._show_banner("")

    def _update_status(self) -> None:
        total = len(self.comments)
        shown = self.proxy.rowCount()
        attention = sum(1 for c in self.comments if self.table_model.condition(c) in model.ATTENTION_CONDITIONS)
        text = f"{shown} of {total} comments" if shown != total else f"{total} comments"
        selected = len(self.selected_ids())
        if selected > 1:
            text += f" · {selected} selected"
        if attention:
            text += f" · {attention} with a changed or unavailable route"
        self.status.setText(text if self.set_row is not None else "")

    # ------------------------------------------------------------------ details
    def _on_form_changed(self) -> None:
        if self.current_id is not None:
            self._dirty = True
            self._update_enabled()

    def _confirm_leave(self) -> bool:
        """Save or discard unsaved edits (Details, or a message being edited
        in the thread) before moving on (True: carry on)."""
        pending = None if self._thread_saving else self.thread.pending_edit()
        if pending is not None and self.current_id is not None:
            what = "comment" if pending[0] is None else "reply"
            answer = QMessageBox.question(self, "Route Comments", f"Save your edit to the {what}?",
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard,
                                          QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save and not self._save_thread_edit(*pending):
                return False
            self.thread.cancel_edit()
        elif self.thread.is_editing() and not self._thread_saving:
            self.thread.cancel_edit()
        if not self._dirty or self.current_id is None:
            return True
        label = model.ref_label(self.by_id.get(self.current_id, {}).get("ref_no"))
        answer = QMessageBox.question(self, "Route Comments", f"Save the changes to comment {label}?",
                                      QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard,
                                      QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Save:
            return self.save_current()
        self._dirty = False
        return True

    def _show_comment(self, cid: Optional[str], keep_image: str = "") -> None:
        same = cid is not None and cid == self.current_id
        self.current_id = cid
        self._dirty = False
        comment = self.by_id.get(cid) if cid else None
        if comment is None:
            n = len(self.selected_ids())
            self.editor_title.setText(f"{n} comments selected — right-click the table to set their status or "
                                      "priority." if n > 1 else
                                      "Select a comment, or add one from the KP Mouse Tool's right-click menu "
                                      "(Add Comment).")
            self.condition_label.hide()
            self._set_quick(None)
            self.form.load({})
            self.form.setEnabled(False)
            self.thread.set_thread(None, [], "")
            self.info.setText("")
            self.tabs.setTabText(0, "Thread")
            self.pictures.set_pictures([], False)
            self.review_split.set_compact(True)
            self._loaded_version = None
            self._update_enabled()
            return
        self.form.setEnabled(True)
        title = (f"<b>{model.ref_label(comment.get('ref_no'))}</b> · "
                 f"{model.REF_LABELS.get(comment.get('ref_kind'), '')} · {model.location_text(comment)}")
        self.editor_title.setText(title)
        self.editor_title.setToolTip(model.location_text(comment))
        code, notes = self.table_model.conditions.get(cid) or (model.COND_NONE, [])
        if code in (model.COND_CHANGED, model.COND_UNAVAILABLE, model.COND_SUPERSEDED):
            self.condition_label.setText(f"<span style='color:{COND_COLORS[code]}'><b>{model.COND_LABELS[code]}."
                                         "</b></span> " + " ".join(notes))
            self.condition_label.show()
        else:
            self.condition_label.hide()
        self._set_quick(comment)
        self.form.load(comment)
        self._loaded_version = comment.get("version")
        replies = self.replies.get(cid, [])
        self.thread.set_thread(comment, replies, default_author())
        self.tabs.setTabText(0, f"Thread ({len(replies)})" if replies else "Thread")
        closed = str(comment.get("status") or "").casefold() in self.table_model.closed
        target = model.first_status(CommentStore.statuses_of(self.set_row), closed=not closed)
        self.close_button.setText("Send && reopen" if closed else "Send && close")
        self.close_button.setToolTip(f"Send the reply (if any) and set the status to {target}" if target else
                                     "The set has no status to change to (Set ▸ Statuses and priorities)")
        self.close_button.setProperty("target_status", target)
        try:
            attachments = self.store.list_attachments(cid)
        except CommentStoreError as exc:
            self._error("Images", exc)
            attachments = []
        shown = str(comment.get("map_image") or "")
        items = [{"attachment_id": row.get("attachment_id"), "path": self.store.attachment_path(row),
                  "name": row.get("original_name") or "", "on_map": bool(shown) and row.get("file_name") == shown}
                 for row in attachments]
        self.pictures.set_pictures(items, True, keep_image or (self.pictures.current_id() if same else ""))
        self.review_split.set_compact(not items)
        self.info.setText(f"Created {str(comment.get('created_utc') or '')[:16].replace('T', ' ')} UTC · updated "
                          f"{str(comment.get('updated_utc') or '')[:16].replace('T', ' ')} UTC · "
                          f"{comment.get('kp_basis') or ''}<br>ID {comment.get('comment_id')}"
                          + (f" · external ID {comment.get('ext_id')}" if comment.get("ext_id") else ""))
        self._update_enabled()

    def _set_quick(self, comment: Optional[Dict]) -> None:
        """The header's status and priority choices, showing ``comment``'s."""
        for combo, key, choices in ((self.quick_status, "status", self.statuses()),
                                    (self.quick_priority, "priority", self.priorities())):
            combo.blockSignals(True)
            combo.clear()
            if comment is not None:
                value = str(comment.get(key) or "")
                # An empty or unlisted value is shown as it is, never as the first choice.
                combo.addItems(([""] if not value else []) + list(choices)
                               + ([value] if value and value not in choices else []))
                combo.setCurrentIndex(max(0, combo.findText(value)))
            combo.blockSignals(False)
            combo.setEnabled(comment is not None)

    def _quick_set(self, key: str) -> None:
        combo = self.quick_status if key == "status" else self.quick_priority
        cid = self.current_id
        comment = self.by_id.get(cid) if cid else None
        if comment is None or combo.currentText() == str(comment.get(key) or ""):
            return
        if self._inline_edit(cid, key, combo.currentText()) is None:
            self._set_quick(self.by_id.get(cid))

    def save_current(self) -> bool:
        cid = self.current_id
        if cid is None or self.store is None or not self._dirty:
            return True
        problems = self.form.problems()
        if problems:
            QMessageBox.warning(self, "Route Comments", "\n".join(problems))
            return False
        values = self.form.values(model.load_custom(self.by_id.get(cid, {})))
        try:
            saved = self.store.update_comment(cid, values, expected_version=self._loaded_version)
        except ConflictError as exc:
            answer = QMessageBox.question(self, "Route Comments", f"{exc}\n\nOverwrite with your version?",
                                          MESSAGEBOX_YES | MESSAGEBOX_NO, MESSAGEBOX_NO)
            if answer != MESSAGEBOX_YES or self.store.get_comment(cid) is None:
                return False
            try:
                saved = self.store.update_comment(cid, values)
            except CommentStoreError as exc2:
                self._error("Save", exc2)
                return False
        except CommentStoreError as exc:
            self._error("Save", exc)
            return False
        remember_author(values.get("author") or "")
        self._dirty = False
        self.by_id[cid] = saved
        self.reload()
        self._message(f"Comment {model.ref_label(saved.get('ref_no'))} saved.", MESSAGE_SUCCESS, 2)
        return True

    def revert_current(self) -> None:
        self._dirty = False
        self._show_comment(self.current_id)

    def delete_selected(self) -> None:
        ids = self.selected_ids()
        if not ids or self.store is None:
            return
        labels = ", ".join(model.ref_label(self.by_id.get(c, {}).get("ref_no")) for c in ids[:10])
        more = f" and {len(ids) - 10} more" if len(ids) > 10 else ""
        answer = QMessageBox.question(self, "Delete comments",
                                      f"Delete {labels}{more} with their replies and images? This cannot be undone.",
                                      MESSAGEBOX_YES | MESSAGEBOX_NO, MESSAGEBOX_NO)
        if answer != MESSAGEBOX_YES:
            return
        try:
            self.store.delete_comments(ids)
        except CommentStoreError as exc:
            self._error("Delete", exc)
            return
        self._dirty = False
        self.current_id = None
        self.reload(keep_selection=False)

    # ------------------------------------------------------------------ thread & images
    def _keep_edits_and_reload(self) -> None:
        """Reload, keeping unsaved Details edits of the current comment."""
        if not self._dirty:
            self.reload()
            return
        cid, keep = self.current_id, self.form.values()
        self.reload()
        if cid in self.by_id:
            self.form.load(dict(self.by_id[cid], **keep))
            self._dirty = True
            self._update_enabled()

    def add_reply(self, status_to: str = "") -> None:
        """Send the reply; with ``status_to`` also set the comment's status (one save)."""
        text = self.reply_text.toPlainText().strip()
        if (not text and not status_to) or self.current_id is None or self.store is None:
            return
        if status_to and not self._confirm_leave():
            return
        try:
            self.store.add_reply(self.current_id, default_author(), text, status_to)
        except CommentStoreError as exc:
            self._error("Reply", exc)
            return
        self.reply_text.clear()
        self._keep_edits_and_reload()
        self.tabs.setCurrentIndex(0)

    def send_and_toggle(self) -> None:
        """*Send & close* (or *Send & reopen* on a closed comment)."""
        target = str(self.close_button.property("target_status") or "")
        if target:
            self.add_reply(status_to=target)

    def _save_thread_edit(self, reply_id: Optional[str], text: str) -> bool:
        """Save a message edited in the thread: the comment's text (``reply_id``
        None, with the same check against changes made elsewhere as a table
        edit) or a reply's. The edit stays open when it cannot be saved."""
        cid = self.current_id
        if cid is None or self.store is None:
            return False
        if reply_id is None and not str(text or "").strip():
            QMessageBox.warning(self, "Route Comments", "A comment needs some text.")
            return False
        self._thread_saving = True
        try:
            if reply_id is None:
                if self._inline_edit(cid, "text", text) is None:
                    return False
            else:
                try:
                    saved = self.store.update_reply(reply_id, text)
                except CommentStoreError as exc:
                    self._error("Reply", exc)
                    return False
                for reply in self.replies.get(cid, []):
                    if reply.get("reply_id") == reply_id:
                        reply.update(saved)
                QTimer.singleShot(0, self._keep_edits_and_reload)
        finally:
            self._thread_saving = False
        self.thread.cancel_edit()
        return True

    def delete_reply(self, reply_id: str) -> None:
        """Delete a reply. One that changed the status (*Send & close*) can
        take that change with it, while the status is still the one it set."""
        cid = self.current_id
        replies = self.replies.get(cid, []) if cid else []
        reply = next((r for r in replies if r.get("reply_id") == reply_id), None)
        comment = self.by_id.get(cid) if cid else None
        if reply is None or comment is None or self.store is None:
            return
        current = str(comment.get("status") or "")
        restore = model.status_to_restore(reply, replies, current, CommentStore.statuses_of(self.set_row))
        choice = self._ask_reply_delete(str(reply.get("status_to") or ""), restore, current)
        if not choice:
            return
        try:
            self.store.delete_reply(reply_id, restore if choice == "restore" else None)
        except CommentStoreError as exc:
            self._error("Delete reply", exc)
            return
        self._keep_edits_and_reload()

    def _ask_reply_delete(self, status_to: str, restore: str, current: str) -> str:
        """``"restore"`` (delete, status back to ``restore``), ``"keep"`` (delete,
        status unchanged) or ``""`` (cancel)."""
        if not status_to:
            answer = QMessageBox.question(self, "Delete reply", "Delete this reply?", MESSAGEBOX_YES | MESSAGEBOX_NO,
                                          MESSAGEBOX_NO)
            return "keep" if answer == MESSAGEBOX_YES else ""
        if not restore:
            answer = QMessageBox.question(
                self, "Delete reply", f"Delete this reply? It set the status to {status_to}; the status has changed "
                f"since and stays {current}.", MESSAGEBOX_YES | MESSAGEBOX_NO, MESSAGEBOX_NO)
            return "keep" if answer == MESSAGEBOX_YES else ""
        box = QMessageBox(QMessageBox.Icon.Question, "Delete reply",
                          f"This reply also set the status to {status_to}.", parent=self)
        back = box.addButton(f"Delete it, status back to {restore}", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton(f"Delete it, keep {current}", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(back)
        qt_exec(box)
        if box.clickedButton() is back:
            return "restore"
        return "keep" if box.clickedButton() is keep else ""

    def _reload_keeping_image(self, attachment_id: str) -> None:
        self._keep_edits_and_reload()
        if self.current_id is not None and attachment_id:
            self._show_image(attachment_id)

    def _show_image(self, attachment_id: str) -> None:
        for row, item in enumerate(self.pictures.items):
            if item.get("attachment_id") == attachment_id:
                self.pictures.strip.setCurrentRow(row)
                return

    def add_images(self) -> None:
        if self.current_id is None:
            return
        folder = str(QSettings().value(f"{SETTINGS}/image_dir", "") or "")
        paths, _f = QFileDialog.getOpenFileNames(
            self, "Add images", folder, "Images (" + " ".join("*" + e for e in model.IMAGE_EXTENSIONS) + ")")
        if not paths:
            return
        QSettings().setValue(f"{SETTINGS}/image_dir", os.path.dirname(paths[0]))
        self.add_image_files(paths)

    def add_image_files(self, paths: List[str]) -> None:
        """Copy image files into the register for the selected comment (unreadable ones are left out)."""
        if self.current_id is None or self.store is None:
            return
        good = [p for p in paths if model.is_image_name(p) and image_size(p).isValid()]
        skipped = [os.path.basename(p) for p in paths if p not in good]
        if skipped:
            QMessageBox.warning(self, "Route Comments", "Not added (not a readable image): " + ", ".join(skipped))
        if not good:
            return
        try:
            rows = self.store.add_attachments(self.current_id, good)
        except CommentStoreError as exc:
            self._error("Images", exc)
            return
        self._reload_keeping_image(rows[-1]["attachment_id"])

    def add_image_data(self, image: QImage) -> None:
        """Save an image (pasted or dropped as data) as a PNG in the register."""
        if self.current_id is None or self.store is None or image is None or image.isNull():
            return
        if image.width() * image.height() > MAX_PIXELS:
            QMessageBox.warning(self, "Route Comments", "The image is too large to add.")
            return
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(WRITE_ONLY)
        image.save(buffer, "PNG")
        buffer.close()
        name = "Pasted image " + _dt.datetime.now().strftime("%Y-%m-%d %H%M%S") + ".png"
        try:
            row = self.store.add_image_bytes(self.current_id, bytes(data), name)
        except CommentStoreError as exc:
            self._error("Images", exc)
            return
        self._reload_keeping_image(row["attachment_id"])

    def paste_image(self) -> None:
        """Ctrl+V outside text boxes: add the clipboard's image (or image files)."""
        if self.current_id is None:
            self._message("Select one comment to paste an image into.")
            return
        paths, image = pictures_from_mime(QApplication.clipboard().mimeData())
        if paths:
            self.add_image_files(paths)
        elif image is not None:
            self.add_image_data(image)
        else:
            self._message("The clipboard holds no image.")

    def remove_image(self, attachment_id: str) -> None:
        item = next((i for i in self.pictures.items if i.get("attachment_id") == attachment_id), None)
        if item is None or self.store is None:
            return
        if QMessageBox.question(self, "Remove image", f"Remove {item.get('name')} from this comment (the copy in "
                                "the register's attachments folder is deleted)?", MESSAGEBOX_YES | MESSAGEBOX_NO,
                                MESSAGEBOX_NO) != MESSAGEBOX_YES:
            return
        try:
            self.store.delete_attachment(attachment_id)
        except CommentStoreError as exc:
            self._error("Remove image", exc)
            return
        self._keep_edits_and_reload()

    def set_map_image(self, attachment_id: str, on: bool) -> None:
        """Show (or stop showing) an image in the comment's map label."""
        if self.current_id is None or self.store is None:
            return
        try:
            self.store.set_map_image(self.current_id, attachment_id if on else None)
        except CommentStoreError as exc:
            self._error("Map image", exc)
            self._keep_edits_and_reload()
            return
        if on:
            self._prepare_map_images()
        self._reload_keeping_image(attachment_id)

    def _prepare_map_images(self) -> None:
        """Make sure an image chosen for the map can be seen: the map and its
        labels on, labels that can hold images, and a word on the scale."""
        if not self.act_show_map.isChecked():
            self.act_show_map.setChecked(True)
        if not self.act_labels.isChecked():
            self.act_labels.setChecked(True)
        layers = self.layers.layers()
        settings = map_layers.label_settings_of(self.layers.find("points"))
        if layers and (not settings.images or any(map_layers.labels_predate_images(x) for x in layers)):
            answer = QMessageBox.question(
                self, "Route Comments", "The comment labels are not set to show images. Update them now? This "
                "replaces label changes made in Layer Properties (Label contents… sets them again).",
                MESSAGEBOX_YES | MESSAGEBOX_NO, MESSAGEBOX_YES)
            if answer == MESSAGEBOX_YES:
                settings.images = True
                map_layers.apply_labels(layers, settings, True)
        if self.iface is not None and self.iface.mapCanvas().scale() > settings.detail_scale:
            self._message(f"The image shows in the comment's label when zoomed in beyond 1:{settings.detail_scale:,}.")

    def _open_file(self, path: str) -> None:
        if path and os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------------------------ capture
    def add_comment_from_capture(self, kind: str, capture: CaptureRoute, data: Dict) -> Optional[Dict]:
        """Add a comment captured with the KP Mouse Tool (asks for its text)."""
        if not self._confirm_leave() or not self.ensure_register():
            return None
        try:
            if kind == model.REF_ROUTE_POINT:
                values = capture.point_values(data["kp_km"])
            elif kind == model.REF_KP_RANGE:
                values = capture.range_values(*data["kps"])
            else:
                values = capture.location_values(data["point"])
        except (ValueError, KeyError) as exc:
            self._error("New comment", exc)
            return None
        statuses, priorities = self.statuses(), self.priorities()
        dialog = NewCommentDialog(self, values, capture, statuses, priorities, self.fields,
                                  statuses[0] if statuses else "", model.default_priority(priorities),
                                  self.set_row["name"])
        if not qt_exec(dialog):
            return None
        try:
            comment = self.store.add_comment(self.set_row["set_id"], dialog.result_values())
        except CommentStoreError as exc:
            self._error("New comment", exc)
            return None
        self.checker.clear()  # the routes may have been edited since the last check
        self.reload()
        self._select_ids([comment["comment_id"]])
        self._message(f"Comment {model.ref_label(comment.get('ref_no'))} added to '{self.set_row['name']}'.",
                      MESSAGE_SUCCESS, 3)
        return comment

    # ------------------------------------------------------------------ import / export
    def import_comments(self) -> None:
        if not self.ensure_register():
            return
        routes: Dict[str, CaptureRoute] = {}

        def route_for(layer) -> CaptureRoute:
            if layer.id() not in routes:
                start = self.start_kp_for(layer) if self.start_kp_for else None
                routes[layer.id()] = CaptureRoute.from_layer(layer, layer.crs(), start)
            return routes[layer.id()]

        def kp_range(layer):
            try:
                route = route_for(layer)
                return route.start_kp_km, route.end_kp_km
            except Exception:  # noqa: BLE001 - shown as row errors ("KPs need a route")
                log_exception("Route Comments: the import route could not be measured")
                return None

        def build(rows, layer):
            route = route_for(layer) if layer is not None else None
            out = []
            for row in rows:
                values = dict(row.values)
                if row.kind == model.REF_LOCATION:
                    ref = route.location_from_wgs84(values["lat"], values["lon"]) if route else \
                        location_without_route(values["lat"], values["lon"])
                elif row.kind == model.REF_KP_RANGE:
                    ref = route.range_values(values["kp_km"], values["kp_end_km"])
                else:
                    ref = route.point_values(values["kp_km"])
                values.update(ref)
                out.append(values)
            return out

        dialog = ImportDialog(self, self.set_row["name"], self.fields, CommentStore.statuses_of(self.set_row),
                              self.priorities(), {i.casefold() for i in self.store.known_ids()},
                              self.reference_layer(), kp_range, build)
        if not qt_exec(dialog):
            return
        try:
            added = self.store.add_comments(self.set_row["set_id"], dialog.result_rows)
        except CommentStoreError as exc:
            self._error("Import", exc)
            return
        self.reload(keep_selection=False)
        self._select_ids([c["comment_id"] for c in added])
        skipped = sum(1 for r in dialog.checked if not r.ok)
        images = sum(int(c.get("attachment_count") or 0) for c in added)
        self._message(f"Imported {len(added)} comment(s) into '{self.set_row['name']}'"
                      + (f" with {images} image(s)" if images else "")
                      + (f"; {skipped} row(s) skipped." if skipped else "."), MESSAGE_SUCCESS, 5)

    def export_comments(self) -> None:
        if self.store is None or self.set_row is None:
            return
        try:
            total = len(self.store.list_comments())
        except CommentStoreError as exc:
            self._error("Export", exc)
            return
        dialog = ExportDialog(self, {"visible": self.proxy.rowCount(), "set": len(self.comments), "register": total},
                              self.set_row["name"])
        if not qt_exec(dialog):
            return
        scope = dialog.scope()
        folder = str(QSettings().value(f"{SETTINGS}/export_dir", "") or os.path.dirname(self.store.path))
        stem = self.set_row["name"] if scope != "register" else self.store.name
        from ..workbench.schema import sanitize_slug
        path, chosen = QFileDialog.getSaveFileName(self, "Export comments",
                                                   os.path.join(folder, f"{sanitize_slug(stem)}_comments.xlsx"),
                                                   "Excel workbook (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv" if "CSV" in chosen else ".xlsx"
        QSettings().setValue(f"{SETTINGS}/export_dir", os.path.dirname(path))
        try:
            headers, rows, about = self.export_data(scope)
            from .exchange import write_table
            write_table(path, headers, rows, about)
        except (CommentStoreError, OSError, ValueError) as exc:
            self._error("Export", exc)
            return
        self._message(f"Exported {len(rows)} comment(s) to {path}.", MESSAGE_SUCCESS, 5)

    def export_data(self, scope: str):
        """``(headers, rows, about)`` for ``scope``: visible / set / register."""
        sets = {s["set_id"]: s for s in self.store.list_sets()}
        if scope == "register":
            comments = sorted(self.store.list_comments(),
                              key=lambda c: (str(sets.get(c["set_id"], {}).get("name") or "").casefold(),
                                             int(c.get("ref_no") or 0)))
            replies = self.store.replies_by_comment()
            fields = {sid: self.store.list_fields(sid) for sid in sets}
            conditions = {cid: cond for cid, (cond, _n) in self.checker.check_all(comments).items()}
            label = "The whole register (every set)"
        else:
            ids = self.visible_ids() if scope == "visible" else [c["comment_id"] for c in self.comments]
            comments = [self.by_id[cid] for cid in ids if cid in self.by_id]
            replies = self.replies
            fields = {self.set_row["set_id"]: self.fields}
            conditions = {cid: cond for cid, (cond, _n) in self.table_model.conditions.items()}
            label = (f"Visible rows of '{self.set_row['name']}' (search: '{self.search.text()}')"
                     if scope == "visible" else f"All of '{self.set_row['name']}'")
        headers, rows = model.export_rows(comments, {k: v["name"] for k, v in sets.items()}, fields, conditions,
                                          replies)
        about = [("Register", self.store.path), ("Exported", _dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
                 ("Scope", label), ("Rows", str(len(rows))), ("Contents", model.EXPORT_NOTE),
                 ("KPs", "As captured on the route named in each row, with that route's start KP and KP "
                         "convention. They are not recomputed; 'Reference condition' says whether the route "
                         "has changed since."),
                 ("Coordinates", "Latitude/Longitude (WGS84): the comment's point, or the start of a KP range. "
                                 "The geometry column has the full point or range line.")]
        return headers, rows, about

    # ------------------------------------------------------------------ map pick
    def _toggle_pick(self, on: bool) -> None:
        if self.iface is None:
            return
        canvas = self.iface.mapCanvas()
        if on:
            if not self.act_show_map.isChecked():
                self.act_show_map.setChecked(True)
            if self.pick_tool is None:
                self.pick_tool = map_layers.CommentPickTool(canvas, self.layers.layers, self._describe)
                self.pick_tool.picked.connect(self._on_picked)
                canvas.mapToolSet.connect(self._on_map_tool_set)
            canvas.setMapTool(self.pick_tool)
        elif self.pick_tool is not None and canvas.mapTool() is self.pick_tool:
            canvas.unsetMapTool(self.pick_tool)

    def _on_map_tool_set(self, new_tool, _old=None) -> None:
        if self.pick_tool is not None and new_tool is not self.pick_tool and self.act_pick.isChecked():
            self.act_pick.blockSignals(True)
            self.act_pick.setChecked(False)
            self.act_pick.blockSignals(False)

    def _describe(self, cid: str) -> str:
        comment = self.by_id.get(cid, {})
        return (f"{model.ref_label(comment.get('ref_no'))} · {comment.get('status') or ''} · "
                f"{model.summary(comment.get('text'), 50)}")

    def _on_picked(self, ids: List[str]) -> None:
        if self._hidden(ids):
            self.clear_filters()
        self._from_map = True
        try:
            self._select_ids(ids)
        finally:
            self._from_map = False
        if ids and not self.isVisible():
            self.show()

    # ------------------------------------------------------------------ misc
    def _show_banner(self, html: str) -> None:
        self.banner.setText(html)
        self.banner.setVisible(bool(html))

    def _banner_link(self, link: str) -> None:
        if link == "new":
            self.new_register()
        elif link == "open":
            self.open_register()
        elif link == "clear":
            self.clear_filters()
        elif link == "reload":
            self._dirty = False
            self._show_banner("")
            self.reload()

    def _poll(self) -> None:
        """Pick up changes another project or QGIS committed to the register."""
        if self.store is None or not self.isVisible():
            return
        version = self.store.data_version()
        if version == self._data_version or version < 0:
            return
        if self._dirty or self.thread.is_editing() or self.table.state() == QAbstractItemView.State.EditingState:
            self._data_version = version
            self._show_banner("The register was changed elsewhere (another project or QGIS). Saving checks for "
                              "conflicts. <a href='reload'>Reload now</a> (discards your unsaved edits)")
            return
        self.checker.clear()
        self.reload()

    def _update_enabled(self) -> None:
        has_set = self.set_row is not None
        has_comment = self.current_id is not None
        for widget in (self.search, self.filter_button, self.map_button, self.pick_button):
            widget.setEnabled(has_set)
        for action in (self.act_rename_set, self.act_delete_set, self.act_fields, self.act_choices,
                       self.act_export):
            action.setEnabled(has_set)
        self.act_import.setEnabled(self.store is not None)
        self.set_combo.setEnabled(self.store is not None)
        self.save_button.setEnabled(has_comment and self._dirty)
        self.revert_button.setEnabled(has_comment and self._dirty)
        self.delete_button.setEnabled(bool(self.selected_ids()))
        for widget in (self.reply_text, self.reply_button):
            widget.setEnabled(has_comment)
        self.close_button.setEnabled(has_comment and bool(self.close_button.property("target_status")))
        count = self.proxy.rowCount() if has_set else 0
        here = self._current_row()
        self.prev_button.setEnabled(count > 0 and here != 0)
        self.next_button.setEnabled(count > 0 and (here < 0 or here < count - 1))

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        if not self._poll_timer.isActive():
            self._poll_timer.start()

    def hideEvent(self, event):  # noqa: N802 - Qt API
        self._poll_timer.stop()
        self._save_window_state()
        super().hideEvent(event)

    def shutdown(self) -> None:
        """Plugin unload: stop timers, release the map tool and canvas items,
        disconnect from the project and close the register (the map layers
        stay in the project: they read the register directly)."""
        self._save_window_state()
        self._poll_timer.stop()
        self._header_timer.stop()
        self._search_timer.stop()
        if self._project_connected:
            project = QgsProject.instance()
            for signal in (project.readProject, project.cleared):
                try:
                    signal.disconnect(self._on_project_changed)
                except (TypeError, RuntimeError):
                    pass
            self._project_connected = False
        self._disconnect_layers()
        if self.iface is not None and self.pick_tool is not None:
            canvas = self.iface.mapCanvas()
            try:
                canvas.mapToolSet.disconnect(self._on_map_tool_set)
            except (TypeError, RuntimeError):
                pass
            if canvas.mapTool() is self.pick_tool:
                canvas.unsetMapTool(self.pick_tool)
            self.pick_tool.deleteLater()
            self.pick_tool = None
        if self.highlight is not None:
            self.highlight.clear()
        self.pictures.shutdown()
        self.thumb_cache.clear()
        if self.store is not None:
            self.store.close()


class _OtherSet(set):
    """Chosen values plus "(other)": any value outside the set's own choices."""

    def __init__(self, chosen: set, known: set):
        super().__init__(chosen)
        self.known = known

    def __contains__(self, value) -> bool:
        return set.__contains__(self, value) or value not in self.known
