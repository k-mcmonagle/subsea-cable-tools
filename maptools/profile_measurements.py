"""Metre-based profile measurements and portable CSV export (no GUI)."""
import csv
import math
from ..slope_utils import interpolate_covered, is_finite

UNITS = {'m': 1.0, 'km': .001, 'ft': 1 / .3048,
         'nautical miles': 1 / 1852, 'miles': 1 / 1609.344}


def measurement(a, b, xs=None, ys=None):
    dx, dz = b[0] - a[0], b[1] - a[1]
    result = {'width_m': abs(dx), 'depth_change_m': dz, 'height_m': abs(dz),
              'endpoint_distance_m': math.hypot(dx, dz),
              'angle_deg': math.degrees(math.atan2(abs(dz), abs(dx))) if dx or dz else None,
              'slope_deg': math.degrees(math.atan2(-dz, dx)) if dx > 0 else
                           math.degrees(math.atan2(dz, -dx)) if dx < 0 else None,
              'seabed_distance_m': None}
    if xs is not None and ys is not None:
        lo, hi = sorted([a[0], b[0]])
        z0, z1 = interpolate_covered(xs, ys, lo), interpolate_covered(xs, ys, hi)
        pts = [(lo, z0)] + [(x, z) for x, z in zip(xs, ys) if lo < x < hi] + [(hi, z1)]
        if all(is_finite(z) for x, z in pts):
            result['seabed_distance_m'] = sum(math.hypot(x1-x0, y1-y0)
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]))
    return result


def write_profile_csv(path, profile, series, slopes_x, slopes, measurements):
    """Long-form observations plus measurement rows; blanks denote unavailable data."""
    fields = ['record', 'source', 'distance_m', 'depth_positive_down_m', 'slope_deg',
              'baseline_m', 'valid', 'native_cell_m', 'sampling', 'datum',
              'x1_m', 'z1_m', 'x2_m', 'z2_m', 'width_m', 'depth_change_m',
              'height_m', 'endpoint_distance_m', 'seabed_distance_m', 'profile_crs',
              'origin_x', 'origin_y', 'target_x', 'target_y', 'window_requested_m', 'route_kp_km', 'terrace_averaging_m', 'axis_labels', 'angle_deg']
    with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for s in series:
            for x, y in zip(s['x'], s['y']):
                writer.writerow({'record': 'depth', 'source': s.get('source_id', s['name']),
                    'distance_m': x, 'route_kp_km': profile.get('route_kp', {}).get(x), 'depth_positive_down_m': y,
                    'valid': int(is_finite(y)), 'native_cell_m': s.get('pixel_size_m'),
                    'sampling': s.get('sampling', 'linear contour crossings'), 'datum': s.get('datum', '')})
        for x, slope, width in zip(slopes_x, slopes, profile.get('slope_baseline_m', [])):
            writer.writerow({'record': 'slope', 'source': profile.get('source_mode', 'auto'),
                             'distance_m': x, 'route_kp_km': profile.get('route_kp', {}).get(x), 'slope_deg': slope, 'baseline_m': width,
                             'valid': int(is_finite(slope))})
        endpoints = profile.get('endpoints') or [(None,None),(None,None)]
        writer.writerow({'record':'metadata', 'source':profile.get('source_mode','auto'),
                         'profile_crs':profile.get('crs',''), 'origin_x':endpoints[0][0],
                         'origin_y':endpoints[0][1], 'target_x':endpoints[1][0], 'target_y':endpoints[1][1],
                         'window_requested_m':profile.get('slope_window_m',0),
                         'terrace_averaging_m':profile.get('terrace_baseline_m',0),
                         'axis_labels':profile.get('axis_labels','distance')})
        for m in measurements:
            row = {'record': 'measurement', 'source': m['source'],
                   'x1_m': m['a'][0], 'z1_m': m['a'][1], 'x2_m': m['b'][0], 'z2_m': m['b'][1]}
            row.update(m['metrics'])
            writer.writerow(row)
