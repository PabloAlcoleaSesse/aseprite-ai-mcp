"""Launch the shared HTTP service with credentials from a private local file."""
import json
import os
from pathlib import Path
import runpy
import sys

settings = json.loads(Path(sys.argv[1]).read_text())
os.environ.update(ASEPRITE_MCP_TOKEN=settings["token"],
                  ASEPRITE_MCP_PORT=str(settings["port"]),
                  ASEPRITE_MCP_HTTP_PORT=str(settings["http_port"]),
                  ASEPRITE_MCP_TRANSPORT="http")
runpy.run_path(str(Path(__file__).with_name("server.py")), run_name="__main__")
