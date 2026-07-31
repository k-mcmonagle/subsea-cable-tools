# -*- coding: utf-8 -*-
"""The BU integration: one description of the whole Y, datumed on the BU.

Pure Python + NumPy; no Qt/QGIS imports.

Real BU operations are planned from the integration straight-line diagram:
the branching unit in the middle, and each of the three lines (trunk, leg 1,
leg 2) specified **outward from the BU** — tail, tail joint, then the cable
beyond, with distances, joints and cable counts all measured from the BU.
This module is that diagram as a data structure. It is the single source the
BU scenarios draw their per-line assemblies, tracked joints and cable-count
references from, so the user states each fact once, against one datum.

The engine-side mechanics this compiles down to already exist:

* per-line assemblies ordered outward from the BU with a ``fill=True``
  remainder row (:func:`cable_system.resolve_assembly`);
* ``ChainState.assembly_datum`` — ``"top_end"`` for a line whose BU end is
  the chain's top (a hanging leg), ``"bottom_end"`` for the trunk;
* ``ChainState.joints`` — material coordinates in *metres from the chain's
  bottom end* (converted here from metres-from-BU);
* ``ChainState.count_ref_m`` / ``count_to_top`` — the count at the chain's
  bottom end (derived here from the count at the BU).

Users of this module never handle those conversions; they ask for
:meth:`BUIntegration.chain_kwargs` (one line) or
:meth:`BUIntegration.lowering_inputs` (the whole lowering scenario).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .cable_system import (
    AssemblyItem,
    BodySpec,
    SegmentSpec,
    assembly_to_json_data,
    parse_assembly,
)

BRANCHES = ("trunk", "leg1", "leg2")


@dataclass
class BranchMakeup:
    """One line of the Y, described outward from the branching unit.

    ``items`` is the assembly with its FIRST row at the BU: typically the BU
    tail segment, the tail joint (a body), then the main cable. Give exactly
    one segment ``fill=True`` so the far end absorbs whatever length the
    geometry demands while every entered feature keeps its distance from the
    BU.

    ``joints`` are extra tracked positions as (label, metres from the BU)
    — bodies in ``items`` are tracked automatically, so only list joints
    that are not physical bodies in the assembly (e.g. a soft marking).

    ``count_at_bu_m`` is the cable count (roto count) at the BU end of this
    line, from the jointing records; ``count_increases_from_bu`` says which
    way the count runs (True: the count grows moving away from the BU along
    this line). The count anywhere on the line, and at its far end, is
    derived — the laid-end count becomes a cross-check, not an input.
    """

    items: List[AssemblyItem] = field(default_factory=list)
    joints: List[Tuple[str, float]] = field(default_factory=list)
    count_at_bu_m: Optional[float] = None
    count_increases_from_bu: bool = True

    # -- structure ----------------------------------------------------------

    def segments(self) -> List[SegmentSpec]:
        return [it for it in self.items if isinstance(it, SegmentSpec)]

    def n_fill(self) -> int:
        return sum(1 for s in self.segments() if s.fill)

    def fixed_length_m(self) -> float:
        """Total length of the fixed (non-fill) rows — the distance from the
        BU to where the remainder row starts."""
        return float(sum(max(0.0, s.length_m)
                         for s in self.segments() if not s.fill))

    def body_positions_from_bu(self) -> List[Tuple[str, float]]:
        """(name, metres from the BU) for every body row, in entry order."""
        out: List[Tuple[str, float]] = []
        s = 0.0
        for it in self.items:
            if isinstance(it, SegmentSpec):
                if not it.fill:
                    s += max(0.0, it.length_m)
                else:
                    # Positions beyond a fill row depend on the resolved
                    # length; bodies after the fill are not supported (see
                    # validate()).
                    s = float("nan")
            else:
                out.append((it.name or "body", s))
        return out

    def joints_from_bu(self) -> List[Tuple[str, float]]:
        """All tracked positions as (label, metres from the BU): the body
        rows (automatic) plus the explicit ``joints`` entries."""
        out = [(n, p) for n, p in self.body_positions_from_bu()
               if p == p]                        # drop NaN (past a fill row)
        out.extend((str(l), float(p)) for l, p in self.joints)
        return out

    def count_at_from_bu(self, s_from_bu_m: float) -> Optional[float]:
        """Cable count at a distance from the BU along this line."""
        if self.count_at_bu_m is None:
            return None
        k = 1.0 if self.count_increases_from_bu else -1.0
        return float(self.count_at_bu_m) + k * float(s_from_bu_m)

    # -- validation ---------------------------------------------------------

    def problems(self, name: str) -> List[str]:
        """Human-readable validation problems (empty = usable)."""
        out: List[str] = []
        if not self.segments():
            out.append(f"{name}: no cable segments entered.")
        if self.n_fill() > 1:
            out.append(f"{name}: more than one remainder (fill) row — at "
                       "most one is allowed.")
        segs = self.segments()
        if segs and self.n_fill() and not segs[-1].fill:
            out.append(f"{name}: the remainder (fill) row must be the LAST "
                       "segment — everything is measured outward from the "
                       "BU, so only the far end can absorb spare length.")
        seen_fill = False
        for it in self.items:
            if isinstance(it, SegmentSpec) and it.fill:
                seen_fill = True
            elif seen_fill:
                out.append(f"{name}: rows after the remainder (fill) row "
                           "have no fixed distance from the BU — move "
                           f"'{getattr(it, 'name', '?')}' before it.")
                break
        fixed = self.fixed_length_m()
        for label, pos in self.joints:
            if pos < 0.0:
                out.append(f"{name}: joint '{label}' is at a negative "
                           "distance from the BU.")
        if not self.n_fill() and segs:
            out.append(f"{name}: no remainder (fill) row — beyond "
                       f"{fixed:.0f} m the end segment's properties would "
                       "be stretched. Add one 'rest of line' row.")
        return out

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "items": assembly_to_json_data(self.items),
            "joints": [[str(l), float(p)] for l, p in self.joints],
            "count_at_bu_m": (None if self.count_at_bu_m is None
                              else float(self.count_at_bu_m)),
            "count_increases_from_bu": bool(self.count_increases_from_bu),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BranchMakeup":
        count = d.get("count_at_bu_m")
        return cls(
            items=parse_assembly(d.get("items") or []),
            joints=[(str(j[0]), float(j[1])) for j in (d.get("joints") or [])],
            count_at_bu_m=None if count is None else float(count),
            count_increases_from_bu=bool(d.get("count_increases_from_bu", True)),
        )


def default_branch(*, tail_length_m: float = 90.0, tail_q_npm: float = 300.0,
                   joint_name: str = "tail joint", joint_kN: float = 1.0,
                   main_name: str = "Main cable", main_q_npm: float = 0.0,
                   ) -> BranchMakeup:
    """A nominal branch: tail, tail joint, then a remainder row for the rest
    of the line (blank weight = the scenario defaults)."""
    return BranchMakeup(items=[
        SegmentSpec(name="BU tail", length_m=float(tail_length_m),
                    q_water_npm=float(tail_q_npm)),
        BodySpec(name=joint_name, point_load_kN=float(joint_kN)),
        SegmentSpec(name=main_name, q_water_npm=float(main_q_npm), fill=True),
    ])


@dataclass
class BUIntegration:
    """The whole integration: the BU body plus its three branches, all
    referenced outward from the BU."""

    bu_weight_kN: float = 15.0
    bu_cda_m2: float = 1.0
    trunk: BranchMakeup = field(default_factory=BranchMakeup)
    leg1: BranchMakeup = field(default_factory=BranchMakeup)
    leg2: BranchMakeup = field(default_factory=BranchMakeup)

    def branch(self, name: str) -> BranchMakeup:
        if name not in BRANCHES:
            raise KeyError(f"unknown branch {name!r}; expected one of {BRANCHES}")
        return getattr(self, name)

    def problems(self) -> List[str]:
        out: List[str] = []
        for name in BRANCHES:
            out.extend(self.branch(name).problems(name))
        return out

    # -- compilation to engine terms ----------------------------------------

    def chain_kwargs(self, name: str, *, bu_at: str,
                     length_m: Optional[float] = None) -> dict:
        """ChainState keyword arguments for one line.

        ``bu_at`` is which end of the CHAIN the BU sits at: ``"top"`` for a
        line hanging from the BU (the legs during a lowering), ``"bottom"``
        for a line whose far end is the BU (the trunk, paid out from the
        vessel). ``length_m`` — the deployed length — is required when
        ``bu_at="top"``: joints and the count reference are stored against
        the chain's bottom end, so the conversion from metres-from-BU needs
        the length. For ``bu_at="bottom"`` the bottom end IS the BU and the
        conversion is the identity (stable under payout), so ``length_m``
        is not needed.
        """
        br = self.branch(name)
        if bu_at not in ("top", "bottom"):
            raise ValueError(f"bu_at must be 'top' or 'bottom' (got {bu_at!r})")
        joints_bu = br.joints_from_bu()
        k = 1.0 if br.count_increases_from_bu else -1.0
        if bu_at == "bottom":
            joints = [(l, p) for l, p in joints_bu]
            count_ref = br.count_at_bu_m
            count_to_top = br.count_increases_from_bu
        else:
            if length_m is None:
                raise ValueError(
                    f"chain_kwargs({name!r}, bu_at='top') needs length_m: "
                    "joints/counts are stored from the chain's bottom end.")
            L = float(length_m)
            joints = [(l, L - p) for l, p in joints_bu if p <= L + 1e-9]
            count_ref = (None if br.count_at_bu_m is None
                         else br.count_at_bu_m + k * L)
            # count = ref - k*s_from_bottom; count_to_top means "+".
            count_to_top = not br.count_increases_from_bu
        return {
            "assembly": list(br.items),
            "assembly_datum": "top_end" if bu_at == "top" else "bottom_end",
            "joints": joints,
            "count_ref_m": count_ref,
            "count_to_top": count_to_top,
        }

    def lowering_inputs(self, *, leg1_length_m: float,
                        leg2_length_m: Optional[float] = None) -> dict:
        """Keyword arguments for :func:`scenarios.bu_deployment` (the
        lowering scenario: legs hang from the BU, trunk pays out from the
        vessel).

        Returns assemblies, joints, count references and the BU body inputs;
        combine with the geometry/schedule arguments at the call site::

            scen.bu_deployment(bathy, defaults=defaults,
                               **integ.lowering_inputs(leg1_length_m=L1,
                                                       leg2_length_m=L2),
                               leg_azimuths_deg=..., ...)
        """
        L1 = float(leg1_length_m)
        L2 = float(leg1_length_m if leg2_length_m is None else leg2_length_m)
        kw1 = self.chain_kwargs("leg1", bu_at="top", length_m=L1)
        kw2 = self.chain_kwargs("leg2", bu_at="top", length_m=L2)
        kwt = self.chain_kwargs("trunk", bu_at="bottom")
        count_refs: Dict[str, float] = {}
        count_dirs: Dict[str, bool] = {}
        for cname, kw in (("leg1", kw1), ("leg2", kw2), ("trunk", kwt)):
            if kw["count_ref_m"] is not None:
                count_refs[cname] = float(kw["count_ref_m"])
                count_dirs[cname] = bool(kw["count_to_top"])
        return {
            "bu_weight_kN": float(self.bu_weight_kN),
            "bu_cda_m2": float(self.bu_cda_m2),
            "leg_assembly": kw1["assembly"],
            "leg2_assembly": kw2["assembly"],
            "trunk_assembly": kwt["assembly"],
            "leg_length_m": L1,
            "leg_lengths_m": (L1, L2),
            "leg1_joints": kw1["joints"],
            "leg2_joints": kw2["joints"],
            "trunk_joints": kwt["joints"],
            "count_refs": count_refs,
            "count_dirs": count_dirs,
        }

    # -- reporting helpers ---------------------------------------------------

    def count_at(self, name: str, s_from_bu_m: float) -> Optional[float]:
        """Cable count at (branch, metres from BU) — the report-side datum."""
        return self.branch(name).count_at_from_bu(s_from_bu_m)

    def far_end_count(self, name: str, length_m: float) -> Optional[float]:
        """The derived count at a line's far end (laid end for a leg) —
        for cross-checking against the as-laid records."""
        return self.count_at(name, float(length_m))

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "bu_weight_kN": float(self.bu_weight_kN),
            "bu_cda_m2": float(self.bu_cda_m2),
            "trunk": self.trunk.to_dict(),
            "leg1": self.leg1.to_dict(),
            "leg2": self.leg2.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BUIntegration":
        return cls(
            bu_weight_kN=float(d.get("bu_weight_kN", 15.0)),
            bu_cda_m2=float(d.get("bu_cda_m2", 1.0)),
            trunk=BranchMakeup.from_dict(d.get("trunk") or {}),
            leg1=BranchMakeup.from_dict(d.get("leg1") or {}),
            leg2=BranchMakeup.from_dict(d.get("leg2") or {}),
        )


def default_integration(*, bu_weight_kN: float = 15.0, bu_cda_m2: float = 1.0,
                        tail_length_m: float = 90.0,
                        tail_q_npm: float = 300.0) -> BUIntegration:
    """A nominal integration with identical tails on all three branches."""
    return BUIntegration(
        bu_weight_kN=float(bu_weight_kN),
        bu_cda_m2=float(bu_cda_m2),
        trunk=default_branch(tail_length_m=tail_length_m, tail_q_npm=tail_q_npm),
        leg1=default_branch(tail_length_m=tail_length_m, tail_q_npm=tail_q_npm),
        leg2=default_branch(tail_length_m=tail_length_m, tail_q_npm=tail_q_npm),
    )
