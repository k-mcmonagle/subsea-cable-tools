# RPL Comparison Feature - Testing Guide

## Implementation Summary

I've implemented a new processing algorithm: **"Translate KP Between RPLs (Points)"**

### What Was Created

1. **`processing/rpl_comparison_utils.py`** - Core utility module
   - `RPLComparator` class handles all KP translation logic
   - Reusable by future tools (mouse tool enhancement, batch processor, etc.)
   - Methods:
     - `get_point_at_kp()` - Find geographic point at a specific KP
     - `calculate_kp_to_point()` - Calculate KP value to a point
     - `nearest_point_on_line()` - Find nearest point on a line
     - `translate_kp()` - Translate KP from source to target RPL
     - `translate_kp_for_point()` - Main method used by algorithm

2. **`processing/translate_kp_from_rpl_to_rpl_algorithm.py`** - Processing Algorithm
   - User-facing tool in Processing Toolbox
   - Takes source point features and translates their KPs to target RPL
   - Includes Distance Cross Course (DCC) calculation

3. **Updated `processing/subsea_cable_processing_provider.py`**
   - Registered the new algorithm so it appears in Processing Toolbox

### Git Commits

- Commit 1: `f1ee733` - Checkpoint before RPL comparison feature implementation
- Commit 2: `d8cad25` - Add RPL comparison feature: KP translation between RPLs with DCC calculation

---

## How to Test in QGIS

### Prerequisites

You need:
- Design RPL (point layer + line layer)
- As-Laid RPL (point layer + line layer)
- Both in same CRS
- Points should have valid geometry

### Test Workflow

1. **Open QGIS**
   - Ensure plugin is active (Subsea Cable Tools should be loaded)

2. **Load Test Data**
   - Import Design RPL (should create design_points + design_lines)
   - Import As-Laid RPL (should create aslaid_points + aslaid_lines)
   - Verify both are in same CRS (e.g., EPSG:4326)

3. **Open Processing Toolbox**
   - Menu: `Processing` → `Toolbox` (or Ctrl+Alt+T)
   - Search for: "Translate KP" or browse to "Subsea Cable Tools" → "RPL Comparison"

4. **Configure Tool**
   - **Source Point Layer**: Select `design_points`
   - **Source Line Layer**: Select `design_lines`
   - **Target Line Layer**: Select `aslaid_lines`
   - **Include Source KP Field**: Leave checked (to preserve original KP)
   - Click `Run`

5. **Review Output**
   - New layer created: e.g., "Translated Points"
   - Inspect attribute table for these fields:
     - `translated_kp` - KP value on target line
     - `spatial_offset_m` - Distance from point to target line
     - `dcc_to_source_line` - Distance Cross Course back to source line
     - `source_line_name` - Should be "design_lines"
     - `target_line_name` - Should be "aslaid_lines"

6. **Validation Checks**
   - ✓ Output points are located on or very near the target line
   - ✓ `spatial_offset_m` is typically small (0-100m for same route)
   - ✓ If `spatial_offset_m` > 1km, there's a significant routing deviation
   - ✓ `dcc_to_source_line` shows how different the two routes are
   - ✓ `translated_kp` values should be similar to original KP (for similar routes)

---

## Expected Behavior

### Design vs. As-Laid Example

**Input:**
```
Design Point Layer (design_points):
  - Feature 1: Repeater 1 at Latitude 45.134567, Longitude -74.234567
    DistCumulative: 50.2 km
  
  - Feature 2: Repeater 2 at Latitude 45.156789, Longitude -74.456789
    DistCumulative: 100.8 km
```

**Configuration:**
- Source Points: design_points
- Source Line: design_lines (total length 200 km)
- Target Line: aslaid_lines (total length 199.2 km)

**Expected Output:**
```
Output Layer (Translated Points):
  - Feature 1 (from Design Repeater 1):
    Geometry: Point on aslaid_lines (nearest to original Repeater 1)
    translated_kp: 49.8 km (approximately)
    spatial_offset_m: 0.32 m (close to target route)
    dcc_to_source_line: 1.2 m (routes diverge slightly here)
    source_line_name: "design_lines"
    target_line_name: "aslaid_lines"
    (+ all original attributes from design_points)
    
  - Feature 2 (from Design Repeater 2):
    Geometry: Point on aslaid_lines
    translated_kp: 99.9 km
    spatial_offset_m: 0.48 m
    dcc_to_source_line: 2.5 m
    source_line_name: "design_lines"
    target_line_name: "aslaid_lines"
    (+ all original attributes from design_points)
```

---

## Troubleshooting

### Tool Doesn't Appear in Processing Toolbox

1. Verify plugin is enabled: `Plugins` → `Manage and Install Plugins` → Search "Subsea Cable Tools" → ensure checked
2. Restart QGIS
3. Check console for errors: `Plugins` → `Python Console` → look for error messages

### Points Not Translated Correctly

1. **Check CRS**: Source points, source line, and target line MUST all be in same CRS
   - Reproject if needed: Right-click layer → Export → Save As → choose correct CRS
2. **Check Line Layers**: Ensure line geometries are valid and continuous
   - Validate: `Vector` → `Validate Geometry`

### Output Has Wrong Values

1. **Check Source KP Field**: If points don't have `DistCumulative` field:
   - Tool will still work, but won't include source KP in output
   - Uncheck "Include Source KP Field" if not needed
2. **Spatial Offset Unexpectedly Large**: Routes may diverge significantly
   - This is expected if Design and As-Laid are very different
   - Check `dcc_to_source_line` to understand route differences

### Algorithm Crashes or Reports Errors

1. Check QGIS Console: `Plugins` → `Python Console` → scroll to find error message
2. Verify all layers:
   - Have valid geometry (no empty geometries)
   - Have same CRS
   - Are line layers (source & target) or point layer (source points)

---

## Key Output Fields Explained

| Field | Type | Meaning |
|-------|------|---------|
| `translated_kp` | Double | KP (km) on target line where source point is translated to |
| `spatial_offset_m` | Double | Distance (m) from source point to target line (perpendicular). Small = on-route. Large = off-route. |
| `dcc_to_source_line` | Double | Distance Cross Course (m) - perpendicular distance from translated point back to source line. Shows how much routes diverge. |
| `source_line_name` | String | Name of source line layer (e.g., "design_lines") |
| `target_line_name` | String | Name of target line layer (e.g., "aslaid_lines") |

---

## Next Steps (After Testing)

Once this is working well, we can build:

1. **Enhanced Mouse Tool** - Real-time dual-RPL display
2. **Batch Comparison** - Multi-feature processing with statistics
3. **Reverse Translation** - Go from As-Laid back to Design KP
4. **Interpolation** - Smooth KP mapping across entire route

---

## Questions for Manual Testing

After you test, please report on:

1. Does the tool appear in Processing Toolbox?
2. Does it run without errors on your test data?
3. Are `translated_kp` values reasonable (within expected range)?
4. Are `spatial_offset_m` values small (0-1m typical for same route)?
5. Any edge cases or unexpected behaviors?

This will help validate the implementation before adding enhancements!
