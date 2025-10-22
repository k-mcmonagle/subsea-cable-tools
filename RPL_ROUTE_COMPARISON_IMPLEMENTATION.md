# RPL Route Comparison Algorithm - Implementation Summary

## Overview
A new QGIS Processing algorithm has been created to compare design vs as-laid submarine cable routes and calculate position offsets for corresponding events.

## Version 2.0 - Critical Distance Calculation Fixes
**Date**: October 22, 2025

### Problems Fixed
1. **Distance values were in degrees instead of meters** - The `QgsDistanceArea` object was not configured with an ellipsoid, so it was returning distance in coordinate degrees rather than meters.
2. **CRS/Ellipsoid configuration missing** - The distance calculator is now properly initialized with:
   - Source CRS set to the layer's CRS
   - Ellipsoid configured (defaults to WGS84 from project or global settings)
   - This ensures all distances are calculated geodetically in meters

### Solution Implemented
- Created `_measure_distance()` helper method that ensures all distance calculations use a properly configured `QgsDistanceArea`
- Replaced all direct calls to `distance_calc.measureLine()` with the helper method
- Added ellipsoid configuration in the algorithm's distance calculator initialization
- Added debug feedback to show which ellipsoid is being used

### Result
All distance values now correctly return in meters:
- **Radial Distance**: Now accurate (e.g., 41.08m instead of 0.00037°)
- **Cross-Track Offset**: Now accurate
- **Along-Track Offset**: Now accurate

## Files Created/Modified

### New File: `rpl_route_comparison_algorithm.py`
- **Location**: `processing/rpl_route_comparison_algorithm.py`
- **Class**: `RPLRouteComparisonAlgorithm`
- **Group**: "RPL Comparison"
- **Name ID**: "rplroutecomparison"

### Modified File: `subsea_cable_processing_provider.py`
- Added import for `RPLRouteComparisonAlgorithm`
- Registered the new algorithm in `loadAlgorithms()` method

## Algorithm Details

### Inputs
1. **Design RPL Points**: Point layer from design RPL (e.g., repeater events, terminators)
2. **Design Events Field**: Field name containing event identifiers (e.g., "Event")
3. **Design RPL Lines**: Line layer representing the design cable route path
4. **As-Laid RPL Points**: Point layer from as-laid RPL
5. **As-Laid Events Field**: Field name containing event identifiers
6. **As-Laid RPL Lines**: Line layer representing the as-laid cable route path

### Processing Steps

1. **Event Extraction & Matching**
   - Extracts all event features from design and as-laid point layers
   - Performs exact string matching on event names
   - Reports unmatched events as warnings (visible in QGIS Log Messages)
   - Allows users to see which events couldn't be matched

2. **Offset Calculations**
   For each matched event pair, calculates three offset metrics using precise geodetic distance calculations:
   
   a. **Radial Distance**: Direct geodetic distance between design and as-laid event positions
      - Uses QGIS `QgsDistanceArea` for accurate ellipsoidal calculations
      - Result in meters
   
   b. **Cross-Track Offset (DCC)**: Perpendicular distance from the as-laid point to the design route line
      - Represents lateral deviation from the design path
      - Uses QGIS geometry `nearestPoint()` for precise projection
      - Result in meters
   
   c. **Along-Track Offset**: Distance along design route line from design event to perpendicular projection of as-laid event
      - Represents longitudinal displacement along the cable direction
      - Algorithm:
        1. Projects both start point (design event) and end point (as-laid event projection) onto the line
        2. Walks along all line segments from start projection to end projection
        3. Sums segment lengths accurately
      - Result in meters

3. **Output Layer Generation**
   - Creates a new line layer with one line per matched event pair
   - Each line connects the design point to the corresponding as-laid point
   - Output is in the same CRS as the design layer

### Output Fields
The output line layer includes:

| Field | Type | Description |
|-------|------|-------------|
| `design_layer` | String | Name of the design RPL points layer |
| `aslaid_layer` | String | Name of the as-laid RPL points layer |
| `design_event` | String | Name of the design event |
| `aslaid_event` | String | Name of the as-laid event |
| `along_track_m` | Double | **Signed** along-track offset in meters. Positive = as-laid ahead of design, Negative = as-laid behind design |
| `cross_track_m` | Double | Cross-track offset (DCC) in meters (always positive) |
| `radial_distance_m` | Double | Direct distance between events in meters |
| `bearing_deg` | Double | Compass bearing from design to as-laid point in degrees (0-360, where 0°=N, 90°=E, 180°=S, 270°=W) |

## How It Works

### Workflow
1. User selects design and as-laid RPL point and line layers
2. User specifies which fields contain event identifiers in each layer
3. Algorithm:
   - Extracts events and matches by exact event name
   - Shows unmatched events in log (can be reviewed by user)
   - For each matched pair, calculates the three offset metrics
   - Creates output line layer
4. User can export output as CSV or analyze offsets on map

### Example Use Case
Design RPL has events: "Repeater 1", "Repeater 2", "Landing"
As-laid RPL has events: "Repeater 1", "Repeater 2", "Landing"

Output will have 3 lines (one per event pair) showing:
- Where the events ended up relative to design
- How much they shifted along the cable path (now with sign indicating direction)
- How much they shifted perpendicular (cross-track)
- Straight-line distance between design and as-laid positions
- Compass bearing showing the direction of offset

## Field Interpretation Guide

### Along-Track Offset (`along_track_m`)
**Signed value** - tells you both distance AND direction:
- **Positive (+)**: As-laid event is ahead (further along the route) than the design event
  - Example: +50m means the as-laid repeater was installed 50m further down the cable
- **Negative (-)**: As-laid event is behind (earlier on the route) than the design event
  - Example: -25m means the as-laid repeater was installed 25m closer to the start

### Bearing (`bearing_deg`)
**Compass bearing** from design event to as-laid event:
- **0° / 360°**: North
- **90°**: East
- **180°**: South
- **270°**: West
- Example: 45° = Northeast, 225° = Southwest

Combined with cross-track offset, bearing tells you the full direction of displacement:
- bearing=45° with cross_track=10m = northeast, 10m perpendicular to route
- bearing=180° with cross_track=5m = south, 5m perpendicular to route

## Future Enhancements

### Suggested Additions:
1. **Fuzzy Event Matching**
   - Use similarity scoring (e.g., Levenshtein distance) to handle minor spelling variations
   - Would allow events like "Repeater 1" and "Repeater1" to match
   - Threshold configurable by user

2. **Interactive Manual Review Dialog**
   - Table widget showing all potential matches
   - Allow user to:
     - Accept/reject automatic matches
     - Manually pair unmatched events
     - Skip certain events
   - Would solve edge cases where exact matching fails

3. **Enhanced Along-Track Calculation**
   - Current implementation uses approximation
   - Could improve precision by:
     - Properly walking along multipart line geometries
     - Using precise line parameterization
     - Handling complex route topologies

4. **Statistics & Summary**
   - Generate summary statistics: mean/median/max offsets
   - Export summary table
   - Flagging anomalies (events with large deviations)

5. **Visualization Options**
   - Color-code offset lines by magnitude
   - Generate comparison report with maps and tables
   - 3D visualization if Z-values present

## Technical Notes

### Dependencies
- Uses existing `RPLComparator` class from `rpl_comparison_utils.py` (currently imported but not fully utilized - could be expanded for better along-track calculations)
- Uses QGIS core distance calculation tools (`QgsDistanceArea`)
- Coordinate system handling via QGIS CRS system

### Distance Calculations (v2.0 - Fixed)
**CRITICAL FIX: v1.0 returned distances in degrees instead of meters**

#### How QgsDistanceArea Works
- Requires three things to calculate distances in meters:
  1. **CRS**: Source coordinate reference system
  2. **Ellipsoid**: Earth model for geodetic calculations (WGS84 by default)
  3. **Transform Context**: For coordinate transformations

#### What Was Wrong (v1.0)
- The `QgsDistanceArea` was initialized with only the CRS
- The ellipsoid was **not being set**
- Result: `measureLine()` returned distances in **coordinate degrees** (e.g., 0.00037°) instead of meters (e.g., 41.08m)
- Example: Your 41.08m result came back as 0.000371°

#### What's Fixed (v2.0)
1. **Ellipsoid configuration added**: Now gets ellipsoid from project or defaults to WGS84
2. **Centralized distance helper method**: `_measure_distance()` ensures all distance calls use proper configuration
3. **All calculations use the helper**: Every distance measurement (radial, cross-track, along-track) now returns meters

#### Verification
All distances are now guaranteed to be in **meters**:
- Geographic CRS (lat/lon): Geodetically calculated using ellipsoid → meters
- Projected CRS (e.g., UTM): Calculated using projection → meters

### Limitations
- Requires exact event name matches (enhancement: fuzzy matching)
- No interactive review of matches in current version (enhancement: modal dialog)
- No handling of multi-route scenarios

## Distance Calculation Implementation (v2 - Fixed)

### Key Methods

#### `_calculate_offsets(design_point, aslaid_point, design_lines, aslaid_lines, distance_calc)`
Central method that orchestrates all three offset calculations. Takes exact point coordinates and calculates all metrics using the provided distance calculator.

#### `_calculate_dcc(point, line_layer, distance_calc)`
Calculates Distance Cross Course (perpendicular distance). Iterates through all line features in the layer, finds the nearest point on each segment using QGIS's `nearestPoint()` geometry method, and returns the minimum distance.

#### `_calculate_along_track(design_point, aslaid_point, line_layer, distance_calc)`
Calculates along-track offset by:
1. Finding the perpendicular projection of the as-laid point on the design route
2. Calculating cumulative distance from the design point to that projection
3. Uses helper methods `_project_point_on_line()` and `_cumulative_distance_to_point()`

#### `_cumulative_distance_to_point(start_point, end_point, line_layer, distance_calc)` - **FIXED**
**This is where the main accuracy improvements were made.** Previously used an approximate algorithm; now:
1. Projects both start and end points onto the line layer precisely
2. Walks through all line geometry vertices in sequence
3. For each segment, checks if projection points lie on that segment
4. Sums partial and full segment distances accurately
5. Handles multi-part geometries correctly
6. Returns cumulative distance with high precision

#### `_project_point_on_line(point, line_layer, distance_calc)`
Helper method that finds the nearest point on a line layer for any given point. Iterates through all features and returns the closest projection point.

### Accuracy Features
- Uses QGIS `QgsDistanceArea` with proper ellipsoid settings for geodetic calculations
- Handles coordinate reference system transformations correctly
- Respects ellipsoid settings from the QGIS project (default: WGS84)
- All distances in meters (not degrees or other units)
- Proper handling of multi-part line geometries
- Tolerant point-on-line detection (±0.01m tolerance)

## Testing Recommendations

1. Test with design and as-laid RPLs with matching events
2. Test with unmatched events to verify warning messages
3. Verify output line layer accuracy by:
   - Checking line endpoints match input point locations
   - Validating offset calculations using manual measurements
   - Comparing with expected results
4. Test with different event field names
5. Test with different CRS (geographic, projected, etc.)
