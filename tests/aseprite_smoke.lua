-- Run with: aseprite --batch --script tests/aseprite_smoke.lua
local script_path = app.fs.isFile("extension/bridge.lua") and "extension/bridge.lua"
  or "integrations/ai-mcp/extension/bridge.lua"
local bridge = dofile(script_path)
local h = bridge.handlers
local function call(op, params)
  -- Exercise the actual JSON decoding used for WebSocket requests, too.
  return h[op](json.decode(json.encode(params or {})))
end
local function rejects(op, params, text)
  local ok, err = pcall(call, op, params)
  assert(not ok and tostring(err):find(text, 1, true), tostring(err))
end

local state = call("create", {width=16, height=16})
local sprite = app.sprite
assert(state.sprite_id == sprite.id and state.rgb)
state = call("add_layer", {sprite_id=sprite.id, name="AI smoke test"})
local layer = app.layer
assert(state.active_layer_id == layer.id and layer.name == "AI smoke test")
local params = {sprite_id=sprite.id, layer_id=layer.id, frame=1,
  pixels={{x=2,y=3,color="#ff0000ff"}, {x=3,y=3,color="#00ff0080"}}}
call("draw_pixels", params)
assert(layer:cel(1).image:getPixel(2,3) == app.pixelColor.rgba(255,0,0,255))
local preview = call("preview", {sprite_id=sprite.id, frame=1})
assert(#preview.pixels == 16*16*4)
local offset = (3*16+2)*4
assert(preview.pixels[offset+1] == 255 and preview.pixels[offset+4] == 255)
app.undo()
assert(layer:cel(1) == nil, "Drawing batch should undo atomically")
app.redo()
local cel = layer:cel(1)
cel.data = "preserve me"
cel.properties.test = "metadata"
params.pixels = {{x=2,y=3,color="#00000000"}}
call("draw_pixels", params)
assert(cel.image:getPixel(2,3) == 0)
assert(cel.image:getPixel(3,3) == app.pixelColor.rgba(0,255,0,128))
assert(cel.data == "preserve me" and cel.properties.test == "metadata")
app.undo()
assert(cel.image:getPixel(2,3) == app.pixelColor.rgba(255,0,0,255))
params.pixels = {{x=1,y=1,color="#ffffffff"}, {x=16,y=1,color="#ffffffff"}}
rejects("draw_pixels", params, "Invalid pixel x")
assert(cel.image:getPixel(1,1) == 0, "Rejected batch must not partially draw")
params.pixels = {{x=1,y=1,color="#ffffffff"}}
layer.isEditable = false
rejects("draw_pixels", params, "locked")
layer.isEditable = true
local frames = call("add_frame", {sprite_id=sprite.id})
assert(frames.frames == 2)
app.undo()
assert(#sprite.frames == 1)
local other = Sprite(2,2)
rejects("draw_pixels", params, "Active sprite changed")
other:close()
app.sprite = sprite
sprite:close()
print("ASEPRITE_MCP_SMOKE_OK")
