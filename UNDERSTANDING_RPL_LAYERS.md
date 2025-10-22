# Understanding RPL Point + Line Layers

## What is an RPL (Route Position List)?

An RPL is the master reference for a submarine cable route. It's typically composed of **TWO layers**:
1. **Point Layer** - Discrete positions/events along the cable
2. **Line Layer** - Continuous cable segments connecting positions

---

## The RPL Structure (In Detail)

### Point Layer (Events)
**What it contains**: Specific locations/events of interest along the cable route

**Typical Features**:
```
PosNo | Event                | Latitude  | Longitude | ApproxDepth | Remarks
------|----------------------|-----------|-----------|-------------|----------
1     | Start of Cable       | 45.123456 | -74.123456| 500m        | Landing point
2     | Repeater 1           | 45.134567 | -74.234567| 4000m       | Repeater amp
3     | Cable Slack Point    | 45.145678 | -74.345678| 3500m       | Slack allowed
4     | Repeater 2           | 45.156789 | -74.456789| 5000m       | Repeater amp
5     | End of Cable         | 45.167890 | -74.567890| 600m        | Landing point
```

**Key Attributes**:
- `PosNo`: Position/feature number
- `Event`: Type of event (StartCable, Repeater, etc.)
- `Latitude`, `Longitude`: Position coordinates
- `DistCumulative`: Distance along route to this position (KP value)
- `ApproxDepth`: Water depth at this location
- `Remarks`: Notes about the position

**Why separate layer?**
- Engineers need to query "where are all repeaters?" or "what's the depth at position X?"
- Point data has different attributes than cable segment data
- Easier to manage/edit specific events

---

### Line Layer (Cable Segments)
**What it contains**: The actual cable route as connected line segments

**Typical Features**:
```
FromPos | ToPos | Bearing | DistBetweenPos | CableCode | BurialDepth | Slack
--------|-------|---------|----------------|-----------|------------|-------
1       | 2     | 125°    | 50.2 km        | SL-56     | 1.2 m      | 2.1%
2       | 3     | 130°    | 12.5 km        | SL-56     | 0.0 m      | 0.5%
3       | 4     | 128°    | 38.1 km        | SL-56     | 2.5 m      | 1.8%
4       | 5     | 132°    | 45.7 km        | SL-56     | 0.8 m      | 1.2%
```

**Key Attributes**:
- `FromPos`, `ToPos`: Connects positions from point layer
- `Bearing`: Direction of segment
- `DistBetweenPos`: Distance between two positions
- `CableCode`: Cable specification/type
- `BurialDepth`: How deep cable is buried in seabed
- `Slack`: Cable slack % in this segment

**The Geometry**:
- **Actual line geometry** from coordinate of FromPos to ToPos
- This is the "truth" for KP calculation
- Multiple segments form the complete cable route

**Why separate layer?**
- Cable segments have different data than individual positions
- Engineers need to analyze routing characteristics (bearing, slack, burial)
- Different operational rules for segments vs. points

---

## How the Two Layers Work Together

### Visual Representation

```
Point Layer                    Line Layer
(Discrete Events)              (Continuous Route)

    P1 (Start)
     •                         P1 ─────────── P2
     |                            (Segment 1)
     |                         Distance: 50.2 km
     |                         Bearing: 125°
     P2 (Repeater 1)           Cable: SL-56
      •                        Burial: 1.2m
      |
      |
     P3 (Slack Point)          P2 ─────── P3
      •                           (Segment 2)
      |                        Distance: 12.5 km
      |                        Bearing: 130°
     P4 (Repeater 2)           Slack: 0.5%
      •
      |
      |
     P5 (End)
      •
      
Geometric Relationship:
- Every line segment connects two consecutive points
- Point geometry = start/end vertices of line segments
- Route topology: P1→P2→P3→P4→P5 (ordered)
```

### Data Relationship

```
Point Layer Record:
  PosNo: 2
  Event: Repeater 1
  Latitude: 45.134567
  Longitude: -74.234567
  DistCumulative: 50.2 km  ← This is the KP value!
  
Line Layer Records:
  Segment 1: FromPos=1, ToPos=2, DistBetweenPos=50.2 km
  Segment 2: FromPos=2, ToPos=3, DistBetweenPos=12.5 km
  
KP Calculation:
  KP at P2 = DistCumulative from P1 + DistBetweenPos(Seg1)
           = 0 + 50.2 = 50.2 km
           
  KP at P3 = KP at P2 + DistBetweenPos(Seg2)
           = 50.2 + 12.5 = 62.7 km
```

---

## Understanding KP (Kilometer Point) Calculation

### What is KP?

**KP (or Chainage)** = cumulative distance from the start of the line

```
Route:        Start ─── 50 km ─── 62.7 km ─── 100.8 km ─── End
              KP 0      KP 50     KP 62.7      KP 100.8     KP 146.5
              
              P1        P2        P3            P4            P5
```

### How KP is Calculated (the algorithm)

The plugin uses **segment-by-segment summation**:

```python
def calculate_kp_at_point(line_geometry, target_point):
    """
    Find the KP value at a specific point on a line.
    """
    total_kp_meters = 0
    
    # Get all vertices of the line
    vertices = line_geometry.asPolyline()  # or asMultiPolyline() for multi-part
    
    # Iterate through segments
    for i in range(len(vertices) - 1):
        vertex1 = vertices[i]
        vertex2 = vertices[i + 1]
        
        # Calculate segment length using geodetic distance
        segment_length = geodetic_distance(vertex1, vertex2)
        
        # If target point is in this segment, interpolate
        if point_is_in_segment(target_point, vertex1, vertex2):
            distance_along_segment = distance_to_point_on_segment(
                target_point, vertex1, vertex2
            )
            total_kp_meters += distance_along_segment
            return total_kp_meters / 1000  # Convert to km
        
        # Otherwise, add full segment and continue
        total_kp_meters += segment_length
    
    return total_kp_meters / 1000  # KP in km
```

**Key Points**:
- Uses **geodetic/ellipsoidal distance** (WGS84 spheroid)
- Handles **any point on the line** (interpolates if between vertices)
- Works for **multi-part lines** (multiple disconnected segments)
- **Cumulative** from start of line

---

## Why This Matters for RPL Comparison

### Design RPL vs. As-Laid RPL

When you import two RPLs, you get:

```
Design RPL (Imported from Excel)
├─ design_points (Point Layer)
│   └─ P1, P2, P3, P4, P5
└─ design_lines (Line Layer)
    └─ Segments connecting P1→P2→P3→P4→P5
    
As-Laid RPL (From actual cable survey)
├─ aslaid_points (Point Layer)
│   └─ P1', P2', P3', P4', P5'
└─ aslaid_lines (Line Layer)
    └─ Segments connecting P1'→P2'→P3'→P4'→P5'
```

**The Problem**:
- Design P2 (Repeater) is at KP 50.2 on design line
- But where is it on the as-laid line?
- It's *spatially nearby* to as-laid line, but NOT at same KP!

**Why?**
- Different survey accuracy/method
- Actual cable routing avoided obstacles
- Seabed conditions required deviations

**The Solution**:
- Find geographic location of Design P2
- Find nearest point on as-laid line
- Calculate KP at that nearest point = "translated KP"

```
Design Line:    P1 ────────── P2 ────────── P3
                KP 0          KP 50.2        KP 62.7

As-Laid Line:   P1' ──────── P2' ──────── P3'
                KP 0         KP 49.8      KP 61.1
                  ▲          ▲
                  │ Same geographic location (approximately)
                  └─ Spatial proximity matching
                  
Translation Result:
  Design KP 50.2 → As-Laid KP 49.8 (with 0.32m spatial offset)
```

---

## Working with RPL Point + Line Layers in Code

### From Import Excel RPL Algorithm

```python
# The import creates TWO outputs from ONE Excel file:

# Output 1: Points Layer (RPL_points)
points_fields = QgsFields()
points_fields.append(QgsField('PosNo', QVariant.Int))
points_fields.append(QgsField('Event', QVariant.String))
points_fields.append(QgsField('DistCumulative', QVariant.Double))
points_fields.append(QgsField('ApproxDepth', QVariant.Double))
points_fields.append(QgsField('Latitude', QVariant.Double))
points_fields.append(QgsField('Longitude', QVariant.Double))
# ... more fields

# For each point row in Excel:
for row_num, row_data in enumerate(excel_data):
    if row_num % 2 == 0:  # Even rows = points
        point_feature = QgsFeature(points_fields)
        point_geom = QgsGeometry.fromPointXY(
            QgsPointXY(lon, lat)
        )
        point_feature.setGeometry(point_geom)
        points_sink.addFeature(point_feature)

# Output 2: Lines Layer (RPL_lines)
lines_fields = QgsFields()
lines_fields.append(QgsField('FromPos', QVariant.Int))
lines_fields.append(QgsField('ToPos', QVariant.Int))
lines_fields.append(QgsField('Bearing', QVariant.Double))
lines_fields.append(QgsField('DistBetweenPos', QVariant.Double))
lines_fields.append(QgsField('Slack', QVariant.Double))
# ... more fields

# For each line row in Excel (connecting consecutive points):
for row_num, row_data in enumerate(excel_data):
    if row_num % 2 == 1:  # Odd rows = lines
        line_feature = QgsFeature(lines_fields)
        line_geom = QgsGeometry.fromPolylineXY([
            prev_point_coords,
            curr_point_coords
        ])
        line_feature.setGeometry(line_geom)
        lines_sink.addFeature(line_feature)
```

### How KP Mouse Tool Uses Both Layers

```python
# Simplified version from kp_mouse_maptool.py

# The tool receives the LINE layer (not point layer)
def __init__(self, canvas, line_layer, ...):
    self.layer = line_layer  # This is the line layer!
    
    # Cache all line geometries for fast lookup
    self.features_geoms = []
    for feature in line_layer.getFeatures():
        self.features_geoms.append(feature.geometry())
    
def mouseMoveEvent(self, event):
    mouse_point = self.toMapCoordinates(event.pos())
    
    # Find closest point on ANY line segment
    closest_point_on_line = None
    min_dist = float('inf')
    
    for feature_geom in self.features_geoms:
        closest_pt_geom = feature_geom.nearestPoint(mouse_point)
        dist = distance_calc.measureLine(
            mouse_point, 
            closest_pt_geom.asPoint()
        )
        if dist < min_dist:
            min_dist = dist
            closest_point_on_line = closest_pt_geom.asPoint()
    
    # Calculate KP along the line
    kp_km = calculate_kp_to_point(closest_point_on_line)
    
    # Display in tooltip
    tooltip = f"KP: {kp_km:.3f}\nDCC: {min_dist:.2f} m"
```

**Key Insight**: 
- The mouse tool only uses the **LINE layer** (for KP calculation)
- The **POINT layer** is optional (useful for querying events, but not needed for KP calculation)
- You can calculate KP to ANY point on the line, not just existing point features

---

## For RPL Comparison Tool Design

### What You Need to Access

```python
class RPLComparator:
    def __init__(self, source_line_layer, target_line_layer):
        """
        Take the LINE layers from both RPLs.
        The point layers are optional (for cross-referencing events later).
        """
        self.source_line = source_line_layer  # e.g., design_lines
        self.target_line = target_line_layer  # e.g., aslaid_lines
        
        # Cache geometries for performance
        self.source_geoms = [f.geometry() for f in source_line_layer.getFeatures()]
        self.target_geoms = [f.geometry() for f in target_line_layer.getFeatures()]
        
    def translate_kp(self, source_kp_km):
        """
        Given a KP on source line, find equivalent on target line.
        
        1. Find geographic point on source line at source_kp_km
        2. Find nearest point on target line
        3. Calculate KP on target line
        """
        # Step 1: Get point on source line at source_kp_km
        source_point = self.get_point_at_kp(
            self.source_geoms, 
            source_kp_km * 1000
        )
        
        # Step 2: Find nearest on target line
        target_point = self.nearest_point_on_lines(
            self.target_geoms,
            source_point
        )
        
        # Step 3: Calculate KP on target
        target_kp_km = self.calculate_kp_to_point(
            self.target_geoms,
            target_point
        )
        
        # Step 4: Calculate offset (how far source_point is from target line)
        offset_m = self.distance_to_line(source_point, self.target_geoms)
        
        return {
            'source_kp': source_kp_km,
            'target_kp': target_kp_km,
            'spatial_offset_m': offset_m,
            'geographic_point': target_point
        }
```

### Optional: Cross-Reference Using Point Layers

```python
def cross_reference_events(self, source_point_layer):
    """
    Optional: If you want to match point events between RPLs.
    """
    results = []
    
    for source_event_feature in source_point_layer.getFeatures():
        source_point = source_event_feature.geometry().asPoint()
        
        # Find nearest point on target line
        target_point = self.nearest_point_on_lines(
            self.target_geoms,
            source_point
        )
        
        # Get KP at this point
        target_kp = self.calculate_kp_to_point(
            self.target_geoms,
            target_point
        )
        
        results.append({
            'event': source_event_feature['Event'],
            'source_kp': source_event_feature['DistCumulative'],
            'target_kp': target_kp,
            'offset': distance(source_point, target_point)
        })
    
    return results
```

---

## Summary: How to Think About It

| Aspect | Point Layer | Line Layer |
|--------|------------|-----------|
| **Contains** | Discrete events/positions | Continuous cable route |
| **Geometry** | Points | LineString (possibly multi-part) |
| **Attributes** | Event-specific (repeater, slack, etc.) | Segment-specific (bearing, burial, slack%) |
| **For KP Calculation** | Optional | Required ✓ |
| **For Cross-Reference** | Useful (find events) | Required ✓ |
| **Data Volume** | Small (5-50 features) | Variable (1-1000+ vertices) |

**For your RPL Comparison tool:**
- **Must use**: Line layers (for KP calculation)
- **Optional**: Point layers (for event cross-reference)
- **Algorithm works on**: Geometric proximity + distance calculations
- **Output**: KP translation + confidence metrics (spatial offset)
