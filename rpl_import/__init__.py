# -*- coding: utf-8 -*-
"""Pure-Python RPL import core.

This package turns a source workbook/CSV into a neutral, ordered
point/segment model ready for the Cable Route Workbench, in testable layers:

- :mod:`.model`    neutral model, import profile, diagnostics
- :mod:`.reader`   workbook/CSV -> SourceGrid (merged cells resolved)
- :mod:`.coords`   coordinate parsing (split DDM, DDM text, decimal degrees)
- :mod:`.detect`   sheet scoring, data-range/layout/column-mapping detection
- :mod:`.parser`   SourceGrid + ImportProfile -> ImportedRpl
- :mod:`.validate` structural/engineering diagnostics with stable rule IDs

Nothing in this package may import ``qgis`` or Qt: QGIS-side adapters
(map preview, commit service, wizard, processing wrapper) live in
``workbench/`` and ``processing/`` and inject geodesy/CRS services in.
"""

from .model import (  # noqa: F401
    Diagnostic,
    ImportedRpl,
    ImportPoint,
    ImportProfile,
    ImportSegment,
    PARSER_VERSION,
    has_errors,
)
