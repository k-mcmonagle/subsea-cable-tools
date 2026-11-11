"""Conservative text-block assembly; coordinates and tolerances are in metres.

No soil interpretation occurs here. Certainty describes the geometric evidence,
never the correctness of the survey interpretation or a statistical probability.
"""

from dataclasses import dataclass
import math
from statistics import median


@dataclass(frozen=True)
class TextLine:
    key: int
    x: float
    y: float
    text: str
    angle: float = 0.0  # counter-clockwise from working-CRS east
    alignment: str = ''


@dataclass
class TextBlock:
    members: tuple
    anchor: int
    text: str
    status: str
    reason: str


def normalized_text(text):
    """Only whitespace/case normalization for duplicate detection, not rewriting."""
    return ' '.join(text.split()).casefold()


def _offset(a, b):
    angle = math.radians(a.angle)
    dx, dy = b.x - a.x, b.y - a.y
    return dx * math.cos(angle) + dy * math.sin(angle), -dx * math.sin(angle) + dy * math.cos(angle)


def compatible(a, b, angle_tolerance=10.0):
    angle_delta = abs((a.angle - b.angle + 180) % 360 - 180)
    return a.alignment == b.alignment and angle_delta <= angle_tolerance


def aligned(a, b):
    if not compatible(a, b):
        return False
    across, up = _offset(a, b)
    across_b, up_b = _offset(b, a)
    slope = math.tan(math.radians(10))
    return (abs(up) > 1e-6 and abs(across) <= abs(up) * slope
            and abs(across_b) <= abs(up_b) * slope)


def estimate_line_gap(lines, neighbor_pairs):
    """Estimate from mutual nearest aligned neighbors, with explicit uncertainty.

    The caller supplies spatial-index neighbors, avoiding a quadratic scan.
    A small sample or mixed scales is not enough evidence for automatic assembly.
    """
    lookup = {line.key: line for line in lines}
    nearest = {}
    for i, j in neighbor_pairs:
        a, b = lookup[i], lookup[j]
        distance = math.hypot(a.x - b.x, a.y - b.y)
        if distance <= 1e-6 or not aligned(a, b):
            continue
        for source, target in ((i, j), (j, i)):
            if source not in nearest or (distance, target) < nearest[source]:
                nearest[source] = (distance, target)
    gaps = sorted(distance for i, (distance, j) in nearest.items()
                  if i < j and j in nearest and nearest[j][1] == i)
    if not gaps:
        return 0.0, False, 'No repeated aligned line spacing; text kept separate.'
    typical = median(gaps)
    middle = gaps[len(gaps) // 4: max(len(gaps) // 4 + 1, 3 * len(gaps) // 4)]
    reliable = len(gaps) >= 3 and max(middle) <= 1.8 * min(middle)
    reason = (f'Automatic line-gap limit {typical * 1.5:.3g} m from {len(gaps)} neighboring pairs.'
              if reliable else 'Too few consistent line gaps; proposed groups need review. Set a line-gap limit after inspection.')
    return typical * 1.5, reliable, reason


def assemble_blocks(lines, pairs, max_gap, anchored=(), reliable_gap=True, cancel=lambda: None):
    """Assemble candidates, marking branches, mixed spacing and overlaps for review.

    ``pairs`` contains all pairs within max_gap (plus coincident points). Distinct
    leader anchors in one component are ambiguous; several leaders sharing one
    text anchor are allowed. Uncertain candidates never become accepted text.
    """
    lookup = {line.key: line for line in lines}
    parent = {key: key for key in lookup}
    problems = {key: set() for key in lookup}
    anchored = set(anchored)

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    choices = {}
    for number, (i, j) in enumerate(pairs):
        if number % 1000 == 0:
            cancel()
        a, b = lookup[i], lookup[j]
        distance = math.hypot(a.x - b.x, a.y - b.y)
        if distance <= 1e-6:
            parent[find(j)] = find(i)
            if not compatible(a, b) or normalized_text(a.text) != normalized_text(b.text):
                problems[i].add('Different text or orientation at the same position.')
            continue
        if distance > max_gap or not aligned(a, b):
            continue
        parent[find(j)] = find(i)
        for source, target in ((a, b), (b, a)):
            _, up = _offset(source, target)
            choices.setdefault((source.key, up > 0), []).append(distance)
    for (key, _direction), distances in choices.items():
        distances.sort()
        if len(distances) > 1 and distances[1] <= distances[0] * 1.25:
            problems[key].add('Competing nearby text lines or adjacent columns.')

    components = {}
    for key in sorted(lookup):
        components.setdefault(find(key), []).append(key)
    result = []
    for members in components.values():
        cancel()
        reference = lookup[members[0]]
        ordered = sorted(members, key=lambda key: (-_offset(reference, lookup[key])[1], key))
        reasons = set().union(*(problems[key] for key in members))
        # Identical overprinted text is one line, but all source IDs are retained.
        representatives, seen_lines = [], set()
        for key in ordered:
            line = lookup[key]
            signature = (round(line.x, 6), round(line.y, 6), normalized_text(line.text))
            if signature not in seen_lines:
                representatives.append(key)
                seen_lines.add(signature)
        gaps = [abs(_offset(lookup[a], lookup[b])[1])
                for a, b in zip(representatives, representatives[1:])]
        if gaps and (min(gaps) <= 1e-6 or max(gaps) > 1.8 * min(gaps)):
            reasons.add('Uneven line spacing; neighboring callouts may have been combined.')
        anchor_positions = {(round(lookup[key].x, 6), round(lookup[key].y, 6))
                            for key in set(members) & anchored}
        if len(anchor_positions) > 1:
            reasons.add('Several distinct leader anchors in the proposed text block.')
        if len(representatives) > 20:
            reasons.add('More than 20 text fragments; inspect possible chained callouts.')
        if len(representatives) > 1 and not reliable_gap:
            reasons.add('Automatic line-spacing estimate is uncertain.')
        text = '\n'.join(lookup[key].text.strip() for key in representatives)
        if reasons:
            status, reason = 'review', ' '.join(sorted(reasons))
        elif len(representatives) == 1:
            status, reason = 'single', 'Original text preserved; no line grouping needed.'
        elif anchor_positions:
            status, reason = 'supported', 'Aligned, consistently spaced lines with one text anchor used by the leader(s).'
        else:
            status, reason = 'layout_only', 'Aligned, consistently spaced lines; no leader evidence. Check the assembled label.'
        result.append(TextBlock(tuple(ordered), representatives[0], text, status, reason))
    return result
