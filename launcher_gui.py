"""Точка входа для PyInstaller (GUI). Не запускать напрямую — используйте `python -m localclass`."""
import multiprocessing
import os
import sys

# то же, что и в localclass/__main__.py: включаем High-DPI до любого импорта Qt
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from localclass.__main__ import main
    sys.exit(main())
