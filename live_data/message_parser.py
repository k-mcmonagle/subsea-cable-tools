"""Live Data Message Parsing

Provides parsing of inbound *strings* into dicts of fields.

The live data tool is designed to be "string-first": TCP reception yields raw
lines which are then parsed using a user-configurable message format.

Supported formats:
- csv_header: First non-empty line is a CSV header row; subsequent lines are data rows.
- csv_fixed: CSV rows with a fixed, user-defined column list.
- kv: Key/value pairs like "lat=... , lon=...".
- json: JSON object per line.
- regex: Regex with named capture groups.

All formats produce:
- headers: list[str] (may be discovered on first message and may grow)
- values: dict[str, str] parsed values (strings as received)

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import csv
import json
import re


FORMAT_CSV_HEADER = "csv_header"
FORMAT_CSV_FIXED = "csv_fixed"
FORMAT_KV = "kv"
FORMAT_JSON = "json"
FORMAT_REGEX = "regex"

SUPPORTED_FORMATS = [
    FORMAT_CSV_HEADER,
    FORMAT_CSV_FIXED,
    FORMAT_KV,
    FORMAT_JSON,
    FORMAT_REGEX,
]


@dataclass
class MessageFormatConfig:
    kind: str = FORMAT_CSV_HEADER

    # CSV
    csv_delimiter: str = ","
    csv_quotechar: str = '"'
    csv_fixed_headers: List[str] = field(default_factory=list)

    # KV
    kv_pair_delimiter: str = ","
    kv_kv_delimiter: str = "="
    kv_strip_whitespace: bool = True

    # JSON
    json_require_object: bool = True

    # REGEX
    regex_pattern: str = ""
    regex_flags: int = 0


@dataclass
class ParserState:
    headers: List[str] = field(default_factory=list)
    _regex: Optional[re.Pattern] = None


class MessageParseError(Exception):
    pass


def _split_and_strip(s: str, delim: str) -> List[str]:
    parts = s.split(delim)
    return [p.strip() for p in parts if p.strip()]


def _csv_parse_row(line: str, delimiter: str, quotechar: str) -> List[str]:
    reader = csv.reader([line], delimiter=delimiter, quotechar=quotechar)
    return next(reader)


def _ensure_unique(headers: List[str]) -> List[str]:
    # Avoid duplicates (QGIS field names must be unique)
    seen: Dict[str, int] = {}
    result: List[str] = []
    for h in headers:
        base = h
        if base in seen:
            seen[base] += 1
            h = f"{base}_{seen[base]}"
        else:
            seen[base] = 0
        result.append(h)
    return result


def parse_line(
    line: str,
    config: MessageFormatConfig,
    state: ParserState,
) -> Tuple[Optional[Dict[str, str]], Optional[List[str]]]:
    """Parse one line.

    Returns:
        (values_dict_or_none, new_headers_or_none)

    Notes:
        - For `csv_header`, the first line updates headers and returns (None, headers).
        - For other formats, headers are discovered on first successful parse.
    """

    if not line or not line.strip():
        return None, None

    kind = config.kind or FORMAT_CSV_HEADER

    if kind == FORMAT_CSV_HEADER:
        if not state.headers:
            headers = _csv_parse_row(line, config.csv_delimiter, config.csv_quotechar)
            headers = [h.strip() for h in headers if h is not None]
            headers = _ensure_unique([h if h else "field" for h in headers])
            state.headers = headers
            return None, list(state.headers)

        row = _csv_parse_row(line, config.csv_delimiter, config.csv_quotechar)
        # Pad/truncate to header length
        if len(row) < len(state.headers):
            row = list(row) + [""] * (len(state.headers) - len(row))
        if len(row) > len(state.headers):
            row = list(row[: len(state.headers)])

        return dict(zip(state.headers, [str(v) for v in row])), None

    if kind == FORMAT_CSV_FIXED:
        headers = config.csv_fixed_headers or []
        if not headers:
            raise MessageParseError("CSV fixed format requires a non-empty column list")

        if not state.headers:
            state.headers = _ensure_unique([h.strip() for h in headers if h is not None and h.strip()])
            return None, list(state.headers)

        row = _csv_parse_row(line, config.csv_delimiter, config.csv_quotechar)
        if len(row) < len(state.headers):
            row = list(row) + [""] * (len(state.headers) - len(row))
        if len(row) > len(state.headers):
            row = list(row[: len(state.headers)])

        return dict(zip(state.headers, [str(v) for v in row])), None

    if kind == FORMAT_KV:
        pairs = _split_and_strip(line, config.kv_pair_delimiter)
        if not pairs:
            return None, None

        result: Dict[str, str] = {}
        for pair in pairs:
            if config.kv_kv_delimiter not in pair:
                # Ignore malformed tokens
                continue
            key, value = pair.split(config.kv_kv_delimiter, 1)
            if config.kv_strip_whitespace:
                key = key.strip()
                value = value.strip()
            if key:
                result[str(key)] = str(value)

        if not result:
            return None, None

        new_headers = None
        if not state.headers:
            state.headers = _ensure_unique(list(result.keys()))
            new_headers = list(state.headers)
        else:
            # If new keys appear, extend headers (so cards/plots can see them)
            added = [k for k in result.keys() if k not in state.headers]
            if added:
                state.headers.extend(_ensure_unique(added))
                new_headers = list(state.headers)

        return result, new_headers

    if kind == FORMAT_JSON:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise MessageParseError(f"Invalid JSON: {e}")

        if config.json_require_object and not isinstance(obj, dict):
            raise MessageParseError("JSON line must be an object (dictionary)")

        if isinstance(obj, dict):
            result = {str(k): "" if v is None else str(v) for k, v in obj.items()}
        else:
            # If allow non-object, store as value
            result = {"value": "" if obj is None else str(obj)}

        new_headers = None
        if not state.headers:
            state.headers = _ensure_unique(list(result.keys()))
            new_headers = list(state.headers)
        else:
            added = [k for k in result.keys() if k not in state.headers]
            if added:
                state.headers.extend(_ensure_unique(added))
                new_headers = list(state.headers)

        return result, new_headers

    if kind == FORMAT_REGEX:
        if not config.regex_pattern:
            raise MessageParseError("Regex format requires a pattern")
        if state._regex is None:
            state._regex = re.compile(config.regex_pattern, config.regex_flags)

        m = state._regex.search(line)
        if not m:
            return None, None

        result = {k: "" if v is None else str(v) for k, v in m.groupdict().items()}
        if not result:
            return None, None

        new_headers = None
        if not state.headers:
            state.headers = _ensure_unique(list(result.keys()))
            new_headers = list(state.headers)
        else:
            added = [k for k in result.keys() if k not in state.headers]
            if added:
                state.headers.extend(_ensure_unique(added))
                new_headers = list(state.headers)

        return result, new_headers

    raise MessageParseError(f"Unsupported message format: {kind}")
