"""
pywin32 Windows service wrapper for Alfred.

Replaces the old WinSW service (alfred-service.xml). Runs the bot as LocalSystem.
Service-control commands (must run from an elevated shell):

    venv\\Scripts\\python service.py install --startup delayed
    venv\\Scripts\\python service.py start
    venv\\Scripts\\python service.py stop
    venv\\Scripts\\python service.py restart
    venv\\Scripts\\python service.py remove

On install, SCM recovery options are set so a crash restarts the service
(10s, then 30s, then give up). Per-device settings stay in .env, which the
deploy script never touches.
"""
import os
import sys
import logging
import threading
from logging.handlers import RotatingFileHandler

try:
    import win32event
    import win32service
    import win32serviceutil
except ImportError:
    win32event = None
    win32service = None
    win32serviceutil = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICE_NAME = "alfred-agent"
DISPLAY_NAME = "Alfred Agent Service"
DESCRIPTION = "Telegram bot agent powered by Ollama, running as a Windows service."

logger = logging.getLogger(__name__)


def setup_logging():
    """Rotating file log under logs/ — SCM does not capture stdout/stderr."""
    logs_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(logs_dir, "alfred-service.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(handler)


class AlfredService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)

    @classmethod
    def Install(cls, *args, **kwargs):
        win32serviceutil.ServiceFramework.Install(*args, **kwargs)
        try:
            cls._set_failure_actions()
        except Exception as e:
            print(f"Warning: could not set service recovery options: {e}")

    @classmethod
    def _set_failure_actions(cls):
        """Restart on crash: after 10s, then 30s, then leave stopped."""
        hscm = win32service.OpenSCManager(
            None, None, win32service.SC_MANAGER_ALL_ACCESS
        )
        try:
            hsvc = win32service.OpenService(
                hscm, cls._svc_name_, win32service.SERVICE_ALL_ACCESS
            )
            try:
                win32service.ChangeServiceConfig2(
                    hsvc,
                    win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
                    (
                        0,   # reset period (seconds)
                        None,  # reboot message
                        None,  # reboot command
                        [
                            (win32service.SC_ACTION_RESTART, 10000),
                            (win32service.SC_ACTION_RESTART, 30000),
                            (win32service.SC_ACTION_NONE, 0),
                        ],
                    ),
                )
            finally:
                win32service.CloseServiceHandle(hsvc)
        finally:
            win32service.CloseServiceHandle(hscm)

    def SvcStop(self):
        """Called by the SCM on a separate thread from SvcDoRun."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        logger.info("Stop requested; shutting down bot...")
        import telegram_bot

        telegram_bot.stop_bot()
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        setup_logging()
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)

        # Service processes start with cwd=%SystemRoot%\System32; repo-root cwd
        # keeps relative paths sane and lets config.py find .env.
        os.chdir(BASE_DIR)
        os.environ["ALFRED_SERVICE_NAME"] = SERVICE_NAME

        logger.info(f"{DISPLAY_NAME} starting (LocalSystem)...")

        import telegram_bot

        bot_thread = threading.Thread(target=telegram_bot.main, daemon=True)
        bot_thread.start()

        win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
        bot_thread.join(timeout=30)
        if bot_thread.is_alive():
            logger.warning("Bot thread did not exit cleanly; forcing exit.")
        logger.info(f"{DISPLAY_NAME} stopped.")


if __name__ == "__main__":
    if win32serviceutil is None:
        print(
            "pywin32 is not installed. Run: "
            ".\\venv\\Scripts\\python -m pip install -r requirements.txt"
        )
        sys.exit(1)
    win32serviceutil.HandleCommandLine(AlfredService)