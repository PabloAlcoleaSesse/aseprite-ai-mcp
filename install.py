"""One-command cross-platform installer for Aseprite AI MCP (macOS, Linux, Windows).

Usage:
    python3 install.py   # macOS / Linux
    py install.py        # Windows

Automatically creates the virtual environment, installs dependencies, packages
and installs the Aseprite extension, configures Codex / ChatGPT, Claude Code,
and Antigravity, and starts the background local MCP service.
"""
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
LABEL = "local.aseprite.ai-mcp"
STAMP = time.strftime("%Y%m%d-%H%M%S")


def venv_python(root: Path = ROOT, system: str | None = None) -> Path:
    system = system or platform.system()
    if system == "Windows":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def venv_pythonw(root: Path = ROOT) -> Path:
    pw = root / ".venv" / "Scripts" / "pythonw.exe"
    return pw if pw.exists() else venv_python(root, "Windows")


def platform_paths(home: Path | None = None, system: str | None = None, env: dict | None = None) -> dict[str, Path]:
    home = home or Path.home()
    system = system or platform.system()
    env = env if env is not None else os.environ

    if system == "Darwin":
        support = home / "Library/Application Support/Aseprite AI MCP"
        aseprite_cfg = home / "Library/Application Support/Aseprite"
    elif system == "Windows":
        local_app_data = Path(env.get("LOCALAPPDATA") or (home / "AppData/Local"))
        roaming_app_data = Path(env.get("APPDATA") or (home / "AppData/Roaming"))
        support = local_app_data / "Aseprite AI MCP"
        aseprite_cfg = roaming_app_data / "Aseprite"
    else:
        xdg_data = Path(env.get("XDG_DATA_HOME") or (home / ".local/share"))
        xdg_config = Path(env.get("XDG_CONFIG_HOME") or (home / ".config"))
        support = xdg_data / "aseprite-ai-mcp"
        aseprite_cfg = xdg_config / "aseprite"

    return {
        "support": support,
        "settings": support / "connection.json",
        "log": support / "server.log",
        "aseprite_cfg": aseprite_cfg,
        "extension": aseprite_cfg / "extensions" / "aseprite-ai-mcp",
        "scripts": aseprite_cfg / "scripts",
        "codex": home / ".codex/config.toml",
        "claude": home / ".claude.json",
        "agy": home / ".gemini/config/mcp_config.json",
    }


def ensure_venv(root: Path = ROOT) -> Path:
    vpy = venv_python(root)
    if not vpy.exists():
        print("Creating Python virtual environment in .venv...")
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
    print("Installing Python requirements...")
    subprocess.run(
        [str(vpy), "-m", "pip", "install", "--quiet", "-r", str(root / "requirements.txt")],
        check=True,
    )
    return vpy


def build_extension_bundle(root: Path = ROOT) -> Path:
    output = root / "ai-mcp.aseprite-extension"
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name in ("package.json", "bridge.lua"):
            archive.write(root / "extension" / name, name)
    return output


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def write_private(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == contents:
                return
        except OSError:
            pass
        backup = path.with_name(f"{path.name}.before-aseprite-{STAMP}")
        shutil.copy2(path, backup)
        try:
            backup.chmod(0o600)
        except OSError:
            pass
    temporary = path.with_name(f"{path.name}.aseprite-tmp-{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(contents)
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def update_codex_toml(existing_text: str, url: str, auth_header: str) -> str:
    block = (
        "[mcp_servers.aseprite]\n"
        f"url = {json.dumps(url)}\n"
        "tool_timeout_sec = 30\n"
        f"http_headers = {{ Authorization = {json.dumps(auth_header)} }}\n"
    )
    pattern = re.compile(r"(?ms)^\[mcp_servers\.aseprite\]\n(?:^(?!\[).*\n?)*")
    if pattern.search(existing_text):
        updated = pattern.sub(block, existing_text)
    else:
        sep = "" if not existing_text or existing_text.endswith("\n") else "\n"
        updated = f"{existing_text}{sep}\n{block}".lstrip("\n")
    tomllib.loads(updated)
    return updated


def configure_clients(paths: dict[str, Path], settings: dict) -> str:
    url = f"http://127.0.0.1:{settings['http_port']}/mcp"
    headers = {"Authorization": "Bearer " + settings["token"]}

    codex_text = paths["codex"].read_text(encoding="utf-8") if paths["codex"].exists() else ""
    codex_updated = update_codex_toml(codex_text, url, headers["Authorization"])
    write_private(paths["codex"], codex_updated)

    claude = read_json(paths["claude"])
    claude.setdefault("mcpServers", {})["aseprite"] = {"type": "http", "url": url, "headers": headers}
    write_private(paths["claude"], json.dumps(claude, indent=2) + "\n")

    agy = read_json(paths["agy"])
    agy.setdefault("mcpServers", {})["aseprite"] = {"serverUrl": url, "headers": headers}
    write_private(paths["agy"], json.dumps(agy, indent=2) + "\n")
    return url


def install_aseprite_files(paths: dict[str, Path], settings: dict, root: Path = ROOT) -> None:
    paths["support"].mkdir(parents=True, exist_ok=True)
    try:
        paths["support"].chmod(0o700)
    except OSError:
        pass
    paths["extension"].mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "bridge.lua"):
        write_private(paths["extension"] / name, (root / "extension" / name).read_text(encoding="utf-8"))

    data = json.dumps(settings, indent=2) + "\n"
    write_private(paths["settings"], data)
    write_private(paths["extension"] / "connection.json", data)

    paths["scripts"].mkdir(parents=True, exist_ok=True)
    connect_script = (
        'local folder = app.fs.joinPath(app.fs.userConfigPath, "extensions", "aseprite-ai-mcp")\n'
        'local file = assert(io.open(app.fs.joinPath(folder, "connection.json"), "r"))\n'
        'local settings = json.decode(file:read("*a"))\n'
        "file:close()\n"
        "if _ai_mcp_live then _ai_mcp_live.disconnect() end\n"
        '_ai_mcp_live = dofile(app.fs.joinPath(folder, "bridge.lua"))\n'
        "_ai_mcp_live.connect(settings.port, settings.token)\n"
    )
    write_private(paths["scripts"] / "Connect AI MCP.lua", connect_script)


def start_service_macos(vpy: Path, paths: dict[str, Path], root: Path = ROOT) -> None:
    plist_path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist = dict(
        Label=LABEL,
        ProgramArguments=[str(vpy), str(root / "run_local.py"), str(paths["settings"])],
        WorkingDirectory=str(root),
        RunAtLoad=True,
        KeepAlive=True,
        ThrottleInterval=10,
        StandardOutPath=str(paths["log"]),
        StandardErrorPath=str(paths["log"]),
    )
    write_private(plist_path, plistlib.dumps(plist).decode("utf-8"))
    uid = os.getuid()
    res = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"],
        check=False,
        capture_output=True,
    )
    if res.returncode != 0:
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], check=True)


def start_service_linux(vpy: Path, paths: dict[str, Path], root: Path = ROOT) -> None:
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd/user"
    unit_path = unit_dir / "aseprite-ai-mcp.service"
    unit = (
        "[Unit]\n"
        "Description=Aseprite AI MCP Local Bridge\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"WorkingDirectory={root}\n"
        f'ExecStart="{vpy}" "{root / "run_local.py"}" "{paths["settings"]}"\n'
        "Restart=always\n"
        "RestartSec=5\n"
        f"StandardOutput=append:{paths['log']}\n"
        f"StandardError=append:{paths['log']}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    write_private(unit_path, unit)
    if shutil.which("systemctl"):
        res = subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
        if res.returncode == 0:
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", "aseprite-ai-mcp.service"],
                check=True,
            )
            subprocess.run(
                ["systemctl", "--user", "restart", "aseprite-ai-mcp.service"],
                check=True,
            )
            return

    # Fallback for non-systemd Linux environments (e.g., WSL / containers):
    with open(paths["log"], "ab") as log_file:
        subprocess.Popen(
            [str(vpy), str(root / "run_local.py"), str(paths["settings"])],
            cwd=str(root),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )


def start_service_windows(paths: dict[str, Path], root: Path = ROOT) -> None:
    pyw = venv_pythonw(root)
    cmd = f'"{pyw}" "{root / "run_local.py"}" "{paths["settings"]}"'
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "AsepriteAIMCP", 0, winreg.REG_SZ, cmd)
    except Exception:
        pass

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    with open(paths["log"], "ab") as log_file:
        subprocess.Popen(
            [str(pyw), str(root / "run_local.py"), str(paths["settings"])],
            cwd=str(root),
            stdout=log_file,
            stderr=log_file,
            creationflags=creationflags,
        )


def main() -> None:
    system = platform.system()
    vpy = ensure_venv(ROOT)
    build_extension_bundle(ROOT)
    paths = platform_paths(system=system)
    settings = read_json(paths["settings"]) or dict(
        token=secrets.token_hex(32),
        port=8765,
        http_port=8766,
        autoconnect=True,
    )
    install_aseprite_files(paths, settings, ROOT)
    url = configure_clients(paths, settings)

    if system == "Darwin":
        start_service_macos(vpy, paths, ROOT)
    elif system == "Windows":
        start_service_windows(paths, ROOT)
    else:
        start_service_linux(vpy, paths, ROOT)

    print(f"Installed Aseprite extension and local service for {system}.")
    print(f"Configured Codex/ChatGPT, Claude Code, and Antigravity at {url}")
    print(f"Extension path: {paths['extension']}")


if __name__ == "__main__":
    main()
