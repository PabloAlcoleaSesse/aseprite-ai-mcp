"""Install the local bridge and add it to Codex, Claude Code and Antigravity.

Run with the project's virtualenv Python. Existing configs are backed up and
unrelated entries are retained. Never prints the generated credentials.
"""
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import time
import tomllib

root = Path(__file__).resolve().parent
home = Path.home()
support = home / "Library/Application Support/Aseprite AI MCP"
extension = home / "Library/Application Support/Aseprite/extensions/aseprite-ai-mcp"
settings_path = support / "connection.json"
label = "local.aseprite.ai-mcp"
plist_path = home / "Library/LaunchAgents" / (label + ".plist")
codex_path = home / ".codex/config.toml"
claude_path = home / ".claude.json"
agy_path = home / ".gemini/config/mcp_config.json"
stamp = time.strftime("%Y%m%d-%H%M%S")


def read_json(path):
    return json.loads(path.read_text() or "{}") if path.exists() else {}


def write_private(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".before-aseprite-" + stamp)
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    temporary = path.with_name(path.name + ".aseprite-tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(contents)
    temporary.replace(path)


def main():
    assert (root / ".venv/bin/python").exists(), "Install requirements first"
    codex_text = codex_path.read_text() if codex_path.exists() else ""
    codex = tomllib.loads(codex_text)
    claude, agy = read_json(claude_path), read_json(agy_path)
    # Avoid overwriting any existing integration with the same name.
    assert "aseprite" not in codex.get("mcp_servers", {}), "Codex aseprite entry already exists"
    assert "aseprite" not in claude.get("mcpServers", {}), "Claude aseprite entry already exists"
    assert "aseprite" not in agy.get("mcpServers", {}), "Antigravity aseprite entry already exists"
    settings = read_json(settings_path) or dict(token=secrets.token_hex(32), port=8765,
                                               http_port=8766, autoconnect=True)
    url = f"http://127.0.0.1:{settings['http_port']}/mcp"
    headers = {"Authorization": "Bearer " + settings["token"]}
    codex_text += ("\n[mcp_servers.aseprite]\nurl = " + json.dumps(url)
        + '\ntool_timeout_sec = 30\nhttp_headers = { Authorization = '
        + json.dumps(headers["Authorization"]) + " }\n")
    tomllib.loads(codex_text)
    claude.setdefault("mcpServers", {})["aseprite"] = dict(type="http", url=url, headers=headers)
    agy.setdefault("mcpServers", {})["aseprite"] = dict(serverUrl=url, headers=headers)
    support.mkdir(parents=True, exist_ok=True)
    support.chmod(0o700)
    extension.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "bridge.lua"):
        write_private(extension / name, (root / "extension" / name).read_text())
    data = json.dumps(settings, indent=2) + "\n"
    write_private(settings_path, data)
    write_private(extension / "connection.json", data)
    write_private(codex_path, codex_text)
    write_private(claude_path, json.dumps(claude, indent=2) + "\n")
    write_private(agy_path, json.dumps(agy, indent=2) + "\n")
    plist = dict(Label=label, ProgramArguments=[str(root / ".venv/bin/python"),
        str(root / "run_local.py"), str(settings_path)], WorkingDirectory=str(root),
        RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
        StandardOutPath=str(support / "server.log"), StandardErrorPath=str(support / "server.log"))
    write_private(plist_path, plistlib.dumps(plist).decode())
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=True)
    print("Installed Aseprite extension and local launch agent.")
    print("Configured Codex, Claude Code and Antigravity at " + url)
    print("Existing configurations backed up with suffix .before-aseprite-" + stamp)


if __name__ == "__main__":
    main()
