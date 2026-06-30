# Image Crop Views App — Design Spec
**Date:** 2026-06-16  
**Status:** Approved

---

## Overview

A PyQt6 desktop application that lets the user open a single image, draw rectangular selections over it, name each selection, and save them as cropped image files. All crops and the original are stored together in an auto-created folder, named after the source image.

---

## User Workflow

1. Launch the app (`python main.py`)
2. Click **Open Image** to load a photo (JPG, PNG, WEBP, TIFF, BMP)
3. Click and drag on the canvas to draw a rectangular selection
4. Drag corner handles to resize the selection
5. Choose a view name from the **Preset** dropdown, or type a **Custom** name (custom overrides preset)
6. Click **Save View**
7. Repeat steps 3–6 for each additional view
8. Click **Open Output Folder** to reveal the folder in Finder

---

## UI Layout

**Main window** — title bar shows `PhotoCrop — [filename]`

```
┌──────────────────────────────────────────────────────┐
│ Menu: File  Help                                      │
├──────────────────────────────────────────────────────┤
│ [Open Image]  [Clear Selection]      filename — WxH  │
├────────────────────────────────────┬─────────────────┤
│                                    │  VIEW NAME      │
│         Image Canvas               │  Preset: [▾]   │
│                                    │  Custom: [   ] │
│   ┌ - - - - - - ┐                  │                 │
│   |  selection  |                  │  [Save View]   │
│   |  WxH px     |                  │                 │
│   └ - - - - - - ┘                  │  ── Saved ──   │
│                                    │  ✓ name_front  │
│                                    │  ✓ name_back   │
│  drag to draw · Esc to clear       │                 │
│                                    │  [Open Folder] │
├────────────────────────────────────┴─────────────────┤
│ Selection: W × H px at (x, y)       Output: path/   │
└──────────────────────────────────────────────────────┘
```

**Canvas behaviour:**
- Image is scaled to fit the canvas (fit-to-window), maintaining aspect ratio, centred on a dark background
- Selection rectangle drawn with a dashed cyan border and darkened overlay outside the selection
- Four corner handles (10×10 px squares) for resize
- Hint text at canvas bottom: `drag to draw selection · drag corners to resize · Esc to clear`
- Selection dimensions shown inside the rectangle in original-image pixels

**Right sidebar (170 px fixed width):**
- Section label: "VIEW NAME"
- Preset dropdown — ordered list: `front, back, left, right, top, bottom, detail, section cut`
- Custom name text field — placeholder `e.g. logo_area`; when non-empty, this value is used as the name (overrides preset)
- **Save View** button — primary action colour
- Saved list — each saved view shown as `✓ [stem]_[name]`
- **Open Output Folder** button at sidebar bottom — reveals folder in system file manager

---

## Architecture

Four source files in the project root:

| File | Responsibility |
|---|---|
| `main.py` | Entry point — creates `QApplication`, instantiates `AppWindow`, runs event loop |
| `app_window.py` | `QMainWindow` subclass — owns toolbar, canvas, sidebar; wires signals between them |
| `canvas_widget.py` | `QGraphicsView` subclass — image display, rubber-band draw, corner-handle resize; exposes selected rect in original-image pixel coordinates |
| `file_manager.py` | Pure functions — creates output folder, copies original, saves crop via Pillow |

---

## Data Flow

```
User drags canvas
  → canvas_widget tracks QMouseEvent coords
  → maps display coords → original-image coords
  → emits selection_changed(QRect) signal

User clicks Save View
  → app_window reads rect from canvas_widget
  → reads name from sidebar (custom field takes priority over dropdown)
  → calls file_manager.save_crop(source_path, rect, name)
    → creates [stem]/ folder next to source if absent
    → copies original into folder if absent
    → opens source with Pillow, crops to rect, saves as [stem]_[name].[ext]
  → sidebar Saved list appends ✓ entry
  → status bar updates output path
```

---

## Output Folder Structure

Folder is created **next to the source image**, named after the image stem.

```
photo_001/
├── photo_001.jpg          ← original, copied on first save
├── photo_001_front.jpg
├── photo_001_back.jpg
├── photo_001_section_cut.jpg
└── photo_001_logo_area.jpg
```

File extension matches the source image's extension.

**Filename sanitization:** View names are sanitized before use in filenames — spaces replaced with underscores, special characters (`/ \ : * ? " < > |`) stripped. The original unsanitized name is shown in the sidebar Saved list.

---

## Preset View Names

`front`, `back`, `left`, `right`, `top`, `bottom`, `detail`, `section cut`

---

## Supported Image Formats

Open: JPG, JPEG, PNG, WEBP, TIFF, TIF, BMP  
Save: same extension as source (Pillow handles encoding)

---

## Error Handling

| Condition | Behaviour |
|---|---|
| No image loaded | Save View button disabled |
| No selection drawn | Save View button disabled |
| Name field empty (custom) and no preset selected | Save View button disabled |
| Name already used for this image | Confirmation dialog: overwrite? |
| Unsupported file format on open | `QMessageBox` error: "Unsupported file format" |
| Write permission error on save | `QMessageBox` error with system error message |
| Open Output Folder before any save | Button disabled until first crop is saved |
| User opens a new image mid-session | Canvas clears, Saved list clears, window title updates, output folder path resets — no confirmation needed (saves are already on disk) |

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `Ctrl+O` | Open Image |
| `Esc` | Clear selection |
| `Ctrl+S` | Save View (if enabled) |

---

## Dependencies

```
PyQt6
Pillow
```

Install: `pip install PyQt6 Pillow`

---

## Out of Scope (v1)

- Batch processing multiple images in one session
- Undo/redo
- Zoom/pan on the canvas
- Freehand / polygon selection
- Configurable preset list
- Export to formats other than source format
