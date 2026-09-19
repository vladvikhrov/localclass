# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: сборка LocalClass.exe (GUI, без консоли) и localclass-node.exe (консольный узел без GUI).
# Запуск: pyinstaller LocalClass.spec  (см. scripts/build_windows.ps1)
import os

block_cipher = None
hidden = ["zeroconf", "zeroconf._utils.ipaddress", "ifaddr", "qrcode", "cryptography.hazmat.backends.openssl"]

gui_a = Analysis(
    ["launcher_gui.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden + ["PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
              "PySide6.QtPdf", "tkinter", "matplotlib", "numpy", "PIL"],
    cipher=block_cipher,
    noarchive=False,
)
gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data, cipher=block_cipher)
gui_exe = EXE(
    gui_pyz, gui_a.scripts, [],
    exclude_binaries=True,
    name="LocalClass",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="installer/localclass.ico" if os.path.exists("installer/localclass.ico") else None,
)

node_a = Analysis(
    ["launcher_node.py"],
    pathex=["."],
    hiddenimports=hidden,
    excludes=["PySide6", "tkinter", "matplotlib", "numpy", "PIL"],
    cipher=block_cipher,
)
node_pyz = PYZ(node_a.pure, node_a.zipped_data, cipher=block_cipher)
node_exe = EXE(
    node_pyz, node_a.scripts, [],
    exclude_binaries=True,
    name="localclass-node",
    console=True,
    upx=False,
)

coll = COLLECT(
    gui_exe, gui_a.binaries, gui_a.zipfiles, gui_a.datas,
    node_exe, node_a.binaries, node_a.zipfiles, node_a.datas,
    strip=False, upx=False, name="LocalClass",
)
