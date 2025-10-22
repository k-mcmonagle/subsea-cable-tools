# RPL Comparison Feature - Quick Reference Guide

## The Problem You're Solving

```
Design RPL      As-Laid RPL
(Survey 2023)   (Survey 2024)

KP 0 ───────── KP 0
    │ Design    │ Actual
    │ Route     │ Route (slightly different)
    ├──X ────── ├──X  
    │ KP 25     │ KP 24.5 (different distance!)
    │           │
    └─ KP 50 ── └─ KP 49 (cumulative drift)
    
User Question:
"What KP on As-Laid corresponds to Design KP 25?"

Answer: KP 24.5 (with 0.32m spatial offset)
```

---

## What You'll Build (4 Components)

### 🏗️ Component 1: Shared Engine (Core)
**File**: `processing/rpl_comparison_utils.py`

```python
class RPLComparator:
    def get_point_on_target_at_source_kp(self, source_kp_km):
        """Design KP 25 → As-Laid KP 24.5 + offset"""
        
    def cross_reference_feature(self, source_point):
        """Design Repeater → As-Laid location"""
        
    def build_kp_lookup_table(self, interval_km):
        """Design KP 0,1,2,3... → As-Laid KP 0,0.98,1.96...→"""
```

**Why Separate:**
- Single source of truth for accuracy
- Reusable by all tools (mouse tool, algorithms)
- Testable independently
- Same proven logic as existing tools

---

### 🎯 Component 2: KP Translator Tool (Quick Win)
**File**: `processing/translate_kp_between_rpls_algorithm.py`

```
User Workflow:
┌────────────────────────────────────┐
│ KP Translator Between RPLs        │
├────────────────────────────────────┤
│ Source Line:  [Design RPL ▼]       │
│ Target Line:  [As-Laid RPL ▼]      │
│ Source KP:    [  50.0  ] km        │
│                                    │
│ ✓ Calculate                        │
├────────────────────────────────────┤
│ Results:                           │
│ Source KP:        50.000 km        │
│ Target KP:        49.805 km        │
│ Spatial Offset:   0.32 m           │
│ Confidence:       ✓ High           │
└────────────────────────────────────┘

Output: New layer with result
```

**Use Cases:**
- "Where is Design KP 50 on As-Laid?"
- "Mark Design survey stations on actual line"
- Batch processing from CSV

---

### 🗺️ Component 3: Dual Reference Mouse Tool (Real-time)
**File**: Enhance `maptools/kp_mouse_maptool.py`

```
Configuration Dialog:
┌─ KP Mouse Tool Config ─────────────┐
│ Primary RPL:    [Design RPL ▼]     │
│ Secondary RPL:  [As-Laid RPL ▼]    │
│ Unit:           [km ▼]             │
│ Show Reverse KP: [✓]               │
│                                    │
│ □ Dual-RPL Mode (NEW!)             │
│   When enabled, shows both RPLs    │
│   side-by-side in tooltip          │
└────────────────────────────────────┘

Live Tooltip Output:
┌─────────────────────────┐
│ Design KP:    50.123 km │
│ As-Laid KP:   49.805 km │
│ Offset:       0.32 m    │
│ DCC:          2.34 km   │
└─────────────────────────┘
  ↑ Updates in real-time as user moves mouse
```

**Benefits:**
- See both KPs simultaneously
- Visual verification
- Spot large deviations (e.g., loops)
- No algorithm overhead

---

### 📊 Component 4: Batch Comparison (Power Users)
**File**: `processing/batch_rpl_comparison_algorithm.py`

```
User Workflow:
┌────────────────────────────────────┐
│ Batch RPL Comparison               │
├────────────────────────────────────┤
│ Source Line:      [Design Line ▼]  │
│ Target Line:      [As-Laid Line ▼] │
│ Cross-ref Events: [Design Events]  │
│ Interval (km):    [  1.0  ]        │
│                                    │
│ ✓ Compare                          │
└────────────────────────────────────┘

Outputs:

1. KP Lookup Table (points every 1 km):
   fid | design_kp | as_laid_kp | offset_m
   1   | 0.0       | 0.0        | 0.0
   2   | 1.0       | 0.98       | 0.15
   3   | 2.0       | 1.96       | 0.28
   ...

2. Cross-referenced Events (if provided):
   fid | event_name | design_kp | as_laid_kp | offset_m
   1   | Start      | 0.0       | 0.0        | 0.0
   2   | Repeater1  | 50.0      | 49.8       | 0.32
   3   | Repeater2  | 100.0     | 99.1       | 0.48
   
3. Statistics Report:
   Mean offset:     0.38 m
   Max offset:      1.2 m
   Min offset:      0.0 m
   Largest gap:     KP 100-105
```

**Use Cases:**
- Generate Design↔As-Laid correspondence table
- Update cable events with new KP values
- Identify problematic routing deviations
- Compliance verification

---

## Key Design Principles

### ✅ Accuracy First
- Reuses proven geodetic distance calculations from mouse tool
- Segment-by-segment KP calculation (handles complex geometries)
- Reports spatial offset (engineer can validate)

### ✅ Modularity
- Core engine separate from UI/tools
- Any new tool can use the shared engine
- Easy to test independently

### ✅ Scalability
- Simple tools for simple tasks (single KP translation)
- Complex algorithms for power users (batch processing)
- Extensible (easy to add interpolation, filtering later)

### ✅ User-Friendly
- Checkbox to enable dual-RPL mode (non-intrusive)
- Real-time feedback (mouse tool)
- Processing toolbox integration (familiar QGIS workflow)
- Clear output fields and statistics

---

## Data Accuracy & Validation

### How Accuracy is Guaranteed

| Aspect | Method |
|--------|--------|
| **Distance Calculation** | `QgsDistanceArea` with ellipsoidal geodesy (same as mouse tool) |
| **Line Geometry** | `unaryUnion` handles multi-part lines correctly |
| **KP Calculation** | Cumulative segment length from start (proven in production) |
| **Nearest Point** | Spatial proximity + segment-by-segment search |
| **Confidence Reporting** | Spatial offset shown to user (>1km = warning) |

### Validation Strategy

1. **Unit Tests**: Core engine against sample Design/As-Laid data
2. **Visual Inspection**: Mouse tool shows translations in real-time
3. **Statistics**: Batch tool reports mean/max/min offsets
4. **User Feedback**: Offset field lets engineer spot anomalies

---

## Implementation Roadmap

```
Phase 1 (Sprint 1-2): Build Foundation
  └─ rpl_comparison_utils.py
     • RPLComparator class
     • Unit tests
     • Performance profiling

Phase 2 (Sprint 3): Quick Win
  └─ translate_kp_between_rpls_algorithm.py
     • Single KP translation
     • CSV batch input
     • Minimal UI

Phase 3 (Sprint 4): Interactive
  └─ Enhance kp_mouse_maptool.py
     • Dual-RPL checkbox
     • Real-time translation
     • Tooltip updates

Phase 4 (Sprint 5-6): Scale It
  └─ batch_rpl_comparison_algorithm.py
     • Multi-feature cross-reference
     • Event mapping
     • Statistics reporting

Phase 5 (Sprint 7): Polish
  └─ Documentation + Testing
     • Help panels
     • CHANGELOG update
     • Edge case validation
```

---

## Why This Approach Works for Submarine Cable Engineering

### 🎯 Accuracy (The #1 Requirement)
- Uses proven algorithms already in production
- Reports confidence metrics (spatial offset)
- No "black box" calculations

### 🔧 Flexibility
- Simple interface for simple tasks
- Powerful algorithms for complex workflows
- Extensible for future needs (interpolation, smoothing, etc.)

### 👨‍💼 Professional Workflow Integration
- Integrates with QGIS Processing (standard tool)
- Exports to tables/CSV (integrates with external analysis)
- Real-time verification (mouse tool)
- Batch automation (scales to large datasets)

### 📈 User Progression
- **Beginner**: "Use KP Translator tool once"
- **Intermediate**: "Use mouse tool for real-time reference"
- **Advanced**: "Batch process entire event lists"

---

## Questions to Validate Design

Before implementation, confirm:

1. **Data Format**: Are Design & As-Laid RPLs always separate layer pairs (point + line)?
2. **Coordinate Systems**: Do they always share same CRS, or should tool transform?
3. **Scale**: Typical project: how many events? How long are cables (10km? 1000km+)?
4. **Accuracy Need**: Sub-meter? Meter-level? Kilometer-level?
5. **Workflow**: Do engineers need batch processing, or mostly single KP lookups?
6. **Deviation Tolerance**: What offset distance triggers a "warning"? (1m? 100m?)

---

## Summary

This design creates a **powerful, accurate, user-friendly system** for comparing RPLs:

- 🏗️ **Modular**: Core engine + pluggable tools
- 📐 **Accurate**: Proven geodetic calculations + confidence metrics
- 🚀 **Scalable**: From simple single-KP lookup to batch automation
- 👨‍💼 **Professional**: Statistics, reporting, edge case handling
- 🧪 **Testable**: Separate concerns make testing straightforward

The engineer can immediately use it (KP Translator), enhance their workflow with it (mouse tool), and scale it for production (batch processor).
