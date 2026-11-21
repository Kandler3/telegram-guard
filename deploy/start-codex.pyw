"""Console-free shortcut entry point for the existing shared Desktop launcher."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
from telegram_guard_local.shared_desktop import run


def show_error(message):
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, "Codex — фоновые подписки", 0x10)


def launch(home, runner=run, notify=show_error):
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            runner(home, "start")
        return True
    except Exception as error:
        message = str(error)
        if "Close Desktop normally first" in message:
            message = "Codex уже открыт. Закрой его обычным способом после завершения текущих задач, затем нажми эту иконку ещё раз."
        elif "executable is missing" in message:
            message = "Не найден компонент установленного Codex. Дождись завершения обновления и повтори запуск. Если ошибка сохраняется, потребуется диагностика установки."
        elif isinstance(error,OSError) and getattr(error,"winerror",None)==14001:
            message = "Windows не смогла запустить компонент Codex (ошибка 14001). Возможно, launcher ссылается на старую сборку после обновления. Попроси агента проверить пути запуска."
        notify(message)
        return False


if __name__ == "__main__":
    launch(Path(__file__).absolute().parent)
