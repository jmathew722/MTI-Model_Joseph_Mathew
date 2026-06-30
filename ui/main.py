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
