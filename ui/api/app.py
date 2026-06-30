import matplotlib
matplotlib.use('Agg')  # headless rendering — must be before pyplot import

from flask import Flask, request, send_file, jsonify
import ezdxf
from ezdxf.fonts import fonts as ezdxf_fonts
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
import matplotlib.pyplot as plt
import tempfile, os, subprocess, traceback, sys, uuid, threading, struct, zipfile, json, time
from io import BytesIO
from pathlib import Path

# Build font cache at startup so ezdxf finds the installed DejaVu fonts.
# Without this the cache file doesn't exist and FontNotFoundError is raised
# the first time any DXF with MTEXT entities is rendered.
ezdxf_fonts.build_system_font_cache()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024  # 150 MB

# ── Model pipeline path ───────────────────────────────────────────────────────
# In Docker the MODEL_DIR env var is set to /app/model/2D-3D-CAD-Test-Generation.
# When running directly from source the path is derived relative to this file.
MODEL_DIR = Path(
    os.getenv('MODEL_DIR') or
    str(Path(__file__).parent.parent.parent / 'model' / '2D-3D-CAD-Test-Generation')
)

# ── In-memory async job store ─────────────────────────────────────────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()

@app.route('/health')
def health():
    return {'status': 'ok'}

@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.dwg', '.dxf'):
        return jsonify({'error': f'Unsupported type: {ext}. Send .dwg or .dxf'}), 400

    try:
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, 'input' + ext)
            f.save(in_path)

            dxf_path = in_path

            # DWG → DXF via LibreDWG (dwg2dxf)
            if ext == '.dwg':
                dxf_path = os.path.join(tmp, 'input.dxf')
                result = subprocess.run(
                    ['dwg2dxf', '-o', dxf_path, in_path],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0 or not os.path.exists(dxf_path):
                    msg = result.stderr.strip() or 'dwg2dxf returned no output'
                    return jsonify({'error': f'DWG conversion failed: {msg}'}), 422

            # DXF → PNG via ezdxf + matplotlib
            doc = ezdxf.readfile(dxf_path)
            msp = doc.modelspace()

            fig = plt.figure(figsize=(22, 17), dpi=150)
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_aspect('equal')
            ax.set_facecolor('white')
            fig.patch.set_facecolor('white')

            ctx = RenderContext(doc)
            out = MatplotlibBackend(ax)
            try:
                Frontend(ctx, out).draw_layout(msp)
            except Exception as render_err:
                # Font errors occur when a DXF has MTEXT and the font isn't
                # found. Retry with a fresh backend; ezdxf will substitute
                # missing glyphs with rectangles instead of crashing.
                if 'font' in str(render_err).lower():
                    print(f'[warn] font issue, retrying with substitution: {render_err}')
                    ax.cla()
                    ax.set_aspect('equal')
                    ax.set_facecolor('white')
                    from ezdxf.addons.drawing.config import Configuration
                    cfg = Configuration.defaults()
                    out2 = MatplotlibBackend(ax)
                    Frontend(RenderContext(doc), out2, config=cfg).draw_layout(msp)
                else:
                    raise

            buf = BytesIO()
            fig.savefig(buf, format='png', bbox_inches='tight',
                        facecolor='white', dpi=150)
            plt.close(fig)
            buf.seek(0)

            return send_file(buf, mimetype='image/png')

    except ezdxf.DXFError as e:
        return jsonify({'error': f'DXF parse error: {e}'}), 422
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Conversion timed out (>120 s)'}), 504
    except Exception:
        traceback.print_exc()
        return jsonify({'error': 'Internal conversion error — check API logs'}), 500

# ── 3D generation endpoints ───────────────────────────────────────────────────

@app.route('/generate3d', methods=['POST'])
def start_generate3d():
    """Accept cropped view images and start the 2D→3D pipeline as a background job."""
    part_name = (request.form.get('part_name') or 'part').strip().replace(' ', '_') or 'part'
    view_files = {name: f for name, f in request.files.items() if f.filename}
    if not view_files:
        return jsonify({'error': 'No view images provided'}), 400

    tmpdir   = tempfile.mkdtemp(prefix='mti_')
    views_dir = os.path.join(tmpdir, part_name)
    output_dir = os.path.join(tmpdir, 'output')
    os.makedirs(views_dir)
    os.makedirs(output_dir)

    saved_views = []
    for view_name, f in view_files.items():
        ext = Path(f.filename).suffix.lower() or '.png'
        f.save(os.path.join(views_dir, f'{view_name}{ext}'))
        saved_views.append(view_name)

    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[job_id] = {
            'status': 'queued', 'progress': 0,
            'message': f'Queued: {", ".join(saved_views)}',
            'has_stl': False, 'has_zip': False,
            'stl_path': None, 'zip_path': None,
            'tmpdir': tmpdir,
        }

    threading.Thread(
        target=_run_pipeline_job,
        args=(job_id, tmpdir, views_dir, output_dir),
        daemon=True,
    ).start()
    return jsonify({'job_id': job_id, 'views': saved_views})


@app.route('/generate3d/status/<job_id>', methods=['GET'])
def get_generate3d_status(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'status':    job['status'],
        'progress':  job['progress'],
        'message':   job['message'],
        'has_stl':   job['has_stl'],
        'has_zip':   job['has_zip'],
        'has_sldprt': job.get('has_sldprt', False),
        'log':       job.get('log', ''),
    })


@app.route('/generate3d/result/<job_id>', methods=['GET'])
def get_generate3d_result(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    result_type = request.args.get('type', 'stl')

    if result_type == 'stl' and job.get('stl_path') and os.path.exists(job['stl_path']):
        return send_file(job['stl_path'], mimetype='model/stl', as_attachment=False)

    if job.get('zip_path') and os.path.exists(job['zip_path']):
        return send_file(job['zip_path'], mimetype='application/zip',
                         as_attachment=True, download_name='model_output.zip')

    return jsonify({'error': 'No result available yet'}), 404


# ── Pipeline runner (background thread) ───────────────────────────────────────

def _run_pipeline_job(job_id, tmpdir, views_dir, output_dir):
    job = _jobs[job_id]

    def _update(progress, message, status='running'):
        job.update({'status': status, 'progress': progress, 'message': message})

    _update(5, 'Preparing pipeline...')

    # Slowly tick progress from 10 → 74 while the subprocess runs
    _alive = [True]
    def _tick():
        while _alive[0]:
            time.sleep(5)
            if _alive[0] and job.get('status') == 'running':
                p = job.get('progress', 10)
                if p < 74:
                    job['progress'] = p + 3
    threading.Thread(target=_tick, daemon=True).start()

    try:
        if not MODEL_DIR.exists():
            raise FileNotFoundError(f'Model directory not found: {MODEL_DIR}')

        cmd = [
            sys.executable, str(MODEL_DIR / 'main.py'),
            '--views-folder', tmpdir,
            '--output', output_dir,
            '--no-export',
        ]
        _update(10, 'Extracting drawing data with Claude AI (this may take 1–3 min)…')
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(MODEL_DIR), timeout=600, env={**os.environ},
        )
        _alive[0] = False
        _update(80, 'Processing output…')

        # Save full stdout for diagnostics (visible in job['log'])
        job['log'] = (proc.stdout or '')[-2000:]

        if proc.returncode not in (0, 8):   # 8 = partial-success in batch mode
            err = (proc.stderr or proc.stdout or '')[-600:]
            _update(0, f'Pipeline error (exit {proc.returncode}): {err}', 'error')
            return

        out = Path(output_dir)

        # Prefer a real STL from SolidWorks; fall back to a preview box
        stl_files  = list(out.rglob('*.stl'))
        sldprt_files = list(out.rglob('*.sldprt'))
        if stl_files:
            job['stl_path'] = str(stl_files[0])
            job['has_stl'] = True
        else:
            preview = _make_preview_stl(output_dir, job_id)
            if preview:
                job['stl_path'] = preview
                job['has_stl'] = True

        # Track whether a real SolidWorks part was produced
        job['has_sldprt'] = bool(sldprt_files)
        if sldprt_files:
            job['sldprt_path'] = str(sldprt_files[0])

        # Package all pipeline outputs as a ZIP for download
        zip_path = _zip_output(output_dir, job_id, tmpdir)
        if zip_path:
            job['zip_path'] = zip_path
            job['has_zip'] = True

        n_macros  = len(list(out.rglob('*.vba')))
        n_parts   = len(sldprt_files)
        part_note = f' · {n_parts} .sldprt built' if n_parts else ' · run macros in SolidWorks to build .sldprt'
        _update(100, f'Complete — {n_macros} macro(s) generated{part_note}', 'done')

    except subprocess.TimeoutExpired:
        _alive[0] = False
        _update(0, 'Pipeline timed out (>7 minutes)', 'error')
    except Exception as e:
        _alive[0] = False
        traceback.print_exc()
        _update(0, f'{type(e).__name__}: {e}', 'error')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_preview_stl(output_dir: str, job_id: str):
    """Build a rough preview STL box from the first extraction JSON found."""
    json_files = list(Path(output_dir).rglob('*_extraction.json'))
    if not json_files:
        return None
    try:
        data = json.loads(json_files[0].read_text(encoding='utf-8'))
    except Exception:
        return None

    values = sorted(
        [d['value'] for d in data.get('dimensions', [])
         if isinstance(d.get('value'), (int, float)) and d['value'] > 0],
        reverse=True,
    )
    if len(values) >= 3:
        w, h, d = values[0], values[1], values[2]
    elif len(values) == 2:
        w, h, d = values[0], values[1], min(values) * 0.5
    elif len(values) == 1:
        w = h = d = values[0]
    else:
        w = h = d = 25.0

    w = min(max(float(w), 1.0), 2000.0)
    h = min(max(float(h), 1.0), 2000.0)
    d = min(max(float(d), 1.0), 2000.0)

    stl_path = os.path.join(output_dir, f'preview_{job_id}.stl')
    _write_box_stl(stl_path, w, h, d)
    return stl_path


def _write_box_stl(path: str, w: float, h: float, d: float) -> None:
    """Write a W×H×D box as binary STL (12 triangles, centered at origin)."""
    hw, hh, hd = w / 2, h / 2, d / 2
    quads = [
        (( 0,  0,  1), [( hw,  hh, hd), (-hw,  hh, hd), (-hw, -hh, hd), ( hw, -hh, hd)]),
        (( 0,  0, -1), [( hw, -hh,-hd), (-hw, -hh,-hd), (-hw,  hh,-hd), ( hw,  hh,-hd)]),
        (( 1,  0,  0), [( hw,  hh, hd), ( hw, -hh, hd), ( hw, -hh,-hd), ( hw,  hh,-hd)]),
        ((-1,  0,  0), [(-hw,  hh,-hd), (-hw, -hh,-hd), (-hw, -hh, hd), (-hw,  hh, hd)]),
        (( 0,  1,  0), [( hw,  hh, hd), ( hw,  hh,-hd), (-hw,  hh,-hd), (-hw,  hh, hd)]),
        (( 0, -1,  0), [(-hw, -hh, hd), (-hw, -hh,-hd), ( hw, -hh,-hd), ( hw, -hh, hd)]),
    ]
    tris = []
    for n, corners in quads:
        tris.append((n, corners[0], corners[1], corners[2]))
        tris.append((n, corners[0], corners[2], corners[3]))

    with open(path, 'wb') as f:
        f.write((b'MTI CAD Preview Model' + b'\x00' * 59)[:80])
        f.write(struct.pack('<I', len(tris)))
        for n, v0, v1, v2 in tris:
            f.write(struct.pack('<fff', *n))
            f.write(struct.pack('<fff', *v0))
            f.write(struct.pack('<fff', *v1))
            f.write(struct.pack('<fff', *v2))
            f.write(b'\x00\x00')


def _zip_output(output_dir: str, job_id: str, tmpdir: str):
    """Zip all pipeline outputs (excluding the preview STL) for download."""
    out = Path(output_dir)
    if not any(out.rglob('*')):
        return None
    zip_path = os.path.join(tmpdir, f'mti_output_{job_id}.zip')
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for item in out.rglob('*'):
                if item.is_file() and not item.name.startswith('preview_'):
                    zf.write(item, item.relative_to(out))
        return zip_path
    except Exception as e:
        print(f'ZIP error: {e}')
        return None


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
