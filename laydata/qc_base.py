# -*- coding: utf-8 -*-
"""Base types for the cable-lay QC engine: checks, parameters and findings.

Everything here is plain Python (only :mod:`numpy` is used by subclasses) so the
engine stays UI- and QGIS-free and can be unit-tested directly. Findings carry
enough context (time span, KP span, position, source, feature id) to be written
to a ``qc_findings`` GeoPackage layer and to drive map / plot highlighting in the
Explorer window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# Reserved key that carries a WKT geometry string on a row dict. Must match
# ``cable_lay_parsers.WKT_KEY`` (kept as a literal here so the engine does not
# import the QGIS-facing parsers module).
WKT_KEY = "__wkt__"


class Severity:
    """QC finding severities (stored as plain strings in the layer)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    ORDER = {INFO: 0, WARNING: 1, ERROR: 2}

    @classmethod
    def rank(cls, value: str) -> int:
        return cls.ORDER.get(value, 0)


@dataclass
class ParamSpec:
    """Declarative description of one check parameter.

    Used to auto-build both the Explorer parameter editors and the processing
    algorithm parameters, so a check declares its inputs in exactly one place.
    """

    name: str
    label: str
    kind: str = "float"  # "float" | "int" | "bool" | "str" | "field" | "choice"
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[Sequence[str]] = None
    help: str = ""

    def coerce(self, value: Any) -> Any:
        """Coerce a raw UI / processing value to the declared kind."""
        if value is None:
            return self.default
        try:
            if self.kind == "int":
                return int(value)
            if self.kind == "float":
                return float(value)
            if self.kind == "bool":
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on")
                return bool(value)
        except (TypeError, ValueError):
            return self.default
        return value


@dataclass
class Finding:
    """A single QC result row."""

    check_id: str
    severity: str
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    kp_start: Optional[float] = None
    kp_end: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    source_file: Optional[str] = None
    feature_fid: Optional[int] = None
    count: int = 1
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_row(self, layer_name: str, run_id: str, run_time: str, wkt_key: str = WKT_KEY) -> Dict[str, Any]:
        """Convert to a plain row dict ready for ``write_layer_to_gpkg``."""
        row: Dict[str, Any] = {
            "check_id": self.check_id,
            "severity": self.severity,
            "message": self.message,
            "value": _num(self.value),
            "threshold": _num(self.threshold),
            "time_start": self.time_start,
            "time_end": self.time_end,
            "kp_start": _num(self.kp_start),
            "kp_end": _num(self.kp_end),
            "source_file": self.source_file,
            "src_layer": layer_name,
            "feature_fid": None if self.feature_fid is None else int(self.feature_fid),
            "n_records": int(self.count),
            "run_id": run_id,
            "run_time": run_time,
        }
        if self.lat is not None and self.lon is not None and _is_finite(self.lat) and _is_finite(self.lon):
            row[wkt_key] = f"POINT({float(self.lon)} {float(self.lat)})"
        else:
            row[wkt_key] = None
        return row


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result  # drop nan


def _is_finite(value) -> bool:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return False
    return result == result and result not in (float("inf"), float("-inf"))


class QcCheck:
    """Abstract base for a QC check.

    Subclasses set the class attributes and implement :meth:`run`. Instances are
    cheap and stateless; parameters are passed to :meth:`run` so one class can be
    reused with different settings (e.g. a precision check per field).
    """

    check_id: str = ""
    name: str = ""
    description: str = ""

    @classmethod
    def param_specs(cls) -> List[ParamSpec]:
        return []

    def default_params(self) -> Dict[str, Any]:
        return {spec.name: spec.default for spec in self.param_specs()}

    def resolve_params(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        resolved = self.default_params()
        if params:
            for spec in self.param_specs():
                if spec.name in params:
                    resolved[spec.name] = spec.coerce(params[spec.name])
        return resolved

    def applicable(self, dataset) -> bool:  # noqa: ARG002 - overridden as needed
        return True

    def run(self, dataset, params: Optional[Dict[str, Any]] = None) -> List[Finding]:
        raise NotImplementedError


class QcRunner:
    """Runs a set of checks over one dataset and collects findings."""

    def __init__(self, dataset):
        self.dataset = dataset

    def run(self, checks: Sequence, progress=None, is_canceled=None) -> List[Finding]:
        """Run ``checks`` (each a ``QcCheck`` or ``(QcCheck, params)`` tuple).

        Optional hooks:
        - ``progress(done, total, check_id)`` for coarse progress feedback.
        - ``is_canceled()`` to request cancellation between checks.
        """
        findings: List[Finding] = []
        total = len(checks)
        for idx, entry in enumerate(checks):
            if is_canceled is not None and is_canceled():
                break
            check, params = entry if isinstance(entry, tuple) else (entry, None)
            if not check.applicable(self.dataset):
                if progress is not None:
                    progress(idx + 1, total, getattr(check, "check_id", ""))
                continue
            findings.extend(check.run(self.dataset, params))
            if progress is not None:
                progress(idx + 1, total, getattr(check, "check_id", ""))
        return findings

    @staticmethod
    def findings_to_rows(
        findings: Sequence[Finding],
        layer_name: str,
        run_id: str,
        run_time: str,
        wkt_key: str = WKT_KEY,
    ) -> List[Dict[str, Any]]:
        return [f.to_row(layer_name, run_id, run_time, wkt_key) for f in findings]
