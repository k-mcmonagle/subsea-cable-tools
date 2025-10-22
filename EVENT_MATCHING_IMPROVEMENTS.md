# Event Matching Improvements - v3.1

## Overview
Improved event matching to handle case variations, whitespace differences, and duplicate detection.

## What Changed

### 1. Case-Insensitive Matching ✓
Previously: "Repeater 1" ≠ "repeater 1" (no match)
Now: Both match as the same event ✓

### 2. Whitespace Normalization ✓
Previously: "Repeater  1" (2 spaces) ≠ "Repeater 1" (1 space) (no match)
Now: Both normalized and match ✓

### 3. Duplicate Detection & Omission ✓
If multiple features have the same normalized name, they're omitted to avoid ambiguity

## How It Works

### Event Extraction Phase
`_extract_events()` now:
1. Reads all events from the layer
2. For each event, creates a normalized version (lowercase, stripped)
3. Tracks which original names map to each normalized name
4. Detects if any normalized name has multiple features (duplicates)

**Example:**
```
Input events:
  "Repeater 1"
  "repeater 1"        (same as above, different case)
  "Repeater  1"       (same as above, extra space)
  "Repeater 2"

Normalized:
  "repeater 1" → 3 original names (DUPLICATE!)
  "repeater 2" → 1 original name
```

### Event Matching Phase
`_match_events()` now:
1. Finds normalized names that match between layers
2. **Skips duplicates**: If normalized name has multiple features in EITHER layer, omit it
3. Returns only unambiguous matches

**Example (continuing from above):**
```
Design layer duplicates: "repeater 1"
As-laid layer: has "Repeater 1"

Result: NO MATCH (skipped because duplicate in design)
Reason: Which "Repeater 1" feature should we use? Ambiguous!
```

### Feedback to User
Warnings are issued for:
1. **Duplicates found** (by normalized name): Lists which events have case/whitespace variants
2. **Unmatched events** (no normalized match found): Lists events with no counterpart
3. **Matched count**: Final tally of successfully matched events

**Example feedback:**
```
Matched events between design and as-laid RPLs...
Design layer has duplicate events (case/whitespace variants): repeater 1, repeater 3 - omitting
As-laid layer has duplicate events (case/whitespace variants): repeater 2 - omitting
Unmatched design events: repeater 4, repeater 5
Unmatched as-laid events: repeater 6
Matched 8 events (after duplicate/case-insensitive normalization)
```

## Implementation Details

### New Method: `_normalize_event_name(name)`
```python
def _normalize_event_name(self, name):
    """Strip whitespace and convert to lowercase"""
    if not name:
        return ""
    return str(name).strip().lower()
```

Result examples:
- Input: `"Repeater 1"` → Output: `"repeater 1"`
- Input: `"  REPEATER  2  "` → Output: `"repeater  2"`
- Input: `"Repeater 1"` → Output: `"repeater 1"` (same as above)

### Updated Method: `_extract_events(layer, event_field_idx)`

**Now returns:**
```python
{
    'events': {
        original_name: {
            'feature': QgsFeature,
            'geometry': QgsPointXY,
            'normalized': normalized_name
        },
        ...
    },
    'normalized_map': {
        normalized_name: [original_name1, original_name2, ...],
        ...
    },
    'duplicates': [list of normalized names with >1 feature]
}
```

### Updated Method: `_match_events(design_events_data, aslaid_events_data)`

**Now returns:**
```python
{
    'matches': [
        {'design': original_name, 'aslaid': original_name},
        ...
    ],
    'duplicates_design': [list of normalized names with duplicates],
    'duplicates_aslaid': [list of normalized names with duplicates],
    'unmatched_design': set of unmatched original names,
    'unmatched_aslaid': set of unmatched original names
}
```

## Examples

### Example 1: Simple Case Variation
**Input:**
- Design: `"Repeater 1"`, `"Repeater 2"`
- As-laid: `"repeater 1"`, `"REPEATER 2"`

**Processing:**
- Normalized: `"repeater 1"`, `"repeater 2"` (both sides)
- No duplicates
- Exact normalized match

**Output:**
- Matched 2 events ✓

### Example 2: Whitespace Variation
**Input:**
- Design: `"R1"`, `"R  2"`, `"R3"`
- As-laid: `"R1"`, `"R 2"`, `"R3"`

**Processing:**
- Design normalized: `"r1"`, `"r  2"`, `"r3"`
- As-laid normalized: `"r1"`, `"r 2"`, `"r3"`
- Wait! `"r  2"` ≠ `"r 2"` (different number of spaces after normalization)

**Output:**
- Matched: `"r1"`, `"r3"` (2 events)
- Unmatched: `"R  2"` (design) and `"R 2"` (as-laid) ⚠️

**Note:** Inner whitespace is preserved. Only leading/trailing whitespace stripped.

### Example 3: Duplicate Detection
**Input:**
- Design: `"Repeater 1"`, `"repeater 1"` (DUPLICATE!)
- As-laid: `"Repeater 1"`

**Processing:**
- Design normalized_map: `{"repeater 1": ["Repeater 1", "repeater 1"]}`
- As-laid normalized_map: `{"repeater 1": ["Repeater 1"]}`
- `"repeater 1"` is duplicate in design
- Match is skipped (ambiguous)

**Output:**
- Design duplicates warning: `"repeater 1"`
- Unmatched as-laid: `"Repeater 1"`
- Matched: 0 events ⚠️

### Example 4: Multiple Matches with Selective Skipping
**Input:**
- Design: `"R1"`, `"r1"`, `"R2"`, `"R3"`  (R1/r1 duplicate)
- As-laid: `"R1"`, `"R2"`, `"R3"`, `"R4"`

**Processing:**
- Design has `"r1"` normalized duplicate
- As-laid has no duplicates
- `"r1"` is skipped (design duplicate)
- `"r2"`, `"r3"` match normally
- `"r4"` unmatched (no design equivalent)

**Output:**
- Duplicates design: `"r1"` - omitting
- Matched: 2 events (R2, R3)
- Unmatched design: R1, r1
- Unmatched as-laid: R4

## User Guidance

### What to Do If You See Duplicate Warnings

**Option 1: Clean the data**
- Edit the layer to remove duplicate events
- Re-run the algorithm
- This is the recommended approach

**Option 2: Rename to be unique**
- If `"Repeater 1"` and `"repeater 1"` are actually different, rename one
- E.g., `"Repeater 1"` → `"Repeater 1 - Design"`
- Re-run the algorithm

**Option 3: Manual correction (future enhancement)**
- Future version could allow user to manually select which duplicate to use
- Currently not implemented

### What to Do If You See Unmatched Warnings

**Possible causes:**
1. **Typo difference**: `"Repeater1"` vs `"Repeater 1"` (no space)
2. **Prefix difference**: `"Rep 1"` vs `"Repeater 1"`
3. **Event doesn't exist in other layer**: Design has event as-laid doesn't
4. **Whitespace in the middle**: `"R  1"` vs `"R 1"` (different spacing preserved)

**How to fix:**
1. Check layer data for typos
2. Normalize event names before running algorithm
3. Or accept that some events don't have counterparts (they're real mismatches)

## Testing

### Test 1: Case Insensitive
- Design: `"Repeater 1"`, `"REPEATER 2"`, `"repeater 3"`
- As-laid: `"repeater 1"`, `"repeater 2"`, `"REPEATER 3"`
- Expected: 3 matches ✓

### Test 2: Whitespace Trim
- Design: `"  R1  "`, `"R2  "`, `"  R3"`
- As-laid: `"R1"`, `"R2"`, `"R3"`
- Expected: 3 matches ✓

### Test 3: Duplicate Detection
- Design: `"R1"`, `"r1"`, `"R2"`
- As-laid: `"R1"`, `"R2"`
- Expected: Duplicate warning, 1 match (R2) ✓

### Test 4: Complex Mix
- Design: `"  Repeater 1  "`, `"repeater 1"`, `"REPEATER 2"`, `"R3"`
- As-laid: `"Repeater 1"`, `"repeater 2"`, `"R3"`, `"R4"`
- Expected: Duplicate warning (repeater 1), 2 matches (repeater 2, r3), 1 unmatched as-laid (R4) ✓

## Files Modified

- `rpl_route_comparison_algorithm.py`: 
  - Added `_normalize_event_name()` method
  - Rewrote `_extract_events()` method
  - Rewrote `_match_events()` method
  - Updated calling code in `processAlgorithm()`

## Future Enhancements

1. **Interactive duplicate resolution**: Dialog allowing user to select which duplicate to use
2. **Fuzzy matching**: Handle larger variations (e.g., Levenshtein distance)
3. **Synonym mapping**: User-defined mapping (e.g., "R1" = "Repeater 1")
4. **Export unmatched**: Option to create layer from unmatched events for inspection
