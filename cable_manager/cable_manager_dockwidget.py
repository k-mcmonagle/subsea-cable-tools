from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QVBoxLayout, QWidget


class CableManagerDockWidget(QDockWidget):
    """Cable Manager dock widget (placeholder)."""

    def __init__(self, iface, parent=None):
        super().__init__("Cable Manager", parent)
        self.iface = iface
        self.setObjectName("CableManagerDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("Cable Manager")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        subtitle = QLabel(
            "Work in progress. This tool will help manage onboard cable data."
        )
        subtitle.setWordWrap(True)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)

        self.setWidget(container)
