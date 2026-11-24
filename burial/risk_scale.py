# -*- coding: utf-8 -*-
"""Scored risk levels belonging to one burial plan."""

from __future__ import annotations

import json
import re

from . import schema


DEFAULT_COLORS = {"low": "#e0b000", "medium": "#ff8c00", "high": "#d62728"}


class RiskScale:
    """Stable level IDs preserve assignments when labels, scores or colours change."""

    def __init__(self, rows):
        self.labels = {"": schema.RISK_LABELS[""]}
        self.scores = {}
        self.colors = {"": "#909090"}
        seen_names = {self.labels[""].casefold()}
        for row in rows:
            key, label = row["id"], row["label"].strip()
            score, color = row["score"], row["color"]
            if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", key) or key in self.labels:
                raise ValueError("Each level needs a unique ID.")
            if not label or label.casefold() in seen_names:
                raise ValueError("Give each level a distinct name. Unassigned is reserved.")
            if type(score) is not int or not 0 <= score <= 2147483647 or score in self.scores.values():
                raise ValueError("Give each level a distinct whole-number score from 0 to 2147483647.")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError("Choose a valid colour for each level.")
            self.labels[key], self.scores[key], self.colors[key] = label, score, color.lower()
            seen_names.add(label.casefold())
        if not self.scores:
            raise ValueError("Add at least one scored level.")
        self.levels = tuple(sorted(self.scores, key=self.scores.get))

    def rows(self):
        return [dict(id=key, label=self.labels[key], score=self.scores[key], color=self.colors[key])
                for key in self.levels]

    def label(self, key):
        return self.labels.get(key or "", key)

    def caption(self, key):
        if key in self.scores and self is not DEFAULT_SCALE:
            return f"{self.scores[key]} · {self.label(key)}"
        return self.label(key)

    def rank(self, key):
        return self.scores.get(key, -1)

    @classmethod
    def from_settings(cls, settings):
        rows = settings.get("risk_scale")
        if rows:
            try:
                scale = cls(rows)
            except (AttributeError, KeyError, TypeError, ValueError):
                return DEFAULT_SCALE
            if scale.rows() != DEFAULT_SCALE.rows():
                return scale
        return DEFAULT_SCALE

    @classmethod
    def from_plan(cls, plan):
        try:
            settings = json.loads((plan or {}).get("params_json") or "{}")
        except (TypeError, ValueError):
            settings = {}
        return cls.from_settings(settings if isinstance(settings, dict) else {})


DEFAULT_SCALE = RiskScale(dict(id=k, label=schema.RISK_LABELS[k], score=schema.RISK_ORDER[k],
                               color=DEFAULT_COLORS[k]) for k in schema.RISK_LEVELS)
