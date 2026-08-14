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

## Deployment & Security: Running as a Windows Service

To securely execute shell commands without exposing your host to administrator-level risks, this bot should be run as a Windows Service under a dedicated, limited local account. We use WinSW for service management.

### 1. Create the Limited Service Account
Open an elevated Command Prompt or PowerShell as Administrator and create a new local user:
```powershell
net user alfredsvc YourSecurePassword123! /add
```
*Ensure this user is a standard user, NOT an Administrator.*

### 2. Configure Minimal Permissions
To remain secure, `alfredsvc` should only have the bare minimum permissions required to run the bot.
Grant `alfredsvc` the following permissions:
- **Read & Execute**: The root project directory (e.g., `d:\Alfred\alfred-agent`).
- **Write**: 
  - The `data/` directory (for memory, conversations, and audit logs).
  - The `logs/` directory (for WinSW service logs).
  - Your designated `SHELL_WORKING_DIR` (from your `.env`), if it requires writing output.

Do not grant `alfredsvc` broader system permissions. Expand access deliberately only when a specific, legitimate workflow requires it.

### 3. Service Configuration (WinSW)
1. Download the latest WinSW executable (e.g., `WinSW-x64.exe`) and place it in the root of the project directory.
2. Rename it to match the XML config name: `alfred-service.exe`.
3. Open `alfred-service.xml` and update the `<password>` field with the password you set for `alfredsvc`.
4. Create the logs directory if it doesn't exist: `mkdir logs`

### 4. Install, Start, Stop, and Uninstall
Open an elevated Administrator terminal in the project directory:

- **Install the service**: `.\alfred-service.exe install`
- **Start the service**: `.\alfred-service.exe start`
- **Check status**: `.\alfred-service.exe status`
- **Stop the service**: `.\alfred-service.exe stop`
- **Uninstall**: `.\alfred-service.exe uninstall`

### 5. Logs location
- **Application Logs**: Bot interactions and memory are saved to `data/conversations.jsonl` and `data/audit_log.jsonl`.
- **Service Logs**: Stdout and Stderr crashes from the Python process are handled by WinSW and safely rolled inside the `logs/` directory (e.g., `logs/alfred-service.out.log` and `logs/alfred-service.err.log`).

### 6. WSL2 Access Note
If you plan to use the `wsl` shell option via the shell execution tool, note that invoking WSL from a non-interactive Windows service account (like `alfredsvc`) has known quirks. WSL distributions are often registered per-user. 
*This requires testing on your specific host.* You may need to explicitly configure `alfredsvc` to have a default WSL distribution or import a rootfs for it. Verify its functionality manually via `runas /user:alfredsvc "wsl -- bash -c 'ls'"` before relying on it in the bot.
