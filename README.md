# Subsea Cable Tools

**Subsea Cable Tools** is a QGIS Plugin created to support working with subsea telecom and power cable data in QGIS.


---

## 🚀 Features

### 🔧 Processing Algorithms
Accessible via the QGIS Processing Toolbox under **Subsea Cable Tools**:
- **Import Excel RPL** – Load route position lists (RPLs) as LineStrings from Excel.
- **Import Bathy from MDB** – Extract bathymetric data from `.mdb` survey databases.
- **Highlight KP Ranges** – Visualise KP ranges on cable routes from a CSV file.
- **Highlight KP Range** - Visualise a single KP range on a cable route.
- **Find Nearest KP** – Get the closest KP on a cable route to a layer of point(s) data.
- **Place KP Points** - Place points along a cable route at a specified interval.
- **Place KP Points from CSV** - Place points along a cable route from a CSV file.
- **Place Single KP Point** - Place a single point along a cable route at a specified KP.


### 🗺️ Map Tool
- **KP Mouse Tool** – An interactive tool that provides the closest KP and DCC of the mouse pointer dynamically. Now features a persistent tooltip for improved usability.

---
## ✨ Version 1.1.0

This version includes several new features and improvements:
- Enhanced KP Mouse Tool with persistent tooltip, improved usability, and continuous KP calculation.
- Added new tools for placing KP points.
- Improved KP Range Highlighter tools.
- Fixed several bugs, including QGIS API compatibility issues and nearest KP calculation.

It's a new tool and not extensively tested yet, so apologies if you encounter issues or find the documentation lacking. I'll continue to test and improve the tools.
