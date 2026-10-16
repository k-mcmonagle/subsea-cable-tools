# -*- coding: utf-8 -*-
"""AGS data files (AGS 3.x and AGS 4.x): a tolerant reader.

Pure python. Reads the file *syntax* into groups of rows keyed by heading;
``cpt.py`` decides what the CPT groups mean.

AGS 4 lines start with a descriptor::

    "GROUP","SCPT"
    "HEADING","LOCA_ID","SCPG_TESN","SCPT_DPTH",...
    "UNIT","","","m",...
    "TYPE","ID","X","2DP",...
    "DATA","CPT01","1","0.02",...

AGS 3 (3.0, 3.1, 3.1a) marks groups and headings with asterisks, gives units
in a ``<UNITS>`` row and continues long data lines in ``<CONT>`` rows::

    "**STCN"
    "*HOLE_ID","*STCN_DPTH","*STCN_RES"
    "<UNITS>","m","MPa"
    "CPT01","0.02","0.15"

Group and heading names are normalised to upper case without the ``*`` or
``**`` markers and without the ``?`` that flags a user-defined (non-standard)
name, so ``?HOLE_LAT`` is read as ``HOLE_LAT``; the names as written are
kept in ``AgsGroup.raw_headings``. Nothing is validated against the AGS
dictionary: unknown groups and headings are kept, missing ones are simply
absent. Problems are collected in ``AgsFile.warnings`` rather than raised,
except for a file that is not AGS at all.
"""

from __future__ import annotations

import csv
import os
import re
from typing import Dict, List, Optional, Tuple

AGS3 = "3"
AGS4 = "4"

_AGS4_DESCRIPTORS = ("GROUP", "HEADING", "UNIT", "TYPE", "DATA")
_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


class AgsError(ValueError):
    """The text is not an AGS data file (or cannot be read at all)."""


class AgsGroup:
    """One group: its headings, units, types and data rows (dicts)."""

    def __init__(self, name: str, raw_name: str = "", line: int = 0):
        self.name = name
        self.raw_name = raw_name or name
        self.line = line
        self.headings: List[str] = []
        self.raw_headings: List[str] = []
        self.units: Dict[str, str] = {}
        self.types: Dict[str, str] = {}
        self.rows: List[Dict[str, str]] = []
        self.row_lines: List[int] = []

    def has(self, heading: str) -> bool:
        return heading in self.headings

    def unit(self, heading: str) -> str:
        return self.units.get(heading, "")

    def column(self, heading: str) -> List[str]:
        return [row.get(heading, "") for row in self.rows]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AgsGroup({self.name!r}, {len(self.headings)} headings, {len(self.rows)} rows)"


class AgsFile:
    """The groups of one AGS file, in file order."""

    def __init__(self, path: str = ""):
        self.path = path
        self.syntax = ""          # AGS3 | AGS4 (from the line format)
        self.groups: Dict[str, AgsGroup] = {}
        self.warnings: List[str] = []
        self.encoding = ""

    @property
    def file_name(self) -> str:
        return os.path.basename(self.path) if self.path else ""

    @property
    def stem(self) -> str:
        """The file name without a recognised AGS/text extension."""
        name = self.file_name
        root, ext = os.path.splitext(name)
        return root if ext.lower() in (".ags", ".txt", ".csv", ".ags4", ".ags3") else name

    def group(self, *names: str) -> Optional[AgsGroup]:
        """The first of ``names`` present in the file, else None."""
        for name in names:
            group = self.groups.get(name)
            if group is not None:
                return group
        return None

    @property
    def declared_version(self) -> str:
        """The AGS edition the file states (TRAN_AGS / PROJ_AGS), else ""."""
        for group, heading in (("TRAN", "TRAN_AGS"), ("PROJ", "PROJ_AGS")):
            table = self.groups.get(group)
            if table is not None:
                for row in table.rows:
                    value = str(row.get(heading) or "").strip()
                    if value:
                        return value
        return ""

    @property
    def version_label(self) -> str:
        """``"AGS 3.1"`` / ``"AGS 4.1.1"`` (declared) or ``"AGS 3"`` / ``"AGS 4"``."""
        declared = self.declared_version
        if declared:
            text = re.sub(r"(?i)^\s*ags\s*", "", declared).strip(" -_vV")
            return f"AGS {text}" if text else f"AGS {self.syntax}"
        return f"AGS {self.syntax}" if self.syntax else "AGS"

    def summary(self) -> str:
        parts = [f"{name} ({len(group.rows)})" for name, group in self.groups.items()]
        return f"{self.version_label}: " + (", ".join(parts) if parts else "no groups")


# -- reading ------------------------------------------------------------------
def read_text(path: str) -> Tuple[str, str]:
    """``(text, encoding)``: UTF-8 (with or without BOM), else Windows-1252.

    AGS 3 and AGS 4.0 files are meant to be ASCII; real files often carry a
    degree sign or an accented name in another code page. Latin-1 is the last
    resort because it decodes any byte sequence.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"  # pragma: no cover


def _split(line: str) -> List[str]:
    try:
        return [str(f) for f in next(csv.reader([line], strict=False))]
    except (StopIteration, csv.Error):
        return []


def _records(text: str):
    """``(line_number, fields, raw line)`` for every non-blank line.

    AGS 3 and AGS 4 both forbid line breaks inside values, so each line is
    one record (CR+LF or LF endings).
    """
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = _split(line)
        if not any(f.strip() for f in fields):
            continue
        yield number, fields, line


def _quoted_split(line: str) -> Optional[List[str]]:
    """Fields of a fully quoted line split on ``","``.

    Real files contain unescaped quotes inside values (``51°46'47.4"`` for
    seconds of arc), which make a CSV parse merge the next field. Used only
    when a line's field count does not match its headings.
    """
    text = line.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].split('","')
    return None


def _fit(fields: List[str], line: str, expected: int, ags: "AgsFile", group_name: str,
         number: int) -> List[str]:
    """``fields``, or the quoted split when only that matches ``expected``."""
    if len(fields) == expected or not expected:
        return fields
    alternative = _quoted_split(line)
    if alternative is not None and len(alternative) == expected:
        ags.warnings.append(f"line {number}: group {group_name} has a value with an unescaped "
                            "quote; the line was split at its quoted commas")
        return alternative
    return fields


def detect_syntax(text: str) -> str:
    """``AGS4`` or ``AGS3`` from the first record, "" when it is neither."""
    for _line, fields, _raw in _records(text):
        first = fields[0].strip()
        if first.upper() in _AGS4_DESCRIPTORS:
            return AGS4
        if first.startswith("**"):
            return AGS3
        return ""
    return ""


def looks_like_ags(path: str, sniff_bytes: int = 4096) -> bool:
    """Cheap check (the first record) for files without an .ags extension."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(sniff_bytes)
    except OSError:
        return False
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]                     # UTF-8 byte order mark
    text = head.decode("latin-1", errors="replace")
    # A partial last line must not confuse the CSV reader.
    if "\n" in text:
        text = text[:text.rfind("\n")]
    return bool(detect_syntax(text))


def read_ags(path: str) -> AgsFile:
    """Parse the AGS file at ``path`` (raises AgsError when it is not AGS)."""
    try:
        text, encoding = read_text(path)
    except OSError as exc:
        raise AgsError(f"{os.path.basename(path)}: {exc}") from exc
    ags = parse_ags(text, path)
    ags.encoding = encoding
    return ags


def parse_ags(text: str, path: str = "") -> AgsFile:
    syntax = detect_syntax(text)
    name = os.path.basename(path) if path else "text"
    if not syntax:
        raise AgsError(f"{name} is not an AGS file: it does not start with a "
                       "\"GROUP\" (AGS 4) or \"**GROUP\" (AGS 3) line.")
    ags = AgsFile(path)
    ags.syntax = syntax
    if syntax == AGS4:
        _parse_ags4(text, ags)
    else:
        _parse_ags3(text, ags)
    if not ags.groups:
        raise AgsError(f"{name}: no AGS groups were found.")
    return ags


def normalise_name(text: str) -> str:
    """``*?HOLE_LAT`` → ``HOLE_LAT``; ``**?PROJ`` → ``PROJ``."""
    return str(text).strip().lstrip("*").lstrip("?").strip().upper()


def _add_group(ags: AgsFile, raw: str, line: int) -> AgsGroup:
    name = normalise_name(raw)
    if not name:
        ags.warnings.append(f"line {line}: a group without a name was skipped")
        name = f"UNNAMED_{line}"
    if name in ags.groups:
        # A repeated group continues the first one (allowed in AGS 3 when a
        # file is concatenated; never silently dropped).
        group = ags.groups[name]
        ags.warnings.append(f"line {line}: group {name} appears again; its rows were appended")
        return group
    group = AgsGroup(name, raw.strip().lstrip("*"), line)
    ags.groups[name] = group
    return group


def _set_headings(group: AgsGroup, raw: List[str], active: List[str]) -> None:
    """Extend the heading line being read (``active``) and the group's
    union of headings; a repeated group may list them in another order.
    A blank heading keeps its column position (its values are dropped)."""
    for heading in raw:
        name = normalise_name(heading)
        active.append(name)
        if not name:
            continue
        if name not in group.headings:
            group.headings.append(name)
            group.raw_headings.append(heading.strip().lstrip("*"))


def _row(group: AgsGroup, values: List[str], line: int, ags: AgsFile,
         headings: List[str]) -> Optional[Dict[str, str]]:
    if not headings:
        ags.warnings.append(f"line {line}: data before the headings of group {group.name} was skipped")
        return None
    extra = values[len(headings):]
    if any(v.strip() for v in extra):
        ags.warnings.append(f"line {line}: group {group.name} row has {len(values)} fields "
                            f"for {len(headings)} headings; the extra fields were ignored")
    padded = list(values[:len(headings)]) + [""] * max(0, len(headings) - len(values))
    return {h: v.strip() for h, v in zip(headings, padded) if h}


def _parse_ags4(text: str, ags: AgsFile) -> None:
    group: Optional[AgsGroup] = None
    headings: List[str] = []
    for line, fields, raw in _records(text):
        kind = fields[0].strip().upper()
        if kind in ("UNIT", "TYPE", "DATA") and group is not None:
            fields = _fit(fields, raw, len(headings) + 1, ags, group.name, line)
        values = fields[1:]
        if kind == "GROUP":
            group = _add_group(ags, values[0] if values else "", line)
            headings = []
        elif group is None:
            ags.warnings.append(f"line {line}: '{fields[0]}' before the first GROUP was skipped")
        elif kind == "HEADING":
            headings = []
            _set_headings(group, values, headings)
            named = [h for h in headings if h]
            if len(set(named)) != len(named):
                ags.warnings.append(f"line {line}: group {group.name} repeats a heading; "
                                    "the last column of that name is used")
        elif kind in ("UNIT", "TYPE"):
            target = group.units if kind == "UNIT" else group.types
            for heading, value in zip(headings, values):
                if heading:
                    target[heading] = value.strip()
        elif kind == "DATA":
            row = _row(group, values, line, ags, headings)
            if row is not None:
                group.rows.append(row)
                group.row_lines.append(line)
        else:
            ags.warnings.append(f"line {line}: unknown descriptor '{fields[0]}' was skipped")


def _parse_ags3(text: str, ags: AgsFile) -> None:
    group: Optional[AgsGroup] = None
    headings: List[str] = []
    expect_headings = False
    units_next = 0          # index of the next unit when a <UNITS> line continues
    for line, fields, raw in _records(text):
        first = fields[0].strip()
        non_empty = [f for f in fields if f.strip()]
        if first.startswith("**") and len(non_empty) == 1:
            group = _add_group(ags, first, line)
            headings = []
            expect_headings = True
            units_next = 0
            continue
        if group is None:
            ags.warnings.append(f"line {line}: data before the first **GROUP was skipped")
            continue
        if expect_headings and non_empty and all(f.strip().startswith("*") for f in non_empty):
            # Headings may continue over several lines (240-character rule).
            _set_headings(group, non_empty, headings)
            continue
        expect_headings = False
        marker = first.upper()
        # A line ending in a comma (outside quotes) continues on the next line.
        continued = raw.rstrip().endswith(",")
        if marker == "<UNITS>":
            values = fields[1:-1] if continued else fields[1:]
            units_next = 1 + len(values) if continued else 0
            for heading, value in zip(headings[1:], values):
                group.units[heading] = value.strip()
            continue
        if units_next and not group.rows and marker != "<CONT>":
            more = continued
            values = fields[:-1] if more else list(fields)
            for heading, value in zip(headings[units_next:], values):
                group.units[heading] = value.strip()
            units_next = units_next + len(values) if more else 0
            continue
        fields = _fit(fields, raw, len(headings), ags, group.name, line)
        if marker == "<CONT>":
            if not group.rows:
                ags.warnings.append(f"line {line}: <CONT> without a previous row in {group.name}")
                continue
            previous = group.rows[-1]
            for heading, value in zip(headings[1:], fields[1:]):
                if heading and value.strip():
                    previous[heading] = (previous.get(heading, "") + value).strip()
            continue
        row = _row(group, fields, line, ags, headings)
        if row is not None:
            group.rows.append(row)
            group.row_lines.append(line)


# -- values ---------------------------------------------------------------------
def number(text) -> Optional[float]:
    """A plain decimal or scientific number, else None (blank, text, '<0.1')."""
    value = str(text if text is not None else "").strip()
    if not value or not _NUMBER.match(value):
        return None
    result = float(value)
    return result if result == result and abs(result) != float("inf") else None
