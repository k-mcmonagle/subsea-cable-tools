# -*- coding: utf-8 -*-
"""Static checks for QGIS 4 / Qt6 compatibility patterns.

This script intentionally scans plugin-owned Python files only. Vendored
dependencies and this checker are excluded because they may contain fallback
patterns by design.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "lib", "__pycache__"}
SKIP_FILES = {
    Path("qgis_compat.py"),
    Path("tests/check_qgis_compat.py"),
}

CHECKS = (
    ("Qt exec_", re.compile(r"\.exec_\(")),
    ("QAction from QtWidgets", re.compile(r"QtWidgets\s+import\s+.*\bQAction\b")),
    ("unscoped QDialog result", re.compile(r"QDialog\.Accepted")),
    ("unscoped QSizePolicy", re.compile(r"QSizePolicy\.Expanding")),
    ("unscoped item-view enum", re.compile(r"QAbstractItemView\.(ExtendedSelection|SingleSelection|SelectRows|DoubleClicked|SelectedClicked|EditKeyPressed)")),
    ("unscoped header resize mode", re.compile(r"QHeaderView\.(Interactive|Stretch|Fixed|ResizeToContents|Custom)")),
    ("unscoped Qgis message level", re.compile(r"Qgis\.(Info|Warning|Critical|Success)")),
    ("unscoped geometry enum", re.compile(r"QgsWkbTypes\.(LineGeometry|PointGeometry|PolygonGeometry|NullGeometry)")),
    ("layer instance type enum", re.compile(r"\.(VectorLayer|RasterLayer)\b")),
    ("QVariant field type", re.compile(r"QVariant\.(String|Double|Int|LongLong|Bool)")),
    ("processing number enum", re.compile(r"QgsProcessingParameterNumber\.(Double|Integer)")),
    ("processing field enum", re.compile(r"QgsProcessingParameterField\.(Numeric|Any)")),
)


def iter_python_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel in SKIP_FILES:
            continue
        files.append(path)
    return files


def main() -> int:
    failures: list[str] = []
    for path in iter_python_files():
        rel = path.relative_to(ROOT)
        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            for label, pattern in CHECKS:
                if pattern.search(line):
                    failures.append(f"{rel}:{line_number}: {label}: {line.strip()}")

    if failures:
        print("QGIS compatibility check failed:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print("QGIS compatibility check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())