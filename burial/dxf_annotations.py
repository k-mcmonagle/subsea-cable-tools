# -*- coding: utf-8 -*-
"""Plain DXF annotations: presentation only, never ground-model evidence."""

import bisect
import json
import math
import re
from dataclasses import asdict

from . import schema

PARAMS_KEY = "subbottom_annotations"
MAX_LABELS = 20_000
MAX_TEXT_CHARS = 4096
MAX_TOTAL_TEXT = 2_000_000
MAX_GROUPS = 256


def number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite annotation coordinate")
    return value


def plain_text(raw, multiline=False):
    """Decode common CAD escapes without evaluating fields or rich text.

    Unknown formatting is reported, rather than deleting arbitrary wording.
    Literal angle brackets remain literal (the renderer uses setPlainText).
    """
    if len(raw) > MAX_TEXT_CHARS:
        raise ValueError("annotation exceeds the text-length safety budget")
    if "%<" in raw:
        raise ValueError("dynamic CAD fields are unsupported")
    if "\ufffd" in raw:
        raise ValueError("annotation encoding could not be decoded")
    raw = re.sub(r"%%([dpc])", lambda m: {"d": "°", "p": "±", "c": "Ø"}[m[1].lower()], raw, flags=re.I)
    raw = re.sub(r"%%[ou]", "", raw, flags=re.I)
    out, i = [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            code = raw[i + 1]
            if raw[i:i + 3] == "\\U+" and re.fullmatch(r"[0-9a-fA-F]{4}", raw[i + 3:i + 7]):
                out.append(chr(int(raw[i + 3:i + 7], 16)))
                i += 7
                continue
            if code in "\\{}":
                out.append(code)
                i += 2
                continue
            if multiline:
                if code in "P~":
                    out.append("\n" if code == "P" else " ")
                    i += 2
                    continue
                if code in "LlOoKk":
                    i += 2
                    continue
                if code in "fFHCcAWTQp":
                    end = raw.find(";", i + 2)
                    if end < 0:
                        raise ValueError("incomplete MTEXT formatting")
                    i = end + 1
                    continue
                raise ValueError(f"unsupported MTEXT escape \\{code}")
        if not (multiline and ch in "{}"):
            out.append(ch)
        i += 1
    text = "".join(out).strip()
    # Join Unicode surrogate escape pairs and reject incomplete pairs.
    try:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError as exc:
        raise ValueError("invalid Unicode annotation") from exc
    if not text or any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise ValueError("empty annotation or unsupported control characters")
    return text


def read_entity(kind, attrs):
    """Model-space, planar TEXT/MTEXT. CAD rotation/fonts are not reproduced."""
    values = dict(attrs)
    if int(values.get(67, "0")) == 1:
        return None
    if any(number(values.get(c, default)) != default for c, default in ((210, 0), (220, 0), (230, 1))):
        raise ValueError("rotated object coordinates")
    if any(number(values.get(c, 0)) != 0 for c in (30, 31, 39)):
        raise ValueError("3D annotation geometry")
    if kind == "TEXT":
        if int(values.get(71, 0)) != 0:
            raise ValueError("mirrored text is unsupported")
        horizontal, vertical = int(values.get(72, 0)), int(values.get(73, 0))
        if (horizontal not in (0, 1, 2, 4) or vertical not in (0, 1, 2, 3)
                or (horizontal == 4 and vertical != 0)):
            raise ValueError("fitted/aligned text is unsupported")
        xcode, ycode = (11, 21) if horizontal or vertical else (10, 20)
        anchor = ([0.5, 0.5] if horizontal == 4 else
                  [{0: 0, 1: 0.5, 2: 1}[horizontal], {0: 1, 1: 1, 2: 0.5, 3: 0}[vertical]])
        raw = values.get(1, "")
    else:
        attachment = int(values.get(71, 1))
        if attachment not in range(1, 10):
            raise ValueError("invalid MTEXT attachment point")
        if int(values.get(75, 0)) != 0:
            raise ValueError("multi-column MTEXT is unsupported")
        xcode, ycode = 10, 20
        anchor = [((attachment - 1) % 3) / 2, ((attachment - 1) // 3) / 2]
        raw = "".join(v for c, v in attrs if c in (1, 3))
    return {"point": [number(values[xcode]), number(values[ycode])],
            "text": plain_text(raw, kind == "MTEXT"), "anchor": anchor,
            "raw_text": raw, "handle": values.get(5, ""), "kind": kind}


def build(drawing, selected, options, kp_map, reference=None, source=None, progress=None):
    """Place labels at converted anchor points. Never extrapolate seabed data."""
    refs = sorted(reference or [], key=lambda s: s[0][0]) if options.reference == "datum" else []
    for a, b in zip(refs, refs[1:]):
        if b[0][0] < a[-1][0] or (b[0][0] == a[-1][0] and abs(b[0][1] - a[-1][1]) > 1e-6):
            raise ValueError("Seabed reference segments overlap or disagree; choose one interpretation.")
    starts = [s[0][0] for s in refs]
    xs = [[p[0] for p in s] for s in refs]
    groups, warnings = [], []
    total = sum(len(drawing.get("annotations", {}).get(name, [])) for name in selected)
    done = 0
    for name in selected:
        labels, missing = [], 0
        for entity in drawing.get("annotations", {}).get(name, []):
            if progress and done % 128 == 0:
                progress(100 * done / max(1, total), "Placing annotations…")
            done += 1
            kp, depth = options.transform_point(entity["point"])
            kp = kp_map.map_kp(kp)[0]
            if options.reference == "datum":
                i = bisect.bisect_right(starts, kp) - 1
                if i < 0 or kp > refs[i][-1][0]:
                    missing += 1
                    continue
                segment, x = refs[i], xs[i]
                j = min(max(0, bisect.bisect_right(x, kp) - 1), len(x) - 2)
                a, b = segment[j], segment[j + 1]
                depth -= a[1] + (b[1] - a[1]) * (kp - a[0]) / (b[0] - a[0])
            labels.append(dict(entity, kp=kp, depth=depth))
        if missing:
            warnings.append(f"{name}: {missing} annotation(s) omitted where no seabed reference exists.")
        if labels:
            groups.append(dict(id=schema.new_id(), name=name, colour="#444444", visible=True, labels=labels,
                               source=dict(source or {}, options=asdict(options), kp_map=kp_map.to_dict())))
    validate(groups)
    if progress:
        progress(100, "Annotations ready")
    return groups, warnings


def validate(groups):
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise ValueError("Invalid annotation groups or annotation layer limit exceeded.")
    count = chars = 0
    ids = set()
    for group in groups:
        if (not isinstance(group, dict) or not isinstance(group.get("id"), str)
                or not group["id"] or group["id"] in ids or not isinstance(group.get("name"), str)
                or not group["name"].strip() or not isinstance(group.get("labels"), list)
                or not group["labels"] or not isinstance(group.get("source", {}), dict)):
            raise ValueError("Invalid saved annotation group.")
        ids.add(group["id"])
        count += len(group["labels"])
        for label in group["labels"]:
            if not isinstance(label, dict) or not isinstance(label.get("text"), str) or not label["text"].strip():
                raise ValueError("Invalid saved annotation text.")
            if len(label["text"]) > MAX_TEXT_CHARS or len(label.get("raw_text", "")) > MAX_TEXT_CHARS:
                raise ValueError("Saved annotation exceeds the text-length safety budget.")
            chars += len(label["text"]) + len(label.get("raw_text", ""))
            for key in ("kp", "depth"):
                if number(label[key]) != label[key]:
                    raise ValueError("Saved annotation coordinates must be numeric.")
            anchor = label.get("anchor", [0, 1])
            if len(anchor) != 2 or any(number(a) != a or a not in (0, 0.5, 1) for a in anchor):
                raise ValueError("Invalid annotation anchor.")
        if count > MAX_LABELS or chars > MAX_TOTAL_TEXT * 2:
            raise ValueError("Saved annotations exceed the plan memory budget.")
    json.dumps(groups, allow_nan=False)


def plan_state(plan):
    """Separate optional key: older horizon editors preserve these annotations."""
    try:
        params = json.loads(plan.get("params_json") or "{}")
        state = params.get(PARAMS_KEY, {"version": 1, "groups": [], "visible": True})
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Unsupported saved annotation format.")
        validate(state["groups"])
        if not isinstance(state.get("visible", True), bool):
            raise ValueError("Invalid annotation visibility setting.")
        return state
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("Invalid saved annotation data.") from exc
