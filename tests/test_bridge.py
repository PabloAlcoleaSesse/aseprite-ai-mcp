import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import struct
import sys
import unittest
import zlib
import httpx

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Bridge, png_from_rgba

TOKEN = "test-token-" + "a" * 40


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = Bridge(TOKEN, timeout=0.2)
        self.listener = await serve(self.bridge.connect, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.listener.close()
        await self.listener.wait_closed()

    async def editor(self):
        peer = await connect(self.url, origin=self.url)
        await peer.send(json.dumps({"token": TOKEN}))
        self.assertEqual(json.loads(await peer.recv()), {"type": "ready"})
        return peer

    async def test_wrong_token_and_browser_origin_rejected(self):
        async with connect(self.url) as peer:
            await peer.send(json.dumps({"token": "wrong"}))
            with self.assertRaises(ConnectionClosed):
                await peer.recv()
        async with connect(self.url, origin="http://example.com") as peer:
            with self.assertRaises(ConnectionClosed):
                await peer.recv()
        self.assertIsNone(self.bridge.peer)

    async def test_disconnected_error(self):
        with self.assertRaisesRegex(RuntimeError, "Open Aseprite"):
            await self.bridge.call("inspect")

    async def test_round_trip_and_editor_error(self):
        peer = await self.editor()
        async with peer:
            for response in ({"ok": True, "result": {"open": True}},
                             {"ok": False, "error": "Layer is locked"}):
                task = asyncio.create_task(self.bridge.call("inspect"))
                request = json.loads(await peer.recv())
                self.assertEqual(request["op"], "inspect")
                await peer.send(json.dumps({"id": request["id"], **response}))
                if response["ok"]:
                    self.assertEqual(await task, {"open": True})
                else:
                    with self.assertRaisesRegex(RuntimeError, "Layer is locked"):
                        await task
        self.assertEqual(self.bridge.pending, {})

    async def test_timeout_cleanup(self):
        peer = await self.editor()
        async with peer:
            with self.assertRaisesRegex(RuntimeError, "may have run"):
                await self.bridge.call("draw_pixels")
            self.assertEqual(self.bridge.pending, {})

    async def test_disconnect_fails_pending_request(self):
        peer = await self.editor()
        task = asyncio.create_task(self.bridge.call("inspect"))
        await peer.recv()
        await peer.close()
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            await task

    async def test_second_editor_cannot_replace_first(self):
        first = await self.editor()
        async with first:
            async with connect(self.url) as second:
                await second.send(json.dumps({"token": TOKEN}))
                with self.assertRaises(ConnectionClosed):
                    await second.recv()
            self.assertIsNotNone(self.bridge.peer)


class PNGTests(unittest.TestCase):
    def test_png_pixels_and_checksums(self):
        png = png_from_rgba(2, 1, [255, 0, 0, 255, 0, 0, 0, 0])
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        while offset < len(png):
            size = struct.unpack(">I", png[offset:offset+4])[0]
            kind = png[offset+4:offset+8]
            data = png[offset+8:offset+8+size]
            crc = struct.unpack(">I", png[offset+8+size:offset+12+size])[0]
            self.assertEqual(crc, zlib.crc32(kind + data))
            if kind == b"IDAT":
                self.assertEqual(zlib.decompress(data), b"\0\xff\0\0\xff\0\0\0\0")
            offset += size + 12


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_http_auth_and_multiple_clients(self):
        with socket.socket() as first, socket.socket() as second:
            first.bind(("127.0.0.1", 0))
            second.bind(("127.0.0.1", 0))
            ws_port, http_port = first.getsockname()[1], second.getsockname()[1]
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).resolve().parents[1] / "server.py"),
            env={**os.environ, "ASEPRITE_MCP_TOKEN": TOKEN,
                 "ASEPRITE_MCP_PORT": str(ws_port), "ASEPRITE_MCP_HTTP_PORT": str(http_port),
                 "ASEPRITE_MCP_TRANSPORT": "http"},
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        url = f"http://127.0.0.1:{http_port}/mcp"
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        response = await client.post(url, json={})
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
                else:
                    self.fail("HTTP server did not start")
                self.assertEqual(response.status_code, 401)
                response = await client.post(url, headers={"Authorization": "Bearer wrong"}, json={})
                self.assertEqual(response.status_code, 401)

            async def discover():
                async with httpx.AsyncClient(headers={"Authorization": "Bearer " + TOKEN}) as client:
                    async with streamable_http_client(url, http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            self.assertEqual(len((await session.list_tools()).tools), 11)
                            result = await session.call_tool("inspect_sprite", {})
                            self.assertTrue(result.isError)  # No editor connected yet.
            await asyncio.gather(discover(), discover(), discover())
        finally:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)

    async def test_stdio_discovery_drawing_and_image_response(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "server.py")],
            env={**os.environ, "ASEPRITE_MCP_TOKEN": TOKEN, "ASEPRITE_MCP_PORT": str(port)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {
                    "inspect_sprite", "create_sprite", "add_layer", "draw_pixels",
                    "fill_rect", "draw_line", "flood_fill", "replace_color", "sample_pixels",
                    "add_frame", "preview_sprite"})
                async with connect(f"ws://127.0.0.1:{port}") as peer:
                    await peer.send(json.dumps({"token": TOKEN}))
                    await peer.recv()

                    async def respond(operation, result):
                        request = json.loads(await peer.recv())
                        self.assertEqual(request["op"], operation)
                        await peer.send(json.dumps({"id": request["id"], "ok": True, "result": result}))
                        return request

                    responder = asyncio.create_task(respond("draw_pixels", {"changed": 1}))
                    result = await session.call_tool("draw_pixels", {
                        "sprite_id": 1, "layer_id": 2, "frame": 1,
                        "pixels": [{"x": 0, "y": 0, "color": "#ff0000ff"}]})
                    request = await responder
                    self.assertFalse(result.isError)
                    self.assertEqual(request["params"]["pixels"][0]["color"], "#ff0000ff")
                    responder = asyncio.create_task(respond("preview", {
                        "width": 1, "height": 1, "pixels": [255, 0, 0, 255]}))
                    result = await session.call_tool("preview_sprite", {"sprite_id": 1})
                    await responder
                    self.assertFalse(result.isError)
                    self.assertEqual(result.content[0].type, "image")
                    self.assertTrue(base64.b64decode(result.content[0].data).startswith(b"\x89PNG"))
                    result = await session.call_tool("draw_pixels", {
                        "sprite_id": 1, "layer_id": 2, "frame": 1,
                        "pixels": [{"x": -1, "y": 0, "color": "red"}]})
                    self.assertTrue(result.isError)


if __name__ == "__main__":
    unittest.main()
