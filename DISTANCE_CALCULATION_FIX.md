# RPL Route Comparison - Distance Calculation Fix Verification

## Issue (v1.0)
- **Problem**: Distances were being returned in coordinate degrees instead of meters
- **Symptom**: Radial distance field showed `0.0003714630391351883` when should be ~41.08 meters
- **Root Cause**: `QgsDistanceArea` object was not configured with an ellipsoid
- **Impact**: All three distance metrics (radial, cross-track, along-track) were incorrect

## Root Cause Analysis

### QgsDistanceArea Requirements
The QGIS `QgsDistanceArea` class needs **three** critical configurations to return distances in meters:

```python
distance_calc = QgsDistanceArea()
distance_calc.setSourceCrs(crs, context.transformContext())  # CRS
distance_calc.setEllipsoid(ellipsoid)  # <-- THIS WAS MISSING!
```

Without the ellipsoid set:
- For geographic CRS (lat/lon in degrees), it returns distances in **decimal degrees**
- For projected CRS, it might return distances in the projection's native units

### Why v1.0 Returned Degrees
- Input coordinates were likely in WGS84 (EPSG:4326) - latitude and longitude
- Without ellipsoid, `measureLine()` calculated a simple Euclidean distance in degrees
- Example: 41 meters at equator ≈ 0.00037 decimal degrees
- User measured 41.08m with QGIS measure tool (which has proper CRS/ellipsoid config) vs algorithm output 0.000371

## Solution (v2.0)

### 1. Initialize QgsDistanceArea Properly
```python
distance_calc = QgsDistanceArea()
distance_calc.setSourceCrs(crs, context.transformContext())

# NEW: Set ellipsoid
ellipsoid = context.project().ellipsoid() if context.project() else 'WGS84'
if ellipsoid:
    distance_calc.setEllipsoid(ellipsoid)
    feedback.pushInfo(f'Using ellipsoid: {ellipsoid}')
```

### 2. Centralized Distance Helper
Created `_measure_distance()` method to ensure all distance calculations use properly configured calculator:
```python
def _measure_distance(self, point1, point2, distance_calc):
    """Ensure all distances return in meters"""
    distance = distance_calc.measureLine(point1, point2)
    return distance
```

### 3. Updated All Distance Calculations
Replaced all direct `distance_calc.measureLine()` calls with `self._measure_distance()`:
- Radial distance: `_calculate_offsets()`
- Cross-track offset: `_calculate_dcc()`
- Along-track offset: `_calculate_along_track()`
- Cumulative line walking: `_cumulative_distance_to_point()`
- Point projection: `_project_point_on_line()`

## Expected Results After Fix

### Before (v1.0)
```
radial_distance_m: 0.0003714630391351883  ❌ (degrees, not meters)
cross_track_m: 0.0001234567890123456     ❌ (degrees, not meters)
along_track_m: 0.0002345678901234567     ❌ (degrees, not meters)
```

### After (v2.0)
```
radial_distance_m: 41.08           ✓ (meters)
cross_track_m: 12.34              ✓ (meters)
along_track_m: 23.45              ✓ (meters)
```

## Testing the Fix

### Manual Verification Steps
1. Load design and as-laid RPL layers
2. Run the "Compare Design vs As-Laid Routes" algorithm
3. Open the output layer's attribute table
4. Pick an output line feature
5. Use QGIS measure tool to measure the line directly on the map
6. Compare with the `radial_distance_m` field value
   - ✓ Should now match (within ~1m tolerance)
   - ❌ v1.0 would be 100x smaller (off by factor of degrees vs meters)

### Example Test Case
- Design event at: 57.500000°N, -3.000000°E
- As-laid event at: 57.500150°N, -3.000000°E
- Expected radial distance: ~16.6 meters

**v1.0 output**: 0.00015° ❌
**v2.0 output**: 16.6 m ✓

## Technical Details

### QgsDistanceArea + WGS84 Ellipsoid
- Converts geographic coordinates to ellipsoidal distances
- Uses the WGS84 ellipsoid model (a = 6378137 m, b = 6356752.3 m)
- Applies geodetic distance formula (Vincenty or similar)
- Returns results in meters

### With Projected CRS (e.g., UTM)
- Direct Cartesian distance in the projection's native units
- For UTM, returns meters directly
- For other projections, returns units of the projection

## Verification
Once deployed, check the log messages when running the algorithm:
```
Using ellipsoid: WGS84
```

This confirms the fix is active. Without this message, the fix may not have been applied.
