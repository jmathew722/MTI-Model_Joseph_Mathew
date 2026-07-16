# Hole Extraction Verification Report — before/after

Positions are `positions_xy_meters` from each part's build plan (corner-origin frame). `source` and `confidence` are the new additive fields; BEFORE plans predate them.

## VPDF-01 (synthetic VECTOR PDF — 4x D0.600 plate, positions NOT in the extraction)
exit before=0 after=0

### BEFORE (vision only)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F002 | (0.1905, 0.127) | (legacy: none) | 0.0 | 0.01524 | LOW |

### AFTER (vector)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F002 | (0.0635, 0.0635) | pdf_vector | 0.95 | 0.01524 | HIGH |
| F002 | (0.0635, 0.1905) | pdf_vector | 0.95 | 0.01524 | HIGH |
| F002 | (0.3175, 0.0635) | pdf_vector | 0.95 | 0.01524 | HIGH |
| F002 | (0.3175, 0.1905) | pdf_vector | 0.95 | 0.01524 | HIGH |

## VDXF-01 (synthetic VECTOR DXF — 4x D0.600 plate, positions NOT in the extraction)
exit before=0 after=0

### BEFORE (vision only)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F002 | (0.1905, 0.127) | (legacy: none) | 0.0 | 0.01524 | LOW |

### AFTER (vector)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F002 | (0.0635, 0.0635) | dxf_entity | 0.97 | 0.01524 | HIGH |
| F002 | (0.0635, 0.1905) | dxf_entity | 0.97 | 0.01524 | HIGH |
| F002 | (0.3175, 0.0635) | dxf_entity | 0.97 | 0.01524 | HIGH |
| F002 | (0.3175, 0.1905) | dxf_entity | 0.97 | 0.01524 | HIGH |

## 135-A (real drawing — SCANNED PDF → raster/Hough fallback)
exit=0  source_pdf=A001351E.PDF
> [2.2/4] Vector hole extraction: 0/1 callout(s) placed exactly from pdf
> [WARN]  extraction: [HIGH] hole-position: H001: vector
> [WARN]  extraction: hole-position: RASTER: this PDF page
> [WARN]  extraction: hole-position: RASTER source: hole

### BEFORE (existing baseline plan)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F002 | (-, -) | (legacy: none) |  | - |  |

### AFTER (this run)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F002 | (0.0127, 0.0127) | vision | 0.0 | 0.0079375 | HIGH |
| F002 | (0.0635, 0.0127) | vision | 0.0 | 0.0079375 | HIGH |

## 117-C-RevB (real drawing — SCANNED PDF → raster/Hough fallback)
exit=0  source_pdf=A001171E.PDF
> [2.2/4] Vector hole extraction: 0/6 callout(s) placed exactly from pdf
> [WARN]  extraction: [HIGH] hole-position: H001: vector
> [WARN]  extraction: [HIGH] hole-position: H002: vector
> [WARN]  extraction: [HIGH] hole-position: H003: vector

### BEFORE (existing baseline plan)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F002 | (-, -) | (legacy: none) |  | - |  |
| F003 | (-, -) | (legacy: none) |  | - |  |
| F004 | (-, -) | (legacy: none) |  | - |  |
| F005 | (-, -) | (legacy: none) |  | - |  |
| F006 | (-, -) | (legacy: none) |  | - |  |

### AFTER (this run)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F002 | (0.056388, 0.048387) | (legacy: none) | 0.0 | 0.01666875 | HIGH |
| F003 | (0.056388, 0.048387) | (legacy: none) | 0.0 | 0.00714375 | LOW |
| F004 | (0.056388, 0.048387) | (legacy: none) | 0.0 | 0.02301875 | CRITICAL |
| F005 | (0.056388, 0.048387) | (legacy: none) | 0.0 | 0.00635 | LOW |
| F006 | (0.056388, 0.048387) | (legacy: none) | 0.0 | 0.00635 | LOW |

## 116-C-Rev90B (real drawing — SCANNED PDF → raster/Hough fallback)
exit=0  source_pdf=A001131E.PDF
> [2.2/4] Vector hole extraction: 0/2 callout(s) placed exactly from pdf
> [WARN]  extraction: [HIGH] hole-position: H001: vector
> [WARN]  extraction: [HIGH] hole-position: H002: vector
> [WARN]  extraction: hole-position: RASTER: this PDF page

### BEFORE (existing baseline plan)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F003 | (-, -) | (legacy: none) |  | - |  |
| F002 | (-, -) | (legacy: none) |  | - |  |

### AFTER (this run)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F003 | (0.041275, 0.030226) | (legacy: none) | 0.0 | 0.01016 | LOW |
| F002 | (0.041275, 0.030226) | (legacy: none) | 0.0 | 0.014732 | LOW |

## 115_C-RevB (real drawing — SCANNED PDF → raster/Hough fallback)
exit=0  source_pdf=A001151E.PDF
> [2.2/4] Vector hole extraction: 0/3 callout(s) placed exactly from pdf
> [WARN]  extraction: [HIGH] hole-position: H001: vector
> [WARN]  extraction: [HIGH] hole-position: H002: vector
> [WARN]  extraction: [HIGH] hole-position: H003: vector

### BEFORE (existing baseline plan)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F003 | (-, -) | (legacy: none) |  | - |  |
| F006 | (-, -) | (legacy: none) |  | - |  |
| F004 | (-, -) | (legacy: none) |  | - |  |
| F005 | (-, -) | (legacy: none) |  | - |  |

### AFTER (this run)
| feature | position (m) | source | conf | dia (m) | tier |
|---|---|---|---|---|---|
| F003 | (0.005461, 0.034925) | (legacy: none) | 0.0 | 0.01031875 | LOW |
| F003 | (0.029337, 0.034925) | (legacy: none) | 0.0 | 0.01031875 | LOW |
| F003 | (0.053213, 0.034925) | (legacy: none) | 0.0 | 0.01031875 | LOW |
| F003 | (0.077089, 0.034925) | (legacy: none) | 0.0 | 0.01031875 | LOW |
| F006 | (0.041275, 0.034925) | (legacy: none) | 0.0 | 0.014684375 | LOW |
| F004 | (-, -) | (legacy: none) | 0.0 | 0.014684375 | LOW |
| F005 | (0.041275, 0.034925) | (legacy: none) | 0.0 | - | LOW |
