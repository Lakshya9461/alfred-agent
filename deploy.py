"""
Deploy/update Alfred to other devices from this machine.

Usage:
    python deploy.py <target> [--dry-run] [--no-elevate]

Targets come from deploy_config.json (gitignored, never deployed). Entries:
    {"local": true, "path": "D:\\Alfred\\alfred-agent"}             # a path on this machine
    {"host": "pc2", "user": "alice", "path": "C:\\alfred-agent"}    # remote via WinRM + UNC admin share

Steps per target:
  1. Mirror the repo with robocopy, excluding .env / data/ / venv/ / logs/
     / __pycache__ / *.pyc — per-device .env and data/ are never touched.
  2. Create the venv if missing and `pip install -r requirements.txt`.
  3. (Re)install the pywin32 service: stop -> remove -> install --startup delayed -> start.

Service install needs an admin shell; deploy.py re-launches itself elevated via
UAC when required (pass --no-elevate to forbid that).
"""
import os
import sys
import json
import argparse
import subprocess
import ctypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "deploy_config.json")

EXCLUDE_DIRS = ["venv", "data", "logs", "__pycache__"]
EXCLUDE_FILES = [".env", "*.pyc", "alfred-service.exe", "deploy_config.json"]

DEFAULT_CONFIG = {
    "dev": {"local": True, "path": BASE_DIR},
    "prod": {"local": True, "path": r"C:\1\alfred-agent"},
}

REMOTE_STEPS = r"""
param($Dest)
$ErrorActionPreference = 'Continue'
Write-Host "[venv] ensuring $Dest\venv ..."
if (-not (Test-Path "$Dest\venv\Scripts\python.exe")) {
    python -m venv "$Dest\venv"
}
& "$Dest\venv\Scripts\python.exe" -m pip install -q -r "$Dest\requirements.txt"
Write-Host "[service] stopping (ignored if absent) ..."
& "$Dest\venv\Scripts\python.exe" "$Dest\service.py" stop 2>$null
Write-Host "[service] removing (ignored if absent) ..."
& "$Dest\venv\Scripts\python.exe" "$Dest\service.py" remove 2>$null
Write-Host "[service] installing ..."
& "$Dest\venv\Scripts\python.exe" "$Dest\service.py" install --startup delayed
if ($LASTEXITCODE -ne 0) { Write-Host "INSTALL FAILED"; exit $LASTEXITCODE }
Write-Host "[service] starting ..."
& "$Dest\venv\Scripts\python.exe" "$Dest\service.py" start
if ($LASTEXITCODE -ne 0) { Write-Host "START FAILED"; exit $LASTEXITCODE }
Write-Host "[ok] $Dest updated"
"""


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True


def require_elevated(no_elevate: bool) -> None:
    if is_admin() or no_elevate:
        return
    print("Service install needs an admin shell; re-launching elevated...")
    ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        subprocess.list2cmdline(sys.argv),
        BASE_DIR,
        1,
    )
    sys.exit(0)


def load_targets() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    print(f"No {CONFIG_FILE} found — creating template.")
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    return dict(DEFAULT_CONFIG)


def run(args: list, dry_run: bool, cwd: str | None = None) -> int:
    print("+ " + " ".join(args))
    if dry_run:
        return 0
    result = subprocess.run(args, cwd=cwd)
    return result.returncode


def robocopy(src: str, dst: str) -> list:
    xd = [os.path.join(src, d) for d in EXCLUDE_DIRS]
    xf = EXCLUDE_FILES
    return (
        ["robocopy", src, dst, "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NP"]
        + ["/XD"] + xd
        + ["/XF"] + xf
    )


def unc_path(target: dict) -> str:
    path = target["path"].rstrip("\\")
    drive = path[0].lower()
    rest = path[2:].lstrip("\\")
    return f"\\\\{target['host']}\\{drive}${rest}"


def deploy_local(target: dict, dry_run: bool) -> int:
    dst = target["path"]
    code = run(robocopy(BASE_DIR, dst), dry_run)
    if code >= 8:
        print(f"robocopy failed (exit {code})")
        return 1

    venv_py = os.path.join(dst, "venv", "Scripts", "python.exe")
    if not os.path.exists(venv_py):
        print(f"[venv] creating {dst}\\venv ...")
        run(["python", "-m", "venv", os.path.join(dst, "venv")], dry_run)

    print("[venv] pip install -r requirements.txt ...")
    run([venv_py, "-m", "pip", "install", "-q", "-r", os.path.join(dst, "requirements.txt")], dry_run)

    service = os.path.join(dst, "service.py")
    for step in ["stop", "remove"]:
        print(f"[service] {step} (ignored if absent) ...")
        run([venv_py, service, step], dry_run)
    print("[service] install --startup delayed ...")
    if run([venv_py, service, "install", "--startup", "delayed"], dry_run) != 0:
        return 1
    print("[service] start ...")
    if run([venv_py, service, "start"], dry_run) != 0:
        return 1
    print(f"[ok] {dst} updated")
    return 0


def deploy_remote(target: dict, dry_run: bool) -> int:
    dst_unc = unc_path(target)
    code = run(robocopy(BASE_DIR, dst_unc), dry_run)
    if code >= 8:
        print(f"robocopy failed (exit {code})")
        return 1

    cred = ""
    password = target.get("password")
    if password:
        cred = (
            f" -Credential (New-Object System.Management.Automation.PSCredential"
            f"('{target['user']}', (ConvertTo-SecureString '{password}' -AsPlainText -Force)))"
        )
    elif target.get("user"):
        cred = f" -Credential {target['user']}"

    # The remote machine runs the same steps as a local deploy.
    script = (
        f"powershell -NoProfile -NonInteractive -Command "
        f"\"Invoke-Command -ComputerName {target['host']} -ScriptBlock {{ "
        f"{REMOTE_STEPS} }} -ArgumentList '{target['path']}'{cred}\""
    )
    print("+ " + script)
    if dry_run:
        return 0
    return subprocess.run(script, shell=True).returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy Alfred to other devices.")
    parser.add_argument("target", help="target name from deploy_config.json")
    parser.add_argument("--dry-run", action="store_true", help="print commands without running them")
    parser.add_argument("--no-elevate", action="store_true", help="never self-elevate")
    args = parser.parse_args()

    require_elevated(args.no_elevate)

    targets = load_targets()
    if args.target not in targets:
        print(f"Unknown target '{args.target}'. Known: {', '.join(targets)}")
        sys.exit(1)

    target = targets[args.target]
    print(f"=== Deploying to '{args.target}' ({target.get('path', target.get('host'))}) ===")
    rc = deploy_local(target, args.dry_run) if target.get("local") else deploy_remote(target, args.dry_run)
    sys.exit(rc)


if __name__ == "__main__":
    main()