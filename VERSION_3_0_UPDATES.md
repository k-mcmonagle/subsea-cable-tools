# RPL Route Comparison - v3.0 Updates

## New Features Added

### 1. Signed Along-Track Distance ✅

**What Changed:**
- `along_track_m` field now includes sign (+ or -)
- Previously: Always returned absolute distance
- Now: Positive means as-laid is ahead, negative means behind

**Technical Implementation:**
- New method: `_calculate_along_track_signed()`
- Calculates distance from route start to design point
- Calculates distance from route start to as-laid projection
- Returns difference (signed)

**Examples:**
```
Design event at 100m along route
As-laid event at 150m along route
→ along_track_m = +50 (as-laid is 50m ahead)

Design event at 100m along route
As-laid event at 75m along route
→ along_track_m = -25 (as-laid is 25m behind)
```

**Use Cases:**
- Identify whether cable was laid ahead of or behind schedule
- Track installation progress relative to design
- Detect significant backtracks or rework

---

### 2. Bearing Angle Field ✅

**What Added:**
- New output field: `bearing_deg`
- Compass bearing from design event to as-laid event
- Range: 0-360 degrees
  - 0°/360° = North
  - 90° = East
  - 180° = South
  - 270° = West

**Technical Implementation:**
- New method: `_calculate_bearing()`
- Uses mathematical atan2() function
- Converts from radians to degrees
- Normalizes to 0-360 range

**Examples:**
```
Design at (0.0, 0.0)
As-laid at (0.001, 0.001)
→ bearing ≈ 45° (Northeast)

Design at (0.0, 0.0)
As-laid at (0.001, -0.001)
→ bearing ≈ 135° (Southeast)

Design at (0.0, 0.0)
As-laid at (-0.001, 0.0)
→ bearing ≈ 270° (West)
```

**Use Cases:**
- Understand directional offset at each event
- Identify systematic biases (e.g., always shifted northwest)
- Combined with cross-track, shows full spatial offset
- Export to visualization tools (arrows, directional indicators)

---

## Implementation Details

### Method: `_calculate_along_track_signed()`

**Algorithm:**
```
1. Find perpendicular projection of as-laid point on design route
2. Get the start point of the route
3. Calculate: distance(route_start → design_point)
4. Calculate: distance(route_start → as-laid_projection)
5. Return: distance_to_projection - distance_to_design
```

**Result Interpretation:**
- Positive: As-laid is further along the route
- Negative: As-laid is earlier on the route
- Magnitude: How far apart they are

### Method: `_calculate_bearing()`

**Algorithm:**
```
1. Calculate delta_lon = as_laid.x - design.x
2. Calculate delta_lat = as_laid.y - design.y
3. Calculate bearing = atan2(delta_lon, delta_lat)
4. Convert radians to degrees
5. Normalize to 0-360 range
```

**Coordinate System Note:**
- For geographic CRS: X=Longitude, Y=Latitude
- For projected CRS: Uses Cartesian X,Y
- Bearing is always compass-relative (0°=North)

---

## Output Field Summary (v3.0)

| Field | Type | Direction | Sign | Example |
|-------|------|-----------|------|---------|
| `along_track_m` | Double | Along route | ±  | +50, -25 |
| `cross_track_m` | Double | Perpendicular | Absolute | 10.5 |
| `bearing_deg` | Double | Design→As-laid | 0-360 | 45, 135, 270 |
| `radial_distance_m` | Double | Direct | Absolute | 41.08 |

---

## Testing Recommendations

### Test 1: Signed Along-Track (As-laid Ahead)
**Setup:**
- Route: Linear, 1000m long
- Design event: 300m along
- As-laid event: 350m along

**Expected:**
- `along_track_m` ≈ +50 ✓

### Test 2: Signed Along-Track (As-laid Behind)
**Setup:**
- Route: Linear, 1000m long
- Design event: 500m along
- As-laid event: 450m along

**Expected:**
- `along_track_m` ≈ -50 ✓

### Test 3: Bearing North
**Setup:**
- Design: (0.0, 0.0)
- As-laid: (0.0, 0.001)

**Expected:**
- `bearing_deg` ≈ 0 or 360 ✓

### Test 4: Bearing Northeast
**Setup:**
- Design: (0.0, 0.0)
- As-laid: (0.001, 0.001)

**Expected:**
- `bearing_deg` ≈ 45 ✓

### Test 5: Bearing South
**Setup:**
- Design: (0.0, 0.0)
- As-laid: (0.0, -0.001)

**Expected:**
- `bearing_deg` ≈ 180 ✓

---

## Backward Compatibility

**Breaking Changes:**
- `along_track_m` now returns signed values (was always positive)
- Users expecting absolute values should use `ABS(along_track_m)` in analysis

**Non-Breaking:**
- All new fields are additions (no removed fields)
- Existing fields unchanged in meaning:
  - `radial_distance_m` (same)
  - `cross_track_m` (same)
  - Event/layer reference fields (same)

**Migration Note:**
If comparing with v2.0 results:
- Negative along-track values are NEW (weren't possible before)
- Previously "behind" events would have shown distance 0 or incorrect values
- v3.0 results are more accurate for events behind the design point

---

## Workflow Example

**Scenario:** Analyzing a 10km subsea cable route with 5 repeater sites

**Data:**
```
Design RPL:  R1@2km, R2@4km, R3@6km, R4@8km, R5@10km
As-laid RPL: R1@2.1km, R2@3.8km, R3@6.5km, R4@7.9km, R5@10.2km
```

**Expected Output:**
```
R1: along_track=+100m, bearing=015°, cross_track=5m
    → Installed 100m ahead (slightly), NNE shift
    
R2: along_track=-200m, bearing=045°, cross_track=8m
    → Installed 200m behind, NE shift
    
R3: along_track=+500m, bearing=135°, cross_track=3m
    → Installed 500m ahead, SE shift
    
R4: along_track=-100m, bearing=270°, cross_track=2m
    → Installed 100m behind, due West shift
    
R5: along_track=+200m, bearing=000°, cross_track=1m
    → Installed 200m ahead, due North shift
```

**Analysis:**
- R2 and R4 were installed behind schedule (negative along-track)
- R1, R3, R5 ahead of schedule
- Mostly shifted east/northeast (bearing 45° area)
- Cross-track offsets small (1-8m) - route followed closely

---

## Files Modified

- `rpl_route_comparison_algorithm.py`: Added methods, new fields, updated calculations
- `RPL_ROUTE_COMPARISON_IMPLEMENTATION.md`: Updated documentation
- `ALONG_TRACK_FIX_EXPLANATION.md`: Existing (from v2.0 fix)
- `DISTANCE_CALCULATION_FIX.md`: Existing (from v2.0 fix)
