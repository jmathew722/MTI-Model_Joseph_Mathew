# Image Crop Views App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a PyQt6 desktop app that lets a user open an image, draw rectangular crop selections, name each one, and save them alongside the original into an auto-created folder.

**Architecture:** Four source files in the project root. `file_manager.py` contains pure functions with no UI dependencies. `canvas_widget.py` is a `QWidget` subclass that handles image display and selection drawing. `app_window.py` wires everything together in a `QMainWindow`. `main.py` is a 6-line entry point.

**Tech Stack:** Python 3.10+, PyQt6, Pillow

---

## File Map

| File | Role |
|---|---|
| `requirements.txt` | Pinned dependencies |
| `file_manager.py` | `sanitize_name(name)`, `save_crop(source_path, rect_tuple, name)` |
| `canvas_widget.py` | `CanvasWidget(QWidget)` — image display, rubber-band draw, corner-handle resize, `selection_changed` signal |
| `app_window.py` | `AppWindow(QMainWindow)` — toolbar, canvas, sidebar, status bar, all signal wiring |
| `main.py` | Entry point |
| `tests/test_file_manager.py` | Unit tests for `file_manager.py` (only testable module) |

---

## Task 1: Project Scaffolding

**Files:**
- Create: `requirements.txt`
- Create: `main.py`
- Create: `file_manager.py`
- Create: `canvas_widget.py`
- Create: `app_window.py`
- Create: `tests/__init__.py`
- Create: `tests/test_file_manager.py`
- Create: `.gitignore`

- [ ] **Step 1: Create requirements.txt**

```
PyQt6>=6.5
Pillow>=10.0
pytest>=7.0
```

- [ ] **Step 2: Install dependencies**

```bash
pip install PyQt6 Pillow pytest
```

Expected: no errors. Verify with `python -c "from PyQt6.QtWidgets import QApplication; print('ok')"` → prints `ok`.

- [ ] **Step 3: Create stub files so imports resolve**

`main.py`:
```python
import sys
from PyQt6.QtWidgets import QApplication
from app_window import AppWindow

def main():
    app = QApplication(sys.argv)
    app.setApplicationName('PhotoCrop')
    window = AppWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
```

`file_manager.py`:
```python
import re
import shutil
from pathlib import Path
from PIL import Image

FORBIDDEN = re.compile(r'[/\\:*?"<>|]')

def sanitize_name(name: str) -> str:
    raise NotImplementedError

def save_crop(source_path: str, rect: tuple, name: str) -> Path:
    raise NotImplementedError
```

`canvas_widget.py`:
```python
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import pyqtSignal

class CanvasWidget(QWidget):
    selection_changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)

    def load_image(self, path: str):
        pass

    def clear_selection(self):
        pass

    def get_selection_rect(self):
        return None
```

`app_window.py`:
```python
from PyQt6.QtWidgets import QMainWindow, QLabel
from canvas_widget import CanvasWidget

class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('PhotoCrop')
        self.setMinimumSize(800, 600)
        self._canvas = CanvasWidget()
        self.setCentralWidget(self._canvas)
```

`tests/__init__.py`: (empty file)

`tests/test_file_manager.py`:
```python
# tests go here in Task 2 and 3
```

`.gitignore`:
```
__pycache__/
*.pyc
.pytest_cache/
.superpowers/
```

- [ ] **Step 4: Verify app launches**

```bash
python main.py
```

Expected: a window titled "PhotoCrop" opens with a dark background. Close it.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt main.py file_manager.py canvas_widget.py app_window.py tests/ .gitignore
git commit -m "feat: project scaffolding and stub files"
```

---

## Task 2: file_manager — sanitize_name

**Files:**
- Modify: `file_manager.py`
- Modify: `tests/test_file_manager.py`

- [ ] **Step 1: Write failing tests for sanitize_name**

Replace `tests/test_file_manager.py` content:

```python
import pytest
from pathlib import Path
from PIL import Image
import file_manager


class TestSanitizeName:
    def test_spaces_become_underscores(self):
        assert file_manager.sanitize_name('section cut') == 'section_cut'

    def test_multiple_spaces(self):
        assert file_manager.sanitize_name('left side view') == 'left_side_view'

    def test_forbidden_slash_stripped(self):
        assert file_manager.sanitize_name('front/back') == 'frontback'

    def test_forbidden_colon_stripped(self):
        assert file_manager.sanitize_name('view:1') == 'view1'

    def test_all_forbidden_chars_stripped(self):
        result = file_manager.sanitize_name(r'a\b:c*d?e"f<g>h|i')
        assert result == 'abcdefghi'

    def test_clean_name_unchanged(self):
        assert file_manager.sanitize_name('front') == 'front'

    def test_clean_name_with_underscore_unchanged(self):
        assert file_manager.sanitize_name('logo_area') == 'logo_area'
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/test_file_manager.py::TestSanitizeName -v
```

Expected: all 7 tests FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement sanitize_name in file_manager.py**

Replace the `sanitize_name` function:

```python
def sanitize_name(name: str) -> str:
    name = FORBIDDEN.sub('', name)
    return name.replace(' ', '_')
```

- [ ] **Step 4: Run tests — expect all pass**

```bash
pytest tests/test_file_manager.py::TestSanitizeName -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add file_manager.py tests/test_file_manager.py
git commit -m "feat: implement sanitize_name with tests"
```

---

## Task 3: file_manager — save_crop

**Files:**
- Modify: `file_manager.py`
- Modify: `tests/test_file_manager.py`

- [ ] **Step 1: Write failing tests for save_crop**

Append to `tests/test_file_manager.py` (after `TestSanitizeName`):

```python
def _make_image(path: Path, width=100, height=80, color='red'):
    Image.new('RGB', (width, height), color=color).save(str(path))


class TestSaveCrop:
    def test_creates_output_folder(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        assert (tmp_path / 'photo_001').is_dir()

    def test_copies_original_into_folder(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        assert (tmp_path / 'photo_001' / 'photo_001.jpg').exists()

    def test_saves_crop_at_expected_path(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        crop_path = file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        assert crop_path == tmp_path / 'photo_001' / 'photo_001_front.jpg'
        assert crop_path.exists()

    def test_crop_has_correct_dimensions(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src, width=100, height=80)
        crop_path = file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        with Image.open(crop_path) as img:
            assert img.size == (50, 40)

    def test_sanitizes_name_in_filename(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        crop_path = file_manager.save_crop(str(src), (0, 0, 50, 40), 'section cut')
        assert crop_path.name == 'photo_001_section_cut.jpg'

    def test_does_not_recopy_original_on_second_save(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        mtime_after_first = (tmp_path / 'photo_001' / 'photo_001.jpg').stat().st_mtime
        file_manager.save_crop(str(src), (20, 20, 30, 30), 'back')
        assert (tmp_path / 'photo_001' / 'photo_001.jpg').stat().st_mtime == mtime_after_first

    def test_returns_path_object(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        result = file_manager.save_crop(str(src), (0, 0, 50, 40), 'front')
        assert isinstance(result, Path)

    def test_png_source_saves_as_png(self, tmp_path):
        src = tmp_path / 'photo_001.png'
        Image.new('RGB', (100, 80)).save(str(src))
        crop_path = file_manager.save_crop(str(src), (0, 0, 50, 40), 'front')
        assert crop_path.suffix == '.png'
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/test_file_manager.py::TestSaveCrop -v
```

Expected: all 8 tests FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement save_crop in file_manager.py**

Replace the `save_crop` function:

```python
def save_crop(source_path: str, rect: tuple, name: str) -> Path:
    source = Path(source_path)
    stem = source.stem
    ext = source.suffix
    folder = source.parent / stem
    folder.mkdir(exist_ok=True)

    original_dest = folder / source.name
    if not original_dest.exists():
        shutil.copy2(source, original_dest)

    safe_name = sanitize_name(name)
    crop_path = folder / f'{stem}_{safe_name}{ext}'

    with Image.open(source_path) as img:
        x, y, w, h = rect
        cropped = img.crop((x, y, x + w, y + h))
        if ext.lower() in ('.jpg', '.jpeg') and cropped.mode in ('RGBA', 'P', 'LA'):
            cropped = cropped.convert('RGB')
        cropped.save(str(crop_path))

    return crop_path
```

- [ ] **Step 4: Run all tests — expect all pass**

```bash
pytest tests/ -v
```

Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add file_manager.py tests/test_file_manager.py
git commit -m "feat: implement save_crop with tests"
```

---

## Task 4: CanvasWidget — Full Implementation

**Files:**
- Overwrite: `canvas_widget.py`

This task builds the complete canvas widget in steps. Each step extends the file.

- [ ] **Step 1: Write the class skeleton and image loading**

Overwrite `canvas_widget.py`:

```python
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QRect, QPoint, pyqtSignal
from PyQt6.QtGui import QPainter, QPixmap, QColor, QPen, QFont

HANDLE_SIZE = 10
HANDLE_HALF = HANDLE_SIZE // 2
MIN_SELECTION_PX = 10  # minimum display pixels for a valid selection


class CanvasWidget(QWidget):
    selection_changed = pyqtSignal(object)  # emits QRect (image coords) or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._pixmap = None       # QPixmap of the loaded image
        self._scale = 1.0         # display_size / original_size ratio
        self._offset = QPoint(0, 0)  # top-left of image in widget coords
        self._selection = None    # QRect in display coords, or None
        self._drag_start = None   # QPoint where current drag began
        self._resize_handle = None  # 'tl','tr','bl','br' or None

    def load_image(self, path: str):
        self._pixmap = QPixmap(path)
        self._selection = None
        self._drag_start = None
        self._resize_handle = None
        self._update_transform()
        self.update()
        self.selection_changed.emit(None)

    def clear_selection(self):
        self._selection = None
        self._drag_start = None
        self._resize_handle = None
        self.update()
        self.selection_changed.emit(None)

    def get_selection_rect(self):
        """Return selection as QRect in original image coordinates, or None."""
        if self._selection is None or self._pixmap is None:
            return None
        sel = self._selection.normalized()
        if sel.width() < MIN_SELECTION_PX or sel.height() < MIN_SELECTION_PX:
            return None
        x = int((sel.x() - self._offset.x()) / self._scale)
        y = int((sel.y() - self._offset.y()) / self._scale)
        w = int(sel.width() / self._scale)
        h = int(sel.height() / self._scale)
        img_w, img_h = self._pixmap.width(), self._pixmap.height()
        x = max(0, min(x, img_w - 1))
        y = max(0, min(y, img_h - 1))
        w = min(w, img_w - x)
        h = min(h, img_h - y)
        if w < 1 or h < 1:
            return None
        return QRect(x, y, w, h)

    def _update_transform(self):
        if self._pixmap is None:
            return
        ww, wh = self.width(), self.height()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        self._scale = min(ww / pw, wh / ph)
        dw = int(pw * self._scale)
        dh = int(ph * self._scale)
        self._offset = QPoint((ww - dw) // 2, (wh - dh) // 2)

    def resizeEvent(self, event):
        self._update_transform()
        self.update()
```

- [ ] **Step 2: Add paintEvent**

Append to `canvas_widget.py` (inside the class, after `resizeEvent`):

```python
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#0d0d1a'))

        if self._pixmap is None:
            painter.setPen(QColor('#555'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'Open an image to get started')
            return

        dw = int(self._pixmap.width() * self._scale)
        dh = int(self._pixmap.height() * self._scale)
        scaled = self._pixmap.scaled(
            dw, dh,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(self._offset, scaled)

        if self._selection:
            self._paint_selection(painter, dw, dh)

        # hint text
        painter.setPen(QColor('#555'))
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        hint_rect = QRect(10, self.height() - 24, self.width() - 20, 20)
        painter.drawText(
            hint_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            'drag to draw selection · drag corners to resize · Esc to clear',
        )

    def _paint_selection(self, painter, disp_w, disp_h):
        sel = self._selection.normalized()
        ox, oy = self._offset.x(), self._offset.y()
        overlay = QColor(0, 0, 0, 128)

        # darken outside selection
        painter.fillRect(QRect(ox, oy, disp_w, sel.top() - oy), overlay)
        painter.fillRect(QRect(ox, sel.bottom(), disp_w, oy + disp_h - sel.bottom()), overlay)
        painter.fillRect(QRect(ox, sel.top(), sel.left() - ox, sel.height()), overlay)
        painter.fillRect(QRect(sel.right(), sel.top(), ox + disp_w - sel.right(), sel.height()), overlay)

        # dashed cyan border
        pen = QPen(QColor('#4fc3f7'), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(sel)

        # corner handles
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#4fc3f7'))
        for rect in self._handle_rects().values():
            painter.drawRect(rect)

        # size label inside selection
        img_sel = self.get_selection_rect()
        if img_sel:
            painter.setPen(QColor('#4fc3f7'))
            font = QFont('monospace', 9)
            painter.setFont(font)
            painter.drawText(sel, Qt.AlignmentFlag.AlignCenter, f'{img_sel.width()} × {img_sel.height()} px')
```

- [ ] **Step 3: Add handle helpers and mouse events**

Append to `canvas_widget.py` (inside the class):

```python
    def _handle_rects(self) -> dict:
        if self._selection is None:
            return {}
        sel = self._selection.normalized()
        return {
            'tl': QRect(sel.left() - HANDLE_HALF, sel.top() - HANDLE_HALF, HANDLE_SIZE, HANDLE_SIZE),
            'tr': QRect(sel.right() - HANDLE_HALF, sel.top() - HANDLE_HALF, HANDLE_SIZE, HANDLE_SIZE),
            'bl': QRect(sel.left() - HANDLE_HALF, sel.bottom() - HANDLE_HALF, HANDLE_SIZE, HANDLE_SIZE),
            'br': QRect(sel.right() - HANDLE_HALF, sel.bottom() - HANDLE_HALF, HANDLE_SIZE, HANDLE_SIZE),
        }

    def _hit_handle(self, pos: QPoint):
        for name, rect in self._handle_rects().items():
            if rect.contains(pos):
                return name
        return None

    def mousePressEvent(self, event):
        if self._pixmap is None or event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.pos()
        handle = self._hit_handle(pos)
        if handle:
            self._resize_handle = handle
            self._drag_start = None
        else:
            self._resize_handle = None
            self._drag_start = pos
            self._selection = QRect(pos, pos)
        self.update()

    def mouseMoveEvent(self, event):
        if self._pixmap is None:
            return
        pos = event.pos()
        if self._resize_handle and self._selection:
            sel = self._selection.normalized()
            if self._resize_handle == 'tl':
                self._selection = QRect(pos, QPoint(sel.right(), sel.bottom()))
            elif self._resize_handle == 'tr':
                self._selection = QRect(QPoint(sel.left(), pos.y()), QPoint(pos.x(), sel.bottom()))
            elif self._resize_handle == 'bl':
                self._selection = QRect(QPoint(pos.x(), sel.top()), QPoint(sel.right(), pos.y()))
            elif self._resize_handle == 'br':
                self._selection = QRect(QPoint(sel.left(), sel.top()), pos)
            self.update()
        elif self._drag_start:
            self._selection = QRect(self._drag_start, pos)
            self.update()
        else:
            cursors = {
                'tl': Qt.CursorShape.SizeFDiagCursor,
                'tr': Qt.CursorShape.SizeBDiagCursor,
                'bl': Qt.CursorShape.SizeBDiagCursor,
                'br': Qt.CursorShape.SizeFDiagCursor,
            }
            handle = self._hit_handle(pos)
            self.setCursor(cursors.get(handle, Qt.CursorShape.CrossCursor))

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_start = None
        self._resize_handle = None
        self.selection_changed.emit(self.get_selection_rect())
        self.update()
```

- [ ] **Step 4: Manual test — canvas**

```bash
python main.py
```

- Open an image (File menu not wired yet — that's Task 5; for now, temporarily hard-code a path in `AppWindow.__init__` by calling `self._canvas.load_image('/path/to/any/image.jpg')` then remove it).
- Alternatively, just verify the "Open an image to get started" placeholder text shows.

Expected: dark window with centered placeholder text.

- [ ] **Step 5: Commit**

```bash
git add canvas_widget.py
git commit -m "feat: implement CanvasWidget with image display and selection"
```

---

## Task 5: AppWindow — Full Implementation

**Files:**
- Overwrite: `app_window.py`

- [ ] **Step 1: Write the full AppWindow**

Overwrite `app_window.py`:

```python
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QComboBox, QLineEdit, QListWidget,
    QFileDialog, QMessageBox, QStatusBar, QLabel,
    QSizePolicy, QToolBar,
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QAction, QDesktopServices, QKeySequence

from canvas_widget import CanvasWidget
import file_manager

PRESET_NAMES = ['front', 'back', 'left', 'right', 'top', 'bottom', 'detail', 'section cut']


class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._source_path = None
        self._output_folder = None
        self._saved_names: list[str] = []
        self._setup_ui()
        self._setup_shortcuts()
        self._update_button_states()

    # ------------------------------------------------------------------ setup

    def _setup_ui(self):
        self.setWindowTitle('PhotoCrop')
        self.setMinimumSize(800, 600)
        self.resize(1100, 700)

        self._setup_menu()
        self._setup_toolbar()

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._canvas = CanvasWidget()
        self._canvas.selection_changed.connect(self._on_selection_changed)
        layout.addWidget(self._canvas)
        layout.addWidget(self._build_sidebar())
        self.setCentralWidget(central)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)

    def _setup_menu(self):
        menu = self.menuBar()
        file_menu = menu.addMenu('File')
        open_action = QAction('Open Image', self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._open_image)
        file_menu.addAction(open_action)
        menu.addMenu('Help')

    def _setup_toolbar(self):
        toolbar = QToolBar()
        toolbar.setMovable(False)

        self._open_btn = QPushButton('Open Image')
        self._open_btn.clicked.connect(self._open_image)
        toolbar.addWidget(self._open_btn)

        self._clear_btn = QPushButton('Clear Selection')
        self._clear_btn.clicked.connect(self._clear_selection)
        toolbar.addWidget(self._clear_btn)

        self._file_label = QLabel('')
        self._file_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._file_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self._file_label)

        self.addToolBar(toolbar)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(170)
        sidebar.setStyleSheet('background: #15151f; border-left: 1px solid #2a2a3a;')

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 14, 12, 14)
        layout.setSpacing(8)

        lbl = QLabel('VIEW NAME')
        lbl.setStyleSheet('color: #888; font-size: 10px; font-weight: 600; letter-spacing: 1px;')
        layout.addWidget(lbl)

        layout.addWidget(self._small_label('Preset'))
        self._preset_dropdown = QComboBox()
        self._preset_dropdown.addItems(PRESET_NAMES)
        self._preset_dropdown.currentTextChanged.connect(self._update_button_states)
        layout.addWidget(self._preset_dropdown)

        layout.addWidget(self._small_label('Custom (overrides preset)'))
        self._custom_name = QLineEdit()
        self._custom_name.setPlaceholderText('e.g. logo_area')
        self._custom_name.textChanged.connect(self._update_button_states)
        layout.addWidget(self._custom_name)

        self._save_btn = QPushButton('Save View')
        self._save_btn.clicked.connect(self._save_view)
        self._save_btn.setStyleSheet(
            'QPushButton { background: #4fc3f7; color: #000; font-weight: 700;'
            ' border-radius: 5px; padding: 7px; }'
            'QPushButton:disabled { background: #2a2a3a; color: #555; }'
        )
        layout.addWidget(self._save_btn)

        saved_lbl = QLabel('── Saved ──')
        saved_lbl.setStyleSheet('color: #888; font-size: 10px; font-weight: 600;'
                                ' letter-spacing: 1px; margin-top: 4px;')
        saved_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(saved_lbl)

        self._saved_list = QListWidget()
        self._saved_list.setStyleSheet(
            'background: transparent; color: #7ec8e3; border: none; font-size: 10px;'
        )
        layout.addWidget(self._saved_list)

        self._open_folder_btn = QPushButton('Open Output Folder')
        self._open_folder_btn.clicked.connect(self._open_folder)
        self._open_folder_btn.setStyleSheet(
            'QPushButton { background: #1e2e1e; color: #4caf50; border: 1px solid #2a4a2a;'
            ' border-radius: 4px; padding: 5px; font-size: 10px; }'
            'QPushButton:disabled { background: #1a1a2a; color: #333; border-color: #222; }'
        )
        layout.addWidget(self._open_folder_btn)

        return sidebar

    def _small_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet('color: #999; font-size: 9px;')
        return lbl

    def _setup_shortcuts(self):
        save_sc = QAction(self)
        save_sc.setShortcut(QKeySequence('Ctrl+S'))
        save_sc.triggered.connect(self._save_view)
        self.addAction(save_sc)

        esc_sc = QAction(self)
        esc_sc.setShortcut(Qt.Key.Key_Escape)
        esc_sc.triggered.connect(self._clear_selection)
        self.addAction(esc_sc)

    # ------------------------------------------------------------------ slots

    def _open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open Image', '',
            'Images (*.jpg *.jpeg *.png *.webp *.tiff *.tif *.bmp)',
        )
        if not path:
            return
        self._canvas.load_image(path)
        if self._canvas._pixmap is None or self._canvas._pixmap.isNull():
            QMessageBox.critical(self, 'Error', 'Could not open image. Unsupported or corrupt file.')
            return
        self._source_path = path
        self._output_folder = None
        self._saved_names.clear()
        self._saved_list.clear()
        stem = Path(path).name
        dims = f'{self._canvas._pixmap.width()} × {self._canvas._pixmap.height()} px'
        self.setWindowTitle(f'PhotoCrop — {stem}')
        self._file_label.setText(f'{stem} — {dims}')
        self._status_bar.clearMessage()
        self._update_button_states()

    def _clear_selection(self):
        self._canvas.clear_selection()

    def _get_current_name(self) -> str:
        custom = self._custom_name.text().strip()
        return custom if custom else self._preset_dropdown.currentText()

    def _save_view(self):
        if not self._save_btn.isEnabled():
            return
        rect = self._canvas.get_selection_rect()
        name = self._get_current_name()

        if name in self._saved_names:
            reply = QMessageBox.question(
                self, 'Overwrite?',
                f"A view named '{name}' already exists. Overwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                return

        try:
            crop_path = file_manager.save_crop(
                self._source_path,
                (rect.x(), rect.y(), rect.width(), rect.height()),
                name,
            )
        except Exception as e:
            QMessageBox.critical(self, 'Save Error', str(e))
            return

        self._output_folder = crop_path.parent

        if name not in self._saved_names:
            self._saved_names.append(name)

        stem = Path(self._source_path).stem
        safe = file_manager.sanitize_name(name)
        label_text = f'✓ {stem}_{safe}'

        for i in range(self._saved_list.count()):
            if self._saved_list.item(i).text() == label_text:
                self._saved_list.takeItem(i)
                break
        self._saved_list.addItem(label_text)

        self._status_bar.showMessage(f'Saved. Output: {self._output_folder}/')
        self._update_button_states()

    def _open_folder(self):
        if self._output_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_folder)))

    def _on_selection_changed(self, rect):
        if rect:
            r = self._canvas.get_selection_rect()
            if r:
                msg = f'Selection: {r.width()} × {r.height()} px at ({r.x()}, {r.y()})'
                if self._output_folder:
                    msg += f'       Output: {self._output_folder}/'
                self._status_bar.showMessage(msg)
        self._update_button_states()

    def _update_button_states(self):
        has_image = self._source_path is not None
        has_sel = self._canvas.get_selection_rect() is not None
        has_name = bool(self._get_current_name())
        self._save_btn.setEnabled(has_image and has_sel and has_name)
        self._clear_btn.setEnabled(has_sel)
        self._open_folder_btn.setEnabled(self._output_folder is not None)
```

- [ ] **Step 2: Run the app and do a full manual workflow test**

```bash
python main.py
```

Checklist:
- [ ] Window opens titled "PhotoCrop", dark theme, right sidebar visible
- [ ] Click **Open Image**, pick any JPG/PNG — image loads, title updates, toolbar shows filename + dimensions
- [ ] Drag a rectangle on the image — dashed cyan border appears, overlay darkens outside, size shown inside rectangle
- [ ] Drag a corner handle — selection resizes correctly
- [ ] Press `Esc` — selection clears
- [ ] Draw another rectangle, pick "front" from dropdown, click **Save View**
- [ ] "✓ filename_front" appears in saved list, status bar shows output path
- [ ] Draw another rectangle, type "logo_area" in the custom field, click **Save View** — saved as `logo_area`
- [ ] Click **Open Output Folder** — Finder opens to the output folder
- [ ] Verify output folder contains: original image + both crops with correct names
- [ ] Draw selection, pick "front" again, click Save View — overwrite dialog appears
- [ ] Click **Open Image** again — saved list clears, title updates

- [ ] **Step 3: Commit**

```bash
git add app_window.py
git commit -m "feat: implement AppWindow with toolbar, sidebar, save flow, and keyboard shortcuts"
```

---

## Task 6: End-to-End Verification

**Files:** none — verification only

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -v
```

Expected: 15 passed, 0 failed.

- [ ] **Step 2: Test section cut preset**

Open an image, draw a selection, choose "section cut" from the dropdown, click Save View.

Expected: file saved as `[stem]_section_cut.[ext]` (space replaced with underscore). Sidebar shows `✓ [stem]_section_cut`.

- [ ] **Step 3: Test overwrite flow**

Draw a selection, save as "front". Draw another selection in a different area, save as "front" again.

Expected: overwrite dialog appears. Click Yes → file is replaced. Click No → file unchanged.

- [ ] **Step 4: Test new image mid-session reset**

Save a few views. Click Open Image and pick a different image.

Expected: saved list clears, window title updates, canvas shows new image, Open Output Folder is disabled again.

- [ ] **Step 5: Test Ctrl+S shortcut**

Draw a selection, ensure a name is set, press `Ctrl+S`.

Expected: view is saved without clicking the button.

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "feat: complete image crop views app"
```
