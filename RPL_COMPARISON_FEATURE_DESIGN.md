# RPL Comparison Feature Design for v1.4.0+
## A Powerful Solution for Multi-RPL Workflows

---

## Executive Summary

After reviewing the plugin codebase, I've identified an elegant opportunity to create a **multi-RPL comparison framework** that solves the KP translation problem while maintaining accuracy and usability for submarine cable engineering workflows.

The core insight is that your existing infrastructure—KP calculation, line layer handling, and distance measurements—can be extended into a **flexible, composable system** where any RPL can be referenced against any other RPL.

---

## Current System Architecture

### How RPLs Work in the Plugin

**RPL Structure (Point + Line Layers):**
- **Point Layer**: Individual cable positions (Start, Repeaters, End of Cable, etc.)
  - Attributes: `PosNo`, `Event`, `DistCumulative`, `CableDistCumulative`, `ApproxDepth`, `Latitude`, `Longitude`, etc.
- **Line Layer**: Cable segments connecting consecutive positions
  - Attributes: `FromPos`, `ToPos`, `Bearing`, `DistBetweenPos`, `Slack`, `CableCode`, `BurialDepth`, etc.

**Key Insight**: The line layer is the "truth" for KP calculation. It's a continuous geometric representation of the cable route.

### Current KP Calculation System

The plugin uses a **robust, segment-based KP calculation** across all tools:

1. **Distance Calculation**: Uses `QgsDistanceArea` with ellipsoidal/geodetic measurements
2. **Chainage Calculation**: Cumulative distance from the start of the line
3. **Multi-segment Handling**: Correctly processes multi-part line geometries
4. **Accuracy**: Proper handling of vertex-by-vertex distance summation

**Examples in Codebase:**
- `kp_mouse_maptool.py`: Lines 240-310 show segment-by-segment KP calculation
- `nearest_kp_algorithm.py`: Lines 200+ implement sophisticated nearest-point-on-path logic
- `place_kp_points_algorithm.py`: Demonstrates interpolation along lines at specific KP values
- `kp_range_highlighter_algorithm.py`: Shows how to extract line segments for a KP range

---

## The RPL Comparison Challenge

### The Problem

When comparing two RPLs (e.g., Design vs. As-Laid):
1. Both have their own KP values (measured along their own geometries)
2. KP values differ because:
   - Different survey dates → different coordinate systems or survey methodology
   - Actual cable sag/slack differs from design
   - Physical constraints alter routing
3. Users need to **correlate points/events between versions** using spatial proximity
4. **Accuracy is critical** because these are engineering decisions affecting submarine cable operations

### Example Scenario
```
Design RPL:      KP 0 -------- KP 50 -------- KP 100
                 (Point A)     (Repeater 1)   (End)

As-Laid RPL:     KP 0 ---- KP 48 -------- KP 99
                 (Point A) (Repeater 1)    (End)
                 ↑ Slight deviations in actual route

User Need:
"What KP on the As-Laid RPL corresponds to KP 25 on the Design RPL?"
Answer: Approximately KP 24.5 on As-Laid RPL (nearby cable position)
```

---

## Proposed Solution: Multi-RPL Translation Engine

I recommend creating a **modular, powerful feature set** built on three pillars:

### Pillar 1: Core RPL Comparison Processor (NEW SHARED UTILITY)
**Location**: `processing/rpl_comparison_utils.py`

A **reusable Python utility module** that all comparison tools use. This ensures accuracy and consistency.

**Key Functions:**
```python
class RPLComparator:
    """
    Handles accurate comparison between two RPL line layers.
    Provides KP translation, nearest-point matching, and cross-referencing.
    """
    
    def __init__(self, source_rpl_line, target_rpl_line, crs, context):
        """Initialize with two line layers and their shared CRS."""
        
    def get_point_on_target_at_source_kp(self, source_kp_km):
        """
        Find the geographic point on the target RPL that is 
        spatially nearest to the given source KP.
        Returns: (target_kp_km, distance_offset_m, target_geometry_point)
        """
        
    def get_nearest_kp_on_target(self, point_xy, max_search_distance_m=None):
        """
        Find the closest KP on target RPL to an arbitrary point.
        Returns: (target_kp_km, distance_to_point_m)
        """
        
    def cross_reference_feature(self, source_point_feature):
        """
        Given a point feature (from source RPL's point layer),
        find its nearest location on target RPL and return both KPs.
        Returns: {
            'source_kp': float,
            'target_kp': float,
            'spatial_offset_m': float,
            'target_geometry_point': QgsPoint
        }
        """
        
    def build_kp_lookup_table(self, interval_km=1.0):
        """
        Build a lookup table mapping source KP → target KP at regular intervals.
        Useful for batch operations and interpolation.
        Returns: [(source_kp, target_kp, offset_m), ...]
        """
```

**Why this approach:**
- ✅ **Single source of truth** for KP calculations
- ✅ **Testable and maintainable** (separate from UI/algorithm code)
- ✅ **Reusable** across multiple tools
- ✅ **Accurate** (uses same geodetic calculations as existing tools)
- ✅ **Extensible** (easy to add interpolation, filtering, etc.)

---

### Pillar 2: KP Translator Processing Algorithm (NEW)
**Location**: `processing/translate_kp_between_rpls_algorithm.py`

A **Processing Tool** that translates KP values from one RPL to another.

**User Workflow:**
1. User selects source RPL line
2. User selects target RPL line
3. User provides source KP value (e.g., 25 km)
4. Tool outputs target KP with statistics (distance offset, confidence)

**Use Cases:**
- "Mark where Design KP 50 is on my As-Laid RPL" → Output: target_kp = 49.8 km
- Batch translate multiple KP values from a CSV table
- Create a points layer showing Design KP marks on As-Laid line

**Algorithm Benefits:**
- Integrates into Processing Toolbox (QGIS native workflow)
- Can be chained with other algorithms
- Batch processing support
- Output to table/layer for further analysis

---

### Pillar 3: Dual-Reference KP Mouse Tool Enhancement (FEATURE ENHANCEMENT)
**Location**: Enhance `maptools/kp_mouse_maptool.py`

Extend the existing KP Mouse Tool with a **second reference RPL** option.

**Enhanced Configuration Dialog:**
```
Current UI:
- Reference Line: [Design RPL]
- Unit: [km]
- Show Reverse KP: [✓]

New UI Addition:
┌─ Compare to Second RPL ──────────────────┐
│ ☐ Enable Dual RPL Mode                   │
│   Primary RPL:   [Design RPL]            │
│   Secondary RPL: [As-Laid RPL]          │
│   ☐ Show translation in tooltip         │
└──────────────────────────────────────────┘
```

**Enhanced Tooltip Display:**
```
Standard (single RPL):
  KP: 50.123
  rKP: 49.877
  DCC: 2.34 km

Dual RPL Mode:
  Design KP: 50.123
  As-Laid KP: 49.805  ← Auto-calculated translation
  Spatial Offset: 0.32 m
  DCC: 2.34 km
```

**Technical Implementation:**
- Lazy-load secondary RPL geometry on first use
- Cache KP lookup table from Pillar 1 for performance
- Show confidence indicator if spatial offset is large (e.g., >1 km)
- Optional: Draw a visual marker on both RPLs simultaneously

**Why This Approach:**
- ✅ Non-intrusive (checkbox to enable)
- ✅ Leverages existing map tool infrastructure
- ✅ Real-time, interactive feedback
- ✅ No new processing/algorithm overhead
- ✅ Engineer can visually verify translations

---

### Pillar 4: Batch RPL Comparison Processor (NEW)
**Location**: `processing/batch_rpl_comparison_algorithm.py`

For users who need to **systematically translate entire datasets** between RPLs.

**Inputs:**
- Source RPL line layer
- Target RPL line layer
- Optional: Source RPL point layer (events/features to cross-reference)
- KP comparison interval (e.g., every 1 km)

**Outputs:**
1. **Lookup table** (points layer with source_kp → target_kp mapping)
2. **Cross-referenced events** (if point layer provided)
   - Original events + their new target KP values
3. **Translation report** (statistics: mean offset, max offset, etc.)

**Use Cases:**
- "Generate a Design↔As-Laid KP correspondence table"
- "Update all our cable events with their new KP on the As-Laid RPL"
- "Check for significant routing deviations (where offset > threshold)"

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     QGIS Plugin Interface                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─ KP Mouse Tool (Enhanced)      ┌─ Processing Algorithms    │
│  │  - Dual RPL mode               │  - KP Translator          │
│  │  - Real-time tooltip           │  - Batch Comparison       │
│  │  - Visual feedback             │  - Event Cross-Reference  │
│  └─────────────┬────────────────────────────┬─────────────────┘
│                │                            │
│  ┌─────────────V────────────────────────────V──────────────────┐
│  │     Shared RPL Comparison Engine (rpl_comparison_utils.py)  │
│  │                                                              │
│  │  • KP calculation (existing + enhanced)                     │
│  │  • Nearest-point matching                                  │
│  │  • Cross-RPL translation logic                             │
│  │  • Interpolation & lookup tables                           │
│  │  • Caching for performance                                 │
│  └─────────────┬──────────────────────────────────────────────┘
│                │
│  ┌─────────────V──────────────────────────────────────────────┐
│  │        QGIS Core Geometry & Distance Calculations           │
│  │        (QgsDistanceArea, QgsGeometry, ellipsoidal)         │
│  └───────────────────────────────────────────────────────────┘
│
└──────────────────────────────────────────────────────────────────┘
```

---

## Implementation Strategy (Phased Approach)

### Phase 1: Foundation (1-2 sprints)
✅ Create `rpl_comparison_utils.py` with core engine
- Implement RPLComparator class
- Heavy unit testing with Design vs As-Laid sample data
- Validate against manual calculations
- **Output**: Accurate, proven core module

### Phase 2: Quick Win (1 sprint)
✅ Create simple KP Translator algorithm
- "Single KP → Single KP" translator
- Minimal UI, maximum clarity
- **Output**: Users can solve "Where is Design KP 50?" immediately

### Phase 3: Enhance Map Tool (1 sprint)
✅ Add dual-RPL mode to KP Mouse Tool
- Adds checkbox to configuration dialog
- Extends tooltip calculation
- **Output**: Real-time, interactive comparison

### Phase 4: Scale It Up (1-2 sprints)
✅ Create batch comparison algorithm
- Multi-feature translation
- CSV import/export
- Cross-reference events
- **Output**: Systematic workflows

### Phase 5: Polish & Documentation (1 sprint)
✅ Add to UI, update help, test edge cases
✅ Update CHANGELOG and README

---

## Technical Considerations for Accuracy

### Why This Will Be Accurate

1. **Reuses proven KP calculation logic**
   - Already validates in `kp_mouse_maptool.py` (used live by engineers)
   - Segment-by-segment approach handles complex geometries
   - Ellipsoidal distance (geodetic) matches submarine cable standards

2. **Handles multi-part line geometries**
   - Like `nearest_kp_algorithm.py`, correctly sums segment lengths

3. **Spatial proximity + offset reporting**
   - User can immediately see if translation confidence is high/low
   - "Offset > 1 km?" → Visual warning

4. **Configuration validation**
   - Requires matching CRS (prevents hidden errors)
   - Validates layer types upfront

### Potential Edge Cases

| Scenario | Solution |
|----------|----------|
| RPLs have very different routing (e.g., loops) | Show spatial offset warning; user validates manually |
| One RPL is shorter than other | Gracefully limit KP range; inform user |
| Line layers are multi-part differently | Use unaryUnion (already used in highlighter) |
| Large spatial offset between RPLs | Show confidence indicator; optionally apply thresholds |

---

## Data Flow Examples

### Example 1: KP Translation (Pillar 2)
```
User Input:
  Source RPL Line: Design_RPL_line
  Target RPL Line: AsLaid_RPL_line
  Source KP: 50.0 km

Processing:
  1. Load Design_RPL_line geometry
  2. Find point on Design line at KP 50.0
  3. Query RPLComparator.get_point_on_target_at_source_kp(50.0)
     a. Identify geographic point on Design at KP 50.0
     b. Find nearest point on As-Laid line (spatial proximity)
     c. Calculate KP value at that point on As-Laid line
  4. Return: Target KP = 49.8 km, Offset = 0.32 m

Output:
  Result Table:
    source_kp | target_kp | spatial_offset_m
    50.0      | 49.8      | 0.32
```

### Example 2: Dual Reference in Mouse Tool (Pillar 3)
```
User Action:
  1. Opens KP Mouse Tool config
  2. Enables "Compare to Second RPL"
  3. Selects Primary: Design RPL, Secondary: As-Laid RPL

Mouse Movement:
  As user moves mouse over map:
    a. Tool calculates KP on Primary (Design) RPL → 50.123 km
    b. Tool calls RPLComparator to translate to Secondary → 49.805 km
    c. Updates tooltip in real-time
    d. Engineer sees both KPs side-by-side

Tooltip Display:
    Design KP: 50.123
    As-Laid KP: 49.805
    Offset: 0.32 m
    DCC: 2.34 km
```

### Example 3: Batch Cross-Reference (Pillar 4)
```
User Input:
  Source: Design_RPL_points (5 events: Start, Repeater1, Repeater2, Repeater3, End)
  Source Line: Design_RPL_line
  Target Line: As-Laid_RPL_line

Processing:
  For each event in Design_RPL_points:
    1. Get its KP on source line
    2. Translate to target KP using RPLComparator
    3. Create new point on target line at that KP
    4. Copy all attributes + add design_kp field

Output:
  As-Laid_mapped_events:
    fid | event_name | design_kp | design_dcc | as_laid_kp | spatial_offset_m
    1   | Start      | 0.0       | N/A        | 0.0        | 0.0
    2   | Repeater1  | 50.0      | 1.2        | 49.8       | 0.32
    3   | Repeater2  | 100.0     | 0.8        | 99.1       | 0.48
    4   | Repeater3  | 150.0     | 1.5        | 148.9      | 0.61
    5   | End        | 200.0     | N/A        | 199.2      | 0.44
```

---

## Recommendation Summary

### What to Build (Priority Order)

| Priority | Feature | Effort | Value | Users |
|----------|---------|--------|-------|-------|
| 1 | `rpl_comparison_utils.py` (Pillar 1) | **Medium** | **Very High** | All downstream |
| 2 | KP Translator Algorithm (Pillar 2) | Small | High | All |
| 3 | Dual-Reference Mouse Tool (Pillar 3) | Medium | Very High | Daily workflows |
| 4 | Batch Comparison (Pillar 4) | Medium | High | Power users |
| 5 | UI Polish & Documentation | Small | High | All |

### Why This Approach is Powerful & User-Friendly

✅ **Modular**: Each piece works independently but builds on a solid foundation
✅ **Accurate**: Reuses battle-tested KP calculation logic
✅ **User-Friendly**: 
   - Simple UI for simple tasks (translate one KP)
   - Real-time feedback (mouse tool)
   - Batch automation (algorithm)
✅ **Scalable**: Easy to add more features later (interpolation, filtering, export)
✅ **Engineering-Grade**: Reports offsets and confidence metrics
✅ **Integrated**: Uses QGIS Processing native workflows

---

## Next Steps

1. **Validate Core Logic**: Show the `rpl_comparison_utils.py` design to a domain expert
2. **Create Proof-of-Concept**: Build Pillar 1 with unit tests on sample Design vs As-Laid data
3. **Implement Pillar 2**: Get immediate value from KP Translator
4. **Gather User Feedback**: Let engineers use it on real data
5. **Iterate**: Scale up to Pillars 3 & 4

This approach ensures **accuracy (critical for submarine cable engineering), usability (engineers get real value immediately), and scalability (grows with user needs)**.
