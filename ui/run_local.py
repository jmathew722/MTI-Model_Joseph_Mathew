"""Run DrawingCrop + 3D pipeline locally without Docker.

Usage:
    python run_local.py

Opens at http://localhost:8080
"""
import os, sys, urllib.request
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
HERE    = Path(__file__).parent.resolve()
API_DIR = HERE / 'api'
WEB_DIR = HERE / 'web'

# Put api/ on sys.path so `from app import app` works
sys.path.insert(0, str(API_DIR))

# Run from api/ so app.py's relative paths resolve correctly
os.chdir(str(API_DIR))

# ── Download pdf.js if not already present ─────────────────────────────────────
_PDF_VER = '3.11.174'
_PDF_CDN = f'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/{_PDF_VER}'
for _fname in ('pdf.min.js', 'pdf.worker.min.js'):
    _dest = WEB_DIR / _fname
    if not _dest.exists():
        print(f'Downloading {_fname} …')
        try:
            urllib.request.urlretrieve(f'{_PDF_CDN}/{_fname}', str(_dest))
            print(f'  saved → {_dest}')
        except Exception as e:
            print(f'  WARNING: could not download {_fname}: {e}')

# ── Import the Flask app ───────────────────────────────────────────────────────
from app import app
from flask import send_from_directory

# ── Serve static web files ─────────────────────────────────────────────────────
@app.route('/')
def _index():
    return send_from_directory(str(WEB_DIR), 'index.html')

@app.route('/<path:filename>')
def _static(filename):
    # Let /api/* fall through to the registered API routes
    if filename.startswith('api/'):
        from flask import abort
        abort(404)
    return send_from_directory(str(WEB_DIR), filename)

# ── Register /api/<route> aliases (nginx strips /api in Docker; we add it here) ─
_existing = [r.rule for r in app.url_map.iter_rules()]
for _rule in list(app.url_map.iter_rules()):
    _api_path = '/api' + _rule.rule
    if _api_path not in _existing and not _rule.rule.startswith('/api'):
        app.add_url_rule(
            _api_path,
            endpoint='api_' + _rule.endpoint,
            view_func=app.view_functions[_rule.endpoint],
            methods=list(_rule.methods - {'HEAD', 'OPTIONS'}),
        )

# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print()
    print('  DrawingCrop + 3D Pipeline')
    print('  http://localhost:8080')
    print()
    app.run(host='0.0.0.0', port=8080, debug=False)
