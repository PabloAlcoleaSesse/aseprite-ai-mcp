"""MCP stdio server with an authenticated, loopback-only Aseprite bridge."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import secrets
import struct
import zlib

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import BaseModel, Field
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


class Bridge:
    def __init__(self, token, timeout=20):
        self.token = token
        self.timeout = timeout
        self.peer = None
        self.pending = {}
        self.lock = asyncio.Lock()

    async def connect(self, peer):
        # Aseprite's IXWebSocket client sends a ws:// origin automatically.
        # Browsers send http(s):// origins, which aren't accepted here.
        origin = peer.request.headers.get("Origin")
        native_origin = f"ws://127.0.0.1:{peer.local_address[1]}"
        if origin is not None and origin != native_origin:
            await peer.close(1008, "Browser connections are not supported")
            return
        try:
            hello = json.loads(await asyncio.wait_for(peer.recv(), 5))
            if not isinstance(hello, dict) or not secrets.compare_digest(
                str(hello.get("token", "")), self.token
            ):
                await peer.close(1008, "Invalid token")
                return
            if self.peer is not None:
                await peer.close(1008, "Another editor is connected")
                return
            self.peer = peer
            await peer.send(json.dumps({"type": "ready"}))
            async for message in peer:
                response = json.loads(message)
                if not isinstance(response, dict):
                    raise ValueError("Expected a response object")
                future = self.pending.get(response.get("id"))
                if future is not None and not future.done():
                    future.set_result(response)
        except (ConnectionClosed, TimeoutError, ValueError, TypeError):
            await peer.close()
        finally:
            if self.peer is peer:
                self.peer = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError("Aseprite disconnected; inspect before retrying edits"))

    async def call(self, operation, **params):
        async with self.lock:
            if self.peer is None:
                raise RuntimeError("Open Aseprite and use File > Scripts > AI MCP Bridge to connect")
            request_id = secrets.token_hex(12)
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            try:
                await self.peer.send(json.dumps({"id": request_id, "op": operation, "params": params}))
                response = await asyncio.wait_for(future, self.timeout)
                if not response.get("ok"):
                    raise RuntimeError(response.get("error", "Aseprite operation failed"))
                return response["result"]
            except TimeoutError as exc:
                raise RuntimeError("Aseprite timed out. The edit may have run; inspect before retrying") from exc
            finally:
                self.pending.pop(request_id, None)


bridge = Bridge(os.environ.get("ASEPRITE_MCP_TOKEN", ""))


@asynccontextmanager
async def lifespan(_server):
    if len(bridge.token) < 32:
        raise ValueError("Set ASEPRITE_MCP_TOKEN to a random token of at least 32 characters")
    port = int(os.environ.get("ASEPRITE_MCP_PORT", "8765"))
    async with serve(bridge.connect, "127.0.0.1", port, max_size=8 * 1024 * 1024):
        yield {}


class LocalTokenVerifier(TokenVerifier):
    async def verify_token(self, token):
        if secrets.compare_digest(token.encode(), bridge.token.encode()):
            return AccessToken(token=token, client_id="local-assistant", scopes=["edit"],
                               resource=http_url)
        return None


http_mode = os.environ.get("ASEPRITE_MCP_TRANSPORT") == "http"
http_port = int(os.environ.get("ASEPRITE_MCP_HTTP_PORT", "8766"))
http_url = f"http://127.0.0.1:{http_port}/mcp"
http_options = {}
if http_mode:
    http_options = dict(host="127.0.0.1", port=http_port, stateless_http=True,
        json_response=True, token_verifier=LocalTokenVerifier(),
        auth=AuthSettings(issuer_url=http_url, resource_server_url=http_url,
                          required_scopes=["edit"], validate_token_resource=True))
mcp = FastMCP("Aseprite Pixel Art", lifespan=None if http_mode else lifespan, **http_options)


class Pixel(BaseModel):
    x: int = Field(ge=0, le=511)
    y: int = Field(ge=0, le=511)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{8}$", description="#RRGGBBAA, including alpha")


@mcp.tool()
async def inspect_sprite() -> dict:
    """Inspect the active sprite, IDs, layers and frames. Call before editing.

    Coordinates are zero-based. Frames are one-based. Edits require explicit
    sprite and layer IDs to prevent editing the wrong document after a UI switch.
    """
    return await bridge.call("inspect")


@mcp.tool()
async def create_sprite(width: int, height: int) -> dict:
    """Create a transparent RGB sprite, from 1 to 512 pixels per dimension."""
    if not 1 <= width <= 512 or not 1 <= height <= 512:
        raise ValueError("Dimensions must be between 1 and 512")
    return await bridge.call("create", width=width, height=height)


@mcp.tool()
async def add_layer(sprite_id: int, name: str) -> dict:
    """Add a named layer to the active sprite as one undoable edit."""
    if not name.strip() or len(name) > 100:
        raise ValueError("Layer name must contain 1 to 100 characters")
    return await bridge.call("add_layer", sprite_id=sprite_id, name=name)


@mcp.tool()
async def draw_pixels(sprite_id: int, layer_id: int, frame: int, pixels: list[Pixel]) -> dict:
    """Set up to 16384 pixels in one undoable edit. Colors use #RRGGBBAA.

    RGB sprites only. Pixels outside the canvas reject the entire edit.
    Transparent pixels erase. Use batches for shapes, outlines and shading.
    """
    if not 1 <= len(pixels) <= 16384:
        raise ValueError("Provide between 1 and 16384 pixels")
    return await bridge.call("draw_pixels", sprite_id=sprite_id, layer_id=layer_id,
                             frame=frame, pixels=[p.model_dump() for p in pixels])


@mcp.tool()
async def add_frame(sprite_id: int) -> dict:
    """Append a frame copied from the last frame, as one undoable edit."""
    return await bridge.call("add_frame", sprite_id=sprite_id)


def png_from_rgba(width, height, pixels):
    if not 1 <= width <= 512 or not 1 <= height <= 512 or len(pixels) != width * height * 4:
        raise ValueError("Invalid preview dimensions or pixel data")
    raw = bytes(pixels)
    rows = b"".join(b"\x00" + raw[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@mcp.tool()
async def preview_sprite(sprite_id: int, frame: int = 1) -> Image:
    """Return a composited PNG for visual feedback. Use after drawing to assess the art."""
    result = await bridge.call("preview", sprite_id=sprite_id, frame=frame)
    return Image(data=png_from_rgba(result["width"], result["height"], result["pixels"]), format="png")


if __name__ == "__main__":
    if http_mode:
        import uvicorn
        app = mcp.streamable_http_app()
        session_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def shared_lifespan(application):
            # HTTP has many MCP sessions; the editor listener belongs to the
            # process, not to a session or individual stateless request.
            async with lifespan(mcp), session_lifespan(application):
                yield

        app.router.lifespan_context = shared_lifespan
        uvicorn.run(app, host="127.0.0.1", port=http_port)
    else:
        mcp.run(transport="stdio")
