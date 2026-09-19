"""GUI-точка входа: `python -m localclass` (или LocalClass.exe). Аргументы те же, что у localclass.node."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from .node import build_parser, make_node
    args = build_parser().parse_args(argv)
    node = make_node(args)

    from PySide6.QtWidgets import QApplication, QMessageBox
    from .ui.app.bridge import Bridge
    from .ui.app.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("LocalClass")
    app.setQuitOnLastWindowClosed(True)
    bridge = Bridge(node)
    try:
        bridge.start()
    except RuntimeError as e:
        QMessageBox.critical(None, "LocalClass", f"Не удалось запустить узел:\n{e}\n\n"
                             "Возможно, порт занят другим экземпляром LocalClass. Измените порт (--port) или закройте другой экземпляр.")
        return 1
    win = MainWindow(bridge)
    win.show()
    if args.create_session:
        bridge.call(node.create_session(args.create_session), lambda _: win.refresh_all())
    elif args.join_code:
        bridge.call(node.join_by_code(args.join_code), lambda _: win.refresh_all())
    code = app.exec()
    bridge.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
