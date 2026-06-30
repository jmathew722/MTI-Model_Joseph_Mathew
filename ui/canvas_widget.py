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
        self._selection = None    # QRect in image coords, or None
        self._drag_start = None   # QPoint (image coords) where current drag began
        self._resize_handle = None  # 'tl','tr','bl','br' or None

    def load_image(self, path: str) -> bool:
        """Load image from path. Returns True on success, False on failure."""
        self._pixmap = None  # clear first so callers always see consistent state
        self._selection = None
        self._drag_start = None
        self._resize_handle = None
        pix = QPixmap(path)
        if pix.isNull():
            self.update()
            self.selection_changed.emit(None)
            return False
        self._pixmap = pix
        self._update_transform()
        self.update()
        self.selection_changed.emit(None)
        return True

    def image_size(self):
        """Return (width, height) of the loaded image, or None if no image."""
        if self._pixmap is None or self._pixmap.isNull():
            return None
        return (self._pixmap.width(), self._pixmap.height())

    def unload(self):
        """Remove the current image and reset all state."""
        self._pixmap = None
        self._selection = None
        self._drag_start = None
        self._resize_handle = None
        self._scale = 1.0
        self._offset = QPoint(0, 0)
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
        # Convert to display pixels to check minimum size
        disp_w = int(sel.width() * self._scale)
        disp_h = int(sel.height() * self._scale)
        if disp_w < MIN_SELECTION_PX or disp_h < MIN_SELECTION_PX:
            return None
        if sel.width() < 1 or sel.height() < 1:
            return None
        return sel

    def _img_to_disp(self, x: int, y: int) -> QPoint:
        """Convert image coordinates to display coordinates."""
        return QPoint(int(x * self._scale) + self._offset.x(),
                      int(y * self._scale) + self._offset.y())

    def _disp_to_img(self, pos: QPoint) -> QPoint:
        """Convert display coordinates to image coordinates (clamped)."""
        if self._pixmap is None:
            return QPoint(0, 0)
        x = int((pos.x() - self._offset.x()) / self._scale)
        y = int((pos.y() - self._offset.y()) / self._scale)
        x = max(0, min(x, self._pixmap.width()))
        y = max(0, min(y, self._pixmap.height()))
        return QPoint(x, y)

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
        super().resizeEvent(event)
        self._update_transform()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#080d14'))

        if self._pixmap is None:
            painter.setPen(QColor('#555'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'Open an image to get started')
            return

        dw = int(self._pixmap.width() * self._scale)
        dh = int(self._pixmap.height() * self._scale)
        ratio = self.devicePixelRatio()
        scaled = self._pixmap.scaled(
            int(dw * ratio), int(dh * ratio),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        painter.drawPixmap(self._offset, scaled)

        if self._selection is not None:
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
        sel_img = self._selection.normalized()
        # Convert selection corners to display coords
        tl = self._img_to_disp(sel_img.left(), sel_img.top())
        br = self._img_to_disp(sel_img.right(), sel_img.bottom())
        sel = QRect(tl, br)  # display-space selection rect

        ox, oy = self._offset.x(), self._offset.y()
        overlay = QColor(0, 0, 0, 128)

        # darken outside selection (clipped to image area)
        painter.fillRect(QRect(ox, oy, disp_w, max(0, sel.top() - oy)), overlay)
        painter.fillRect(QRect(ox, sel.bottom(), disp_w, max(0, oy + disp_h - sel.bottom())), overlay)
        painter.fillRect(QRect(ox, sel.top(), max(0, sel.left() - ox), sel.height()), overlay)
        painter.fillRect(QRect(sel.right(), sel.top(), max(0, ox + disp_w - sel.right()), sel.height()), overlay)

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

    def _handle_rects(self) -> dict:
        if self._selection is None:
            return {}
        sel = self._selection.normalized()
        tl = self._img_to_disp(sel.left(), sel.top())
        br = self._img_to_disp(sel.right(), sel.bottom())
        tr = QPoint(br.x(), tl.y())
        bl = QPoint(tl.x(), br.y())
        return {
            'tl': QRect(tl.x() - HANDLE_HALF, tl.y() - HANDLE_HALF, HANDLE_SIZE, HANDLE_SIZE),
            'tr': QRect(tr.x() - HANDLE_HALF, tr.y() - HANDLE_HALF, HANDLE_SIZE, HANDLE_SIZE),
            'bl': QRect(bl.x() - HANDLE_HALF, bl.y() - HANDLE_HALF, HANDLE_SIZE, HANDLE_SIZE),
            'br': QRect(br.x() - HANDLE_HALF, br.y() - HANDLE_HALF, HANDLE_SIZE, HANDLE_SIZE),
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
            img_pos = self._disp_to_img(pos)
            self._drag_start = img_pos
            self._selection = QRect(img_pos, img_pos)
        self.update()

    def mouseMoveEvent(self, event):
        if self._pixmap is None:
            return
        pos = event.pos()
        if self._resize_handle and self._selection is not None:
            img_pos = self._disp_to_img(pos)
            sel = self._selection.normalized()
            if self._resize_handle == 'tl':
                self._selection = QRect(img_pos, QPoint(sel.right(), sel.bottom()))
            elif self._resize_handle == 'tr':
                self._selection = QRect(QPoint(sel.left(), img_pos.y()), QPoint(img_pos.x(), sel.bottom()))
            elif self._resize_handle == 'bl':
                self._selection = QRect(QPoint(img_pos.x(), sel.top()), QPoint(sel.right(), img_pos.y()))
            elif self._resize_handle == 'br':
                self._selection = QRect(QPoint(sel.left(), sel.top()), img_pos)
            self.update()
        elif self._drag_start is not None:
            img_pos = self._disp_to_img(pos)
            self._selection = QRect(self._drag_start, img_pos)
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
