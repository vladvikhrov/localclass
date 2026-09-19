"""Точка входа для PyInstaller (GUI). Не запускать напрямую — используйте `python -m localclass`."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from localclass.__main__ import main
    sys.exit(main())
