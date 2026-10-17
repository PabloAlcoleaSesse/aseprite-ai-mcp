local socket, dialog

local function integer(value, low, high, label)
  assert(type(value) == "number" and value % 1 == 0 and value >= low and value <= high,
    "Invalid " .. label)
  return value
end

local function active(params)
  local sprite = app.sprite
  assert(sprite and sprite.id == params.sprite_id, "Active sprite changed; inspect again")
  assert(sprite.colorMode == ColorMode.RGB, "This prototype supports RGB sprites only")
  assert(sprite.width <= 512 and sprite.height <= 512, "Maximum sprite size is 512 x 512")
  return sprite
end

local function layerList(layers, result)
  for _, layer in ipairs(layers) do
    result[#result+1] = {id=layer.id, name=layer.name, group=layer.isGroup,
      editable=layer.isEditable, visible=layer.isVisible}
    if layer.isGroup then layerList(layer.layers, result) end
  end
end

local function inspect()
  local sprite = app.sprite
  if not sprite then return {open=false} end
  local layers = {}
  layerList(sprite.layers, layers)
  return {open=true, sprite_id=sprite.id, width=sprite.width, height=sprite.height,
    rgb=sprite.colorMode == ColorMode.RGB, filename=sprite.filename,
    layers=layers, frames=#sprite.frames,
    active_layer_id=app.layer and app.layer.id or 0,
    active_frame=app.frame and app.frame.frameNumber or 1}
end

local function findLayer(layers, id, ancestorsEditable)
  for _, layer in ipairs(layers) do
    local editable = ancestorsEditable and layer.isEditable
    if layer.id == id then
      assert(editable, "Layer or parent group is locked")
      return layer
    end
    if layer.isGroup then
      local found = findLayer(layer.layers, id, editable)
      if found then return found end
    end
  end
end

local handlers = {}
handlers.inspect = inspect

handlers.create = function(p)
  integer(p.width, 1, 512, "width")
  integer(p.height, 1, 512, "height")
  app.sprite = Sprite(p.width, p.height, ColorMode.RGB)
  return inspect()
end

handlers.add_layer = function(p)
  local sprite = active(p)
  assert(type(p.name) == "string" and #p.name > 0 and #p.name <= 400, "Invalid layer name")
  app.transaction("AI: add layer", function()
    local layer = sprite:newLayer()
    layer.name = p.name
    app.layer = layer
  end)
  return inspect()
end

handlers.add_frame = function(p)
  local sprite = active(p)
  assert(#sprite.frames < 256, "Maximum 256 frames")
  app.transaction("AI: add frame", function() app.frame = sprite:newFrame() end)
  return inspect()
end

handlers.draw_pixels = function(p)
  local sprite = active(p)
  integer(p.frame, 1, #sprite.frames, "frame")
  local layer = findLayer(sprite.layers, p.layer_id, true)
  assert(layer and not layer.isGroup and not layer.isTilemap and not layer.isReference
    and not layer.isBackground, "Choose a normal transparent image layer")
  assert(p.pixels and #p.pixels >= 1 and #p.pixels <= 16384, "Invalid pixel batch size")
  -- Validate everything before mutating the document.
  local pixels = {}
  for _, pixel in ipairs(p.pixels) do
    integer(pixel.x, 0, sprite.width-1, "pixel x")
    integer(pixel.y, 0, sprite.height-1, "pixel y")
    assert(type(pixel.color) == "string" and #pixel.color == 9
      and pixel.color:match("^#%x%x%x%x%x%x%x%x$"), "Colors must be #RRGGBBAA")
    local c = pixel.color
    pixels[#pixels+1] = {x=pixel.x, y=pixel.y, color=app.pixelColor.rgba(
      tonumber(c:sub(2,3),16), tonumber(c:sub(4,5),16),
      tonumber(c:sub(6,7),16), tonumber(c:sub(8,9),16))}
  end
  local cel = layer:cel(p.frame)
  if cel then
    for _, other in ipairs(sprite.cels) do
      assert(other == cel or other.image.id ~= cel.image.id,
        "Unlink this cel in Aseprite before editing it")
    end
  end
  -- Keep existing off-canvas image data and all cel metadata.
  local left, top, right, bottom = 0, 0, sprite.width, sprite.height
  if cel then
    left, top = math.min(left, cel.position.x), math.min(top, cel.position.y)
    right = math.max(right, cel.position.x + cel.image.width)
    bottom = math.max(bottom, cel.position.y + cel.image.height)
  end
  assert(right-left <= 2048 and bottom-top <= 2048, "Cel bounds are too large")
  local image = Image(right-left, bottom-top, ColorMode.RGB)
  if cel then image:drawImage(cel.image, Point(cel.position.x-left, cel.position.y-top)) end
  for _, pixel in ipairs(pixels) do image:drawPixel(pixel.x-left, pixel.y-top, pixel.color) end
  app.transaction("AI: draw pixels", function()
    if cel then
      cel.image = image
      cel.position = Point(left, top)
    else
      sprite:newCel(layer, p.frame, image, Point(left, top))
    end
  end)
  return {changed=#pixels, sprite_id=sprite.id, layer_id=layer.id, frame=p.frame}
end

handlers.preview = function(p)
  local sprite = active(p)
  integer(p.frame, 1, #sprite.frames, "frame")
  local image = Image(sprite.spec)
  image:drawSprite(sprite, p.frame)
  local pixels, pc = {}, app.pixelColor
  for y=0,sprite.height-1 do
    for x=0,sprite.width-1 do
      local value = image:getPixel(x,y)
      pixels[#pixels+1] = pc.rgbaR(value)
      pixels[#pixels+1] = pc.rgbaG(value)
      pixels[#pixels+1] = pc.rgbaB(value)
      pixels[#pixels+1] = pc.rgbaA(value)
    end
  end
  return {width=sprite.width, height=sprite.height, pixels=pixels}
end

local function status(text)
  if dialog then dialog:modify{id="status", text=text} end
end

local function disconnect()
  local old = socket
  socket = nil
  if old then old:close() end
  status("Disconnected")
end

local function connect(port, token)
  integer(port, 1024, 65535, "port")
  assert(#token >= 32, "Paste the server token (at least 32 characters)")
  disconnect()
  local ws
  local function send(payload)
    -- The network can close before the queued UI callback is delivered.
    -- Don't let that race stop Aseprite's WebSocket reconnect loop.
    local ok = pcall(function() ws:sendText(json.encode(payload)) end)
    if not ok then status("Connection interrupted; reconnecting...") end
  end
  ws = WebSocket{url=string.format("ws://127.0.0.1:%d", port), deflate=false,
    onreceive=function(kind, data)
      if socket ~= ws then return end
      if kind == WebSocketMessageType.OPEN then
        send{token=token}
      elseif kind == WebSocketMessageType.TEXT then
        local decoded, request = pcall(json.decode, data)
        if not decoded then status("Invalid message from server"); return end
        if request.type == "ready" then status("Connected"); return end
        local ok, result = pcall(function()
          assert(handlers[request.op], "Unknown operation")
          return handlers[request.op](request.params)
        end)
        app.refresh()
        if ok then send{id=request.id, ok=true, result=result}
        else send{id=request.id, ok=false, error=tostring(result)} end
      elseif kind == WebSocketMessageType.CLOSE or kind == WebSocketMessageType.ERROR then
        status("Disconnected; check server and token")
      end
    end}
  socket = ws
  ws:connect()
  status("Connecting...")
end

function init(plugin)
  local defaults = {port=8765, token=""}
  local configPath = app.fs.joinPath(plugin.path, "connection.json")
  if app.fs.isFile(configPath) then
    local ok, config = pcall(function()
      local file = assert(io.open(configPath, "r"))
      local content = file:read("*a")
      file:close()
      return json.decode(content)
    end)
    if ok then defaults = config end
  end
  plugin:newCommand{id="AIMCPBridge", title="AI MCP Bridge", group="file_scripts",
    onclick=function()
      if dialog then dialog:close() end
      dialog = Dialog{title="AI MCP Bridge", onclose=function() dialog=nil end}
      dialog:number{id="port", label="Port", text=tostring(plugin.preferences.port or defaults.port), decimals=0}
      dialog:entry{id="token", label="Token", text=defaults.token}
      dialog:label{id="status", label="Status", text=socket and "Connection enabled" or "Disconnected"}
      dialog:button{text="Connect", onclick=function()
        local data = dialog.data
        local ok, err = pcall(connect, data.port, data.token)
        if ok then plugin.preferences.port = data.port else status(tostring(err)) end
      end}
      dialog:button{text="Disconnect", onclick=disconnect}
      dialog:show{wait=false}
    end}
  if app.isUIAvailable and defaults.autoconnect and #defaults.token >= 32 then
    local ok, err = pcall(connect, defaults.port, defaults.token)
    if not ok then print("AI MCP Bridge: " .. tostring(err)) end
  end
end

function exit(plugin)
  disconnect()
  if dialog then dialog:close() end
end

-- Also allow the integration tests to exercise the exact extension implementation.
return {handlers=handlers, connect=connect, disconnect=disconnect}
