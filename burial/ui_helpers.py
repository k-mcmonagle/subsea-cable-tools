# -*- coding: utf-8 -*-
"""Shared UI helpers for the Burial Planner dock and tabs.

- Theme-aware semantic colours (light/dark QGIS themes) so status text,
  badges and risk colouring stay legible on dark UI themes.
- ``MoveEventDialog``: confirm/adjust an event move with an editable KP and
  an optional reason (used by profile drags and table KP edits).
- ``ask_reason``: the optional-reason prompt with a per-session
  "don't ask again" suppression.
- ``preserve_table_view``: keep a QTableWidget's selection and scroll
  position across a full rebuild.
- ``menu_tool_button``: collapse several related actions into one compact
  drop-down button (narrow-dock layout).
- ``enable_column_menu``: header right-click column visibility, persisted
  by label with optional default-hidden columns.
- ``ComboColumnDelegate``: drop-down cells painted from item data (one
  editor on demand instead of a ``QComboBox`` widget per row).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QObject, Qt
from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
)

from ..qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    ITEM_DATA_USER_ROLE,
    TOOLBUTTON_POPUP_MODE_INSTANT,
)

try:
    from qgis.PyQt import sip as _sip
except ImportError:  # pragma: no cover - very old bindings
    try:
        import sip as _sip
    except ImportError:
        _sip = None


def _sip_isdeleted(obj) -> bool:
    if _sip is None:
        return False
    try:
        return bool(_sip.isdeleted(obj))
    except (TypeError, RuntimeError):
        return False

# (light, dark) hex pairs for each semantic colour.
_COLORS = {
    "hint": ("#666666", "#9e9e9e"),
    "ok": ("#1b5e20", "#7cc47f"),
    "warn": ("#b36b00", "#e6a23c"),
    "error": ("#b71c1c", "#ef6b62"),
    "info": ("#0d47a1", "#64b5f6"),
    "risk_high": ("#d62728", "#ff5252"),
    "risk_medium": ("#ff8c00", "#ffa733"),
    "risk_low": ("#e0b000", "#e6c84a"),
    "risk_unassigned": ("#909090", "#9e9e9e"),
    "event_candidate": ("#b36b00", "#e6a23c"),
    "event_confirmed": ("#1b5e20", "#7cc47f"),
    "event_conflict": ("#b71c1c", "#ef6b62"),
}

# Status-badge (background, foreground) pairs per theme.
_BADGES = {
    "draft": (("#e8f5e9", "#1b5e20"), ("#1b3a24", "#81c784")),
    "stale": (("#fff3cd", "#7a4f00"), ("#4a3a10", "#ffd54f")),
    "issued": (("#e3f2fd", "#0d47a1"), ("#103049", "#64b5f6")),
}


def is_dark_theme() -> bool:
    """True when the application palette is dark (QGIS Night Mapping…)."""
    try:
        window = QApplication.palette().color(QPalette.ColorRole.Window)
    except AttributeError:  # Qt5 enum spelling
        window = QApplication.palette().color(QPalette.Window)
    return window.lightness() < 128


def color(name: str) -> str:
    """Semantic colour as a hex string for the active theme."""
    light, dark = _COLORS.get(name, ("#666666", "#9e9e9e"))
    return dark if is_dark_theme() else light


def qcolor(name: str) -> QColor:
    return QColor(color(name))


def hint_style() -> str:
    return f"color: {color('hint')};"


def status_style(kind: str = "") -> str:
    """Bold coloured stylesheet for a status label ('' clears emphasis)."""
    if not kind:
        return ""
    return f"color: {color(kind)}; font-weight: 600;"


def badge_style(status: str) -> str:
    entry = _BADGES.get(status)
    if entry is None:
        return ""
    background, foreground = entry[1] if is_dark_theme() else entry[0]
    return (f"background:{background};color:{foreground};"
            "padding:2px 8px;border-radius:3px;")


class MoveEventDialog(QDialog):
    """Confirm an event move: editable target KP + optional reason.

    Used for profile-line drags and table KP edits so the user can fine-tune
    the exact KP (the dragged value is only a starting point) and record why
    the boundary moved. Cancel aborts the move entirely.
    """

    def __init__(self, event_label: str, current_kp: float, new_kp: float,
                 lo: Optional[float] = None, hi: Optional[float] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Move event")
        layout = QVBoxLayout(self)
        note = QLabel(
            f"Move {event_label} from KP {current_kp:.3f}. Adjust the exact "
            "KP below; the move is validated against neighbouring events "
            "and the plan scope.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.kp_spin = QDoubleSpinBox()
        self.kp_spin.setDecimals(3)
        if lo is not None and hi is not None and hi > lo:
            self.kp_spin.setRange(min(lo, hi), max(lo, hi))
        else:
            self.kp_spin.setRange(0.0, 100000.0)
        self.kp_spin.setSuffix(" km")
        self.kp_spin.setValue(float(new_kp))
        form.addRow("New KP:", self.kp_spin)
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText(
            "optional — recorded in the change log")
        form.addRow("Reason:", self.reason_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kp_spin.setFocus()
        self.kp_spin.selectAll()

    def values(self) -> Tuple[float, str]:
        return self.kp_spin.value(), self.reason_edit.text().strip()


# One per QGIS session: the user can silence the optional-reason prompt for
# non-move edits (add/split/merge). Move dialogs always appear — they carry
# the editable KP, which is the confirmation itself.
_session_skip_reason = False


def ask_reason(parent, title: str,
               prompt: str = "Reason (optional):") -> Optional[str]:
    """Optional-reason prompt. Returns None when cancelled, else the text.

    A "don't ask again this session" checkbox suppresses future prompts
    (returning "" immediately) until QGIS restarts.
    """
    global _session_skip_reason
    if _session_skip_reason:
        return ""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    form = QFormLayout()
    reason_edit = QLineEdit()
    reason_edit.setPlaceholderText("optional — recorded in the change log")
    form.addRow(prompt, reason_edit)
    layout.addLayout(form)
    skip_check = QCheckBox("Don't ask for a reason again this session")
    layout.addWidget(skip_check)
    buttons = QDialogButtonBox()
    buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    reason_edit.setFocus()
    from ..qgis_compat import DIALOG_ACCEPTED, qt_exec

    if qt_exec(dialog) != DIALOG_ACCEPTED:
        return None
    if skip_check.isChecked():
        _session_skip_reason = True
    return reason_edit.text().strip()


@contextmanager
def preserve_table_view(table, id_column: int = 0):
    """Keep selection + scroll position across a QTableWidget rebuild.

    Rows are re-matched by the id stored in ``ITEM_DATA_USER_ROLE`` of
    ``id_column``. Selection is restored with the selection model's signals
    blocked so a refresh never re-triggers map highlighting.
    """
    selected_ids = set()
    current_id = None
    model = table.selectionModel()
    if model is not None:
        for index in model.selectedRows():
            item = table.item(index.row(), id_column)
            if item is not None:
                selected_ids.add(item.data(ITEM_DATA_USER_ROLE))
        current = table.currentRow()
        if current >= 0:
            item = table.item(current, id_column)
            if item is not None:
                current_id = item.data(ITEM_DATA_USER_ROLE)
    v_scroll = table.verticalScrollBar().value()
    h_scroll = table.horizontalScrollBar().value()

    yield

    model = table.selectionModel()
    if (selected_ids or current_id is not None) and model is not None:
        from qgis.PyQt.QtCore import QItemSelectionModel

        flags_enum = getattr(QItemSelectionModel, "SelectionFlag",
                             QItemSelectionModel)
        select_flags = flags_enum.Select | flags_enum.Rows
        current_flags = flags_enum.NoUpdate
        blocked = model.blockSignals(True)
        try:
            for row in range(table.rowCount()):
                item = table.item(row, id_column)
                if item is None:
                    continue
                row_id = item.data(ITEM_DATA_USER_ROLE)
                index = table.model().index(row, id_column)
                if row_id in selected_ids:
                    model.select(index, select_flags)
                if current_id is not None and row_id == current_id:
                    model.setCurrentIndex(index, current_flags)
        except Exception:
            pass
        finally:
            model.blockSignals(blocked)
    table.verticalScrollBar().setValue(v_scroll)
    table.horizontalScrollBar().setValue(h_scroll)


@contextmanager
def silent_rebuild(table):
    """Block per-item signals and repaints while a table is rebuilt.

    ``setItem`` emits ``itemChanged``/``cellChanged`` per cell and repaints
    per row; a full rebuild of a large table delivered thousands of slots
    that only checked a ``_loading`` flag. Selection-model signals are left
    to ``preserve_table_view``.
    """
    blocked = table.blockSignals(True)
    table.setUpdatesEnabled(False)
    try:
        yield
    finally:
        table.setUpdatesEnabled(True)
        table.blockSignals(blocked)
        try:
            table.viewport().update()
        except (AttributeError, RuntimeError):
            pass


def coalesced(parent, fn: Callable, interval_ms: int = 0) -> Callable:
    """A slot that runs ``fn`` once per event-loop burst.

    ``load_plan`` emits several model signals back-to-back and tabs
    connected to more than one of them rebuilt their tables two or three
    times per plan open. Connecting this wrapper instead collapses a burst
    into a single deferred refresh.
    """
    from qgis.PyQt.QtCore import QTimer

    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(int(interval_ms))
    timer.timeout.connect(fn)

    def trigger(*_args) -> None:
        if not timer.isActive():
            timer.start()

    trigger._timer = timer  # keep a reference alive with the slot
    return trigger


def _column_labels(table) -> List[str]:
    labels = []
    for column in range(table.columnCount()):
        item = table.horizontalHeaderItem(column)
        labels.append(item.text() if item is not None else f"Column {column + 1}")
    return labels


class _ColumnMenu(QObject):
    """Header right-click column chooser, owned by its table.

    A ``QObject`` child of the table with bound-method slots — never a
    nested closure. PyQt does not keep a Python closure connected to a
    signal alive on its own: once the cyclic garbage collector freed the
    old ``show_menu`` closure, the next header right-click dispatched into
    freed memory and took the whole QGIS process down with an access
    violation. The table (C++ parent) keeps this helper alive for exactly
    as long as the header can emit, and the menu is resolved from
    ``exec``'s return value so no per-action lambdas exist either.
    """

    def __init__(self, table, settings_key: str,
                 always_visible: Sequence[int],
                 default_hidden: Sequence[str]):
        super().__init__(table)
        self._table = table
        self._header = table.horizontalHeader()
        self._settings_key = settings_key
        self._always_visible = tuple(always_visible or ())
        self._default_hidden = set(default_hidden or ())
        self._labels = _column_labels(table)

    def _alive(self) -> bool:
        try:
            return (not _sip_isdeleted(self._table)
                    and not _sip_isdeleted(self._header))
        except (AttributeError, RuntimeError, TypeError):
            return False

    def _label(self, column: int) -> str:
        if column < len(self._labels) and self._labels[column]:
            return self._labels[column]
        return f"Column {column + 1}"

    def save(self) -> None:
        import json

        from qgis.PyQt.QtCore import QSettings

        if not self._alive():
            return
        table = self._table
        hidden = [self._labels[column] for column in range(table.columnCount())
                  if column < len(self._labels)
                  and table.isColumnHidden(column)]
        QSettings().setValue(self._settings_key, json.dumps(
            {"hidden": hidden, "known": list(self._labels)}))

    def show_all(self) -> None:
        if not self._alive():
            return
        for column in range(self._table.columnCount()):
            self._table.setColumnHidden(column, False)
        self.save()

    def reset(self) -> None:
        if not self._alive():
            return
        for column in range(self._table.columnCount()):
            self._table.setColumnHidden(
                column, column not in self._always_visible
                and self._label(column) in self._default_hidden)
        self.save()

    def show_menu(self, position) -> None:
        from ..qgis_compat import qt_exec

        if not self._alive():
            return
        table = self._table
        # No parent: the menu is a one-shot local. A table-parented menu
        # would outlive every right-click and pile up on the table.
        menu = QMenu()
        for column in range(table.columnCount()):
            action = menu.addAction(self._label(column))
            action.setCheckable(True)
            action.setChecked(not table.isColumnHidden(column))
            action.setData(column)
            if column in self._always_visible:
                action.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Show all columns").setData("all")
        menu.addAction("Reset to default columns").setData("reset")
        try:
            chosen = qt_exec(menu, self._header.mapToGlobal(position))
            data = chosen.data() if chosen is not None else None
        finally:
            menu.deleteLater()
        if data is None or not self._alive():
            return
        if data == "all":
            self.show_all()
        elif data == "reset":
            self.reset()
        else:
            try:
                column = int(data)
            except (TypeError, ValueError):
                return
            if 0 <= column < table.columnCount():
                table.setColumnHidden(column, not table.isColumnHidden(column))
                self.save()


def enable_column_menu(table, settings_key: str,
                       always_visible: Sequence[int] = (0,),
                       default_hidden: Sequence[str] = ()) -> None:
    """Right-click on a table header toggles column visibility (persisted).

    The narrow-dock escape hatch for wide tables: secondary columns can be
    hidden per machine without losing them for good.

    Visibility is persisted by *header label*, so inserting a column in a
    later version never re-targets a saved choice at the wrong column. A
    column the saved state has never seen follows ``default_hidden`` (labels
    of optional columns that start hidden — e.g. positions and reverse KP);
    legacy index lists are honoured on tables whose columns did not change.

    The handler lives in a ``_ColumnMenu`` object parented to the table
    (see its docstring for why a closure slot crashed QGIS).
    """
    from ..qgis_compat import CONTEXT_MENU_POLICY_CUSTOM

    header = table.horizontalHeader()
    header.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
    header.setToolTip("Right-click to choose which columns are shown.")
    previous = getattr(table, "_column_menu", None)
    if previous is not None:
        try:
            header.customContextMenuRequested.disconnect(previous.show_menu)
        except (TypeError, RuntimeError):
            pass
    helper = _ColumnMenu(table, settings_key, always_visible, default_hidden)
    header.customContextMenuRequested.connect(helper.show_menu)
    # Belt and braces: the C++ parent keeps the helper alive, and so does
    # this Python-side reference on the table wrapper.
    table._column_menu = helper
    apply_saved_column_visibility(table, settings_key, always_visible,
                                  default_hidden)


def apply_saved_column_visibility(table, settings_key: str,
                                  always_visible: Sequence[int] = (0,),
                                  default_hidden: Sequence[str] = ()) -> None:
    """Restore column visibility saved by ``enable_column_menu``."""
    import json

    from qgis.PyQt.QtCore import QSettings

    labels = _column_labels(table)
    default_hidden = set(default_hidden or ())
    raw = QSettings().value(settings_key)
    hidden_labels = None
    known = set()
    try:
        saved = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        saved = None
    if isinstance(saved, dict):
        hidden_labels = {str(v) for v in (saved.get("hidden") or [])}
        known = {str(v) for v in (saved.get("known") or [])}
    elif isinstance(saved, list):
        # Legacy index list (pre-label persistence).
        hidden_labels = set()
        for column in saved:
            try:
                column = int(column)
            except (TypeError, ValueError):
                continue
            if 0 <= column < len(labels):
                hidden_labels.add(labels[column])
        known = set(labels)
    for column, label in enumerate(labels):
        if column in always_visible:
            table.setColumnHidden(column, False)
            continue
        if hidden_labels is not None and label in known:
            table.setColumnHidden(column, label in hidden_labels)
        else:
            table.setColumnHidden(column, label in default_hidden)


# Item data roles used by ComboColumnDelegate (ints: Qt6 enums do not
# support arithmetic, and setData/data accept plain ints on both bindings).
COMBO_VALUE_ROLE = int(ITEM_DATA_USER_ROLE) + 16
COMBO_FLAG_ROLE = int(ITEM_DATA_USER_ROLE) + 17


class ComboColumnDelegate(QStyledItemDelegate):
    """Drop-down cells without a ``QComboBox`` widget per row.

    A ``setCellWidget`` combo per row costs a real widget (create, style,
    lay out, destroy on every rebuild) — thousands of rows froze the GUI for
    the duration of every refresh. The delegate paints the current label
    plus a drop-down arrow from item data and only creates a combo for the
    cell being edited; a single click on the cell opens it with its popup.

    ``options_for(index)`` returns ``[(value, label), ...]`` for an index or
    ``None`` when the cell is not a drop-down; ``on_commit(index, value)``
    is called after the item's text/value are updated.
    """

    def __init__(self, table, options_for: Callable, on_commit: Callable,
                 parent=None):
        super().__init__(parent or table)
        self._table = table
        self._options_for = options_for
        self._on_commit = on_commit
        table.clicked.connect(self._open_on_click)

    @staticmethod
    def mark_item(item, value: str, label: str) -> None:
        """Stamp a table item as a drop-down cell showing ``label``."""
        item.setText(label)
        item.setData(COMBO_VALUE_ROLE, value or "")
        item.setData(COMBO_FLAG_ROLE, True)

    def _open_on_click(self, index) -> None:
        if index.isValid() and bool(index.data(COMBO_FLAG_ROLE)):
            self._table.edit(index)

    def createEditor(self, parent, option, index):
        options = self._options_for(index)
        if options is None:
            return None
        from qgis.PyQt.QtWidgets import QComboBox

        combo = QComboBox(parent)
        for value, label in options:
            combo.addItem(label, value)
        combo.activated.connect(lambda _i, c=combo: self._commit(c))
        return combo

    def _commit(self, combo) -> None:
        self.commitData.emit(combo)
        self.closeEditor.emit(combo)

    def setEditorData(self, editor, index) -> None:
        current = index.data(COMBO_VALUE_ROLE)
        position = editor.findData(current if current is not None else "")
        editor.setCurrentIndex(max(0, position))
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(0, editor.showPopup)

    def setModelData(self, editor, model, index) -> None:
        value = editor.currentData()
        value = "" if value is None else str(value)
        if str(index.data(COMBO_VALUE_ROLE) or "") == value:
            return
        model.setData(index, editor.currentText())
        model.setData(index, value, COMBO_VALUE_ROLE)
        self._on_commit(index, value)

    def paint(self, painter, option, index) -> None:
        is_combo = bool(index.data(COMBO_FLAG_ROLE))
        if is_combo:
            # Reserve the arrow gutter so long labels elide before it.
            option.rect.setRight(option.rect.right() - 14)
        super().paint(painter, option, index)
        if not is_combo:
            return
        rect = option.rect
        rect.setRight(rect.right() + 14)
        size = max(3, min(5, rect.height() // 4))
        cx = rect.right() - size - 4
        cy = rect.center().y()
        from qgis.PyQt.QtCore import QPointF
        from qgis.PyQt.QtGui import QPolygonF

        arrow = QPolygonF([QPointF(cx - size, cy - size / 2.0),
                           QPointF(cx + size, cy - size / 2.0),
                           QPointF(cx, cy + size / 2.0)])
        painter.save()
        try:
            painter.setPen(getattr(Qt, "PenStyle", Qt).NoPen)
            painter.setBrush(option.palette.text())
            painter.drawPolygon(arrow)
        finally:
            painter.restore()


def menu_tool_button(text: str,
                     entries: Sequence[Optional[Tuple[str, Callable]]],
                     tooltip: str = "", parent=None) -> QToolButton:
    """One compact drop-down button for several related actions.

    ``entries``: (label, slot) pairs; ``None`` inserts a separator.
    """
    button = QToolButton(parent)
    button.setText(text)
    button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
    if tooltip:
        button.setToolTip(tooltip)
    menu = QMenu(button)
    for entry in entries:
        if entry is None:
            menu.addSeparator()
            continue
        label, slot = entry
        menu.addAction(label, slot)
    button.setMenu(menu)
    return button
