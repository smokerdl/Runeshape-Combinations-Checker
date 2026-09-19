"""
main.py

Точка входа. setQuitOnLastWindowClosed(False) — программа живёт в трее
и не завершается при закрытии окна настроек. Завершение только через
_quit_app() в MainWindow (крестик или "Выход" в меню трея).
"""
import sys
from pathlib import Path
import os

from PySide6.QtWidgets import QApplication, QMessageBox


_SINGLE_INSTANCE_MUTEX_NAME = "Local\\RuneshapeCombinationsChecker"
_ALREADY_EXISTS = 183  # ERROR_ALREADY_EXISTS


def _acquire_single_instance():
    """Создаёт именованный Windows mutex и удерживает его до завершения процесса.

    Если другой экземпляр уже владеет mutex, возвращает None. В отличие от
    lock-файла, Windows автоматически освобождает mutex при аварийном
    завершении процесса, поэтому отдельный stale-файл не может заблокировать
    запуск навсегда.
    """
    if os.name != "nt":
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, f"CreateMutexW failed with error {error}")

    if ctypes.get_last_error() == _ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None

    return handle


def _release_single_instance(handle) -> None:
    if os.name != "nt" or handle is True or handle is None:
        return

    import ctypes

    ctypes.windll.kernel32.CloseHandle(handle)




def main() -> None:
    instance_handle = _acquire_single_instance()

    app = QApplication(sys.argv)
    app.setApplicationName("Runeshape Combinations Checker")
    app.setOrganizationName("smokerdl")

    if instance_handle is None:
        QMessageBox.information(
            None,
            "Runeshape Combinations Checker",
            "Программа уже запущена. Найдите её в системном трее.",
        )
        return

    try:
        # Импортируем окно только после проверки mutex, чтобы второй экземпляр
        # не успевал запускать загрузку конфигурации/данных до завершения.
        from settings_window import MainWindow

        here = Path(__file__).resolve().parent
        os.chdir(here)

        # False — чтобы программа жила в трее даже когда окно настроек скрыто
        app.setQuitOnLastWindowClosed(False)

        window = MainWindow()
        window.show()

        sys.exit(app.exec())
    finally:
        _release_single_instance(instance_handle)


if __name__ == "__main__":
    main()
