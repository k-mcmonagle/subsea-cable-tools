# -*- coding: utf-8 -*-
"""Span & Slack Analysis GeoPackage: scenarios, sampled profiles and result layers.

One GeoPackage per QGIS project, by default in a ``span_analysis`` folder
beside the project file (``<project>_span_analysis.gpkg``); the path is kept
in the project's custom properties and can be changed (open another file,
or start a new one). Unsaved projects use the plugin folder in the QGIS
profile, as the Workbench and Burial Planner do.

Tables (geometryless GeoPackage layers, created through OGR, then read and
written row by row with SQL in transactions):

* ``sa_meta``      key / value (schema version, created)
* ``sa_scenario``  one row per scenario: name and settings (JSON)
* ``sa_profile``   the seabed profile sampled for a scenario (JSON), so a
                   scenario reopens without sampling again

Result layers (``spans_<scenario>``) are written on request through OGR,
outside any SQL transaction (the GeoPackage is locked inside one).
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence

from qgis.core import QgsProject

from ..burial import gpkg_sql
from ..processing.cable_lay_parsers import WKT_KEY, fields_from_specs, write_layer_to_gpkg
from ..qgis_compat import WKB_LINESTRING, WKB_NO_GEOMETRY
from ..workbench.schema import gpkg_folder_for, new_id, sanitize_slug, utc_now_iso

PROJECT_SCOPE = "SubseaCableTools"
PROJECT_KEY_GPKG = "span_analysis_gpkg"
FOLDER_NAME = "span_analysis"
SCHEMA_VERSION = 1

TABLE_META = "sa_meta"
TABLE_SCENARIO = "sa_scenario"
TABLE_PROFILE = "sa_profile"
META_FIELDS = [("key", "str"), ("value", "str")]
SCENARIO_FIELDS = [("scenario_id", "str"), ("name", "str"), ("settings_json", "str"), ("summary_json", "str"),
                   ("created_utc", "str"), ("modified_utc", "str")]
PROFILE_FIELDS = [("scenario_id", "str"), ("signature", "str"), ("profile_json", "str"), ("modified_utc", "str")]
TABLES = {TABLE_META: META_FIELDS, TABLE_SCENARIO: SCENARIO_FIELDS, TABLE_PROFILE: PROFILE_FIELDS}
RESULT_FIELDS = [("scenario", "str"), ("check", "str"), ("level", "str"), ("kp_from", "float"),
                 ("kp_to", "float"), ("length_m", "float"), ("value", "float"), ("unit", "str"),
                 ("message", "str")]


def project_gpkg_path(project: Optional[QgsProject] = None) -> Optional[str]:
    project = project or QgsProject.instance()
    value, ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, "")
    return value if ok and value else None


def set_project_gpkg_path(path: str, project: Optional[QgsProject] = None) -> None:
    project = project or QgsProject.instance()
    project.writeEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, path)


def default_gpkg_path(project: Optional[QgsProject] = None) -> str:
    """``<project folder>/span_analysis/<project>_span_analysis.gpkg``."""
    project = project or QgsProject.instance()
    path = project.fileName()
    if path:
        stem = os.path.splitext(os.path.basename(path))[0]
    else:
        stem = sanitize_slug(project.title()) if project.title() else "project"
    return os.path.join(gpkg_folder_for(path), FOLDER_NAME, f"{stem}_span_analysis.gpkg")


class SpanStore:
    """Scenario registry in one GeoPackage (main thread only)."""

    def __init__(self, gpkg_path: str):
        self.gpkg_path = gpkg_path

    # -- lifecycle ----------------------------------------------------------
    def exists(self) -> bool:
        """Whether the file holds a scenario registry (read-only probe)."""
        return os.path.exists(self.gpkg_path) and gpkg_sql.has_table(self.gpkg_path, TABLE_META)

    def ensure_created(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.gpkg_path))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        context = QgsProject.instance().transformContext()
        for table, specs in TABLES.items():
            if not (os.path.exists(self.gpkg_path) and gpkg_sql.has_table(self.gpkg_path, table)):
                gpkg_sql.close(self.gpkg_path)  # OGR writes outside the SQL connection
                write_layer_to_gpkg(self.gpkg_path, table, fields_from_specs(specs), WKB_NO_GEOMETRY, [], context)
        meta = self.read_meta()
        if "schema_version" not in meta:
            self.write_meta("schema_version", str(SCHEMA_VERSION))
            self.write_meta("created_utc", utc_now_iso())

    def close(self) -> None:
        gpkg_sql.close(self.gpkg_path)

    def _conn(self):
        return gpkg_sql.connect(self.gpkg_path)

    # -- meta -----------------------------------------------------------------
    def read_meta(self) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in gpkg_sql.read_rows(self._conn(), TABLE_META) if r.get("key")}

    def write_meta(self, key: str, value: str) -> None:
        gpkg_sql.upsert_rows(self._conn(), TABLE_META, "key", [{"key": key, "value": value}])

    # -- scenarios --------------------------------------------------------------
    def list_scenarios(self) -> List[Dict]:
        rows = gpkg_sql.read_rows(self._conn(), TABLE_SCENARIO)
        return sorted(rows, key=lambda r: (str(r.get("name") or "").lower(), str(r.get("created_utc") or "")))

    def get_scenario(self, scenario_id: str) -> Optional[Dict]:
        rows = gpkg_sql.read_rows(self._conn(), TABLE_SCENARIO, "scenario_id = ?", (scenario_id,))
        return rows[0] if rows else None

    def save_scenario(self, name: str, settings: Dict, scenario_id: Optional[str] = None,
                      summary: Optional[Dict] = None) -> str:
        now = utc_now_iso()
        existing = self.get_scenario(scenario_id) if scenario_id else None
        row = {
            "scenario_id": scenario_id or new_id(),
            "name": name,
            "settings_json": json.dumps(settings, sort_keys=True),
            "summary_json": json.dumps(summary or {}, sort_keys=True),
            "created_utc": (existing or {}).get("created_utc") or now,
            "modified_utc": now,
        }
        gpkg_sql.upsert_rows(self._conn(), TABLE_SCENARIO, "scenario_id", [row])
        return row["scenario_id"]

    def delete_scenario(self, scenario_id: str) -> None:
        conn = self._conn()
        with gpkg_sql.transaction(conn):
            gpkg_sql.delete_where(conn, TABLE_SCENARIO, "scenario_id = ?", (scenario_id,))
            gpkg_sql.delete_where(conn, TABLE_PROFILE, "scenario_id = ?", (scenario_id,))

    @staticmethod
    def settings_of(row: Optional[Dict]) -> Dict:
        try:
            return json.loads((row or {}).get("settings_json") or "{}")
        except (TypeError, ValueError):
            return {}

    # -- sampled profiles ---------------------------------------------------------
    def save_profile(self, scenario_id: str, signature: str, profile: Dict) -> None:
        row = {"scenario_id": scenario_id, "signature": signature, "profile_json": json.dumps(profile),
               "modified_utc": utc_now_iso()}
        gpkg_sql.upsert_rows(self._conn(), TABLE_PROFILE, "scenario_id", [row])

    def load_profile(self, scenario_id: str) -> Optional[Dict]:
        rows = gpkg_sql.read_rows(self._conn(), TABLE_PROFILE, "scenario_id = ?", (scenario_id,))
        if not rows:
            return None
        try:
            data = json.loads(rows[0].get("profile_json") or "{}")
        except (TypeError, ValueError):
            return None
        data["signature"] = rows[0].get("signature") or ""
        return data

    # -- result layers ---------------------------------------------------------------
    @staticmethod
    def results_layer_name(scenario_name: str) -> str:
        return f"spans_{sanitize_slug(scenario_name).lower()}"

    def write_results_layer(self, scenario_name: str, rows: Sequence[Dict]) -> str:
        """Rewrite the scenario's findings layer (EPSG:4326 lines); returns its name.

        ``rows`` carry the RESULT_FIELDS and a WKT line under ``WKT_KEY``.
        """
        name = self.results_layer_name(scenario_name)
        self.close()  # OGR writes outside the SQL connection (the file would be locked)
        write_layer_to_gpkg(self.gpkg_path, name, fields_from_specs(RESULT_FIELDS), WKB_LINESTRING, list(rows),
                            QgsProject.instance().transformContext())
        return name


__all__ = ["SpanStore", "WKT_KEY", "default_gpkg_path", "project_gpkg_path", "set_project_gpkg_path"]
