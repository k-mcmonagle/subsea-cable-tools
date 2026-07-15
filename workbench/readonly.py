# -*- coding: utf-8 -*-
"""Small UI helpers for issued/read-only workbench entities."""

from __future__ import annotations

from qgis.PyQt.QtWidgets import QLabel


def make_readonly_banner(parent=None) -> QLabel:
    label = QLabel("Issued - create a new revision to edit.", parent)
    label.setStyleSheet(
        "QLabel { background: #fff3cd; color: #6b4e00; "
        "border: 1px solid #e0b84b; padding: 4px 6px; }"
    )
    label.setVisible(False)
    return label
