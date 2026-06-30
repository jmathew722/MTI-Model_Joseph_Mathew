import math
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QComboBox, QLineEdit, QListWidget, QListWidgetItem,
    QFileDialog, QMessageBox, QStatusBar, QLabel,
    QSizePolicy, QFrame, QSpinBox, QScrollArea, QToolBar,
    QStackedWidget,
)
from PyQt6.QtCore import Qt, QUrl, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import (
    QAction, QDesktopServices, QKeySequence, QPainter, QColor, QPen, QFont, QPixmap,
)

from canvas_widget import CanvasWidget
import file_manager

# ── Palette ────────────────────────────────────────────────────────────────────
BG         = '#080d14'
PANEL      = '#0d1520'
PANEL2     = '#111d2b'
ACCENT     = '#00b4d8'
ACCENT_DIM = '#005f73'
BORDER     = '#1a2840'
TEXT       = '#cdd9e5'
TEXT_MUTED = '#4a6070'
SUCCESS_BG = '#0a1f12'
SUCCESS    = '#4ade80'
WARN       = '#fbbf24'
DANGER     = '#ef4444'

PRESET_NAMES = ['front', 'back', 'left', 'right', 'side', 'top', 'bottom', 'detail', 'section cut']

FEATURE_LIST = [
    'Boss Extrude', 'Cut Extrude', 'Hole Wizard', 'Fillets', 'Chamfers',
    'Linear/Circular Patterns', 'Mirror (bodies, features)', 'Revolved Boss',
    'Sheet Metal - Base Flange/Tab', 'Sheet Metal - Edge Flange',
    'Sheet Metal - Hem', 'Cosmetic Threads',
]

CUBE_V = [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]
CUBE_E = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]


# ── DWG/DXF Conversion Worker ─────────────────────────────────────────────────
class ConvertWorker(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self):
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import ezdxf
            from ezdxf.addons.drawing import RenderContext, Frontend
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

            ext = Path(self._path).suffix.lower()
            self.progress.emit(20, 'PARSING')

            dxf_path = self._path
            if ext == '.dwg':
                import subprocess, tempfile, os
                self.progress.emit(30, 'CONVERTING DWG')
                tmp = tempfile.mkdtemp()
                dxf_path = os.path.join(tmp, 'input.dxf')
                res = subprocess.run(
                    ['dwg2dxf', '-o', dxf_path, self._path],
                    capture_output=True, text=True, timeout=120,
                )
                if res.returncode != 0 or not os.path.exists(dxf_path):
                    self.error.emit(
                        f'DWG conversion failed.\n{res.stderr.strip()}\n\n'
                        f'dwg2dxf (LibreDWG) must be on your PATH for .dwg support.\n'
                        f'Use the Docker/web version for DWG files.'
                    )
                    return

            self.progress.emit(55, 'RENDERING')
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
            except Exception as e:
                if 'font' in str(e).lower():
                    ax.cla()
                    ax.set_aspect('equal')
                    ax.set_facecolor('white')
                    from ezdxf.addons.drawing.config import Configuration
                    Frontend(RenderContext(doc), MatplotlibBackend(ax),
                             config=Configuration.defaults()).draw_layout(msp)
                else:
                    raise

            self.progress.emit(85, 'SAVING')
            out_path = str(Path(self._path).with_suffix('.png'))
            fig.savefig(out_path, format='png', bbox_inches='tight',
                        facecolor='white', dpi=150)
            plt.close(fig)
            self.finished.emit(out_path)

        except ImportError as e:
            self.error.emit(
                f'ezdxf / matplotlib not installed.\n'
                f'Run: pip3 install ezdxf matplotlib\n\n{e}'
            )
        except Exception:
            import traceback
            self.error.emit(f'Conversion error:\n{traceback.format_exc()}')


# ── STL Wireframe Viewer ───────────────────────────────────────────────────────
class StlViewerWidget(QWidget):
    """Rotating wireframe cube placeholder for the 3D viewer pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._rx = 0.45
        self._ry = 0.6
        self._drag = None
        self._auto = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def _tick(self):
        if self._auto:
            self._ry += 0.008
        self.update()

    def _proj(self, x, y, z, cx, cy, s):
        cxr, sxr = math.cos(self._rx), math.sin(self._rx)
        y2 = y * cxr - z * sxr
        z2 = y * sxr + z * cxr
        cyr, syr = math.cos(self._ry), math.sin(self._ry)
        x2 = x * cyr + z2 * syr
        z3 = -x * syr + z2 * cyr
        fov = 4.0 / (4.0 + z3 + 2.0)
        return cx + x2 * s * fov, cy + y2 * s * fov

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(BG))
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        s = min(w, h) * 0.18

        # Floor grid
        p.setPen(QPen(QColor(26, 40, 64, 160), 1))
        for i in range(-5, 6):
            gx1, gy1 = self._proj(i * 0.4, 1.0, -2.0, cx, cy, s)
            gx2, gy2 = self._proj(i * 0.4, 1.0,  2.0, cx, cy, s)
            p.drawLine(int(gx1), int(gy1), int(gx2), int(gy2))
            gx1, gy1 = self._proj(-2.0, 1.0, i * 0.4, cx, cy, s)
            gx2, gy2 = self._proj( 2.0, 1.0, i * 0.4, cx, cy, s)
            p.drawLine(int(gx1), int(gy1), int(gx2), int(gy2))

        # Cube edges
        p.setPen(QPen(QColor(ACCENT), 1.5))
        for i1, i2 in CUBE_E:
            x1, y1 = self._proj(*CUBE_V[i1], cx, cy, s)
            x2, y2 = self._proj(*CUBE_V[i2], cx, cy, s)
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

        # Axis lines from bottom-left-back corner
        ax0, ay0 = self._proj(-1.0,  1.0, -1.0, cx, cy, s)
        axX, ayX = self._proj( 2.2,  1.0, -1.0, cx, cy, s)
        axY, ayY = self._proj(-1.0, -2.2, -1.0, cx, cy, s)
        axZ, ayZ = self._proj(-1.0,  1.0,  2.2, cx, cy, s)
        p.setPen(QPen(QColor(DANGER), 2))
        p.drawLine(int(ax0), int(ay0), int(axX), int(ayX))
        p.setPen(QPen(QColor(SUCCESS), 2))
        p.drawLine(int(ax0), int(ay0), int(axY), int(ayY))
        p.setPen(QPen(QColor(ACCENT), 2))
        p.drawLine(int(ax0), int(ay0), int(axZ), int(ayZ))

        # Empty state labels
        r = self.rect()
        f = QFont()
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(74, 96, 112, 90))
        p.drawText(r.adjusted(0, r.height() // 2 + 18, 0, 0),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                   'NO MODEL GENERATED')
        f2 = QFont()
        f2.setPixelSize(9)
        p.setFont(f2)
        p.setPen(QColor(74, 96, 112, 55))
        p.drawText(r.adjusted(0, r.height() // 2 + 34, 0, 0),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                   'Analyze views to build a 3D model')

    def mousePressEvent(self, e):
        self._drag = e.pos()
        self._auto = False
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag:
            dx = e.pos().x() - self._drag.x()
            dy = e.pos().y() - self._drag.y()
            self._ry += dx * 0.01
            self._rx += dy * 0.01
            self._drag = e.pos()

    def mouseReleaseEvent(self, e):
        self._drag = None
        self._auto = True
        self.setCursor(Qt.CursorShape.OpenHandCursor)


# ── Drawing Reference Widget ───────────────────────────────────────────────────
class DwgRefWidget(QWidget):
    """Shows the currently loaded drawing as a reference in the 3D tab."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self._pixmap: Optional[QPixmap] = None

    def set_image(self, path: Optional[str]):
        self._pixmap = QPixmap(path) if path else None
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BG))
        if self._pixmap is None or self._pixmap.isNull():
            r = self.rect()
            f = QFont()
            f.setPixelSize(10)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(74, 96, 112, 90))
            p.drawText(r.adjusted(0, -20, 0, 0),
                       Qt.AlignmentFlag.AlignCenter, 'NO DRAWING LOADED')
            f2 = QFont()
            f2.setPixelSize(9)
            p.setFont(f2)
            p.setPen(QColor(74, 96, 112, 55))
            p.drawText(r.adjusted(0, 16, 0, 0),
                       Qt.AlignmentFlag.AlignCenter,
                       'Open a drawing in the Drawing Crop tab first')
            return
        w, h = self.width(), self.height()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        scale = min(w / pw, h / ph) * 0.94
        dw, dh = int(pw * scale), int(ph * scale)
        ox, oy = (w - dw) // 2, (h - dh) // 2
        ratio = self.devicePixelRatio()
        scaled = self._pixmap.scaled(
            int(dw * ratio), int(dh * ratio),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        p.drawPixmap(ox, oy, scaled)


# ── Loading Bar ────────────────────────────────────────────────────────────────
class LoadingBar(QWidget):
    """3-pixel progress bar for DWG/DXF conversion feedback."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(3)
        self._pct = 0.0
        self._active = False

    def activate(self):
        self._pct = 0.0
        self._active = True
        self.update()

    def set_progress(self, pct: float):
        self._pct = float(pct)
        self.update()

    def deactivate(self):
        self._active = False
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(PANEL2))
        if self._active and self._pct > 0:
            p.fillRect(0, 0, int(self.width() * self._pct / 100), self.height(), QColor(ACCENT))


# ── AppWindow ─────────────────────────────────────────────────────────────────
class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._source_path: Optional[str] = None
        self._output_folder: Optional[Path] = None
        self._saved_names: list[str] = []
        self._feature_counts: dict[str, int] = {}
        self._convert_worker: Optional[ConvertWorker] = None
        self._fake_timer: Optional[QTimer] = None
        self._fake_pct = 0.0
        self._setup_ui()
        self._setup_shortcuts()
        self._update_button_states()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle('DrawingCrop')
        self.setMinimumSize(900, 650)
        self.resize(1200, 750)
        self.setStyleSheet(f'QMainWindow {{ background: {BG}; }}')
        self._setup_menu()
        self._setup_toolbar()

        central = QWidget()
        central.setStyleSheet(f'background: {BG};')
        vlay = QVBoxLayout(central)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        vlay.addWidget(self._build_tabbar())

        self._loading_bar = LoadingBar()
        vlay.addWidget(self._loading_bar)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f'background: {BG};')
        self._stack.addWidget(self._build_crop_panel())
        self._stack.addWidget(self._build_3d_panel())
        vlay.addWidget(self._stack)

        self.setCentralWidget(central)

        self._status_bar = QStatusBar()
        self._status_bar.setStyleSheet(
            f'QStatusBar {{ background: {PANEL}; color: {TEXT_MUTED};'
            f' font-family: monospace; font-size: 11px;'
            f' border-top: 1px solid {BORDER}; }}'
        )
        self._status_bar.showMessage('Ready — open an engineering drawing to begin')
        self.setStatusBar(self._status_bar)

    def _setup_menu(self):
        menu = self.menuBar()
        menu.setStyleSheet(
            f'QMenuBar {{ background: {PANEL}; color: {TEXT}; border-bottom: 1px solid {BORDER}; }}'
            f'QMenuBar::item:selected {{ background: {ACCENT_DIM}; }}'
            f'QMenu {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; }}'
            f'QMenu::item:selected {{ background: {ACCENT_DIM}; }}'
        )
        file_menu = menu.addMenu('File')
        open_act = QAction('Open Drawing', self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self._open_image)
        file_menu.addAction(open_act)

    def _setup_toolbar(self):
        tb = QToolBar()
        tb.setMovable(False)
        tb.setStyleSheet(
            f'QToolBar {{ background: {PANEL}; border-bottom: 1px solid {BORDER};'
            f' padding: 4px 10px; spacing: 6px; }}'
        )

        logo = QLabel('DRAWINGCROP')
        logo.setStyleSheet(
            f'color: {ACCENT}; font-size: 11px; font-weight: 700;'
            f' letter-spacing: 3px; background: transparent; padding-right: 4px;'
        )
        tb.addWidget(logo)
        tb.addWidget(self._vsep())

        self._open_btn = self._mk_btn('Open Drawing', primary=True)
        self._open_btn.clicked.connect(self._open_image)
        tb.addWidget(self._open_btn)

        self._clear_btn = self._mk_btn('Clear Selection')
        self._clear_btn.clicked.connect(self._clear_selection)
        tb.addWidget(self._clear_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        spacer.setStyleSheet('background: transparent;')
        tb.addWidget(spacer)

        self._file_label = QLabel('')
        self._file_label.setStyleSheet(
            f'color: {TEXT_MUTED}; font-size: 10px; font-family: monospace; background: transparent;'
        )
        tb.addWidget(self._file_label)
        tb.addWidget(self._vsep())

        reset_btn = QPushButton('RESET')
        reset_btn.clicked.connect(self._reset_app)
        reset_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {DANGER}; font-size: 10px;'
            f' font-weight: 700; border: 1px solid #7f1d1d; border-radius: 3px; padding: 4px 10px; }}'
            f'QPushButton:hover {{ background: #7f1d1d; color: #fca5a5; }}'
        )
        tb.addWidget(reset_btn)
        self.addToolBar(tb)

    def _vsep(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.Shape.VLine)
        f.setFixedSize(1, 20)
        f.setStyleSheet(f'background: {BORDER};')
        return f

    def _build_tabbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(32)
        bar.setStyleSheet(f'background: {BG}; border-bottom: 1px solid {BORDER};')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(0)

        self._tab_crop = self._mk_tab('DRAWING CROP')
        self._tab_crop.clicked.connect(lambda: self._switch_tab(0))
        lay.addWidget(self._tab_crop)

        self._tab_3d = self._mk_tab('3D CONVERT  [BETA]')
        self._tab_3d.clicked.connect(lambda: self._switch_tab(1))
        lay.addWidget(self._tab_3d)

        lay.addStretch()
        self._tab_crop.setChecked(True)
        return bar

    def _mk_tab(self, label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setStyleSheet(
            f'QPushButton {{ background: transparent; border: none;'
            f' border-bottom: 2px solid transparent; color: {TEXT_MUTED};'
            f' font-size: 9px; font-weight: 700; padding: 0 16px; border-radius: 0; }}'
            f'QPushButton:hover {{ color: {TEXT}; }}'
            f'QPushButton:checked {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}'
        )
        return btn

    def _switch_tab(self, idx: int):
        self._tab_crop.setChecked(idx == 0)
        self._tab_3d.setChecked(idx == 1)
        self._stack.setCurrentIndex(idx)

    # ── Crop panel ─────────────────────────────────────────────────────────────

    def _build_crop_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(f'background: {BG};')
        lay = QHBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._canvas = CanvasWidget()
        self._canvas.selection_changed.connect(self._on_selection_changed)
        lay.addWidget(self._canvas)
        lay.addWidget(self._build_crop_sidebar())
        return panel

    def _build_crop_sidebar(self) -> QWidget:
        container = QWidget()
        container.setFixedWidth(240)
        container.setStyleSheet(f'background: {PANEL}; border-left: 1px solid {BORDER};')

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f'QScrollArea {{ border: none; background: {PANEL}; }}'
            f'QScrollBar:vertical {{ background: {PANEL}; width: 5px; }}'
            f'QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 2px; }}'
            f'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}'
        )

        inner = QWidget()
        inner.setStyleSheet(f'background: {PANEL};')
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(0)

        self._info_label = QLabel('No drawing loaded')
        self._info_label.setStyleSheet(
            f'color: {TEXT_MUTED}; font-size: 10px; font-family: monospace;'
            f' background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 3px;'
            f' padding: 6px 8px; margin-bottom: 14px;'
        )
        self._info_label.setWordWrap(True)
        lay.addWidget(self._info_label)

        lay.addWidget(self._section_hdr('VIEWS'))

        lay.addWidget(self._lbl('PRESET'))
        self._preset_dropdown = QComboBox()
        self._preset_dropdown.addItems(PRESET_NAMES)
        self._preset_dropdown.currentTextChanged.connect(self._update_button_states)
        self._style_cb(self._preset_dropdown)
        lay.addWidget(self._preset_dropdown)
        lay.addSpacing(6)

        lay.addWidget(self._lbl('CUSTOM NAME (overrides preset)'))
        self._custom_name = QLineEdit()
        self._custom_name.setPlaceholderText('e.g. iso_detail')
        self._custom_name.textChanged.connect(self._update_button_states)
        self._style_le(self._custom_name)
        lay.addWidget(self._custom_name)
        lay.addSpacing(8)

        self._save_btn = QPushButton('Save View')
        self._save_btn.clicked.connect(self._save_view)
        self._save_btn.setStyleSheet(
            f'QPushButton {{ background: {ACCENT}; color: #000; font-weight: 700;'
            f' font-size: 12px; border-radius: 3px; padding: 7px; border: none; }}'
            f'QPushButton:hover {{ background: #00d0f0; }}'
            f'QPushButton:disabled {{ background: {ACCENT_DIM}; color: #1a3a44; }}'
        )
        lay.addWidget(self._save_btn)
        lay.addSpacing(8)

        lay.addWidget(self._lbl('Saved views'))
        self._saved_list = QListWidget()
        self._saved_list.setFixedHeight(86)
        self._saved_list.setStyleSheet(
            f'QListWidget {{ background: {PANEL2}; color: {SUCCESS}; border: 1px solid {BORDER};'
            f' border-radius: 3px; font-size: 10px; font-family: monospace; padding: 4px; }}'
            f'QListWidget::item {{ padding: 1px 0; }}'
        )
        lay.addWidget(self._saved_list)
        lay.addSpacing(18)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f'color: {BORDER}; background: {BORDER};')
        div.setFixedHeight(1)
        lay.addWidget(div)
        lay.addSpacing(14)

        lay.addWidget(self._section_hdr('FEATURE TALLY'))

        lay.addWidget(self._lbl('Feature'))
        self._feature_dropdown = QComboBox()
        self._feature_dropdown.addItems(FEATURE_LIST)
        self._style_cb(self._feature_dropdown)
        lay.addWidget(self._feature_dropdown)
        lay.addSpacing(6)

        lay.addWidget(self._lbl('Count'))
        count_row = QWidget()
        count_row.setStyleSheet('background: transparent;')
        cr_lay = QHBoxLayout(count_row)
        cr_lay.setContentsMargins(0, 0, 0, 0)
        cr_lay.setSpacing(6)

        self._count_spin = QSpinBox()
        self._count_spin.setRange(0, 999)
        self._count_spin.setValue(1)
        self._count_spin.setStyleSheet(
            f'QSpinBox {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER};'
            f' border-radius: 3px; padding: 4px 6px; font-size: 12px; }}'
            f'QSpinBox:focus {{ border-color: {ACCENT}; }}'
            f'QSpinBox::up-button, QSpinBox::down-button {{ background: {BORDER}; width: 16px; border: none; }}'
        )
        cr_lay.addWidget(self._count_spin)

        add_btn = QPushButton('Add')
        add_btn.clicked.connect(self._add_feature)
        add_btn.setStyleSheet(
            f'QPushButton {{ background: {ACCENT_DIM}; color: {ACCENT}; border: 1px solid {ACCENT_DIM};'
            f' border-radius: 3px; padding: 4px 12px; font-weight: 700; font-size: 11px; }}'
            f'QPushButton:hover {{ background: {ACCENT}; color: #000; border-color: {ACCENT}; }}'
        )
        cr_lay.addWidget(add_btn)
        lay.addWidget(count_row)
        lay.addSpacing(8)

        lay.addWidget(self._lbl('Tallied features'))
        self._feature_list = QListWidget()
        self._feature_list.setFixedHeight(116)
        self._feature_list.setStyleSheet(
            f'QListWidget {{ background: {PANEL2}; color: {WARN}; border: 1px solid {BORDER};'
            f' border-radius: 3px; font-size: 10px; font-family: monospace; padding: 4px; }}'
            f'QListWidget::item {{ padding: 1px 0; }}'
        )
        lay.addWidget(self._feature_list)
        lay.addSpacing(4)

        clear_feat_btn = QPushButton('Clear Tally')
        clear_feat_btn.clicked.connect(self._clear_features)
        clear_feat_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {TEXT_MUTED}; border: 1px solid {BORDER};'
            f' border-radius: 3px; padding: 3px 8px; font-size: 10px; }}'
            f'QPushButton:hover {{ color: {DANGER}; border-color: {DANGER}; }}'
        )
        lay.addWidget(clear_feat_btn)
        lay.addStretch()

        self._open_folder_btn = QPushButton('Open Output Folder')
        self._open_folder_btn.clicked.connect(self._open_folder)
        self._open_folder_btn.setStyleSheet(
            f'QPushButton {{ background: {SUCCESS_BG}; color: {SUCCESS}; border: 1px solid #1b4a28;'
            f' border-radius: 3px; padding: 6px; font-size: 10px; margin-top: 8px; }}'
            f'QPushButton:disabled {{ background: {PANEL2}; color: {TEXT_MUTED}; border-color: {BORDER}; }}'
            f'QPushButton:hover:!disabled {{ background: #0f2a1a; }}'
        )
        lay.addWidget(self._open_folder_btn)

        scroll.setWidget(inner)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll)
        return container

    # ── 3D panel ───────────────────────────────────────────────────────────────

    def _build_3d_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(f'background: {BG};')
        lay = QHBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        stl_pane = QWidget()
        stl_pane.setStyleSheet(f'background: {BG};')
        stl_lay = QVBoxLayout(stl_pane)
        stl_lay.setContentsMargins(0, 0, 0, 0)
        stl_lay.setSpacing(0)
        stl_lay.addWidget(self._pane_hdr('STL VIEWER', accent=True))
        self._stl_viewer = StlViewerWidget()
        stl_lay.addWidget(self._stl_viewer)
        lay.addWidget(stl_pane)

        dwg_pane = QWidget()
        dwg_pane.setStyleSheet(f'background: {BG}; border-left: 1px solid {BORDER};')
        dwg_lay = QVBoxLayout(dwg_pane)
        dwg_lay.setContentsMargins(0, 0, 0, 0)
        dwg_lay.setSpacing(0)
        dwg_lay.addWidget(self._pane_hdr('ENGINEERING DRAWING', accent=False))
        self._dwg_ref = DwgRefWidget()
        dwg_lay.addWidget(self._dwg_ref)
        lay.addWidget(dwg_pane)

        lay.addWidget(self._build_3d_sidebar())
        return panel

    def _pane_hdr(self, title: str, accent: bool) -> QWidget:
        hdr = QWidget()
        hdr.setFixedHeight(30)
        hdr.setStyleSheet(f'background: {PANEL}; border-bottom: 1px solid {BORDER};')
        h = QHBoxLayout(hdr)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(8)
        dot = QLabel('●')
        dot.setStyleSheet(
            f'color: {ACCENT if accent else WARN}; font-size: 6px; background: transparent;'
        )
        h.addWidget(dot)
        lbl = QLabel(title)
        lbl.setStyleSheet(
            f'color: {TEXT_MUTED}; font-size: 8px; font-weight: 700;'
            f' letter-spacing: 2px; background: transparent;'
        )
        h.addWidget(lbl)
        h.addStretch()
        return hdr

    def _build_3d_sidebar(self) -> QWidget:
        container = QWidget()
        container.setFixedWidth(240)
        container.setStyleSheet(f'background: {PANEL}; border-left: 1px solid {BORDER};')

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f'QScrollArea {{ border: none; background: {PANEL}; }}'
            f'QScrollBar:vertical {{ background: {PANEL}; width: 5px; }}'
            f'QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 2px; }}'
            f'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}'
        )

        inner = QWidget()
        inner.setStyleSheet(f'background: {PANEL};')
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(0)

        lay.addWidget(self._section_hdr('MODEL STATUS'))

        gen_box = QWidget()
        gen_box.setStyleSheet(
            f'background: {PANEL2}; border: 1px dashed {BORDER}; border-radius: 3px;'
        )
        gb_lay = QVBoxLayout(gen_box)
        gb_lay.setContentsMargins(10, 12, 10, 12)
        gb_lay.setSpacing(10)

        gen_info = QLabel(
            'Load a ZIP with saved views to begin 3D reconstruction. '
            'Import your exported ZIP from the Drawing Crop tab and '
            'the model will appear in the viewer.'
        )
        gen_info.setWordWrap(True)
        gen_info.setStyleSheet(
            f'color: {TEXT_MUTED}; font-size: 9px; background: transparent; border: none;'
        )
        gb_lay.addWidget(gen_info)

        gen_btn = QPushButton('⚙  GENERATE 3D MODEL')
        gen_btn.setEnabled(False)
        gen_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {TEXT_MUTED};'
            f' border: 1px solid {BORDER}; border-radius: 3px; font-size: 10px; padding: 5px; }}'
        )
        gb_lay.addWidget(gen_btn)
        lay.addWidget(gen_box)
        lay.addSpacing(14)

        lay.addWidget(self._section_hdr('WARNINGS'))

        self._warn_list = QListWidget()
        self._warn_list.setStyleSheet(
            f'QListWidget {{ background: transparent; border: none; padding: 0; }}'
            f'QListWidget::item {{ background: {PANEL2}; color: {TEXT_MUTED};'
            f' border-left: 2px solid {TEXT_MUTED}; padding: 5px 7px;'
            f' margin-bottom: 4px; font-size: 9px; font-style: italic; }}'
        )
        item = QListWidgetItem('ℹ  No model generated yet — warnings will appear here')
        self._warn_list.addItem(item)
        lay.addWidget(self._warn_list)
        lay.addStretch()

        self._dl_model_btn = QPushButton('⬇  Download 3D Model (.stl)')
        self._dl_model_btn.setEnabled(False)
        self._dl_model_btn.setStyleSheet(
            f'QPushButton {{ background: {PANEL2}; color: {TEXT_MUTED};'
            f' border: 1px solid {BORDER}; border-radius: 3px;'
            f' font-size: 11px; padding: 7px 12px; font-weight: 700; }}'
        )
        lay.addWidget(self._dl_model_btn)

        scroll.setWidget(inner)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll)
        return container

    # ── Widget factories ───────────────────────────────────────────────────────

    def _mk_btn(self, text: str, primary=False) -> QPushButton:
        btn = QPushButton(text)
        if primary:
            btn.setStyleSheet(
                f'QPushButton {{ background: {ACCENT}; color: #000; font-weight: 700;'
                f' font-size: 11px; border-radius: 3px; padding: 4px 12px; border: none; }}'
                f'QPushButton:hover {{ background: #00d0f0; }}'
            )
        else:
            btn.setStyleSheet(
                f'QPushButton {{ background: transparent; color: {TEXT}; font-size: 11px;'
                f' border: 1px solid {BORDER}; border-radius: 3px; padding: 4px 12px; }}'
                f'QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}'
                f'QPushButton:disabled {{ color: {TEXT_MUTED}; }}'
            )
        return btn

    def _section_hdr(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f'color: {ACCENT}; font-size: 9px; font-weight: 700; letter-spacing: 2px;'
            f' padding-bottom: 6px; margin-bottom: 6px;'
            f' border-bottom: 1px solid {ACCENT_DIM}; background: transparent;'
        )
        return lbl

    def _lbl(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f'color: {TEXT_MUTED}; font-size: 9px; margin-bottom: 3px; background: transparent;'
        )
        return lbl

    def _style_cb(self, cb: QComboBox):
        cb.setStyleSheet(
            f'QComboBox {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER};'
            f' border-radius: 3px; padding: 4px 8px; font-size: 11px; }}'
            f'QComboBox:focus {{ border-color: {ACCENT}; }}'
            f'QComboBox::drop-down {{ border: none; width: 18px; }}'
            f'QComboBox QAbstractItemView {{ background: {PANEL2}; color: {TEXT};'
            f' border: 1px solid {BORDER}; selection-background-color: {ACCENT_DIM}; outline: none; }}'
        )

    def _style_le(self, le: QLineEdit):
        le.setStyleSheet(
            f'QLineEdit {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER};'
            f' border-radius: 3px; padding: 4px 8px; font-size: 11px; }}'
            f'QLineEdit:focus {{ border-color: {ACCENT}; }}'
        )

    def _setup_shortcuts(self):
        save_sc = QAction(self)
        save_sc.setShortcut(QKeySequence('Ctrl+S'))
        save_sc.triggered.connect(self._save_view)
        self.addAction(save_sc)

        esc_sc = QAction(self)
        esc_sc.setShortcut(Qt.Key.Key_Escape)
        esc_sc.triggered.connect(self._clear_selection)
        self.addAction(esc_sc)

    # ── File handling ──────────────────────────────────────────────────────────

    def _open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open Engineering Drawing', '',
            'All Supported (*.jpg *.jpeg *.png *.webp *.tiff *.tif *.bmp *.pdf *.dwg *.dxf)'
            ';;Images (*.jpg *.jpeg *.png *.webp *.tiff *.tif *.bmp)'
            ';;PDF (*.pdf)'
            ';;CAD Files (*.dwg *.dxf)',
        )
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext == '.pdf':
            try:
                path = str(file_manager.pdf_page_to_png(path))
            except Exception as e:
                QMessageBox.critical(self, 'PDF Error', f'Could not convert PDF:\n{e}')
                return
            self._load_path(path)
        elif ext in ('.dwg', '.dxf'):
            self._start_conversion(path)
        else:
            self._load_path(path)

    def _load_path(self, path: str):
        if not self._canvas.load_image(path):
            QMessageBox.critical(self, 'Error', 'Could not open drawing. Unsupported or corrupt file.')
            return
        self._source_path = path
        self._output_folder = None
        self._saved_names.clear()
        self._saved_list.clear()
        self._feature_counts.clear()
        self._feature_list.clear()
        stem = Path(path).stem
        w, h = self._canvas.image_size()
        self.setWindowTitle(f'DrawingCrop — {stem}')
        self._file_label.setText(f'{stem}   {w} × {h} px')
        self._info_label.setText(f'{Path(path).name}\n{w} × {h} px')
        self._status_bar.showMessage(f'Loaded {Path(path).name}  ·  {w} × {h} px')
        self._dwg_ref.set_image(path)
        self._update_button_states()

    # ── DWG/DXF conversion ─────────────────────────────────────────────────────

    def _start_conversion(self, path: str):
        self._loading_bar.activate()
        self._loading_bar.set_progress(10)
        self._fake_pct = 10.0

        self._fake_timer = QTimer(self)
        self._fake_timer.timeout.connect(self._fake_tick)
        self._fake_timer.start(120)

        self._convert_worker = ConvertWorker(path, self)
        self._convert_worker.progress.connect(self._on_conv_progress)
        self._convert_worker.finished.connect(self._on_conv_done)
        self._convert_worker.error.connect(self._on_conv_error)
        self._convert_worker.start()

    def _fake_tick(self):
        if self._fake_pct < 88.0:
            self._fake_pct += (88.0 - self._fake_pct) * 0.06
            self._loading_bar.set_progress(self._fake_pct)

    def _on_conv_progress(self, pct: int, _label: str):
        if self._fake_timer:
            self._fake_timer.stop()
        self._loading_bar.set_progress(pct)

    def _on_conv_done(self, png_path: str):
        if self._fake_timer:
            self._fake_timer.stop()
        self._loading_bar.set_progress(100)
        QTimer.singleShot(450, self._loading_bar.deactivate)
        self._load_path(png_path)

    def _on_conv_error(self, msg: str):
        if self._fake_timer:
            self._fake_timer.stop()
        self._loading_bar.deactivate()
        QMessageBox.critical(self, 'Conversion Error', msg)

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _reset_app(self):
        self._source_path = None
        self._output_folder = None
        self._saved_names.clear()
        self._saved_list.clear()
        self._feature_counts.clear()
        self._feature_list.clear()
        self._custom_name.clear()
        self._preset_dropdown.setCurrentIndex(0)
        self._canvas.unload()
        self._dwg_ref.set_image(None)
        self.setWindowTitle('DrawingCrop')
        self._file_label.setText('')
        self._info_label.setText('No drawing loaded')
        self._status_bar.showMessage('Ready — open an engineering drawing to begin')
        self._update_button_states()

    def _clear_selection(self):
        self._canvas.clear_selection()
        self._custom_name.clear()
        self._preset_dropdown.setCurrentIndex(0)

    def _get_current_name(self) -> str:
        custom = self._custom_name.text().strip()
        return custom if custom else self._preset_dropdown.currentText()

    def _add_feature(self):
        feature = self._feature_dropdown.currentText()
        count = self._count_spin.value()
        if count <= 0:
            return
        self._feature_counts[feature] = self._feature_counts.get(feature, 0) + count
        self._refresh_feature_list()
        self._count_spin.setValue(1)
        if self._source_path:
            try:
                file_manager.save_feature_counts(self._source_path, self._feature_counts)
                self._status_bar.showMessage(
                    f'Feature tally saved  ·  {self._output_folder or ""}'
                )
            except Exception as e:
                self._status_bar.showMessage(f'Feature save error: {e}')

    def _refresh_feature_list(self):
        self._feature_list.clear()
        for feat, cnt in self._feature_counts.items():
            self._feature_list.addItem(f'{feat}  ×{cnt}')

    def _clear_features(self):
        self._feature_counts.clear()
        self._feature_list.clear()

    def _save_view(self):
        if not self._save_btn.isEnabled():
            return
        rect = self._canvas.get_selection_rect()
        name = self._get_current_name()
        safe_name = file_manager.sanitize_name(name)

        if safe_name in self._saved_names:
            reply = QMessageBox.question(
                self, 'Overwrite?',
                f"View '{safe_name}' already saved. Overwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                return

        try:
            crop_path = file_manager.save_crop(
                self._source_path,
                (rect.x(), rect.y(), rect.width(), rect.height()),
                safe_name,
            )
        except Exception as e:
            QMessageBox.critical(self, 'Save Error', str(e))
            return

        self._output_folder = crop_path.parent
        if safe_name not in self._saved_names:
            self._saved_names.append(safe_name)

        stem = Path(self._source_path).stem
        label_text = f'{stem}_{safe_name}'
        for i in range(self._saved_list.count()):
            if self._saved_list.item(i).text() == label_text:
                self._saved_list.takeItem(i)
                break
        self._saved_list.addItem(label_text)

        try:
            file_manager.save_feature_counts(self._source_path, self._feature_counts)
        except Exception as e:
            self._status_bar.showMessage(f'Feature save error: {e}')

        self._canvas.clear_selection()
        self._custom_name.clear()
        self._preset_dropdown.setCurrentIndex(0)
        self._status_bar.showMessage(
            f'Saved  {stem}_{safe_name}   ·   {self._output_folder}/'
        )
        self._update_button_states()

    def _open_folder(self):
        if self._output_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_folder)))

    def _on_selection_changed(self, rect):
        if rect:
            msg = f'Selection  {rect.width()} × {rect.height()} px   at ({rect.x()}, {rect.y()})'
            if self._output_folder:
                msg += f'   ·   {self._output_folder}/'
            self._status_bar.showMessage(msg)
        self._update_button_states()

    def _update_button_states(self):
        has_image = self._source_path is not None
        has_sel = self._canvas.get_selection_rect() is not None
        has_name = bool(file_manager.sanitize_name(self._get_current_name()))
        self._save_btn.setEnabled(has_image and has_sel and has_name)
        self._clear_btn.setEnabled(has_sel)
        self._open_folder_btn.setEnabled(self._output_folder is not None)

    def closeEvent(self, event):
        if self._convert_worker and self._convert_worker.isRunning():
            self._convert_worker.terminate()
            self._convert_worker.wait(2000)
        super().closeEvent(event)
