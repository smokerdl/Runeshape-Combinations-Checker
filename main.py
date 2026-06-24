"""
main.py

Точка входа. setQuitOnLastWindowClosed(False) — программа живёт в трее
и не завершается при закрытии окна настроек. Завершение только через
_quit_app() в MainWindow (крестик или "Выход" в меню трея).
"""
import sys
from pathlib import Path
import os

from PySide6.QtWidgets import QApplication
from settings_window import MainWindow


def main() -> None:
    here = Path(__file__).resolve().parent
    os.chdir(here)

    app = QApplication(sys.argv)
    app.setApplicationName("Runeshape Combinations Checker")
    app.setOrganizationName("smokerdl")
    # False — чтобы программа жила в трее даже когда окно настроек скрыто
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
