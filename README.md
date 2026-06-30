# How to Run DrawingCrop (Web / Docker)

DrawingCrop is an engineering drawing annotation tool. The web version runs entirely in Docker — no Python installation required. It consists of two services:

- **drawingcrop** — the browser UI (nginx serving `web/index.html`) on port 8080
- **api** — a Python/Flask backend that handles DWG/DXF conversion and 2D→3D generation

---

## Prerequisites

| Requirement | Where to get it |
|---|---|
| Docker Desktop | https://www.docker.com/products/docker-desktop |
| Anthropic API key | https://console.anthropic.com → API Keys |

Make sure Docker Desktop is **open and running** before proceeding (look for the whale icon in your taskbar).

---

## Step 1 — Set your API key

The backend requires an Anthropic API key to run the 2D→3D generation pipeline.

Create (or edit) the file `ui/.env` in this project and add your key:

```
ANTHROPIC_API_KEY=
```

A template `.env` file is already included in the `ui/` folder — just replace `your-key-here` with your real key and save.

> **Never commit your `.env` file.** It is listed in `.gitignore` and should stay local.

---

## Step 2 — Build and start the containers

Open a terminal, navigate to the `ui/` folder, and run:

```bash
docker compose up --build
```

The first build takes a few minutes — Docker compiles LibreDWG from source and installs all Python packages. Subsequent starts (without `--build`) are much faster.

You will see logs from both services stream in your terminal. The app is ready when you see lines like:

```
drawingcrop-1  | nginx: ... start worker processes
api-1          | * Running on http://0.0.0.0:5000
```

---

## Step 3 — Open the app

Open your browser and go to:

```
http://localhost:8080
```

---

## How to use the web app

1. **Open a drawing** — click **Open Drawing** or drag and drop a file onto the page.
   - Accepted formats: JPG, PNG, WEBP, TIFF, BMP, PDF, DWG, DXF
   - DWG/DXF files are automatically converted to PNG by the API backend.
2. **Draw a selection** — click and drag on the image to draw a rectangle. Drag the corner handles to resize it.
3. **Name the view** — choose a preset view name from the dropdown (front, back, left, right, top, bottom, side, detail, section cut) or type a custom name.
4. **Queue the view** — click **Queue View**. Repeat for as many views as you need.
5. **Tally features** *(optional)* — use the Feature Tally panel to count engineering features (Boss Extrude, Fillets, Holes, etc.).
6. **Download results** — click **⬇ Download ZIP** to get a single archive containing all your crops, the original drawing, and the feature tally.

### 2D → 3D Generation *(optional)*

After queuing views, click **Generate 3D Model**. The backend runs the MTI pipeline (powered by Claude AI) and produces:

- A `.stl` 3D preview file you can view directly in the browser
- A `.zip` archive containing SolidWorks macros (`.vba`) and, if SolidWorks is available, a `.sldprt` part file

Generation typically takes **1–3 minutes** depending on drawing complexity.

---

## ZIP output structure

```
drawing_name/
  drawing_name.png              ← full original drawing
  drawing_name_front.png        ← saved view crop
  drawing_name_section_cut.png  ← another saved crop
  drawing_name_features.txt     ← feature tally (if used)
```

---

## Stopping the app

Press `Ctrl+C` in the terminal where `docker compose up` is running, then optionally run:

```bash
docker compose down
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot connect to Docker daemon` | Docker Desktop is not running — open it and wait for "Engine running" |
| `ANTHROPIC_API_KEY variable is not set` | Make sure `ui/.env` exists and contains your key |
| Port 8080 already in use | Stop whatever is using port 8080, or change the port in `docker-compose.yml` (`"8080:80"` → `"9090:80"`) and open `http://localhost:9090` |
| DWG conversion fails | The file may be an unsupported DWG version. Try saving it as DXF from your CAD software first |
| 3D generation times out | The pipeline has a 7-minute limit. Try with a simpler drawing or fewer views |

---

## File reference

| File | Purpose |
|---|---|
| `docker-compose.yml` | Defines the two services (drawingcrop + api) and port mapping |
| `web/index.html` | The entire browser UI — self-contained, no build step |
| `web/Dockerfile` | nginx container that serves `index.html` |
| `api/app.py` | Flask backend — DWG/DXF conversion + 3D generation endpoints |
| `api/Dockerfile` | Python container — compiles LibreDWG, installs dependencies |
| `.env` | Your local API key (never commit this file) |
