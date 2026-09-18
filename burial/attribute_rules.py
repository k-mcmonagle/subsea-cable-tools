# -*- coding: utf-8 -*-
"""Attribute conditions shared by exclusion rules and risk checks.

Pure python (no QGIS imports). One vocabulary of *condition* dicts is used
wherever a feature attribute decides something — which polygons of a soils
layer count as an exclusion, or which risk level a scanned feature gets:

- ``{"match": "ROCK"}`` — exact value match (case-insensitive text);
- ``{"min": 0, "max": 5, "min_inclusive": true, "max_inclusive": false}``
  — numeric range; either side may be omitted (open). The inclusive flags
  default to *true* when absent so rules stored by older plugin versions
  (``a-b`` = both ends inclusive) keep their meaning; the editor writes
  the flags explicitly so bins like ``0 ≤ x < 5`` / ``5 ≤ x < 10`` are
  unambiguous;
- ``{"expression": "\"Height_m\" > 2 AND \"Class\" = 'ROCK'"}`` — a QGIS
  expression over the whole feature. Expressions need the QGIS API, so
  the QGIS-side code evaluates them once per feature and hands the result
  in as ``expression_hits`` (aligned with :func:`expression_texts`).

Risk rules carry an extra ``"risk"`` key; exclusion match rules do not.
Older plugin versions ignore keys they do not know: a range rule keeps
working as an inclusive range and an expression rule simply never fires.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

KIND_VALUE = "value"
KIND_RANGE = "range"
KIND_EXPRESSION = "expression"
KINDS = [KIND_VALUE, KIND_RANGE, KIND_EXPRESSION]
KIND_LABELS = {
    KIND_VALUE: "Value equals",
    KIND_RANGE: "Number in range",
    KIND_EXPRESSION: "QGIS expression",
}

_EPS = 1e-12


def is_null(value) -> bool:
    """True for python None and for a null QVariant (QGIS 3 attribute)."""
    if value is None:
        return True
    is_null_method = getattr(value, "isNull", None)
    if callable(is_null_method):
        try:
            return bool(is_null_method())
        except Exception:
            return False
    return False


def to_number(value) -> Optional[float]:
    """Attribute value -> float, or None when it is null or not numeric."""
    if is_null(value):
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            number = float(str(value).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None
    if number != number:  # NaN
        return None
    return number


def rule_kind(rule: Dict) -> str:
    if not isinstance(rule, dict):
        return ""
    if "expression" in rule:
        return KIND_EXPRESSION
    if "match" in rule:
        return KIND_VALUE
    if rule.get("min") is not None or rule.get("max") is not None:
        return KIND_RANGE
    return ""


def rule_matches(rule: Dict, value, expression_hit: Optional[bool] = None
                 ) -> bool:
    """Does one condition fire for this attribute value?

    ``expression_hit`` is the pre-evaluated result for an expression rule
    (None = not evaluated -> never fires).
    """
    kind = rule_kind(rule)
    if kind == KIND_EXPRESSION:
        return bool(expression_hit)
    if kind == KIND_VALUE:
        if is_null(value):
            return False
        wanted = str(rule.get("match") or "").strip()
        if str(value).strip().casefold() == wanted.casefold():
            return True
        # "7" should match a numeric field holding 7.0: compare as numbers
        # when both sides are numeric.
        wanted_number = to_number(wanted)
        number = to_number(value) if wanted_number is not None else None
        return number is not None and abs(number - wanted_number) <= _EPS
    if kind != KIND_RANGE:
        return False
    number = to_number(value)
    if number is None:
        return False
    minimum = rule.get("min")
    maximum = rule.get("max")
    if minimum is not None:
        low = float(minimum)
        if rule.get("min_inclusive", True):
            if number < low - _EPS:
                return False
        elif number <= low + _EPS:
            return False
    if maximum is not None:
        high = float(maximum)
        if rule.get("max_inclusive", True):
            if number > high + _EPS:
                return False
        elif number >= high - _EPS:
            return False
    return True


def expression_texts(rules: Sequence[Dict]) -> List[str]:
    """The expression rules' texts, in rule order (drives expression_hits)."""
    return [str(rule.get("expression") or "") for rule in rules
            if isinstance(rule, dict) and rule_kind(rule) == KIND_EXPRESSION]


def first_matching_rule(rules: Sequence[Dict], value,
                        expression_hits: Optional[Sequence[bool]] = None,
                        has_value: bool = True) -> Optional[Dict]:
    """First rule that fires, or None. ``has_value`` False means the
    feature has no such attribute — only expression rules can then fire."""
    expression_index = 0
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        kind = rule_kind(rule)
        if kind == KIND_EXPRESSION:
            hit = None
            if expression_hits is not None \
                    and expression_index < len(expression_hits):
                hit = expression_hits[expression_index]
            expression_index += 1
            if rule_matches(rule, None, hit):
                return rule
            continue
        if not has_value:
            continue
        if rule_matches(rule, value):
            return rule
    return None


def any_rule_matches(rules: Sequence[Dict], value,
                     expression_hits: Optional[Sequence[bool]] = None,
                     has_value: bool = True) -> bool:
    return first_matching_rule(rules, value, expression_hits, has_value) \
        is not None


def validate_rule(rule: Dict) -> Optional[str]:
    """None when the rule is well formed, else a short problem text."""
    kind = rule_kind(rule)
    if kind == KIND_VALUE:
        if not str(rule.get("match") or "").strip():
            return "value is blank"
        return None
    if kind == KIND_EXPRESSION:
        if not str(rule.get("expression") or "").strip():
            return "expression is blank"
        return None
    if kind == KIND_RANGE:
        low = to_number(rule.get("min")) if rule.get("min") is not None else None
        high = to_number(rule.get("max")) if rule.get("max") is not None else None
        if rule.get("min") is not None and low is None:
            return "lower bound is not a number"
        if rule.get("max") is not None and high is None:
            return "upper bound is not a number"
        if low is not None and high is not None and low > high:
            return "lower bound is above the upper bound"
        return None
    return "no value, range or expression"


def _num(value) -> str:
    return f"{float(value):g}"


def describe_rule(rule: Dict, attribute: str = "") -> str:
    """Human-readable condition, e.g. ``0 ≤ Height_m < 5``."""
    name = (attribute or "value").strip() or "value"
    kind = rule_kind(rule)
    if kind == KIND_VALUE:
        return f"{name} = {rule.get('match')}"
    if kind == KIND_EXPRESSION:
        return f"expression: {rule.get('expression')}"
    if kind == KIND_RANGE:
        low = rule.get("min")
        high = rule.get("max")
        parts: List[str] = []
        if low is not None:
            parts.append(f"{_num(low)} "
                         f"{'≤' if rule.get('min_inclusive', True) else '<'}")
        parts.append(name)
        if high is not None:
            parts.append(f"{'≤' if rule.get('max_inclusive', True) else '<'} "
                         f"{_num(high)}")
        return " ".join(parts)
    return "(invalid rule)"


def polygon_match_rules(config: Dict) -> List[Dict]:
    """The polygon-class rule's conditions as one rule list.

    ``match_values`` (the original comma list of exact values) and the
    newer ``match_rules`` (numeric ranges) both count — a polygon matches
    when *any* condition fires.
    """
    rules: List[Dict] = [{"match": str(value)}
                         for value in (config.get("match_values") or [])
                         if str(value).strip()]
    for rule in config.get("match_rules") or []:
        if isinstance(rule, dict) and rule_kind(rule):
            rules.append(rule)
    return rules
