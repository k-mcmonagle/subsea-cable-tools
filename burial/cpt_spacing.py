"""Along-route spacing of active, placed CPTs (no QGIS dependencies)."""

import math


def spacing_summary(placements, start_kp, end_kp):
    """Count CPTs in the plan scope and summarise adjacent KP gaps in metres.

    Coincident tests count separately and contribute a zero gap. End gaps
    between a scope boundary and its first/last CPT are not CPT spacing.
    """
    lo, hi = sorted((float(start_kp), float(end_kp)))
    kps = []
    for row in placements:
        if row.get('status') != 'placed':
            continue
        try:
            kp = float(row.get('kp'))
        except (TypeError, ValueError):
            continue
        if math.isfinite(kp) and lo <= kp <= hi:
            kps.append(kp)
    kps.sort()
    gaps = [(b - a) * 1000.0 for a, b in zip(kps, kps[1:])]
    return {'count': len(kps), 'average_m': sum(gaps) / len(gaps) if gaps else None,
            'min_m': min(gaps) if gaps else None, 'max_m': max(gaps) if gaps else None}


def spacing_text(summary):
    count = summary['count']
    if summary['average_m'] is None:
        return f'CPT spacing in plan scope: {count} CPT(s); at least two are needed.'
    return (f"CPT spacing in plan scope ({count} CPTs): average {summary['average_m']:,.1f} m"
            f" · min {summary['min_m']:,.1f} m · max {summary['max_m']:,.1f} m")
