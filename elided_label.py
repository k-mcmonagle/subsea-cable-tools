"""A single-line status label for resizable Qt panels."""
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtWidgets import QLabel

from .qgis_compat import SIZE_POLICY_IGNORED, SIZE_POLICY_PREFERRED, TEXT_ELIDE_RIGHT


class ElidedLabel(QLabel):
    """Keep the full message available to callers, tooltips and screen readers."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(SIZE_POLICY_IGNORED, SIZE_POLICY_PREFERRED)
        self.setText(text)

    def text(self):
        return self._full_text

    def setText(self, text):
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self.setAccessibleName(self._full_text)
        self._apply_elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._apply_elide()

    def _apply_elide(self):
        width = max(0, self.contentsRect().width() - 2 * self.margin())
        super().setText(self.fontMetrics().elidedText(self._full_text, TEXT_ELIDE_RIGHT, width))
