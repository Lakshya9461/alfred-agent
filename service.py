"""
pywin32 Windows service wrapper for Alfred.

Replaces the old WinSW service (alfred-service.xml). Runs the bot as LocalSystem.
Service-control commands (must run from an elevated shell):

    venv\\Scripts\\python service.py install --startup auto
    venv\\Scripts\\python service.py start
    venv\\Scripts\\python service.py stop
    venv\\Scripts\\python service.py restart
    venv\\Scripts\\python service.py remove

On install, SCM recovery options are set so a crash restarts the service
(10s, then 30s, then give up). Per-device settings stay in .env, which the
deploy script never touches.
"""
import glob
import os
import shutil
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

if win32serviceutil is None:
    sys.stderr.write(
        "pywin32 is not installed in this venv.\n"
        "Install dependencies first, then retry:\n"
        "    .\\venv\\Scripts\\python -m pip install -r requirements.txt\n"
    )
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICE_NAME = "alfred-agent"
DISPLAY_NAME = "Alfred Agent Service"
DESCRIPTION = "Telegram bot agent powered by Ollama, running as a Windows service."

logger = logging.getLogger(__name__)


def _ensure_host_in_scripts():
    """pythonservice.exe must run from venv\\Scripts (where python.exe lives),
    or Python 3.13's path resolver treats an exe at the venv root as a base
    install and never adds the venv site-packages to sys.path — so
    'servicemanager' can't be imported and the service dies with
    'unable to locate the service manager'. Also copy the load-time DLLs
    (python*.dll, vcruntime140*.dll, pywintypes*.dll) next to the host exe."""
    scripts = os.path.join(sys.prefix, "Scripts")
    os.makedirs(scripts, exist_ok=True)
    src_host = os.path.join(sys.prefix, "pythonservice.exe")
    if os.path.exists(src_host) and not os.path.exists(
        os.path.join(scripts, "pythonservice.exe")
    ):
        shutil.copy2(src_host, os.path.join(scripts, "pythonservice.exe"))

    home = None
    cfg = os.path.join(sys.prefix, "pyvenv.cfg")
    if os.path.exists(cfg):
        for line in open(cfg, encoding="utf-8"):
            if line.strip().startswith("home"):
                home = line.split("=", 1)[1].strip()
                break
    for pattern in ("python*.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        for src in glob.glob(os.path.join(home, pattern)) if home else []:
            dst = os.path.join(scripts, os.path.basename(src))
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
    pywin32_sys = os.path.join(
        sys.prefix, "Lib", "site-packages", "pywin32_system32"
    )
    for pattern in ("pywintypes*.dll", "pythoncom*.dll"):
        for src in glob.glob(os.path.join(pywin32_sys, pattern)):
            dst = os.path.join(scripts, os.path.basename(src))
            if not os.path.exists(dst):
                shutil.copy2(src, dst)


def _scripts_host_exe():
    """Service ImagePath host: prefer the venv Scripts copy (works on all
    supported Pythons); fall back to the venv-root host win32serviceutil
    installs by default."""
    host = os.path.join(sys.prefix, "Scripts", "pythonservice.exe")
    if os.path.exists(host):
        return host
    return os.path.join(sys.prefix, "pythonservice.exe")


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
        """Restart on crash: after 10s, then 30s, then leave stopped. A
        non-zero reset period (1 day) forgets past failures, so a crash storm
        on one boot doesn't permanently suppress the service on later boots."""
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
                        86400,   # reset period (seconds) — count failures only within a day
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
    # pywin32's getopt requires options to precede the command token
    # (`service.py --startup auto install`). People naturally type
    # `service.py install --startup auto`, so reorder the command to the end
    # when everything after it is an option.
    if len(sys.argv) > 2:
        first = sys.argv[1]
        if (
            not first.startswith("-")
            and first in ("install", "update", "remove", "start", "stop", "restart", "debug")
            and any(a.startswith("-") for a in sys.argv[2:])
        ):
            sys.argv = [sys.argv[0]] + sys.argv[2:] + [first]
    # After any reorder the command token sits at the end (getopt options were
    # moved before it). Use the last arg so `install --startup auto` and the
    # canonical `--startup auto install` both hit this branch.
    cmd = sys.argv[-1] if len(sys.argv) > 1 else None
    if cmd in ("install", "update"):
        _ensure_host_in_scripts()
        host = _scripts_host_exe()
        if os.path.exists(host):
            print(f"[service] hosting from {host}")
            AlfredService._exe_name_ = host
    win32serviceutil.HandleCommandLine(AlfredService)