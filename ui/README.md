# PhotoCrop / DrawingCrop

Engineering drawing annotation tool — select named views (crops) from images or PDFs and export them as a structured ZIP.

## Two ways to run

### Desktop app (PyQt6)

Requires Python 3.10+ and pip.

```bash
pip3 install -r requirements.txt
python3 main.py
```

### Web version (Docker)

Requires Docker Desktop.

```bash
docker compose up --build
```

Open http://localhost:8080 in your browser. No Python needed.

## How to use

### Desktop app
1. Click **Open Image** (or File → Open Image / Cmd+O) and select a JPG, PNG, WEBP, TIFF, BMP, or PDF.
   - PDFs are automatically converted to PNG (150 DPI) and saved alongside the original before loading.
2. **Draw a selection** by clicking and dragging on the image. Drag the corner handles to resize.
3. Pick a **preset view name** from the dropdown (front, back, left, right, side, top, bottom, detail, section cut) or type a **custom name** in the text field — the custom name overrides the preset.
4. Click **Save View** (or Cmd+S). The crop is saved to a folder named after the image, created next to the source file.
5. Press **Esc** or click **Clear Selection** to start a new selection.
6. Click **Open Output Folder** to reveal the output directory in Finder.

### Web / Docker app
1. Open a drawing with **Open Drawing** or drag-and-drop — accepts JPG, PNG, WEBP, TIFF, BMP, and **PDF**.
2. Draw a selection rectangle; drag corner handles to resize.
3. Choose a preset view name or type a custom name, then click **Queue View**.
4. Optionally tally engineering features (Boss Extrude, Fillets, etc.) in the Feature Tally panel.
5. Click **⬇ Download ZIP** to get a single archive with everything.

## Output structure

### Desktop app
```
/path/to/source/
  image.jpg          ← original source
  image/
    image.jpg        ← copy of original
    image_front.jpg  ← saved crops
    image_back.jpg
    image_section_cut.jpg
```

### Web / Docker ZIP
```
drawing_name/
  drawing_name.png              ← full original drawing
  drawing_name_front.png        ← saved crops
  drawing_name_section_cut.png
  drawing_name_features.txt     ← feature tally (if used)
```

## Project files

| File | Purpose |
|------|---------|
| `main.py` | Entry point — creates QApplication and shows AppWindow |
| `app_window.py` | Main window: toolbar, sidebar, open/save logic |
| `canvas_widget.py` | Image display + rubber-band selection with corner handles |
| `file_manager.py` | Pure functions: sanitize names, save crops, PDF→PNG conversion |
| `tests/test_file_manager.py` | Unit tests for file_manager (run with `pytest`) |
| `web/index.html` | Self-contained browser app (no backend needed) |
| `web/Dockerfile` | nginx container — downloads pdf.js at build time |
| `docker-compose.yml` | Compose config — exposes port 8080 |

## Tests

```bash
pytest
```

All 16 tests should pass.
