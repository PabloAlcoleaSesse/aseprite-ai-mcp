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

local function findLayerAny(layers, id)
  for _, layer in ipairs(layers) do
    if layer.id == id then return layer end
    if layer.isGroup then
      local found = findLayerAny(layer.layers, id)
      if found then return found end
    end
  end
end

local function parseColor(c)
  assert(type(c) == "string" and #c == 9 and c:match("^#%x%x%x%x%x%x%x%x$"),
    "Colors must be #RRGGBBAA")
  local a = tonumber(c:sub(8,9),16)
  if a == 0 then return 0 end
  return app.pixelColor.rgba(
    tonumber(c:sub(2,3),16), tonumber(c:sub(4,5),16),
    tonumber(c:sub(6,7),16), a)
end

local function normalizePixel(v)
  if app.pixelColor.rgbaA(v) == 0 then return 0 end
  return v
end

local function formatColor(v)
  local pc = app.pixelColor
  local a = pc.rgbaA(v)
  if a == 0 then return "#00000000" end
  return string.format("#%02x%02x%02x%02x", pc.rgbaR(v), pc.rgbaG(v), pc.rgbaB(v), a)
end

local function editCel(p, label, mutate)
  local sprite = active(p)
  integer(p.frame, 1, #sprite.frames, "frame")
  local layer = findLayer(sprite.layers, p.layer_id, true)
  assert(layer and not layer.isGroup and not layer.isTilemap and not layer.isReference
    and not layer.isBackground, "Choose a normal transparent image layer")
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
  local changed = mutate(image, left, top, sprite)
  app.transaction(label, function()
    if cel then
      cel.image = image
      cel.position = Point(left, top)
    else
      sprite:newCel(layer, p.frame, image, Point(left, top))
    end
  end)
  return {changed=changed, sprite_id=sprite.id, layer_id=layer.id, frame=p.frame}
end

handlers.draw_pixels = function(p)
  local sprite = active(p)
  assert(p.pixels and #p.pixels >= 1 and #p.pixels <= 16384, "Invalid pixel batch size")
  local pixels = {}
  for _, pixel in ipairs(p.pixels) do
    integer(pixel.x, 0, sprite.width-1, "pixel x")
    integer(pixel.y, 0, sprite.height-1, "pixel y")
    pixels[#pixels+1] = {x=pixel.x, y=pixel.y, color=parseColor(pixel.color)}
  end
  return editCel(p, "AI: draw pixels", function(image, left, top)
    for _, pixel in ipairs(pixels) do
      image:drawPixel(pixel.x-left, pixel.y-top, pixel.color)
    end
    return #pixels
  end)
end

handlers.fill_rect = function(p)
  local sprite = active(p)
  local rx = integer(p.x, 0, sprite.width-1, "x")
  local ry = integer(p.y, 0, sprite.height-1, "y")
  local rw = integer(p.width, 1, sprite.width - rx, "width")
  local rh = integer(p.height, 1, sprite.height - ry, "height")
  local color = parseColor(p.color)
  return editCel(p, "AI: fill rect", function(image, left, top)
    for y = ry, ry + rh - 1 do
      for x = rx, rx + rw - 1 do
        image:drawPixel(x - left, y - top, color)
      end
    end
    return rw * rh
  end)
end

handlers.draw_line = function(p)
  local sprite = active(p)
  local x0 = integer(p.x0, 0, sprite.width-1, "x0")
  local y0 = integer(p.y0, 0, sprite.height-1, "y0")
  local x1 = integer(p.x1, 0, sprite.width-1, "x1")
  local y1 = integer(p.y1, 0, sprite.height-1, "y1")
  local color = parseColor(p.color)
  return editCel(p, "AI: draw line", function(image, left, top)
    local dx, sx = math.abs(x1 - x0), x0 < x1 and 1 or -1
    local dy, sy = -math.abs(y1 - y0), y0 < y1 and 1 or -1
    local err, count = dx + dy, 0
    while true do
      image:drawPixel(x0 - left, y0 - top, color)
      count = count + 1
      if x0 == x1 and y0 == y1 then break end
      local e2 = 2 * err
      if e2 >= dy then err = err + dy; x0 = x0 + sx end
      if e2 <= dx then err = err + dx; y0 = y0 + sy end
    end
    return count
  end)
end

handlers.flood_fill = function(p)
  local sprite = active(p)
  local sx = integer(p.x, 0, sprite.width-1, "x")
  local sy = integer(p.y, 0, sprite.height-1, "y")
  local color = parseColor(p.color)
  return editCel(p, "AI: flood fill", function(image, left, top)
    local target = normalizePixel(image:getPixel(sx - left, sy - top))
    if target == color then return 0 end
    local w, h = sprite.width, sprite.height
    local visited = {}
    local qx, qy = {sx}, {sy}
    visited[sy * w + sx] = true
    local head, tail = 1, 1
    while head <= tail do
      local x, y = qx[head], qy[head]
      head = head + 1
      image:drawPixel(x - left, y - top, color)
      local neighbors = {{x-1,y}, {x+1,y}, {x,y-1}, {x,y+1}}
      for i = 1, 4 do
        local nx, ny = neighbors[i][1], neighbors[i][2]
        if nx >= 0 and nx < w and ny >= 0 and ny < h then
          local key = ny * w + nx
          if not visited[key] and normalizePixel(image:getPixel(nx - left, ny - top)) == target then
            visited[key] = true
            tail = tail + 1
            qx[tail], qy[tail] = nx, ny
          end
        end
      end
    end
    return tail
  end)
end

handlers.replace_color = function(p)
  local fromColor = parseColor(p.from_color)
  local toColor = parseColor(p.to_color)
  return editCel(p, "AI: replace color", function(image, left, top, sprite)
    if fromColor == toColor then return 0 end
    local count = 0
    for y = 0, sprite.height - 1 do
      for x = 0, sprite.width - 1 do
        if normalizePixel(image:getPixel(x - left, y - top)) == fromColor then
          image:drawPixel(x - left, y - top, toColor)
          count = count + 1
        end
      end
    end
    return count
  end)
end

handlers.sample_pixels = function(p)
  local sprite = active(p)
  local frame = integer(p.frame or 1, 1, #sprite.frames, "frame")
  local x0 = integer(p.x or 0, 0, sprite.width-1, "x")
  local y0 = integer(p.y or 0, 0, sprite.height-1, "y")
  local w = integer(p.width or (sprite.width - x0), 1, sprite.width - x0, "width")
  local h = integer(p.height or (sprite.height - y0), 1, sprite.height - y0, "height")
  local image = Image(sprite.spec)
  if p.layer_id and p.layer_id ~= 0 then
    local layer = findLayerAny(sprite.layers, p.layer_id)
    assert(layer and not layer.isGroup, "Choose an image layer to sample")
    local cel = layer:cel(frame)
    if cel then image:drawImage(cel.image, cel.position) end
  else
    image:drawSprite(sprite, frame)
  end
  local counts, order, pixels = {}, {}, {}
  local includeAll = (w * h <= 64)
  for y = y0, y0 + h - 1 do
    for x = x0, x0 + w - 1 do
      local v = normalizePixel(image:getPixel(x, y))
      local hex = formatColor(v)
      if v ~= 0 then
        if not counts[hex] then
          counts[hex] = 0
          order[#order+1] = hex
        end
        counts[hex] = counts[hex] + 1
      end
      if (v ~= 0 or includeAll) and #pixels < 4096 then
        pixels[#pixels+1] = {x=x, y=y, color=hex}
      end
    end
  end
  table.sort(order, function(a, b)
    if counts[a] ~= counts[b] then return counts[a] > counts[b] end
    return a < b
  end)
  local palette = {}
  for _, hex in ipairs(order) do
    palette[#palette+1] = {color=hex, count=counts[hex]}
  end
  return {
    sprite_id=sprite.id, frame=frame, layer_id=p.layer_id or 0,
    region={x=x0, y=y0, width=w, height=h},
    palette=palette, pixels=pixels
  }
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
          if not handlers[request.op] then
            local path = app.fs.joinPath(app.fs.userConfigPath, "extensions", "aseprite-ai-mcp", "bridge.lua")
            if app.fs.isFile(path) then
              local reloaded = dofile(path)
              if reloaded and reloaded.handlers then handlers = reloaded.handlers end
            end
          end
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
