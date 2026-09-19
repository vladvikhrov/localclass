"""Точка входа для PyInstaller (headless-узел). Эквивалент `python -m localclass.node`."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from localclass.node import main
    sys.exit(main())
