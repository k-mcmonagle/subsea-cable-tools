# -*- coding: utf-8 -*-
"""Route Comments widgets: the comment table (model, filter, inline editors),
the reply thread and the splitter that lays the thread beside the images."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QAbstractTableModel, QDate, QModelIndex, QSortFilterProxyModel, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QKeySequence, QPainter, QPalette, QPixmap
from qgis.PyQt.QtWidgets import (QApplication, QComboBox, QDateEdit, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
                                 QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                                 QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget)

try:
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt5: QShortcut is in QtWidgets
    from qgis.PyQt.QtWidgets import QShortcut

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
    ("kind", "Type", False), ("route", "Route", False), ("images", "Images", True),
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

    def is_editable(self, key: str, comment: Optional[Dict] = None) -> bool:
        if key == "kp":
            return comment is None or comment.get("ref_kind") in (model.REF_ROUTE_POINT, model.REF_KP_RANGE)
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
        if key == "kp":
            return model.kp_text(comment)
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
                help_text = ("Nearest KP is calculated from the map position. Double-click to edit coordinates, "
                             "or use Edit position…." if comment.get("ref_kind") == model.REF_LOCATION else
                             "Double-click or press F2 to edit KP in km (0.001 km = 1 m). "
                             "For a range, enter both ends, e.g. 12.345 – 13.456. You will confirm the move.")
                return model.location_text(comment) + "\n" + help_text
        if role == Qt.ItemDataRole.ForegroundRole and str(comment.get("status") or "").casefold() in self.closed:
            return QBrush(QColor(130, 130, 130))
        if role == Qt.ItemDataRole.TextAlignmentRole and key in ("ref", "kp", "replies", "images"):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.row() < len(self.comments) and \
                self.is_editable(self.key(index.column()), self.comments[index.row()]):
            flags |= ITEM_FLAG_EDITABLE
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802 - Qt API
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or self.edit_handler is None:
            return False
        comment = self.comments[index.row()]
        key = self.key(index.column())
        if not self.is_editable(key, comment) or value == self.edit_value(comment, key):
            return False
        cid = comment.get("comment_id")
        saved = self.edit_handler(cid, key, value)
        if saved is None:
            return False
        # Saving pending Details/thread edits or a confirmation can run an event
        # loop and reload/reorder the model. Find the comment again by its ID.
        row = self.row_of(cid)
        if row >= 0:
            self.comments[row] = saved
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.columns) - 1))
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

    kp_edit_started = pyqtSignal(str, object)
    kp_edit_finished = pyqtSignal(object)

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
        edit = QLineEdit(parent)
        if key == "kp":
            edit.setPlaceholderText("12.345 or 12.345 – 13.456")
            edit.setToolTip("KP in km, to 3 decimal places (1 m). For a range, enter both ends separated by – or to. "
                            "The map previews valid KPs as you type. Enter confirms the move; Esc cancels.")
            self.kp_edit_started.emit(str(index.data(ID_ROLE)), edit)
        return edit

    def destroyEditor(self, editor, index):  # noqa: N802 - Qt API
        self.kp_edit_finished.emit(editor)
        super().destroyEditor(editor, index)

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
        if self._key(index) == "kp":
            rect.setWidth(max(rect.width(), 220))
        if isinstance(editor, QPlainTextEdit):
            rect.setHeight(max(rect.height(), editor.fontMetrics().lineSpacing() * 5 + 10))
            rect.setWidth(max(rect.width(), 280))
        editor.setGeometry(rect)


# --------------------------------------------------------------------------- thread
class _Bubble(QFrame):
    """One message: the comment itself (``reply_id`` None) or a reply. Its
    ``⋯`` button, or a right-click, offers copy, edit and delete; editing
    puts a text box in place of the text (Ctrl+Enter saves, Esc cancels)."""

    def __init__(self, reply_id: Optional[str], author: str, when: str, text: str, mine: bool, original: bool,
                 status_to: str = "", edited: str = "", parent=None):
        super().__init__(parent)
        self.reply_id = reply_id
        self.original = original
        self.text = text
        self.status_to = status_to
        self.editor: Optional[QPlainTextEdit] = None
        self.setObjectName("rcBubble")
        tint = "rgba(0, 120, 215, 0.13)" if mine else "rgba(128, 128, 128, 0.13)"
        edge = "border-left: 3px solid rgb(230, 120, 0);" if original else ""
        self.setStyleSheet(f"#rcBubble {{ background: {tint}; border-radius: 8px; {edge} }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 3, 3, 6)
        layout.setSpacing(1)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(2)
        head = QLabel(f"<b>{_esc(author or '—')}</b>&nbsp;&nbsp;<span style='color:gray'>{_esc(when)}"
                      + (" · comment" if original else "") + (" · edited" if edited else "") + "</span>")
        head.setTextFormat(Qt.TextFormat.RichText)
        if edited:
            head.setToolTip(f"Edited {str(edited)[:16].replace('T', ' ')} UTC")
        self.more = QToolButton()
        self.more.setText("⋯")
        self.more.setAutoRaise(True)
        self.more.setToolTip("Copy, edit or delete " + ("the comment" if original else "this reply"))
        top.addWidget(head, 1)
        top.addWidget(self.more, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        self.body = QLabel(text)
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.body)
        if not text:
            # Only once it is in the bubble: showing a widget with no parent opens it as a window.
            self.body.hide()
        if status_to:
            change = QLabel(f"<span style='color:gray'>Status → </span><b>{_esc(status_to)}</b>")
            change.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(change)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    def start_edit(self) -> QPlainTextEdit:
        self.editor = QPlainTextEdit(self.text)
        self.editor.setTabChangesFocus(True)
        lines = self.editor.fontMetrics().lineSpacing()
        rows = min(8, max(3, len(self.text.splitlines()) + 1 + len(self.text) // 70))   # the text, plus a line
        self.editor.setFixedHeight(lines * rows + 12)
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip("Save the edit (Ctrl+Enter)")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Leave it as it was (Esc)")
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout = self.layout()
        layout.insertWidget(2, self.editor)
        layout.insertLayout(3, buttons)
        self.body.hide()
        self.more.setEnabled(False)
        cursor = self.editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End if hasattr(cursor, "MoveOperation") else cursor.End)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()
        return self.editor

    def edited_text(self) -> str:
        return self.editor.toPlainText().strip() if self.editor is not None else self.text

    def changed(self) -> bool:
        return self.editor is not None and self.edited_text() != str(self.text or "").strip()


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class ThreadView(QScrollArea):
    """The comment and its replies as a conversation, oldest first. The user's
    own messages (by author name) sit on the right, others on the left. Any
    message can be copied, edited in place or deleted; the dock saves."""

    delete_requested = pyqtSignal(str)          # a reply
    reply_edited = pyqtSignal(str, str)         # reply id, new text
    comment_edited = pyqtSignal(str)            # the comment's new text
    comment_delete_requested = pyqtSignal()

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
        self._data = (None, [], "")
        self._editing: Optional[_Bubble] = None
        self._to_end = False
        self._scroll_to: Optional[int] = None
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)

    def clear(self) -> None:
        for bubble in self.bubbles:
            row = bubble.parentWidget()
            self._layout.removeWidget(row)
            row.hide()          # deleted on the next turn of the event loop; not drawn meanwhile
            row.deleteLater()
        self.bubbles = []
        self._editing = None

    def set_thread(self, comment: Optional[Dict], replies: Sequence[Dict], me: str, keep_scroll: bool = False) -> None:
        position = self.verticalScrollBar().value()
        self.clear()
        self._data = (comment, list(replies), me)
        if comment is None:
            return
        me = (me or "").casefold()
        when = lambda iso: str(iso or "")[:16].replace("T", " ")  # noqa: E731
        items = [(None, comment.get("author") or "", comment.get("comment_date") or when(comment.get("created_utc")),
                  comment.get("text") or "", True, "", "")]
        items += [(r.get("reply_id"), r.get("author") or "", when(r.get("created_utc")), r.get("text") or "", False,
                   r.get("status_to") or "", r.get("edited_utc") or "") for r in replies]
        for reply_id, author, stamp, text, original, status_to, edited in items:
            mine = bool(me) and author.casefold() == me and not original
            bubble = _Bubble(reply_id, author, stamp, text, mine, original, status_to, edited)
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            if mine:
                line.addStretch(1)
            line.addWidget(bubble, 6)
            if not mine:
                line.addStretch(1)
            bubble.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
            bubble.customContextMenuRequested.connect(lambda pos, b=bubble: self._menu(b, b.mapToGlobal(pos)))
            bubble.more.clicked.connect(
                lambda _checked=False, b=bubble: self._menu(b, b.more.mapToGlobal(b.more.rect().bottomLeft())))
            self._layout.insertWidget(self._layout.count() - 1, row)
            self.bubbles.append(bubble)
        if keep_scroll:
            self._to_end, self._scroll_to = False, position
        else:
            self._to_end, self._scroll_to = True, None   # the newest message in view once laid out
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _range_changed(self, _lo, hi) -> None:
        if self._to_end:
            self.verticalScrollBar().setValue(hi)
            self._to_end = False
        elif self._scroll_to is not None:
            self.verticalScrollBar().setValue(min(self._scroll_to, hi))
            self._scroll_to = None

    def _menu(self, bubble: _Bubble, global_pos) -> None:
        menu = QMenu(self)
        menu.addAction("Copy text", lambda: QApplication.clipboard().setText(bubble.text))
        if self._editing is None:
            what = "comment" if bubble.original else "reply"
            menu.addAction(f"Edit {what}", lambda: self.edit(bubble))
            if bubble.original:
                menu.addAction("Delete comment…", self.comment_delete_requested.emit)
            else:
                menu.addAction("Delete reply…", lambda: self.delete_requested.emit(bubble.reply_id))
        qt_exec(menu, global_pos)
        menu.deleteLater()

    # -- editing in place --------------------------------------------------------------
    def edit(self, bubble: _Bubble) -> None:
        if self._editing is not None or bubble not in self.bubbles:
            return
        editor = bubble.start_edit()
        self._editing = bubble
        bubble.save_button.clicked.connect(self.finish_edit)
        bubble.cancel_button.clicked.connect(self.cancel_edit)
        for keys, slot in (("Ctrl+Return", self.finish_edit), ("Ctrl+Enter", self.finish_edit),
                           ("Escape", self.cancel_edit)):
            QShortcut(QKeySequence(keys), editor, activated=slot, context=Qt.ShortcutContext.WidgetShortcut)
        self.ensureWidgetVisible(bubble)

    def edit_reply(self, reply_id: Optional[str]) -> None:
        """Start editing the comment (None) or a reply."""
        bubble = next((b for b in self.bubbles if b.reply_id == reply_id), None)
        if bubble is not None:
            self.edit(bubble)

    def is_editing(self) -> bool:
        return self._editing is not None

    def pending_edit(self) -> Optional[Tuple[Optional[str], str]]:
        """``(reply id, or None for the comment, new text)`` when an open edit changed the text."""
        bubble = self._editing
        if bubble is None or not bubble.changed():
            return None
        return bubble.reply_id, bubble.edited_text()

    def finish_edit(self) -> None:
        """Ask the dock to save; the edit stays open until it reloads (so nothing is lost if it cannot)."""
        bubble = self._editing
        if bubble is None:
            return
        if not bubble.changed():
            self.cancel_edit()
        elif bubble.reply_id is None:
            self.comment_edited.emit(bubble.edited_text())
        else:
            self.reply_edited.emit(bubble.reply_id, bubble.edited_text())

    def cancel_edit(self) -> None:
        if self._editing is None:
            return
        comment, replies, me = self._data
        self.set_thread(comment, replies, me, keep_scroll=True)


# --------------------------------------------------------------------------- layout
QWIDGETSIZE_MAX = 16777215


class AdaptiveSplitter(QSplitter):
    """Two panes side by side when there is room, one above the other when
    narrow (with a margin so it does not flap). The user's split is kept for
    each way round; the second pane can be made compact (nothing to show)
    and gets its room back when it has content again."""

    def __init__(self, wide_px: int = 600, narrow_px: int = 540, compact_px: Sequence[int] = (230, 150),
                 parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.wide_px, self.narrow_px = wide_px, narrow_px
        self.compact_px = tuple(compact_px)      # (width side by side, height stacked)
        self.preferred: Dict[str, List[int]] = {}
        self.compact = False
        self.setChildrenCollapsible(False)
        self.splitterMoved.connect(self._moved)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.adapt)

    def side_by_side(self) -> bool:
        return self.orientation() == Qt.Orientation.Horizontal

    def _key(self) -> str:
        return "h" if self.side_by_side() else "v"

    def _moved(self, *_args) -> None:
        if not self.compact:
            self.preferred[self._key()] = self.sizes()

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._timer.start()

    def adapt(self) -> None:
        """Turn to suit the width (after a resize)."""
        width = self.width()
        if self.side_by_side() and width < self.narrow_px:
            self.setOrientation(Qt.Orientation.Vertical)
        elif not self.side_by_side() and width >= self.wide_px:
            self.setOrientation(Qt.Orientation.Horizontal)
        else:
            return
        self._apply()

    def set_compact(self, on: bool) -> None:
        on = bool(on)
        if on and not self.compact and all(size > 0 for size in self.sizes()):
            self.preferred[self._key()] = self.sizes()
        changed = on != self.compact
        self.compact = on
        if changed:
            self._apply()

    def _apply(self) -> None:
        second = self.widget(1)
        if second is None:
            return
        across = self.side_by_side()
        if self.compact:
            second.setMaximumWidth(self.compact_px[0] if across else QWIDGETSIZE_MAX)
            second.setMaximumHeight(QWIDGETSIZE_MAX if across else self.compact_px[1])
        else:
            second.setMaximumSize(QWIDGETSIZE_MAX, QWIDGETSIZE_MAX)
        total = sum(self.sizes()) or (self.width() if across else self.height())
        if total <= 0:
            return
        if self.compact:
            cap = min(self.compact_px[0 if across else 1], total // 2)
            self.setSizes([total - cap, cap])
            return
        sizes = self.preferred.get(self._key())
        if sizes and len(sizes) == 2 and sum(sizes) > 0:
            first = int(round(total * sizes[0] / float(sum(sizes))))
        else:
            first = int(total * (0.55 if across else 0.6))
        self.setSizes([first, total - first])

    def state(self) -> Dict[str, List[int]]:
        return {key: [int(v) for v in sizes] for key, sizes in self.preferred.items()}

    def restore(self, state) -> None:
        if isinstance(state, dict):
            for key in ("h", "v"):
                sizes = state.get(key)
                if isinstance(sizes, (list, tuple)) and len(sizes) == 2:
                    try:
                        self.preferred[key] = [max(0, int(v)) for v in sizes]
                    except (TypeError, ValueError):
                        pass
        self._apply()
