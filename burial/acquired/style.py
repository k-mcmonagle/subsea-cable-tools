# -*- coding: utf-8 -*-
"""Colours shared by the Assessment plot and the burial graph pages, so screen and paper match."""

from __future__ import annotations

from . import bins as B

DATASET_COLOURS = [(31, 119, 180), (255, 127, 14), (44, 160, 44), (148, 103, 189), (140, 86, 75),
                   (227, 119, 194), (127, 127, 127), (188, 189, 34), (23, 190, 207)]
GOVERNING = (13, 52, 120)
TARGET = (0, 128, 0)
BELOW_FILL = (211, 47, 47, 110)
NO_DATA_SOURCE = (220, 220, 220)
STATUS_LEVELS = [(B.STATUS_MEETS, (76, 175, 80), "Meets target"), (B.STATUS_BELOW, (211, 47, 47), "Below target"),
                 (B.STATUS_NO_DATA, (200, 200, 200), "No data"), (B.STATUS_NO_TARGET, (144, 202, 249), "No target")]
STATUS_COLOURS = {level: colour for level, colour, _name in STATUS_LEVELS}
FAINT_ALPHA = 110


def dataset_colour(index: int):
    return DATASET_COLOURS[index % len(DATASET_COLOURS)]


def faint(colour, alpha: int = FAINT_ALPHA):
    return (*colour[:3], alpha)
