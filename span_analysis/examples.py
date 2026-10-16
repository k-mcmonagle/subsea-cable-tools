# -*- coding: utf-8 -*-
"""Built-in examples: synthetic seabeds the user can load, run and change.

The first is a verification case with a closed-form answer (a flat seabed with
one depression, see :mod:`cable_seabed.verification`); the others show
typical features. Each is a full settings dictionary for
:meth:`span_analysis.setup_panel.SetupPanel.apply_settings`.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Tuple

from ..cable_seabed import planning as pl

G = 9.80665

_LW = {"w_kg_m": 10.0 / G, "ei_knm2": 1.0, "mbr_m": 1.5, "diameter_mm": 20.0, "npts_kn": 20.0}
_DA = {"w_kg_m": 60.0 / G, "ei_knm2": 20.0, "mbr_m": 2.0, "diameter_mm": 50.0, "npts_kn": 60.0}


def _base(length_m: float, spacing_m: float = 0.25, depth_m: float = 100.0, slope_deg: float = 0.0,
          wave_height_m: float = 0.0, wave_length_m: float = 50.0, roughness_m: float = 0.0) -> Dict:
    return {
        "version": 1,
        "route": {"source": "synthetic", "synthetic": {
            "length_m": length_m, "spacing_m": spacing_m, "start_kp": 0.0, "depth_m": depth_m,
            "slope_deg": slope_deg, "wave_height_m": wave_height_m, "wave_length_m": wave_length_m,
            "roughness_m": roughness_m, "roughness_length_m": 6.0, "seed": 1}},
        "seabed": {"filter_m": 0.0, "smoothing": "", "features": []},
        "cable": {"source": "manual", "manual": dict(_LW), "bodies": []},
        "friction": {"mu": pl.DEFAULT_MU, "mu_lat": pl.DEFAULT_MU, "zones_on": False, "zones": [], "table": []},
        "control": {"mode": pl.TENSION, "value": 2.0, "direction": 1, "sections": [], "live": True},
        "criteria": {"allowable": True},
    }


def depression() -> Dict:
    s = _base(1000.0)
    s["seabed"]["features"] = [{"kind": "depression", "kp": 0.5, "height": 2.0, "length": 40.0, "crest": 38.0,
                                "name": "Depression"}]
    s["control"]["value"] = 2.0
    return s


def boulder() -> Dict:
    s = _base(600.0)
    s["seabed"]["features"] = [{"kind": "boulder", "kp": 0.3, "height": 1.0, "length": 2.0, "crest": 0.0,
                                "name": "Boulder"}]
    s["control"]["value"] = 5.0
    return s


def crossing() -> Dict:
    s = _base(400.0, spacing_m=0.1)
    s["seabed"]["features"] = [{"kind": "pipe", "kp": 0.2, "height": 0.5, "length": 0.5, "crest": 0.0,
                                "name": "Pipeline crossing"}]
    s["cable"]["manual"] = dict(_DA)
    s["control"]["value"] = 10.0
    return s


def sand_waves() -> Dict:
    s = _base(3000.0, spacing_m=0.5, slope_deg=2.0, wave_height_m=1.2, wave_length_m=45.0, roughness_m=0.15)
    s["cable"]["bodies"] = [{"name": "R1", "kind": "Repeater", "kp": 2.1, "length": 4.0, "weight": 4.0,
                             "diameter": 300.0}]
    s["control"]["value"] = 2.0
    return s


EXAMPLES: List[Tuple[str, str, object]] = [
    ("Verification: flat seabed with a depression",
     "A 40 m wide, 2 m deep trough in a flat seabed, light cable (10 N/m). The Summary compares the model with "
     "the closed form. Move the tension: at 2 kN the sag (1 m) is less than the depth and the cable spans the "
     "trough; below 1 kN it touches the bottom and two shorter spans form.", depression),
    ("Boulder on a flat seabed",
     "A 1 m high boulder at 5 kN: a span either side, each sqrt(2 h T / w) long.", boulder),
    ("Pipeline crossing, armoured cable",
     "A 0.5 m pipeline lying on the seabed, armoured cable (60 N/m, EI 20 kN·m²) at 10 kN. Use Detail (EI) on a "
     "span for the bend radius at the crossing.", crossing),
    ("Sand waves on a slope, with a repeater",
     "3 km of 1.2 m sand waves on a 2° slope with roughness, light cable and one repeater.", sand_waves),
]


def example_settings(index: int) -> Dict:
    return copy.deepcopy(EXAMPLES[index][2]())
