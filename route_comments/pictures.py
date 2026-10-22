# -*- coding: utf-8 -*-
"""The Route Comments image panel: a viewer, thumbnails, add / paste / drop.

Images are read with :class:`QImageReader` at the size they are shown
(thumbnails at thumbnail size, the viewer at most :data:`VIEW_MAX_SIDE`
pixels), turned upright from their EXIF orientation, and refused above
:data:`MAX_PIXELS` unless the format can be read reduced (JPEG). Only the
picture on show is kept at viewing size; thumbnails live in a small
bounded cache the dock clears with the register. Nothing is read from the
network: drops and pastes take local files or image data only.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QIcon, QImage, QImageReader, QPainter, QPalette, QPixmap
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel, QListView, QListWidget,
                                 QListWidgetItem, QMenu, QPushButton, QSizePolicy, QStyle, QToolButton, QVBoxLayout,
                                 QWidget)

from ..qgis_compat import CONTEXT_MENU_POLICY_CUSTOM, MOUSE_BUTTON_LEFT, qt_exec
from . import model

THUMB_PX = 56
VIEW_MAX_SIDE = 3200            # the viewer never holds more than this on a side
MAX_PIXELS = 60_000_000         # larger images (other than JPEG, read reduced) are not decoded
THUMB_CACHE_SIZE = 200
ID_ROLE = Qt.ItemDataRole.UserRole
PATH_ROLE = Qt.ItemDataRole.UserRole + 2


def read_image(path: str, max_side: int = 0) -> Tuple[QImage, str]:
    """``(image, problem)``: ``path`` upright, reduced to fit ``max_side``
    (0: full size). A null image comes with the reason."""
    if not path or not os.path.isfile(path):
        return QImage(), "The image file is missing."
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid():
        return QImage(), f"Cannot read this image: {reader.errorString()}"
    jpeg = bytes(reader.format()).lower() in (b"jpeg", b"jpg")
    if size.width() * size.height() > MAX_PIXELS and not jpeg:
        return QImage(), (f"Too large to preview ({size.width()} × {size.height()} pixels). "
                          "Double-click to open it in your image viewer.")
    if max_side and max(size.width(), size.height()) > max_side:
        reader.setScaledSize(size.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return QImage(), f"Cannot read this image: {reader.errorString()}"
    return image, ""


def image_size(path: str) -> QSize:
    """The image's size from its header (invalid when unreadable)."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    return reader.size()


def pictures_from_mime(mime) -> Tuple[List[str], Optional[QImage]]:
    """``(local image files, image data)`` carried by a drop or the clipboard."""
    paths = []
    if mime is not None and mime.hasUrls():
        for url in mime.urls():
            path = os.path.normpath(url.toLocalFile()) if url.isLocalFile() else ""
            if path and model.is_image_name(path) and os.path.isfile(path):
                paths.append(path)
    image = None
    if not paths and mime is not None and mime.hasImage():
        data = mime.imageData()
        image = data if isinstance(data, QImage) else QImage(data) if data is not None else None
        if image is not None and image.isNull():
            image = None
    return paths, image


class ThumbnailCache:
    """Thumbnails by file, size and modification time; the oldest go first."""

    def __init__(self, limit: int = THUMB_CACHE_SIZE):
        self.limit = limit
        self._items: "OrderedDict[tuple, QIcon]" = OrderedDict()

    def get(self, path: str, px: int) -> Optional[QIcon]:
        key = self._key(path, px)
        icon = self._items.get(key)
        if icon is not None:
            self._items.move_to_end(key)
        return icon

    def make(self, path: str, px: int) -> QIcon:
        image, _problem = read_image(path, px)
        if image.isNull():
            return QIcon()
        icon = QIcon(QPixmap.fromImage(image))
        self._items[self._key(path, px)] = icon
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        return icon

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    @staticmethod
    def _key(path: str, px: int) -> tuple:
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            stamp = 0
        return os.path.normcase(path), stamp, int(px)


def _event_pos(event) -> QPointF:
    return event.position() if hasattr(event, "position") else QPointF(event.localPos())


class PictureView(QWidget):
    """One image: fitted to the view, or zoomed (wheel) and panned (drag)."""

    open_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(120, 90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.customContextMenuRequested.connect(self._menu)
        self.image = QImage()
        self.message = ""
        self._fit = True
        self._scale = 1.0
        self._offset = QPointF(0, 0)        # image top-left in widget pixels
        self._drag: Optional[QPointF] = None
        self._scaled: Optional[QPixmap] = None
        self._scaled_key = None

    def set_image(self, image: QImage, message: str = "") -> None:
        self.image = image if image is not None else QImage()
        self.message = message
        self._scaled, self._scaled_key = None, None
        self._fit = True
        self._layout_image()
        self.update()

    def clear(self) -> None:
        self.set_image(QImage(), "")

    def fit(self) -> None:
        self._fit = True
        self._layout_image()
        self.update()

    def actual_size(self) -> None:
        if self.image.isNull():
            return
        self._zoom_to(1.0, QPointF(self.width() / 2, self.height() / 2))

    def _fit_scale(self) -> float:
        if self.image.isNull() or self.width() < 2 or self.height() < 2:
            return 1.0
        return min(self.width() / self.image.width(), self.height() / self.image.height(), 1.0)

    def _layout_image(self) -> None:
        if self.image.isNull():
            return
        if self._fit:
            self._scale = self._fit_scale()
            self._offset = QPointF((self.width() - self.image.width() * self._scale) / 2,
                                   (self.height() - self.image.height() * self._scale) / 2)

    def _zoom_to(self, scale: float, anchor: QPointF) -> None:
        low = min(self._fit_scale(), 1.0) * 0.5
        scale = max(low, min(8.0, scale))
        image_point = (anchor - self._offset) / self._scale
        self._scale = scale
        self._offset = anchor - image_point * scale
        self._fit = False
        self.update()

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._layout_image()

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if self.image.isNull():
            return
        steps = event.angleDelta().y() / 120.0
        if steps:
            self._zoom_to(self._scale * (1.25 ** steps), _event_pos(event))
        event.accept()

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        if event.button() == MOUSE_BUTTON_LEFT and not self.image.isNull():
            self._drag = _event_pos(event)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt API
        if self._drag is not None:
            pos = _event_pos(event)
            self._offset += pos - self._drag
            self._drag = pos
            self._fit = False
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt API
        self._drag = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt API
        self.open_requested.emit()
        event.accept()

    def _menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Fit to the panel", self.fit)
        menu.addAction("Actual size", self.actual_size)
        menu.addSeparator()
        menu.addAction("Open in your image viewer", self.open_requested.emit)
        copy = menu.addAction("Copy image", lambda: QApplication.clipboard().setImage(self.image))
        copy.setEnabled(not self.image.isNull())
        qt_exec(menu, self.mapToGlobal(pos))
        menu.deleteLater()

    def paintEvent(self, event):  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Base))
        if self.image.isNull():
            if self.message:
                painter.setPen(self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text))
                painter.drawText(self.rect().adjusted(12, 12, -12, -12),
                                 int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), self.message)
            painter.end()
            return
        target = QRectF(self._offset.x(), self._offset.y(), self.image.width() * self._scale,
                        self.image.height() * self._scale)
        if self._scale < 1.0:
            # Smoothly reduced once per scale, then drawn as is (repaints stay cheap).
            ratio = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
            key = (round(self._scale, 4), ratio)
            if self._scaled_key != key:
                size = QSize(max(1, round(target.width() * ratio)), max(1, round(target.height() * ratio)))
                pixmap = QPixmap.fromImage(self.image.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                                             Qt.TransformationMode.SmoothTransformation))
                pixmap.setDevicePixelRatio(ratio)
                self._scaled, self._scaled_key = pixmap, key
            painter.drawPixmap(target.topLeft(), self._scaled)
        else:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._scale < 2.0)
            visible = target.intersected(QRectF(self.rect()))
            if not visible.isEmpty():
                source = QRectF((visible.x() - target.x()) / self._scale, (visible.y() - target.y()) / self._scale,
                                visible.width() / self._scale, visible.height() / self._scale)
                painter.drawImage(visible, self.image, source)
        painter.end()


class PicturePanel(QWidget):
    """The selected comment's images: viewer, thumbnails and their actions.
    Files dropped on the panel and pasted images are passed to the dock."""

    add_requested = pyqtSignal()
    paste_requested = pyqtSignal()
    files_dropped = pyqtSignal(list)
    image_dropped = pyqtSignal(QImage)
    remove_requested = pyqtSignal(str)
    map_image_changed = pyqtSignal(str, bool)
    open_requested = pyqtSignal(str)

    def __init__(self, cache: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.cache = cache
        self.items: List[Dict] = []
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(2)
        self.title = QLabel()
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.prev_button = self._tool("◀", "Previous image", lambda: self.step(-1))
        self.next_button = self._tool("▶", "Next image", lambda: self.step(1))
        head.addWidget(self.title, 1)
        head.addWidget(self.prev_button)
        head.addWidget(self.next_button)
        layout.addLayout(head)

        self.view = PictureView()
        self.view.open_requested.connect(self._open_current)
        layout.addWidget(self.view, 1)

        self.strip = QListWidget()
        self.strip.setViewMode(QListView.ViewMode.IconMode)
        self.strip.setFlow(QListView.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMovement(QListView.Movement.Static)
        self.strip.setIconSize(QSize(THUMB_PX, THUMB_PX))
        self.strip.setGridSize(QSize(THUMB_PX + 8, THUMB_PX + 8))
        self.strip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bar = self.strip.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        self.strip.setFixedHeight(THUMB_PX + 8 + 2 * self.strip.frameWidth() + bar)
        self.strip.setAcceptDrops(False)
        self.strip.currentRowChanged.connect(self._show_row)
        self.strip.itemDoubleClicked.connect(lambda item: self.open_requested.emit(item.data(PATH_ROLE) or ""))
        layout.addWidget(self.strip)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.add_button = QPushButton("Add…")
        self.add_button.setToolTip("Add image files to this comment (or drop them here)")
        self.add_button.clicked.connect(self.add_requested.emit)
        self.paste_button = QPushButton("Paste")
        self.paste_button.setToolTip("Add the image on the clipboard, e.g. a screenshot (Ctrl+V)")
        self.paste_button.clicked.connect(self.paste_requested.emit)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.clicked.connect(self._remove_current)
        self.on_map = QCheckBox("Show on map")
        self.on_map.setToolTip("Show this image in the comment's map label (one image per comment), "
                               "when zoomed in to the detail labels")
        self.on_map.toggled.connect(self._map_toggled)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.paste_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        buttons.addWidget(self.on_map)
        layout.addLayout(buttons)

        self._thumb_queue: List[QListWidgetItem] = []
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(0)
        self._thumb_timer.timeout.connect(self._next_thumbnail)
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(30)   # stepping quickly through comments reads only the last image
        self._load_timer.timeout.connect(self._load_current)
        self._syncing = False
        self.set_pictures([], False)

    @staticmethod
    def _tool(text: str, tip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setAutoRaise(True)
        button.clicked.connect(slot)
        return button

    # -- contents -----------------------------------------------------------------
    def set_pictures(self, items: Sequence[Dict], enabled: bool, keep: str = "") -> None:
        """Show ``items`` (``attachment_id``, ``path``, ``name``, ``on_map``),
        selecting ``keep`` (an attachment id) when it is among them."""
        self._thumb_timer.stop()
        self._thumb_queue = []
        self.items = list(items)
        self._syncing = True
        try:
            self.strip.clear()
            row_to_show = 0
            for row, data in enumerate(self.items):
                item = QListWidgetItem()
                item.setData(ID_ROLE, data.get("attachment_id"))
                item.setData(PATH_ROLE, data.get("path"))
                missing = not os.path.isfile(data.get("path") or "")
                item.setToolTip(str(data.get("name") or "") + (" (shown on the map)" if data.get("on_map") else "")
                                + (" (file missing)" if missing else ""))
                icon = self.cache.get(data.get("path") or "", THUMB_PX * 2)
                if icon is not None:
                    item.setIcon(icon)
                elif not missing:
                    self._thumb_queue.append(item)
                self.strip.addItem(item)
                if keep and data.get("attachment_id") == keep:
                    row_to_show = row
            if self.items:
                self.strip.setCurrentRow(row_to_show)
        finally:
            self._syncing = False
        self.strip.setVisible(len(self.items) > 1)
        for widget in (self.add_button, self.paste_button):
            widget.setEnabled(enabled)
        self.setAcceptDrops(enabled)
        if self._thumb_queue:
            self._thumb_timer.start()
        if not self.items:
            self._load_timer.stop()
            self.view.set_image(QImage(), "No images. Drop image files here, paste one (Ctrl+V) or Add…"
                                if enabled else "")
        self._update_controls(enabled)
        if self.items:
            self._load_timer.start()

    def current(self) -> Optional[Dict]:
        row = self.strip.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def current_id(self) -> str:
        item = self.current()
        return str(item.get("attachment_id") or "") if item else ""

    def step(self, delta: int) -> None:
        row = self.strip.currentRow() + delta
        if 0 <= row < self.strip.count():
            self.strip.setCurrentRow(row)

    def _show_row(self, _row: int) -> None:
        if self._syncing:
            return
        self._update_controls(self.add_button.isEnabled())
        self._load_timer.start()

    def _update_controls(self, enabled: bool) -> None:
        item = self.current()
        row, count = self.strip.currentRow(), len(self.items)
        self.title.setText(f"{row + 1} / {count} · {item.get('name') or ''}" if item and count > 1 else
                           (str(item.get("name") or "") if item else "Images"))
        self.title.setToolTip(str(item.get("path") or "") if item else "")
        self.prev_button.setVisible(count > 1)
        self.next_button.setVisible(count > 1)
        self.prev_button.setEnabled(row > 0)
        self.next_button.setEnabled(0 <= row < count - 1)
        self.remove_button.setVisible(count > 0)   # a narrow pane with no image keeps Add and Paste
        self.on_map.setVisible(count > 0)
        self.remove_button.setEnabled(enabled and item is not None)
        self.on_map.blockSignals(True)
        self.on_map.setChecked(bool(item and item.get("on_map")))
        self.on_map.blockSignals(False)
        self.on_map.setEnabled(enabled and item is not None)

    def _load_current(self) -> None:
        item = self.current()
        if item is None:
            return
        image, problem = read_image(item.get("path") or "", VIEW_MAX_SIDE)
        self.view.set_image(image, problem)

    def _next_thumbnail(self) -> None:
        """One thumbnail per turn of the event loop, so selection stays quick."""
        while self._thumb_queue:
            item = self._thumb_queue.pop(0)
            try:
                path = item.data(PATH_ROLE) or ""
            except RuntimeError:   # the strip was cleared
                continue
            icon = self.cache.make(path, THUMB_PX * 2)
            item.setIcon(icon if not icon.isNull() else QIcon())
            return
        self._thumb_timer.stop()

    # -- actions ------------------------------------------------------------------
    def _open_current(self) -> None:
        item = self.current()
        if item:
            self.open_requested.emit(item.get("path") or "")

    def _remove_current(self) -> None:
        if self.current_id():
            self.remove_requested.emit(self.current_id())

    def _map_toggled(self, on: bool) -> None:
        if self.current_id():
            self.map_image_changed.emit(self.current_id(), bool(on))

    # -- drop -----------------------------------------------------------------------
    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        paths, image = pictures_from_mime(event.mimeData())
        if paths or image is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802 - Qt API
        self.dragEnterEvent(event)

    def dropEvent(self, event):  # noqa: N802 - Qt API
        paths, image = pictures_from_mime(event.mimeData())
        if paths:
            self.files_dropped.emit(paths)
        elif image is not None:
            self.image_dropped.emit(image)
        event.acceptProposedAction()

    def flush(self) -> None:
        """Read the waiting thumbnails and the current image now (tests, screenshots)."""
        while self._thumb_queue:
            self._next_thumbnail()
        self._thumb_timer.stop()
        if self._load_timer.isActive():
            self._load_timer.stop()
            self._load_current()

    def shutdown(self) -> None:
        self._thumb_timer.stop()
        self._load_timer.stop()
        self._thumb_queue = []
        self.strip.clear()
        self.items = []
        self.view.clear()
