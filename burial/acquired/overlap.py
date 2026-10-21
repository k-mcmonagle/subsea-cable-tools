# -*- coding: utf-8 -*-
"""Overlapping deliveries within a dataset: found on import, resolved by a recorded rule.

Two current files of one dataset overlap when their time ranges meet: the
window runs from the later first record to the earlier last record, and
both files must have records in it (a file ending at 23:59:59 and the next
starting at 00:00:00 do not overlap). "Older" and "newer" are the order the
files were delivered (imported), not the times they hold.

Each overlap carries a rule, chosen at import and changeable later:

* ``newer``    the newer delivery's records win in the window (the default);
* ``existing`` the older delivery's records win in the window;
* ``split``    the older wins before the split time, the newer from it on;
* ``both``     both are kept (e.g. two real passes logged by two systems).

:func:`keep_masks` turns the files and their overlap records into one keep
mask per file, the same every time for the same records. Records without a
time are never affected. Nothing is deleted: a record a rule leaves out is
still in its file and can be shown as replaced data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

RULE_NEWER, RULE_EXISTING, RULE_SPLIT, RULE_BOTH = "newer", "existing", "split", "both"
RULES: Dict[str, str] = {
    RULE_NEWER: "Newer delivery wins",
    RULE_EXISTING: "Keep existing",
    RULE_SPLIT: "Split at a time",
    RULE_BOTH: "Keep both as separate passes",
}


@dataclass
class FileTimes:
    """What the rules need of a file: its identity, delivery order and record times."""
    file_id: str
    seq: int
    time: np.ndarray
    name: str = ""

    def span(self):
        finite = self.time[np.isfinite(self.time)]
        if not finite.size:
            return None
        return float(finite.min()), float(finite.max())


@dataclass
class Overlap:
    older_id: str
    newer_id: str
    t0: float
    t1: float
    older_count: int
    newer_count: int
    rule: str = RULE_NEWER
    split_utc: Optional[float] = None
    overlap_id: str = ""
    reason: str = ""
    extra: Dict = field(default_factory=dict)

    def problems(self) -> List[str]:
        out = []
        if self.rule not in RULES:
            out.append(f"'{self.rule}' is not an overlap rule.")
        if self.rule == RULE_SPLIT:
            if self.split_utc is None or not self.t0 <= self.split_utc <= self.t1:
                out.append("The split time must lie within the overlap.")
        return out


def _within(time: np.ndarray, t0: float, t1: float) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return (time >= t0) & (time <= t1)


def find_overlaps(existing: Sequence[FileTimes], new: FileTimes) -> List[Overlap]:
    """Overlaps of ``new`` with each current file of the dataset (default rule: newer wins)."""
    found: List[Overlap] = []
    span = new.span()
    if span is None:
        return found
    for other in sorted(existing, key=lambda f: f.seq):
        other_span = other.span()
        if other_span is None or other.file_id == new.file_id:
            continue
        t0, t1 = max(span[0], other_span[0]), min(span[1], other_span[1])
        if t0 > t1:
            continue
        older_count = int(_within(other.time, t0, t1).sum())
        newer_count = int(_within(new.time, t0, t1).sum())
        if older_count and newer_count:
            found.append(Overlap(other.file_id, new.file_id, t0, t1, older_count, newer_count))
    return found


def keep_masks(files: Sequence[FileTimes], overlaps: Sequence[Overlap]) -> Dict[str, np.ndarray]:
    """One boolean keep mask per file id, after every overlap's rule (delivery order)."""
    masks = {f.file_id: np.ones(f.time.size, dtype=bool) for f in files}
    by_id = {f.file_id: f for f in files}
    seq = {f.file_id: f.seq for f in files}
    usable = [o for o in overlaps if o.older_id in by_id and o.newer_id in by_id and not o.problems()]
    for o in sorted(usable, key=lambda o: (seq[o.newer_id], seq[o.older_id])):
        older, newer = by_id[o.older_id], by_id[o.newer_id]
        in_old, in_new = _within(older.time, o.t0, o.t1), _within(newer.time, o.t0, o.t1)
        if o.rule == RULE_NEWER:
            masks[o.older_id] &= ~in_old
        elif o.rule == RULE_EXISTING:
            masks[o.newer_id] &= ~in_new
        elif o.rule == RULE_SPLIT:
            with np.errstate(invalid="ignore"):
                masks[o.older_id] &= ~(in_old & (older.time >= o.split_utc))
                masks[o.newer_id] &= ~(in_new & (newer.time < o.split_utc))
    return masks


def describe(o: Overlap, older_name: str, newer_name: str, utc_text) -> str:
    """One line for the review step, e.g. "1,204 records 23:40–00:20 overlap Day_14.csv (newer wins)"."""
    rule = RULES.get(o.rule, o.rule).lower()
    return (f"{o.newer_count:,} records {utc_text(o.t0)}–{utc_text(o.t1)} overlap {older_name} "
            f"({o.older_count:,} there; {rule})")
