# alfred-agent

A Telegram bot agent powered by an Ollama backend, capable of executing shell commands, searching the web, and maintaining memory.

## Setup

1. **Clone the repository.**
2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   .\venv\Scripts\activate
   ```
3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
4. **Configure environment variables:**
   - Copy `.env.example` to `.env`.
   - Fill in the required values in `.env` (Telegram token, allowed user IDs, etc.).

## Running Manually for Testing

1. Activate the virtual environment.
2. Run the entrypoint:
   ```bash
   python main.py
   ```

## Deployment: Running as a Windows Service (pywin32)

The bot runs as a Windows service via pywin32 (`service.py`), replacing the old
WinSW setup. The service runs as **LocalSystem** — no per-account config and no
password, and it keeps running after you log off. On install, the Service
Control Manager is configured to auto-restart the service if it crashes.

### Local install (once per machine)

1. Install dependencies: `pip install -r requirements.txt` (adds `pywin32`).
2. Open an **Administrator** PowerShell (right-click → "Run as administrator",
   *not* just any terminal — see below), cd to the project directory, then:
   ```powershell
   .\venv\Scripts\python service.py install --startup delayed
   .\venv\Scripts\python service.py start
   ```
3. Check it: `Get-Service alfred-agent`.

Service-control commands (`stop` / `restart` / `remove`) also need an
Administrator shell.

> **"Error installing service: Access is denied. (5)"** means the shell is not
> elevated. pywin32's install must (a) register the service in the Service
> Control Manager and (b) copy `pythonservice.exe` into the venv plus a
> `pywintypes*.dll` into the Python base dir — the DLL copy is the part that
> fails from a non-admin shell. A non-elevated attempt is harmless; just retry
> from an elevated shell. To confirm elevation:
> ```powershell
> ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
> ```
>
> **Still "Access is denied" from an elevated shell?** Your Python came from the
> Microsoft Store: pywin32 copies `pywintypes*.dll` next to the base `python*.dll`
> inside `C:\Program Files\WindowsApps\...`, a directory protected even from
> Administrators. Fixes:
> - **Best:** install the same Python version from python.org, delete the venv,
>   recreate it (`python -m venv venv`), reinstall deps, retry install.
> - **Quick:** grant write on the Store package folder and retry:
>   ```powershell
>   icacls "C:\Program Files\WindowsApps\<store python package>" /grant Administrators:F /T
>   ```
>   (may be reset by Store updates; prefer the python.org install)

Logs: the bot's normal logs stay in `data/` (`conversations.jsonl`,
`audit_log.jsonl`); service-level Python logging goes to
`logs\alfred-service.log` (rotated, kept in the repo dir). The SCM does **not**
capture stdout/stderr.

### Updating other devices (`deploy.py`)

`deploy.py` pushes the repo and (re)installs the service on one or more targets
from this machine. Targets live in `deploy_config.json` (gitignored; a template
is created on first run). Each entry is either a local path or a remote host:

```json
{
  "dev":  { "local": true, "path": "D:\\Alfred\\alfred-agent" },
  "prod": { "local": true, "path": "C:\\1\\alfred-agent" },
  "pc2":  { "host": "pc2", "user": "alice", "path": "C:\\alfred-agent" }
}
```

Deploy a target:

```powershell
.\venv\Scripts\python deploy.py dev --dry-run   # preview commands only
.\venv\Scripts\python deploy.py prod            # full deploy (UAC elevation if needed)
```

Per target, `deploy.py`:

1. **Mirrors the repo** with `robocopy /MIR`, excluding `.env`, `data/`, `venv/`,
   `logs/`, `__pycache__`, `*.pyc`. **`.env` and `data/` are never touched** —
   each device keeps its own token, Ollama URL, and memory.
2. Creates the target's `venv` (if missing) and `pip install -r requirements.txt`.
3. Reinstalls the service: `stop` → `remove` → `install --startup delayed` → `start`.

Remote targets are copied to the host's admin share (`\\host\c$`) and updated
via WinRM (`Invoke-Command`); the host must have WinRM enabled and the user must
be an administrator there. Remote credentials can be stored in the (gitignored)
`deploy_config.json` `password` field or entered interactively.

### Updating via Telegram

The `/update` command in Telegram already runs `git pull --ff-only` and restarts.
Under the pywin32 service, `monitor.restart_bot()` detects service mode and
spawns a detached `service.py restart` (no `RESTART_COMMAND` needed in `.env`).

### WSL2 note

If you use the `wsl` shell option, note that invoking WSL from a non-interactive
service account (including LocalSystem) has known quirks — WSL distributions are
registered per-user, and SYSTEM may run as a different default user. Verify
manually before relying on it: `psexec -s wsl -- bash -c 'whoami'`.
