"""Spacing uses active CPTs in scope, including coincident tests."""
from ..burial.cpt_spacing import spacing_summary


def test_spacing_filters_and_sorts():
    rows = [{'kp': kp, 'status': 'placed'} for kp in (4, 1, 1, 2, 10, float('nan'))]
    rows += [{'kp': 3, 'status': 'superseded import'}, {'kp': 3, 'status': 'too far'}]
    summary = spacing_summary(rows, 4, 1)
    assert summary == {'count': 4, 'average_m': 1000.0, 'min_m': 0.0, 'max_m': 2000.0}


def test_spacing_requires_two_cpts():
    for rows in ([], [{'kp': 2, 'status': 'placed'}]):
        summary = spacing_summary(rows, 0, 4)
        assert summary['average_m'] is summary['min_m'] is summary['max_m'] is None
