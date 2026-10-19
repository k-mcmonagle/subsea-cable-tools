# -*- coding: utf-8 -*-
"""Route Comments widgets: the comment table (model, filter, inline editors)
and the reply thread."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from qgis.PyQt.QtCore import QAbstractTableModel, QDate, QModelIndex, QSortFilterProxyModel, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QPainter, QPalette, QPixmap
from qgis.PyQt.QtWidgets import (QApplication, QComboBox, QDateEdit, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                                 QPlainTextEdit, QScrollArea, QSizePolicy, QStyledItemDelegate, QVBoxLayout, QWidget)

from ..qgis_compat import CONTEXT_MENU_POLICY_CUSTOM, ITEM_FLAG_EDITABLE, qt_exec
from . import model

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ID_ROLE = Qt.ItemDataRole.UserRole
COND_COLORS = {model.COND_OK: "#4f9a55", model.COND_SUPERSEDED: "#d49a00", model.COND_CHANGED: "#c62828",
               model.COND_UNAVAILABLE: "#7f1d1d", model.COND_NONE: "#9e9e9e"}
BASE_COLUMNS = [  # key, label, shown by default
    ("ref", "No.", True), ("kp", "KP", True), ("text", "Comment", True), ("status", "Status", True),
    ("priority", "Priority", True), ("condition", "Reference", True), ("replies", "Replies", True),
    ("author", "Author", True), ("date", "Date", True),
    ("kind", "Type", False), ("route", "Route", False), ("images", "Images", False),
    ("tags", "Tags", False), ("ext_id", "External ID", False), ("updated", "Updated", False),
]
# Table column -> register column, for the columns edited in place.
EDITABLE = {"text": "text", "status": "status", "priority": "priority", "author": "author",
            "date": "comment_date", "tags": "tags", "ext_id": "ext_id"}


def dot_icon(color: str) -> QIcon:
    pixmap = QPixmap(12, 12)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(QColor(color).darker(130))
    painter.drawEllipse(1, 1, 10, 10)
    painter.end()
    return QIcon(pixmap)


def readable_selection(view) -> None:
    """White text on the highlight colour, also when the view has no focus
    (some styles draw dark text on a strong blue, or a pale inactive selection)."""
    palette = view.palette()
    highlight = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(group, QPalette.ColorRole.Highlight, highlight)
        palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    view.setPalette(palette)


# --------------------------------------------------------------------------- table
class CommentTableModel(QAbstractTableModel):
    """Comments of one set. ``edit_handler(comment_id, column key, value)``
    saves an in-place edit and returns the saved comment (or None)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.comments: List[Dict] = []
        self.fields: List[model.CustomField] = []
        self.conditions: Dict[str, tuple] = {}
        self.closed: set = set()
        self.statuses: List[str] = []
        self.priorities: List[str] = []
        self.columns = list(BASE_COLUMNS)
        self.edit_handler: Optional[Callable] = None
        self._icons = {code: dot_icon(color) for code, color in COND_COLORS.items()}

    def reset(self, comments, fields, conditions, closed, statuses=(), priorities=()) -> None:
        self.beginResetModel()
        self.comments = list(comments)
        self.fields = list(fields)
        self.conditions = dict(conditions)
        self.closed = set(closed)
        self.statuses, self.priorities = list(statuses), list(priorities)
        self.columns = list(BASE_COLUMNS) + [(f"cf:{f.field_id}", f.name, False) for f in self.fields]
        self.endResetModel()

    def column_keys(self) -> List[str]:
        return [key for key, _label, _shown in self.columns]

    def key(self, column: int) -> str:
        return self.columns[column][0] if 0 <= column < len(self.columns) else ""

    def field_for(self, key: str) -> Optional[model.CustomField]:
        return next((f for f in self.fields if f"cf:{f.field_id}" == key), None)

    def is_editable(self, key: str) -> bool:
        return key in EDITABLE or key.startswith("cf:")

    def rowCount(self, parent=QModelIndex()):  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.comments)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802 - Qt API
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole \
                and 0 <= section < len(self.columns):
            return self.columns[section][1]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole \
                and self.is_editable(self.key(section)):
            return "Double-click a cell to edit it"
        return None

    def condition(self, comment: Dict) -> str:
        return (self.conditions.get(comment.get("comment_id")) or (model.COND_NONE, []))[0]

    def _value(self, comment: Dict, key: str):
        """``(display text, sort key)``."""
        if key == "ref":
            return model.ref_label(comment.get("ref_no")), int(comment.get("ref_no") or 0)
        if key == "kp":
            kp = comment.get("kp_km")
            return model.kp_text(comment), (float(kp) if kp is not None else float("inf"))
        if key == "text":
            return model.summary(comment.get("text"), 120), str(comment.get("text") or "").casefold()
        if key == "date":
            return comment.get("comment_date") or "", comment.get("comment_date") or ""
        if key == "condition":
            code = self.condition(comment)
            return model.COND_LABELS.get(code, ""), code
        if key == "kind":
            return model.REF_LABELS.get(comment.get("ref_kind"), ""), comment.get("ref_kind") or ""
        if key == "route":
            text = model.route_text(comment)
            return text, text.casefold()
        if key in ("replies", "images"):
            n = int(comment.get("reply_count" if key == "replies" else "attachment_count") or 0)
            return (str(n) if n else ""), n
        if key == "updated":
            text = str(comment.get("updated_utc") or "")[:16].replace("T", " ")
            return text, text
        if key.startswith("cf:"):
            fld = self.field_for(key)
            value = model.load_custom(comment).get(key[3:])
            text = model.custom_text(fld, value) if fld else ""
            if fld is not None and fld.kind == model.FIELD_NUMBER and value is not None:
                return text, float(value)
            return text, text.casefold()
        text = str(comment.get(key) or "")
        return text, text.casefold()

    def edit_value(self, comment: Dict, key: str):
        """The value an in-place editor starts from."""
        if key.startswith("cf:"):
            return model.load_custom(comment).get(key[3:])
        return comment.get(EDITABLE.get(key, key))

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.comments):
            return None
        comment = self.comments[index.row()]
        key = self.key(index.column())
        if role == Qt.ItemDataRole.DisplayRole:
            return self._value(comment, key)[0]
        if role == Qt.ItemDataRole.EditRole:
            return self.edit_value(comment, key)
        if role == SORT_ROLE:
            return self._value(comment, key)[1]
        if role == ID_ROLE:
            return comment.get("comment_id")
        if role == Qt.ItemDataRole.DecorationRole and key == "condition":
            return self._icons.get(self.condition(comment))
        if role == Qt.ItemDataRole.ToolTipRole:
            if key == "condition":
                return "\n".join((self.conditions.get(comment.get("comment_id")) or ("", []))[1])
            if key == "text":
                return str(comment.get("text") or "")
            if key == "kp":
                return model.location_text(comment)
        if role == Qt.ItemDataRole.ForegroundRole and str(comment.get("status") or "").casefold() in self.closed:
            return QBrush(QColor(130, 130, 130))
        if role == Qt.ItemDataRole.TextAlignmentRole and key in ("ref", "kp", "replies", "images"):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and self.is_editable(self.key(index.column())):
            flags |= ITEM_FLAG_EDITABLE
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802 - Qt API
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or self.edit_handler is None:
            return False
        comment = self.comments[index.row()]
        key = self.key(index.column())
        if value == self.edit_value(comment, key):
            return False
        saved = self.edit_handler(comment.get("comment_id"), key, value)
        if saved is None:
            return False
        self.comments[index.row()] = saved
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), len(self.columns) - 1))
        return True

    def row_of(self, comment_id: str) -> int:
        for row, comment in enumerate(self.comments):
            if comment.get("comment_id") == comment_id:
                return row
        return -1


class CommentProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.flt = model.Filter()
        self.blobs: Dict[str, str] = {}
        self.setSortRole(SORT_ROLE)

    def set_filter(self, flt: model.Filter) -> None:
        self.flt = flt
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802 - Qt API
        source = self.sourceModel()
        comment = source.comments[row]
        return model.matches(comment, self.flt, self.blobs.get(comment.get("comment_id"), ""), source.closed,
                             source.condition(comment))


class CommentDelegate(QStyledItemDelegate):
    """In-place editors: choice lists for status, priority and choice fields,
    a date picker for dates, several lines for the comment text."""

    def __init__(self, table_model: CommentTableModel, parent=None):
        super().__init__(parent)
        self.table_model = table_model

    def _key(self, index) -> str:
        source = index.model().mapToSource(index) if hasattr(index.model(), "mapToSource") else index
        return self.table_model.key(source.column())

    def createEditor(self, parent, option, index):  # noqa: N802 - Qt API
        key = self._key(index)
        fld = self.table_model.field_for(key) if key.startswith("cf:") else None
        if key in ("status", "priority") or (fld is not None and fld.kind in (model.FIELD_CHOICE,
                                                                                 model.FIELD_BOOLEAN)):
            combo = QComboBox(parent)
            if key == "status":
                combo.addItems(self.table_model.statuses)
                combo.setEditable(True)
            elif key == "priority":
                combo.addItems(self.table_model.priorities)
                combo.setEditable(True)
            elif fld.kind == model.FIELD_BOOLEAN:
                combo.addItems(["", "Yes", "No"])
            else:
                combo.addItems([""] + list(fld.choices))
                combo.setEditable(True)
            return combo
        if key == "date" or (fld is not None and fld.kind == model.FIELD_DATE):
            edit = QDateEdit(parent)
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setMinimumDate(QDate(1900, 1, 1))
            edit.setSpecialValueText(" ")
            return edit
        if key == "text":
            edit = QPlainTextEdit(parent)
            edit.setTabChangesFocus(True)
            return edit
        return QLineEdit(parent)

    def setEditorData(self, editor, index):  # noqa: N802 - Qt API
        value = index.data(Qt.ItemDataRole.EditRole)
        key = self._key(index)
        if isinstance(editor, QComboBox):
            fld = self.table_model.field_for(key)
            if fld is not None and fld.kind == model.FIELD_BOOLEAN:
                editor.setCurrentIndex(0 if value is None else (1 if value else 2))
            else:
                editor.setCurrentText("" if value is None else str(value))
        elif isinstance(editor, QDateEdit):
            date = QDate.fromString(str(value or ""), "yyyy-MM-dd")
            editor.setDate(date if date.isValid() else editor.minimumDate())
        elif isinstance(editor, QPlainTextEdit):
            editor.setPlainText("" if value is None else str(value))
        else:
            fld = self.table_model.field_for(key)
            editor.setText(model.custom_text(fld, value) if fld is not None else ("" if value is None else str(value)))

    def setModelData(self, editor, item_model, index):  # noqa: N802 - Qt API
        key = self._key(index)
        fld = self.table_model.field_for(key)
        if isinstance(editor, QComboBox):
            if fld is not None and fld.kind == model.FIELD_BOOLEAN:
                value = {1: True, 2: False}.get(editor.currentIndex())
            else:
                value = editor.currentText().strip()
        elif isinstance(editor, QDateEdit):
            value = "" if editor.date() == editor.minimumDate() else editor.date().toString("yyyy-MM-dd")
        elif isinstance(editor, QPlainTextEdit):
            value = editor.toPlainText().strip()
        else:
            value = editor.text().strip()
        item_model.setData(index, value, Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802 - Qt API
        rect = option.rect
        if isinstance(editor, QPlainTextEdit):
            rect.setHeight(max(rect.height(), editor.fontMetrics().lineSpacing() * 5 + 10))
            rect.setWidth(max(rect.width(), 280))
        editor.setGeometry(rect)


# --------------------------------------------------------------------------- thread
class _Bubble(QFrame):
    def __init__(self, author: str, when: str, text: str, mine: bool, original: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("rcBubble")
        tint = "rgba(0, 120, 215, 0.13)" if mine else "rgba(128, 128, 128, 0.13)"
        edge = "border-left: 3px solid rgb(230, 120, 0);" if original else ""
        self.setStyleSheet(f"#rcBubble {{ background: {tint}; border-radius: 8px; {edge} }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 5, 9, 6)
        layout.setSpacing(1)
        head = QLabel(f"<b>{_esc(author or '—')}</b>&nbsp;&nbsp;<span style='color:gray'>{_esc(when)}"
                      + (" · comment" if original else "") + "</span>")
        head.setTextFormat(Qt.TextFormat.RichText)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(head)
        layout.addWidget(body)
        self.text = text
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class ThreadView(QScrollArea):
    """The comment and its replies as a conversation, oldest first. The user's
    own messages (by author name) sit on the right, others on the left."""

    delete_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(2, 2, 2, 2)
        self._layout.setSpacing(5)
        self._layout.addStretch(1)
        self.setWidget(self._body)
        self.bubbles: List[_Bubble] = []

    def clear(self) -> None:
        for bubble in self.bubbles:
            row = bubble.parentWidget()
            self._layout.removeWidget(row)
            row.deleteLater()
        self.bubbles = []

    def set_thread(self, comment: Optional[Dict], replies: Sequence[Dict], me: str) -> None:
        self.clear()
        if comment is None:
            return
        me = (me or "").casefold()
        when = lambda iso: str(iso or "")[:16].replace("T", " ")  # noqa: E731
        items = [(None, comment.get("author") or "", comment.get("comment_date") or when(comment.get("created_utc")),
                  comment.get("text") or "", True)]
        items += [(r.get("reply_id"), r.get("author") or "", when(r.get("created_utc")), r.get("text") or "", False)
                  for r in replies]
        for reply_id, author, stamp, text, original in items:
            mine = bool(me) and author.casefold() == me and not original
            bubble = _Bubble(author, stamp, text, mine, original)
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            if mine:
                line.addStretch(1)
            line.addWidget(bubble, 6)
            if not mine:
                line.addStretch(1)
            bubble.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
            bubble.customContextMenuRequested.connect(
                lambda pos, b=bubble, rid=reply_id: self._menu(b, rid, pos))
            self._layout.insertWidget(self._layout.count() - 1, row)
            self.bubbles.append(bubble)
        self.verticalScrollBar().rangeChanged.connect(self._scroll_to_end)

    def _scroll_to_end(self, _lo, hi) -> None:
        self.verticalScrollBar().setValue(hi)
        try:
            self.verticalScrollBar().rangeChanged.disconnect(self._scroll_to_end)
        except (TypeError, RuntimeError):
            pass

    def _menu(self, bubble, reply_id, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Copy text", lambda: QApplication.clipboard().setText(bubble.text))
        if reply_id:
            menu.addAction("Delete reply…", lambda: self.delete_requested.emit(reply_id))
        qt_exec(menu, bubble.mapToGlobal(pos))
