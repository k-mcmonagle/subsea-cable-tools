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
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Optional, Sequence, Tuple

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
    QToolButton,
    QVBoxLayout,
)

from ..qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    ITEM_DATA_USER_ROLE,
    TOOLBUTTON_POPUP_MODE_INSTANT,
)

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


def enable_column_menu(table, settings_key: str,
                       always_visible: Sequence[int] = (0,)) -> None:
    """Right-click on a table header toggles column visibility (persisted).

    The narrow-dock escape hatch for wide tables: secondary columns can be
    hidden per machine without losing them for good.
    """
    import json

    from qgis.PyQt.QtCore import QSettings

    from ..qgis_compat import CONTEXT_MENU_POLICY_CUSTOM, qt_exec

    header = table.horizontalHeader()
    header.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
    header.setToolTip("Right-click to choose which columns are shown.")

    def save() -> None:
        hidden = [column for column in range(table.columnCount())
                  if table.isColumnHidden(column)]
        QSettings().setValue(settings_key, json.dumps(hidden))

    def show_menu(position) -> None:
        menu = QMenu(table)
        for column in range(table.columnCount()):
            item = table.horizontalHeaderItem(column)
            label = item.text() if item is not None else str(column + 1)
            action = menu.addAction(label or f"Column {column + 1}")
            action.setCheckable(True)
            action.setChecked(not table.isColumnHidden(column))
            if column in always_visible:
                action.setEnabled(False)
            action.toggled.connect(
                lambda checked, c=column:
                (table.setColumnHidden(c, not checked), save()))
        qt_exec(menu, header.mapToGlobal(position))

    header.customContextMenuRequested.connect(show_menu)
    try:
        hidden = json.loads(QSettings().value(settings_key) or "[]")
    except (ValueError, TypeError):
        hidden = []
    for column in hidden:
        try:
            column = int(column)
        except (TypeError, ValueError):
            continue
        if column not in always_visible and column < table.columnCount():
            table.setColumnHidden(column, True)


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
