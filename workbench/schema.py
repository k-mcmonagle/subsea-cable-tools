# -*- coding: utf-8 -*-
"""Cable Route Workbench GeoPackage schema.

Declarative field specs for the workbench registry tables plus the per-RPL
spatial layer schemas. Field specs are ``(name, type_str)`` tuples using the
same type strings as processing/cable_lay_parsers.py (``str``/``float``/``int``).

Table overview (all registry tables are geometryless GPKG layers):

- wb_meta            key/value store (schema_version, created_utc)
- wb_assembly        assembly headers (cable or rigging)
- wb_assembly_item   ordered sections/bodies of an assembly
- wb_route           route identity grouping RPL revisions
- wb_rpl             RPL registry (points/lines layer names + settings)
- wb_fit             assembly <-> RPL fit anchors
- wb_makeup          revision-ready physical cable make-up for a segment
- wb_makeup_item     ordered assembly placements and joints in a make-up
- wb_event_rule      RPL event classification rules (body|geographic|installation)
- wb_component       CRA-style topology: components (rpl|assembly|node)
- wb_port            CRA-style topology: ports on components
- wb_connection      CRA-style topology: undirected port-to-port edges
- wb_system          named systems (membership derived from the port graph)

Per-RPL spatial layers mirror the Import Excel RPL output schema verbatim,
plus ``rpl_id`` and ``SeqNo`` bookkeeping columns. ``PosNo`` is a document
identity and is never auto-renumbered; ordering lives in ``SeqNo``.
"""

from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Tuple

SCHEMA_VERSION = 5

# Registry table names ------------------------------------------------------
TABLE_META = "wb_meta"
TABLE_ASSEMBLY = "wb_assembly"
TABLE_ASSEMBLY_ITEM = "wb_assembly_item"
TABLE_ROUTE = "wb_route"
TABLE_RPL = "wb_rpl"
TABLE_FIT = "wb_fit"
TABLE_MAKEUP = "wb_makeup"
TABLE_MAKEUP_ITEM = "wb_makeup_item"
TABLE_EVENT_RULE = "wb_event_rule"
TABLE_COMPONENT = "wb_component"
TABLE_PORT = "wb_port"
TABLE_CONNECTION = "wb_connection"
TABLE_SYSTEM = "wb_system"
# Route-suitability / burial-assessment rules engine (schema v2)
TABLE_RULE_SET = "wb_rule_set"
TABLE_RULE = "wb_rule"
TABLE_ASSESSMENT = "wb_assessment"
TABLE_ASSESSMENT_RANGE = "wb_assessment_range"

FieldSpec = Tuple[str, str]

META_FIELDS: List[FieldSpec] = [
    ("key", "str"),
    ("value", "str"),
]

ASSEMBLY_FIELDS: List[FieldSpec] = [
    ("assembly_id", "str"),
    ("name", "str"),
    ("kind", "str"),          # cable | rigging
    ("description", "str"),
    ("source", "str"),        # manual | rpl_extract | catenary_json | excel
    ("source_ref", "str"),
    ("total_cable_len_m", "float"),
    ("created_utc", "str"),
    ("modified_utc", "str"),
    ("rev_label", "str"),
    ("status", "str"),
    ("supersedes_id", "str"),
    ("issued_utc", "str"),
]

# One table for both sections and bodies; ``kind`` discriminates and ``seq``
# is the authoritative order. Matches the V2 catenary assembly columns plus
# the V3 hydro columns so catenary JSON round-trips losslessly.
ASSEMBLY_ITEM_FIELDS: List[FieldSpec] = [
    ("item_id", "str"),
    ("assembly_id", "str"),
    ("seq", "int"),
    ("kind", "str"),                  # section | body
    ("name", "str"),
    ("length_m", "float"),
    ("cable_dist_start_m", "float"),  # derived cache
    ("q_water_npm", "float"),
    ("q_air_npm", "float"),
    ("point_load_kN", "float"),
    ("friction_mu", "float"),
    ("bending_stiffness_kNm2", "float"),
    ("min_bend_radius_m", "float"),
    ("diameter_m", "float"),
    ("cd_normal", "float"),
    ("cd_tangential", "float"),
    ("cable_type", "str"),
    ("cable_code", "str"),
    ("fiber_pair", "str"),
    ("color_hex", "str"),
    ("remarks", "str"),
]

ROUTE_FIELDS: List[FieldSpec] = [
    ("route_id", "str"),
    ("name", "str"),
    # Manual workbench grouping. wb_system is also populated by topology
    # assignment for the Systems tab, so this is deliberately a lightweight
    # reference rather than derived membership.
    ("system_id", "str"),
    ("description", "str"),
    ("created_utc", "str"),
    ("modified_utc", "str"),
    ("notes", "str"),
]

RPL_FIELDS: List[FieldSpec] = [
    ("rpl_id", "str"),
    ("name", "str"),
    ("kind", "str"),                 # planned | as_laid
    ("points_layer", "str"),
    ("lines_layer", "str"),
    ("source_file", "str"),
    ("slack_mode", "str"),           # hold_slack | hold_cable
    ("depth_source_config", "str"),  # JSON
    ("created_utc", "str"),
    ("modified_utc", "str"),
    ("notes", "str"),
    ("route_id", "str"),
    ("rev_label", "str"),
    ("status", "str"),
    ("supersedes_id", "str"),
    ("issued_utc", "str"),
]

FIT_FIELDS: List[FieldSpec] = [
    ("fit_id", "str"),
    ("assembly_id", "str"),
    ("rpl_id", "str"),
    ("anchor_kp_km", "float"),
    ("anchor_cable_dist_m", "float"),
    ("direction", "int"),  # +1 = assembly runs with increasing KP
    ("params_json", "str"),
    ("created_utc", "str"),
]

MAKEUP_FIELDS: List[FieldSpec] = [
    ("makeup_id", "str"),
    ("route_id", "str"),
    ("name", "str"),
    ("rev_label", "str"),
    ("status", "str"),
    ("supersedes_id", "str"),
    ("created_utc", "str"),
    ("modified_utc", "str"),
    ("notes", "str"),
]

MAKEUP_ITEM_FIELDS: List[FieldSpec] = [
    ("makeup_item_id", "str"),
    ("makeup_id", "str"),
    ("seq", "int"),
    ("kind", "str"),             # assembly | joint
    ("assembly_id", "str"),      # assembly placement only
    ("name", "str"),             # placement label / joint identifier
    ("direction", "int"),        # +1 normal, -1 reversed
    ("use_start_m", "float"),    # optional cut/use interval in assembly domain
    ("use_end_m", "float"),
    ("params_json", "str"),      # future loading/joint planning extension
    ("notes", "str"),
]

EVENT_RULE_FIELDS: List[FieldSpec] = [
    ("rule_id", "str"),
    ("pattern", "str"),    # regex matched case-insensitively against Event text
    ("category", "str"),   # body | geographic (legacy "installation" reads as geographic)
    ("body_type", "str"),  # subtype: joint | repeater | ... / crossing | boundary | ...
    ("priority", "int"),   # lower number wins
]

COMPONENT_FIELDS: List[FieldSpec] = [
    ("component_id", "str"),
    ("kind", "str"),        # route | assembly | node (legacy: rpl)
    ("subject_id", "str"),  # wb_route.route_id / wb_assembly.assembly_id, NULL for nodes
    ("name", "str"),
    ("node_type", "str"),   # nodes only: bmh | bu | joint | other
    ("lat", "float"),       # nodes only, optional
    ("lon", "float"),
    ("system_id", "str"),   # cached derived assignment
]

PORT_FIELDS: List[FieldSpec] = [
    ("port_id", "str"),
    ("component_id", "str"),
    ("label", "str"),  # A/B for cable-segment endpoints; named sides for nodes
]

CONNECTION_FIELDS: List[FieldSpec] = [
    ("connection_id", "str"),
    ("port_a_id", "str"),
    ("port_b_id", "str"),
]

SYSTEM_FIELDS: List[FieldSpec] = [
    ("system_id", "str"),
    ("name", "str"),
    ("notes", "str"),
]

# Rules engine (schema v2) --------------------------------------------------
# A rule set is an ordered stack of rules (like Excel conditional formatting).
# An assessment applies one rule set to one RPL and produces per-method
# KP-range verdicts (allowed | risk | excluded) with provenance.

RULE_SET_FIELDS: List[FieldSpec] = [
    ("rule_set_id", "str"),
    ("name", "str"),
    ("description", "str"),
    ("methods_json", "str"),   # JSON list, e.g. ["plough","jet","surface"]
    ("created_utc", "str"),
    ("modified_utc", "str"),
]

RULE_FIELDS: List[FieldSpec] = [
    ("rule_id", "str"),
    ("rule_set_id", "str"),
    ("seq", "int"),            # evaluation order (top-to-bottom)
    ("name", "str"),
    ("enabled", "int"),        # 0/1
    ("kind", "str"),           # threshold_profile | proximity | polygon_class | kp_range_table | manual
    ("action", "str"),         # exclude | risk | allow
    ("risk_level", "int"),     # 1..3 for action=risk, else 0
    ("methods_json", "str"),   # JSON subset of the set's methods this rule applies to
    ("config_json", "str"),    # kind-specific payload (+ optional scope_ranges)
    ("notes", "str"),
]

ASSESSMENT_FIELDS: List[FieldSpec] = [
    ("assessment_id", "str"),
    ("rpl_id", "str"),
    ("rule_set_id", "str"),
    ("name", "str"),
    ("sample_step_m", "float"),
    ("min_range_km", "float"),
    ("rules_snapshot_json", "str"),  # frozen rules at run time (reproducibility)
    ("ranges_layer", "str"),         # spatial output layer name
    ("status", "str"),               # "" | stale | current
    ("run_utc", "str"),
    ("created_utc", "str"),
    ("modified_utc", "str"),
]

ASSESSMENT_RANGE_FIELDS: List[FieldSpec] = [
    ("range_id", "str"),
    ("assessment_id", "str"),
    ("method", "str"),
    ("start_kp", "float"),
    ("end_kp", "float"),
    ("status", "str"),          # allowed | risk | excluded
    ("risk_level", "int"),      # resolved severity 0..4
    ("fired_rules_json", "str"),
    ("dominant_rule_id", "str"),
    ("notes", "str"),
]

REGISTRY_TABLES: Dict[str, List[FieldSpec]] = {
    TABLE_META: META_FIELDS,
    TABLE_ASSEMBLY: ASSEMBLY_FIELDS,
    TABLE_ASSEMBLY_ITEM: ASSEMBLY_ITEM_FIELDS,
    TABLE_ROUTE: ROUTE_FIELDS,
    TABLE_RPL: RPL_FIELDS,
    TABLE_FIT: FIT_FIELDS,
    TABLE_MAKEUP: MAKEUP_FIELDS,
    TABLE_MAKEUP_ITEM: MAKEUP_ITEM_FIELDS,
    TABLE_EVENT_RULE: EVENT_RULE_FIELDS,
    TABLE_COMPONENT: COMPONENT_FIELDS,
    TABLE_PORT: PORT_FIELDS,
    TABLE_CONNECTION: CONNECTION_FIELDS,
    TABLE_SYSTEM: SYSTEM_FIELDS,
    TABLE_RULE_SET: RULE_SET_FIELDS,
    TABLE_RULE: RULE_FIELDS,
    TABLE_ASSESSMENT: ASSESSMENT_FIELDS,
    TABLE_ASSESSMENT_RANGE: ASSESSMENT_RANGE_FIELDS,
}

# Primary key field per table (single-column keys).
TABLE_KEYS: Dict[str, str] = {
    TABLE_ASSEMBLY: "assembly_id",
    TABLE_ASSEMBLY_ITEM: "item_id",
    TABLE_ROUTE: "route_id",
    TABLE_RPL: "rpl_id",
    TABLE_FIT: "fit_id",
    TABLE_MAKEUP: "makeup_id",
    TABLE_MAKEUP_ITEM: "makeup_item_id",
    TABLE_EVENT_RULE: "rule_id",
    TABLE_COMPONENT: "component_id",
    TABLE_PORT: "port_id",
    TABLE_CONNECTION: "connection_id",
    TABLE_SYSTEM: "system_id",
    TABLE_RULE_SET: "rule_set_id",
    TABLE_RULE: "rule_id",
    TABLE_ASSESSMENT: "assessment_id",
    TABLE_ASSESSMENT_RANGE: "range_id",
}

# Per-RPL spatial layers ----------------------------------------------------
# Mirrors processing/import_excel_rpl_algorithm.py output plus bookkeeping.
RPL_POINT_FIELDS: List[FieldSpec] = [
    ("rpl_id", "str"),
    ("SeqNo", "int"),
    ("PosNo", "int"),
    ("Event", "str"),
    ("DistCumulative", "float"),
    ("CableDistCumulative", "float"),
    ("ApproxDepth", "float"),
    ("Remarks", "str"),
    ("ChartNo", "int"),
    ("Latitude", "float"),
    ("Longitude", "float"),
    ("SourceFile", "str"),
]

RPL_LINE_FIELDS: List[FieldSpec] = [
    ("rpl_id", "str"),
    ("SeqNo", "int"),
    ("FromPos", "int"),
    ("ToPos", "int"),
    ("Bearing", "float"),
    ("DistBetweenPos", "float"),
    ("Slack", "float"),
    ("CableDistBetweenPos", "float"),
    ("CableCode", "str"),
    ("FiberPair", "str"),
    ("CableType", "str"),
    ("LayDirection", "str"),
    ("LayVessel", "str"),
    ("ProtectionMethod", "str"),
    ("DateInstalled", "str"),
    ("TargetBurialDepth", "float"),
    ("BurialDepth", "float"),
    ("TerritorialWater", "str"),
    ("EEZ", "str"),
    ("SourceFile", "str"),
]

# Event classification defaults ---------------------------------------------
# (pattern, category, subtype, priority). Matched case-insensitively with
# re.search against the point Event text.
#
# Every RPL point has a place on the map; the classification answers what
# else it is. Two natures, not mutually exclusive:
#   - "body"       — a physical component of the cable assembly (repeater,
#                    joint, branching unit, armour transition). Moves with
#                    the assembly if the make-up changes.
#   - "geographic" — a reference to a place (crossing, alter-course, boundary,
#                    water-depth mark, operational mark). Stays put if the
#                    assembly changes.
# One event text can match rules of BOTH natures ("JT-3 / AC12"): the
# classifier reports both, taking each nature's subtype from its own
# best-priority match. Unmatched events default to geographic with a
# validation note — never silently a body.
#
# The subtype is a free vocabulary; the defaults below seed the common ones
# and users can add their own rules (e.g. "equaliser", "maritime boundary").
CATEGORY_BODY = "body"
CATEGORY_GEOGRAPHIC = "geographic"
CATEGORY_BOTH = "both"          # review/display value; rules carry one nature
# Legacy stored value from schemas that had a third "installation" bucket;
# read paths normalise it to geographic.
LEGACY_CATEGORY_INSTALLATION = "installation"

DEFAULT_EVENT_RULES: List[Tuple[str, str, str, int]] = [
    (r"branching\s*unit|\bbu\b", CATEGORY_BODY, "bu", 10),
    (r"repeater|\brptr\b", CATEGORY_BODY, "repeater", 20),
    (r"equali[sz]er|\beq\b", CATEGORY_BODY, "equaliser", 25),
    (r"joint|\bjt\b|bujb|\bjb\d|\bujb", CATEGORY_BODY, "joint", 30),
    (r"\bbmh\b|beach\s*man\s*hole|beach\s*manhole", CATEGORY_BODY, "bmh", 40),
    (r"^\s*tr\b|transition", CATEGORY_BODY, "transition", 45),  # armour/cable type transitions (Tr DAS/SA)
    (r"crossing|\bxing\b", CATEGORY_GEOGRAPHIC, "crossing", 50),
    (r"\brbp\s*\d*\b|route\s*branch", CATEGORY_GEOGRAPHIC, "route_branch", 55),
    (r"\bpldn\b|\bplup\b|plough|burial|\bdse\b|start\s+of|end\s+of|\bsol\b|\beol\b|slack\s*box",
     CATEGORY_GEOGRAPHIC, "operations", 60),
    (r"\bacp?\s*\d*\b|alter\s*course", CATEGORY_GEOGRAPHIC, "alter_course", 70),
    (r"\bwd\s*\d", CATEGORY_GEOGRAPHIC, "water_depth", 75),   # water depth marks (WD 1000)
    (r"boundary|\beez\b|territorial|median\s*line", CATEGORY_GEOGRAPHIC, "boundary", 78),
]

# Route-suitability defaults ------------------------------------------------
# Rule actions and the severity lattice used by rules_engine.evaluate():
#   allowed (0) < risk 1 < risk 2 < risk 3 < excluded (4)
RULE_ACTION_EXCLUDE = "exclude"
RULE_ACTION_RISK = "risk"
RULE_ACTION_ALLOW = "allow"

RULE_KIND_THRESHOLD = "threshold_profile"
RULE_KIND_PROXIMITY = "proximity"
RULE_KIND_POLYGON = "polygon_class"
RULE_KIND_KP_TABLE = "kp_range_table"
RULE_KIND_MANUAL = "manual"

SEVERITY_ALLOWED = 0
SEVERITY_EXCLUDED = 4

STATUS_ALLOWED = "allowed"
STATUS_RISK = "risk"
STATUS_EXCLUDED = "excluded"

DEFAULT_ASSESSMENT_METHODS: List[str] = ["plough", "jet", "surface"]

STATUS_DRAFT = "draft"
STATUS_ISSUED = "issued"

# Seed rule-set template. Only kinds that need no project-specific layer are
# seeded (depth/slope thresholds); the user adds proximity/soil/table rules.
# Each entry: (name, kind, action, risk_level, methods, config_dict).
DEFAULT_RULE_SET_NAME = "Burial Assessment"
DEFAULT_RULES: List[Tuple[str, str, str, int, List[str], Dict]] = [
    ("Water depth > 1500 m", RULE_KIND_THRESHOLD, RULE_ACTION_EXCLUDE, 0, ["plough"],
     {"profile": "depth", "op": ">", "value": 1500.0, "value2": None, "abs": False}),
    ("Water depth > 2000 m", RULE_KIND_THRESHOLD, RULE_ACTION_EXCLUDE, 0, ["jet"],
     {"profile": "depth", "op": ">", "value": 2000.0, "value2": None, "abs": False}),
    ("Seabed slope > 10 deg", RULE_KIND_THRESHOLD, RULE_ACTION_EXCLUDE, 0, ["plough", "jet"],
     {"profile": "slope", "op": ">", "value": 10.0, "value2": None, "abs": True}),
    ("Seabed slope 5-10 deg (caution)", RULE_KIND_THRESHOLD, RULE_ACTION_RISK, 2, ["plough", "jet"],
     {"profile": "slope", "op": "between", "value": 5.0, "value2": 10.0, "abs": True}),
]


def sanitize_slug(name: str) -> str:
    """Sanitise a human name into a safe GeoPackage table-name fragment."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")
    return slug or "unnamed"


def rpl_points_layer_name(rpl_name: str) -> str:
    return f"rpl_{sanitize_slug(rpl_name)}_points"


def rpl_lines_layer_name(rpl_name: str) -> str:
    return f"rpl_{sanitize_slug(rpl_name)}_lines"


def fit_bodies_layer_name(fit_name: str) -> str:
    return f"wb_fit_bodies_{sanitize_slug(fit_name)}"


def fit_sections_layer_name(fit_name: str) -> str:
    return f"wb_fit_sections_{sanitize_slug(fit_name)}"


def assessment_ranges_layer_name(assessment_name: str) -> str:
    return f"wb_assess_{sanitize_slug(assessment_name)}_ranges"


def next_rev_label(existing) -> str:
    """Return the next friendly revision label from existing labels/rows."""
    max_n = 0
    for value in existing or []:
        label = value.get("rev_label") if isinstance(value, dict) else value
        match = re.search(r"\brev\s*(\d+)\b", str(label or ""), re.IGNORECASE)
        if match:
            max_n = max(max_n, int(match.group(1)))
    return f"Rev {max_n + 1}"


def unique_layer_name(existing, base: str) -> str:
    """Return ``base`` unless it already exists, then append ``_2`` etc."""
    existing_names = {str(name) for name in (existing or []) if name}
    if base not in existing_names:
        return base
    index = 2
    while f"{base}_{index}" in existing_names:
        index += 1
    return f"{base}_{index}"


def _writable(folder: str) -> bool:
    """Probe by actually creating a file: ``os.access`` lies on Windows ACLs."""
    if not folder or not os.path.isdir(folder):
        return False
    probe = os.path.join(folder, ".sct_write_probe_%s" % os.getpid())
    try:
        with open(probe, "w"):
            pass
    except OSError:
        return False
    try:
        os.remove(probe)
    except OSError:
        pass
    return True


def unsaved_project_folder() -> str:
    """Folder to hold plugin GeoPackages when the project has never been saved.

    The current working directory is not usable: QGIS launched from a Windows
    shortcut inherits ``C:\\WINDOWS\\system32``, which is not writable, so the
    GeoPackage cannot be created at all. Fall back to a plugin folder inside the
    active QGIS profile, then the user home, then the system temp folder —
    never the current working directory.
    """
    candidates = []
    try:
        from qgis.core import QgsApplication

        settings_dir = QgsApplication.qgisSettingsDirPath()
        if settings_dir:
            candidates.append(os.path.join(settings_dir, "subsea_cable_tools"))
    except Exception:
        pass
    candidates.append(os.path.join(os.path.expanduser("~"), "subsea_cable_tools"))
    for folder in candidates:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            continue
        if _writable(folder):
            return folder
    home = os.path.expanduser("~")
    if _writable(home):
        return home
    import tempfile

    return tempfile.gettempdir()


def gpkg_folder_for(project_path: str) -> str:
    """Writable folder for a per-project GeoPackage.

    Prefers the folder beside the project file; falls back to
    :func:`unsaved_project_folder` when the project is unsaved or its folder is
    not writable (read-only share, revoked cloud-sync folder, …).
    """
    if project_path:
        folder = os.path.dirname(project_path)
        if _writable(folder):
            return folder
    return unsaved_project_folder()


def default_gpkg_path(project_path: str, project_title: str = "") -> str:
    """Default workbench GeoPackage path beside the project file."""
    if project_path:
        stem = os.path.splitext(os.path.basename(project_path))[0]
        # Keep pointing at an existing file even if the folder probe fails now.
        beside = os.path.join(os.path.dirname(project_path), f"{stem}_workbench.gpkg")
        if os.path.exists(beside):
            return beside
    else:
        stem = sanitize_slug(project_title) if project_title else "project"
    return os.path.join(gpkg_folder_for(project_path), f"{stem}_workbench.gpkg")


def new_id() -> str:
    """Generate a UUIDv7 string (time-ordered, per the CRA spec preference)."""
    import random

    unix_ms = int(time.time() * 1000)
    rand_a = random.getrandbits(12)
    rand_b = random.getrandbits(62)
    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76          # version 7
    value |= rand_a << 64
    value |= 0b10 << 62         # variant
    value |= rand_b
    hex_str = f"{value:032x}"
    return f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
