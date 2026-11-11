"""Synthetic annotation layout tests, without survey data or QGIS."""

import itertools
import math

from ..processing.annotation_blocks import TextLine, assemble_blocks, estimate_line_gap


def assemble(lines, gap=12, anchors=(), reliable=True):
    pairs = [(a.key, b.key) for a, b in itertools.combinations(lines, 2)]
    return assemble_blocks(lines, pairs, gap, anchors, reliable)


def test_reading_order_and_independent_certainty():
    lines = [TextLine(3, 0, 0, 'third'), TextLine(1, 0, 20, 'first'), TextLine(2, 0, 10, 'second')]
    block = assemble(lines, anchors=[2])[0]
    assert block.text == 'first\nsecond\nthird'
    assert block.status == 'supported' and block.anchor == 1
    assert assemble(lines)[0].status == 'layout_only'
    assert assemble(lines[:1])[0].status == 'single'


def test_rotated_text_and_alignment_separation():
    lines = [TextLine(1, -10, 0, 'first', 90, 'left'), TextLine(2, 0, 0, 'second', 90, 'left'),
             TextLine(3, 10, 0, 'other', 90, 'right')]
    blocks = assemble(lines)
    assert [block.text for block in blocks] == ['first\nsecond', 'other']


def test_branching_columns_and_multiple_anchor_blocks_require_review():
    lines = [TextLine(1, 0, 10, 'a'), TextLine(2, 0, 0, 'b'), TextLine(3, 1, 0, 'c')]
    assert assemble(lines)[0].status == 'review'
    assert assemble(lines[:2], anchors=[1, 2])[0].status == 'review'


def test_duplicate_overprints_keep_ids_and_different_overprints_need_review():
    block = assemble([TextLine(1, 0, 0, 'same'), TextLine(2, 0, 0, 'Same')])[0]
    assert block.text == 'same' and block.status == 'single'
    assert set(block.members) == {1, 2}
    assert assemble([TextLine(1, 0, 0, 'one'), TextLine(2, 0, 0, 'two')])[0].status == 'review'


def test_uneven_gaps_and_uncertain_spacing_do_not_become_supported():
    lines = [TextLine(1, 0, 0, 'a'), TextLine(2, 0, 3, 'b'), TextLine(3, 0, 13, 'c')]
    assert assemble(lines, anchors=[1])[0].status == 'review'
    assert assemble(lines[:2], anchors=[1], reliable=False)[0].status == 'review'


def test_repeated_spacing_and_sparse_estimates():
    lines = [TextLine(i * 2 + j, i * 100, j * 10, str(j)) for i in range(5) for j in range(2)]
    pairs = [(a.key, b.key) for a, b in itertools.combinations(lines, 2)]
    gap, reliable, _ = estimate_line_gap(lines, pairs)
    assert math.isclose(gap, 15) and reliable
    assert estimate_line_gap(lines[:2], [(0, 1)])[1] is False
    assert estimate_line_gap(lines[:1], [])[0] == 0


def test_cancellation_is_observed():
    def cancel():
        raise RuntimeError('cancelled')
    try:
        assemble_blocks([TextLine(1, 0, 0, 'a')], [], 10, cancel=cancel)
    except RuntimeError as error:
        assert str(error) == 'cancelled'
    else:
        raise AssertionError('Cancellation was ignored')
