# AI editing in Aseprite through MCP

A small local bridge for an AI assistant to draw directly in your open Aseprite
document, inspect its layers and frames, and see PNG previews of its work.
The assistant supplies the intelligence; this extension supplies drawing tools.
It does not require an image-generation service or an API key of its own.

```text
AI assistant(s) → MCP (stdio or shared local HTTP) → Python server
                                                       ↕ authenticated local WebSocket
                                                  Aseprite Lua extension → live sprite
```

## Quick setup on macOS (Codex, Claude Code, Antigravity)

Requires Python 3.10+ and an Aseprite build with Lua scripting and the
[WebSocket API](https://www.aseprite.org/api/websocket).
The source checkout here has these APIs (`./build/bin/aseprite`). Aseprite must be
running in GUI mode for live editing.

From this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
python3 package_extension.py
.venv/bin/python install_macos.py
```

`install_macos.py` performs a complete local installation:

1. Generates a random 32-byte token in `~/Library/Application Support/Aseprite AI MCP/connection.json` (`0600` permissions).
2. Installs the extension into `~/Library/Application Support/Aseprite/extensions/aseprite-ai-mcp` with `autoconnect` enabled.
3. Registers and starts a `launchd` agent (`local.aseprite.ai-mcp`) running `run_local.py`, which hosts the authenticated streamable HTTP MCP endpoint on `http://127.0.0.1:8766/mcp` and the Aseprite WebSocket listener on `ws://127.0.0.1:8765`.
4. Configures **Codex** (`~/.codex/config.toml`), **Claude Code** (`~/.claude.json`), and **Antigravity** (`~/.gemini/config/mcp_config.json`) to connect to the shared local HTTP service (backing up existing config files before modifying them).

When Aseprite starts in GUI mode, the extension automatically connects to the local bridge. If Aseprite was already open before installing, run **File → Scripts → Connect AI MCP** (or **File → Scripts → AI MCP Bridge** and click **Connect**).

## Manual stdio setup

If you prefer a single-client `stdio` server instead of the shared background HTTP service:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
python3 package_extension.py
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Keep the generated token for both ends of the connection.

1. In Aseprite, open Preferences → Extensions → Add Extension and select
   `ai-mcp.aseprite-extension`. Restart Aseprite if the menu does not appear.
2. Configure your assistant to launch the stdio server using the Python executable
   in `.venv`, the absolute path to `server.py`, and `ASEPRITE_MCP_TOKEN` as an
   environment variable. A generic MCP configuration is shown below; adapt the
   enclosing configuration format to your client.
3. Start or enable that MCP server in your assistant.
4. In Aseprite, choose File → Scripts → AI MCP Bridge. Paste the same token,
   leave the port at `8765`, and click Connect. Allow Aseprite's socket permission
   prompt if it appears. The status should become Connected.
5. Ask the assistant to call `inspect_sprite`, then create or edit a sprite.

```json
{
  "mcpServers": {
    "aseprite": {
      "command": "/absolute/path/to/integrations/ai-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/integrations/ai-mcp/server.py"],
      "env": {
        "ASEPRITE_MCP_TOKEN": "PASTE_YOUR_GENERATED_TOKEN_HERE",
        "ASEPRITE_MCP_PORT": "8765"
      }
    }
  }
}
```

Only one server may use a given WebSocket port, and only one editor connects to that
server. In HTTP mode (`run_local.py`), multiple MCP clients can connect concurrently
to the same server so their processes do not compete for the WebSocket port. Closing
the bridge dialog leaves the connection running; reopen it and choose Disconnect to stop.

## Try it

> Create a 32×32 pixel-art slime with a transparent background and five colors.
> Put the outline and shading on separate layers. Use batches of pixels, then
> preview the sprite and refine it. Leave the document open for me to save.

Available tools:

| Tool | Purpose |
| --- | --- |
| `inspect_sprite` | Active sprite, dimensions, layer IDs and frame count |
| `create_sprite` | New transparent RGB sprite |
| `add_layer` | Named layer, with undo |
| `draw_pixels` | Batch of exact RGBA pixels, with undo |
| `fill_rect` | Fill a rectangular region with `#RRGGBBAA`, with undo |
| `draw_line` | Draw a 1px Bresenham line between two points, with undo |
| `flood_fill` | 4-connected bucket fill from `(x, y)`, with undo |
| `replace_color` | Swap all occurrences of a `#RRGGBBAA` color on a layer/frame, with undo |
| `sample_pixels` | Read exact `#RRGGBBAA` pixel colors and palette counts from a region/layer |
| `add_frame` | Append a copy of the last frame, with undo |
| `preview_sprite` | Composited PNG returned as an MCP image |

Inspect before editing: sprite and layer IDs are explicit. Coordinates start at
zero; frame numbers start at one. Colors are `#RRGGBBAA`; `#00000000` erases a pixel.
Use Aseprite's normal Undo and Save commands. The extension does not save files.
Each drawing batch, layer addition or frame addition is one transaction.

## Scope and limitations

- Verified end-to-end against Aseprite in both batch mode (`tests/aseprite_smoke.lua`)
  and live GUI mode over MCP.
- RGB sprites up to 512×512; at most 16,384 pixels per drawing call.
- Frame creation stops at 256 frames. Layers must be ordinary, unlocked,
  transparent image layers; locked parent groups also prevent edits.
- Unlink linked cels in Aseprite before drawing to avoid changing other frames.
- Pixel edits retain off-canvas image data (cel bounds up to 2048×2048), position
  relative to the canvas, and cel metadata. Selection masks are not applied.
- Previews flatten the visible layers for the requested frame. Hidden layers can
  still be edited. No arbitrary Lua execution, file access tools or remote listener.
- Listeners bind only to `127.0.0.1`, require a shared token and reject
  browser-origin (`http`/`https`) WebSocket connections.
- After a timeout or disconnect, inspect before retrying: an edit may have run
  even if its response was lost. Changes to pixels made manually between tool calls
  are not detected as conflicts; avoid simultaneous manual and AI edits.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
aseprite --batch --script tests/aseprite_smoke.lua
```

Tests cover real localhost WebSocket authentication, browser-origin rejection,
disconnection, timeouts, request routing, single-editor enforcement, PNG contents,
concurrent authenticated HTTP MCP clients, stdio MCP tool discovery/drawing/image
output, and Aseprite Lua handlers (layer creation, RGBA drawing, transparency,
cel metadata preservation, atomic undo/redo, and error rejection).
