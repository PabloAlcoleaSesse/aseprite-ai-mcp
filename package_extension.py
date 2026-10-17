"""Create the installable extension without bundling Python or credentials."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parent
output = root / "ai-mcp.aseprite-extension"
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for name in ("package.json", "bridge.lua"):
        archive.write(root / "extension" / name, name)
print(output)
