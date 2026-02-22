------------------------------------------------------------
-- ProPresenter Remote Control via Clicker + HTTP Server + OBS Control
------------------------------------------------------------

proRemote = proRemote or {}

------------------------------------------------------------
-- CONFIG
------------------------------------------------------------

-- Bible look enforcement
proRemote.check_for_bible = true
proRemote.BIBLE_CHECK_INTERVAL_SEC = 0.75
proRemote.BIBLE_MACRO_COOLDOWN_SEC = 2.5

-- Clicker keys (Hammerspoon keycodes)
-- 69 = 'E' on US keyboard, 78 = 'N' on US keyboard
proRemote.nextSlideKey = 69
proRemote.prevSlideKey = 78

-- ProPresenter endpoints
proRemote.PROPRESENTER_ACTIVE_BASE  = "http://localhost:49232/v1/presentation/active"
proRemote.PROPRESENTER_FOCUSED_BASE = "http://localhost:49232/v1/presentation/focused"
proRemote.PROPRESENTER_UUID_BASE    = "http://localhost:49232/v1/presentation"
proRemote.PROPRESENTER_SLIDE_INDEX  = "http://localhost:49232/v1/presentation/slide_index"

proRemote.PROPRESENTER_LOOK_CURRENT = "http://localhost:49232/v1/look/current"
proRemote.BIBLE_LOOK_NAME           = "Bible"
proRemote.BIBLE_MACRO_TRIGGER_URL   = "http://localhost:49232/v1/macro/69293C79-69BB-4061-86E1-76F627CB3085/trigger"

-- Try to force a "real" presentation into focus if focused is announcements
proRemote.PROPRESENTER_ACTIVE_FOCUS = "http://localhost:49232/v1/presentation/active/focus"
proRemote.FOCUSED_RECHECK_DELAY_SEC = 0.20

-- Clear announcements layer
proRemote.PROPRESENTER_CLEAR_ANNOUNCEMENTS = "http://localhost:49232/v1/clear/layer/announcements"

proRemote.PROPRESENTER_PRESENTATION_BASE = proRemote.PROPRESENTER_ACTIVE_BASE

-- Chunked streams (kept; unused currently)
proRemote.USE_CHUNKED_STREAMS = true
proRemote.CURL_PATH = "/usr/bin/curl"
proRemote.STREAM_RESTART_DELAY_SEC = 1.0

-- HTTP server
proRemote.HTTP_SERVER_PORT      = 1337
-- CHANGED: allow LAN access (Cloudflare Tunnel still works either way)
proRemote.HTTP_SERVER_INTERFACE = "127.0.0.1"

-- OBS Bridge (Node)
proRemote.OBS_BRIDGE_ENABLED = true
proRemote.OBS_BRIDGE_BASE    = "http://127.0.0.1:17777"

-- Set this to output of: which node
proRemote.NODE_PATH          = "/opt/homebrew/bin/node"
proRemote.OBS_BRIDGE_SCRIPT  = "/Users/icas/obs-bridge/server.mjs"
proRemote.OBS_BRIDGE_WORKDIR = "/Users/icas/obs-bridge"

-- OBS transitions
proRemote.OBS_DEFAULT_TRANSITION_NAME  = "Cut"
proRemote.OBS_SPECIAL_TRANSITION_NAME  = "Old Film Logo"
proRemote.OBS_FALLBACK_TRANSITION_NAME = "Fade"
proRemote.OBS_FALLBACK_TRANSITION_MS   = 500

-- Scenes that should use Old Film Logo (fallback Fade 500ms) when entering/leaving via control panel
proRemote.OBS_SPECIAL_TRANSITION_SCENES = {
  ["Stream Start"]  = true,
  ["Testimonies"]   = true,
  ["Stream Pause"]  = true,
  ["Thanks Screen"] = true,
}

-- Preset ProPresenter presentations (UUID triggers)
proRemote.PRESET_STARTING_ANNOUNCEMENTS_UUID = "C62E6449-3FD6-42C1-BDF4-CABCA5F8E491"
proRemote.PRESET_PTZ_CAMERA_UUID            = "D47223A2-73BD-4C86-BB82-0D95E90D83F5"
proRemote.PRESET_ENDING_ANNOUNCEMENTS_UUID  = "9CAAE21A-5AB2-41B3-B004-4135B36E134B"

-- Utility presentations for one-shot triggers
proRemote.PRESET_BLANK_PREVIEW_UUID   = "7475C13E-FE99-4AF1-8760-526A845A1860"
proRemote.PRESET_IMAC_SCREEN_UUID     = "AC813C59-FF90-483F-8532-406CF8DD056A"
proRemote.SAFECLEAR_DELAY_SEC         = 0.50

-- Service Logo dictionary
-- Add/remove entries here.
proRemote.SERVICE_LOGOS = {
  { name = "Basic Service Logo",     uuid = "4ED2B2D8-EFE7-4875-BE88-186756A5E57E" },
  { name = "Communion Service Logo", uuid = "82668B6D-5B98-4640-94E3-C69173FA4183" },
  { name = "Youth Meeting Logo",     uuid = "4B871221-EC8A-47A3-86F2-3E2D27311303" },
}

------------------------------------------------------------
-- NEW: Macro dictionary (NAME triggers)
-- ProPresenter API: /v1/macro/[name]/trigger
-- Add/remove entries here (names must match exactly).
------------------------------------------------------------

proRemote.MACROS = {
  { name = "Bible Macro" },
  { name = "Malayalam Songs Macro" },
  { name = "[Aud] Malayalam Song Macro" },
  { name = "English Songs Macro" },
  { name = "[Aud] English Songs Macro" },
  { name = "Presentation Macro" },
  { name = "Presentation Streamer Macro" },
}

------------------------------------------------------------
-- Timer proxy config (use EXACT working ProPresenter URLs)
------------------------------------------------------------

proRemote.PROPRESENTER_TIMER_START = "http://localhost:49232/v1/timer/Service%20Countdown/start"
proRemote.PROPRESENTER_TIMER_STOP  = "http://localhost:49232/v1/timer/Service%20Countdown/stop"
proRemote.PROPRESENTER_TIMER_RESET = "http://localhost:49232/v1/timer/Service%20Countdown/reset"
proRemote.TIMER_STOP_RESET_DELAY_SEC = 0.5

------------------------------------------------------------
-- Utility
------------------------------------------------------------

local function trim(s)
  if type(s) ~= "string" then return "" end
  return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function ltrim(s)
  if type(s) ~= "string" then return "" end
  return (s:gsub("^%s+", ""))
end

local function decodeJson(str)
  if type(str) ~= "string" or str == "" then return nil end
  str = ltrim(str)
  local first = str:sub(1,1)
  if first ~= "{" and first ~= "[" then return nil end
  local ok, result = pcall(function() return hs.json.decode(str) end)
  if ok then return result end
  return nil
end

local function nowSec()
  return hs.timer.secondsSinceEpoch()
end

local function sleepSec(sec)
  sec = tonumber(sec) or 0
  if sec <= 0 then return end
  pcall(function()
    hs.timer.usleep(math.floor(sec * 1000000))
  end)
end

local function jsonResponse(obj)
  return hs.json.encode(obj or {})
end

local function toBoolish(v)
  v = tostring(v or ""):lower()
  return (v == "1" or v == "true" or v == "yes" or v == "on")
end

------------------------------------------------------------
-- Timer actions (proxy to ProPresenter)
------------------------------------------------------------

local function proTimerStart()
  hs.http.asyncGet(proRemote.PROPRESENTER_TIMER_START, {}, function() end)
end

local function proTimerStopReset()
  hs.http.asyncGet(proRemote.PROPRESENTER_TIMER_STOP, {}, function() end)
  hs.timer.doAfter(proRemote.TIMER_STOP_RESET_DELAY_SEC or 0.5, function()
    hs.http.asyncGet(proRemote.PROPRESENTER_TIMER_RESET, {}, function() end)
  end)
end

------------------------------------------------------------
-- Auto-select ACTIVE vs FOCUSED based on slide_index
------------------------------------------------------------

local function refreshPresentationBase()
  local ok, status, body = pcall(function()
    return hs.http.get(proRemote.PROPRESENTER_SLIDE_INDEX, { ["accept"]="application/json" })
  end)

  if not ok or status ~= 200 or not body or body == "" then
    proRemote.PROPRESENTER_PRESENTATION_BASE = proRemote.PROPRESENTER_FOCUSED_BASE
    return
  end

  local data = decodeJson(body)
  if not data or not data.presentation_index or not data.presentation_index.index then
    proRemote.PROPRESENTER_PRESENTATION_BASE = proRemote.PROPRESENTER_FOCUSED_BASE
  else
    proRemote.PROPRESENTER_PRESENTATION_BASE = proRemote.PROPRESENTER_ACTIVE_BASE
  end
end

local function currentMode()
  return (proRemote.PROPRESENTER_PRESENTATION_BASE == proRemote.PROPRESENTER_ACTIVE_BASE) and "active" or "focused"
end

------------------------------------------------------------
-- Bible enforcement
------------------------------------------------------------

local function activePresentationHasSingleGroupWithColon()
  local ok, status, body = pcall(function()
    return hs.http.get(proRemote.PROPRESENTER_ACTIVE_BASE, { ["accept"]="application/json" })
  end)
  if not ok or status ~= 200 or not body or body == "" then return false end

  local data = decodeJson(body)
  local pres = data and data.presentation
  local groups = pres and pres.groups
  if type(groups) ~= "table" then return false end
  if #groups ~= 1 then return false end

  local gname = groups[1] and groups[1].name
  if type(gname) ~= "string" then return false end
  return gname:find(":", 1, true) ~= nil
end

proRemote._bible_lastCondition = proRemote._bible_lastCondition or false
proRemote._bible_lastMacroAt   = proRemote._bible_lastMacroAt   or 0

local function enforceBibleLookIfNeeded()
  if not proRemote.check_for_bible then
    proRemote._bible_lastCondition = false
    return
  end

  local cond = activePresentationHasSingleGroupWithColon()
  local rising = (cond == true and proRemote._bible_lastCondition == false)
  proRemote._bible_lastCondition = cond
  if not rising then return end

  local now = nowSec()
  if (now - (proRemote._bible_lastMacroAt or 0)) < proRemote.BIBLE_MACRO_COOLDOWN_SEC then return end

  local ok, status, body = pcall(function()
    return hs.http.get(proRemote.PROPRESENTER_LOOK_CURRENT, { ["accept"]="application/json" })
  end)
  if not ok or status ~= 200 or not body or body == "" then return end

  local look = decodeJson(body)
  local lookName = look and look.id and look.id.name

  if lookName ~= proRemote.BIBLE_LOOK_NAME then
    proRemote._bible_lastMacroAt = now
    hs.http.asyncGet(proRemote.BIBLE_MACRO_TRIGGER_URL, {}, function() end)
  end
end

local function startBibleTimer()
  if proRemote.bibleTimer then proRemote.bibleTimer:stop() end
  proRemote.bibleTimer = hs.timer.doEvery(proRemote.BIBLE_CHECK_INTERVAL_SEC, function()
    pcall(enforceBibleLookIfNeeded)
  end)
end

------------------------------------------------------------
-- Slide actions (picker)
------------------------------------------------------------

local function triggerNextSlide()
  refreshPresentationBase()
  hs.http.asyncGet(proRemote.PROPRESENTER_PRESENTATION_BASE .. "/next/trigger", {}, function() end)
end

local function triggerPreviousSlide()
  refreshPresentationBase()
  hs.http.asyncGet(proRemote.PROPRESENTER_PRESENTATION_BASE .. "/previous/trigger", {}, function() end)
end

local function triggerFocusedSlide(index)
  if type(index) ~= "number" or index < 0 then return end
  refreshPresentationBase()
  local url = string.format("%s/%d/trigger", proRemote.PROPRESENTER_PRESENTATION_BASE, index)
  hs.http.asyncGet(url, {}, function() end)
end

------------------------------------------------------------
-- NEW: Clicker key listener (THIS WAS MISSING)
------------------------------------------------------------

local function startClickerListener()
  -- Stop existing listener (reload-safe)
  if proRemote._clickerTap then
    pcall(function() proRemote._clickerTap:stop() end)
    proRemote._clickerTap = nil
  end

  proRemote._clickerTap = hs.eventtap.new({ hs.eventtap.event.types.keyDown }, function(e)
    local keyCode = e:getKeyCode()
    if keyCode == proRemote.nextSlideKey then
      pcall(triggerNextSlide)
      return true -- swallow so it doesn't type
    elseif keyCode == proRemote.prevSlideKey then
      pcall(triggerPreviousSlide)
      return true -- swallow so it doesn't type
    end
    return false
  end)

  proRemote._clickerTap:start()
end

------------------------------------------------------------
-- Helpers: focused + destination check
------------------------------------------------------------

local function fetchFocusedInfo()
  local ok, status, body = pcall(function()
    return hs.http.get(proRemote.PROPRESENTER_FOCUSED_BASE, { ["accept"]="application/json" })
  end)
  if not ok or status ~= 200 or not body or body == "" then return nil end
  local obj = decodeJson(body)
  if type(obj) ~= "table" then return nil end
  if type(obj.uuid) ~= "string" or obj.uuid == "" then return nil end
  return obj
end

local function fetchPresentationByUUID(uuid)
  if type(uuid) ~= "string" or uuid == "" then return nil, nil end
  local url = string.format("%s/%s", proRemote.PROPRESENTER_UUID_BASE, uuid)
  local ok, status, body = pcall(function()
    return hs.http.get(url, { ["accept"]="application/json" })
  end)
  if not ok or status ~= 200 or not body or body == "" then return nil, body end
  local obj = decodeJson(body)
  return obj, body
end

local function presentationDestinationFromObj(fullObj)
  local pres = fullObj and fullObj.presentation
  local dest = pres and pres.destination
  if type(dest) == "string" then return dest end
  return ""
end

local function activeUUIDFromObj(activeObj)
  local pres = activeObj and activeObj.presentation
  local id = pres and pres.id
  local uuid = id and id.uuid
  if type(uuid) == "string" then return uuid end
  return ""
end

local function blankPresentationResponse(reason)
  reason = tostring(reason or "blank")
  return string.format('{"presentation":null,"reason":"%s"}', hs.http.encodeForQuery(reason)):gsub("%%22", '"')
end

local function isBlankPreviewUUID(uuid)
  return type(uuid) == "string" and uuid ~= "" and uuid == proRemote.PRESET_BLANK_PREVIEW_UUID
end

------------------------------------------------------------
-- Unified Presentation Fetcher (ACTIVE or FOCUSED MODE)
------------------------------------------------------------

local function fetchFullPresentationJSON()
  refreshPresentationBase()

  ----------------------------------------------------------
  -- ACTIVE MODE
  -- If active is Blank Preview, DO NOT show it.
  -- Instead: try showing focused (with the same filtering rules).
  ----------------------------------------------------------
  if currentMode() == "active" then
    local ok, status, body = pcall(function()
      return hs.http.get(proRemote.PROPRESENTER_ACTIVE_BASE, { ["accept"]="application/json" })
    end)
    if not ok or status ~= 200 or not body then
      return '{"error":"cannot fetch active presentation"}'
    end

    local activeObj = decodeJson(body)
    local activeUUID = activeUUIDFromObj(activeObj)

    if isBlankPreviewUUID(activeUUID) then
      -- Fall through to focused logic below
    else
      return body
    end
  end

  ----------------------------------------------------------
  -- FOCUSED MODE (also used as fallback when active is Blank Preview)
  -- If focused is Blank Preview, show nothing.
  ----------------------------------------------------------
  local focused = fetchFocusedInfo()
  if not focused or not focused.uuid then
    return blankPresentationResponse("no_focused")
  end

  if isBlankPreviewUUID(focused.uuid) then
    return blankPresentationResponse("blank_preview")
  end

  local fullObj, fullBody = fetchPresentationByUUID(focused.uuid)
  if not fullObj or not fullBody then
    return '{"error":"cannot fetch presentation by uuid"}'
  end

  local dest = presentationDestinationFromObj(fullObj)

  ----------------------------------------------------------
  -- Existing rule: if focused destination=announcements, try /active/focus once
  ----------------------------------------------------------
  if dest == "announcements" then
    pcall(function()
      hs.http.get(proRemote.PROPRESENTER_ACTIVE_FOCUS, { ["accept"]="application/json" })
    end)

    sleepSec(proRemote.FOCUSED_RECHECK_DELAY_SEC)

    local focused2 = fetchFocusedInfo()
    if not focused2 or not focused2.uuid then
      return blankPresentationResponse("focused_is_announcements")
    end

    if isBlankPreviewUUID(focused2.uuid) then
      return blankPresentationResponse("blank_preview")
    end

    local fullObj2, fullBody2 = fetchPresentationByUUID(focused2.uuid)
    if not fullObj2 or not fullBody2 then
      return blankPresentationResponse("focused_is_announcements")
    end

    local dest2 = presentationDestinationFromObj(fullObj2)
    if dest2 == "presentation" then
      return fullBody2
    end

    return blankPresentationResponse("focused_is_announcements")
  end

  return fullBody
end

------------------------------------------------------------
-- Thumbnail Fetch
------------------------------------------------------------

local function fetchThumbnail(uuid, index)
  if not uuid or index == nil then
    return "Missing uuid or index", 400, "text/plain; charset=utf-8"
  end

  local url = string.format(
    "%s/%s/thumbnail/%d?quality=800&thumbnail_type=png",
    proRemote.PROPRESENTER_UUID_BASE, uuid, index
  )

  local status, body, headers = hs.http.doRequest(url, "GET", nil, { ["Accept"]="image/png" })
  if status ~= 200 or not body then
    return "Error fetching thumbnail", 500, "text/plain; charset=utf-8"
  end

  local contentType = (headers and headers["Content-Type"]) or "image/png"
  return body, 200, contentType
end

------------------------------------------------------------
-- ProPresenter one-shot actions for control panel
------------------------------------------------------------

local function proTriggerPresentationUUID(uuid)
  if type(uuid) ~= "string" or uuid == "" then return false end
  local url = string.format("http://localhost:49232/v1/presentation/%s/trigger", uuid)
  hs.http.asyncGet(url, {}, function() end)
  return true
end

local function proTriggerPresentationUUIDAfter(uuid, delaySec)
  delaySec = tonumber(delaySec) or 0
  if delaySec <= 0 then
    return proTriggerPresentationUUID(uuid)
  end
  hs.timer.doAfter(delaySec, function()
    proTriggerPresentationUUID(uuid)
  end)
  return true
end

local function proClearAnnouncementsLayer()
  hs.http.asyncGet(proRemote.PROPRESENTER_CLEAR_ANNOUNCEMENTS, {}, function() end)
end

------------------------------------------------------------
-- NEW: ProPresenter macros (name-based trigger)
------------------------------------------------------------

local function proTriggerMacroByName(macroName)
  macroName = trim(macroName)
  if macroName == "" then return false end

  -- /v1/macro/[name]/trigger (name must be URL-encoded)
  local url = string.format(
    "http://localhost:49232/v1/macro/%s/trigger",
    hs.http.encodeForQuery(macroName)
  )

  hs.http.asyncGet(url, {}, function() end)
  return true
end

local function getMacrosList()
  local items = {}
  for _, it in ipairs(proRemote.MACROS or {}) do
    if type(it) == "table" then
      local nm = tostring(it.name or "")
      nm = trim(nm)
      if nm ~= "" then
        table.insert(items, { name = nm })
      end
    end
  end
  return items
end

local function macroNameAllowed(name)
  name = trim(name)
  if name == "" then return false end
  for _, it in ipairs(proRemote.MACROS or {}) do
    if type(it) == "table" and trim(it.name or "") == name then
      return true
    end
  end
  return false
end

------------------------------------------------------------
-- OBS Bridge start/watch
------------------------------------------------------------

proRemote._bridge = proRemote._bridge or { task=nil, running=false }

local function bridgeHealth()
  if not proRemote.OBS_BRIDGE_ENABLED then return false end
  local status = hs.http.get(proRemote.OBS_BRIDGE_BASE .. "/health", { ["accept"]="application/json" })
  return status == 200
end

local function bridgeKillTask()
  if proRemote._bridge.task then
    pcall(function() proRemote._bridge.task:terminate() end)
    proRemote._bridge.task = nil
  end
end

local function bridgeStart()
  if not proRemote.OBS_BRIDGE_ENABLED then return end
  if bridgeHealth() then
    proRemote._bridge.running = true
    return
  end

  bridgeKillTask()

  -- IMPORTANT: streaming callback MUST return boolean (true = keep streaming)
  local function streamFn(task, stdOut, stdErr)
    return true
  end

  local function exitFn(task, exitCode, stdOut, stdErr)
    proRemote._bridge.running = false
    hs.timer.doAfter(1.0, function() bridgeStart() end)
    return true
  end

  local t = hs.task.new(proRemote.NODE_PATH, exitFn, streamFn, { proRemote.OBS_BRIDGE_SCRIPT })
  if not t then return end

  if proRemote.OBS_BRIDGE_WORKDIR and proRemote.OBS_BRIDGE_WORKDIR ~= "" then
    pcall(function() t:setWorkingDirectory(proRemote.OBS_BRIDGE_WORKDIR) end)
  end

  proRemote._bridge.task = t
  t:start()

  hs.timer.doAfter(0.8, function()
    proRemote._bridge.running = bridgeHealth()
  end)
end

local function bridgeWatchdogStart()
  if proRemote.bridgeWatchdog then proRemote.bridgeWatchdog:stop() end
  proRemote.bridgeWatchdog = hs.timer.doEvery(4.0, function()
    if not bridgeHealth() then
      proRemote._bridge.running = false
      bridgeStart()
    else
      proRemote._bridge.running = true
    end
  end)
end

local function bridgeTrySetScene(sceneName, transitionName, durationMs)
  if not proRemote.OBS_BRIDGE_ENABLED then return false end
  if type(sceneName) ~= "string" or sceneName == "" then return false end

  transitionName = tostring(transitionName or "")
  durationMs = tonumber(durationMs)

  local url = string.format(
    "%s/scene/set?name=%s&transition=%s&duration=%s",
    proRemote.OBS_BRIDGE_BASE,
    hs.http.encodeForQuery(sceneName),
    hs.http.encodeForQuery(transitionName),
    hs.http.encodeForQuery(durationMs and tostring(durationMs) or "")
  )

  local ok, status = pcall(function()
    return hs.http.get(url, { ["accept"]="application/json" })
  end)

  return ok and status == 200
end

local function obsSetSceneWithTransitionPolicy(sceneName)
  local isSpecial = proRemote.OBS_SPECIAL_TRANSITION_SCENES[sceneName] == true

  if not isSpecial then
    bridgeTrySetScene(sceneName, proRemote.OBS_DEFAULT_TRANSITION_NAME, nil)
    return
  end

  local okOld = bridgeTrySetScene(sceneName, proRemote.OBS_SPECIAL_TRANSITION_NAME, nil)
  if okOld then return end

  local okFade = bridgeTrySetScene(sceneName, proRemote.OBS_FALLBACK_TRANSITION_NAME, proRemote.OBS_FALLBACK_TRANSITION_MS)
  if okFade then return end

  bridgeTrySetScene(sceneName, proRemote.OBS_DEFAULT_TRANSITION_NAME, nil)
end

------------------------------------------------------------
-- Service logos API helpers
------------------------------------------------------------

local function getServiceLogosList()
  local items = {}
  for _, it in ipairs(proRemote.SERVICE_LOGOS or {}) do
    if type(it) == "table" then
      local nm = tostring(it.name or "")
      local uu = tostring(it.uuid or "")
      if nm ~= "" and uu ~= "" then
        table.insert(items, { name = nm, uuid = uu })
      end
    end
  end
  return items
end

------------------------------------------------------------
-- HTTP Helpers
------------------------------------------------------------

local function cleanPath(path)
  local p = path and path:match("^[^?]+") or ""
  return p ~= "" and p or "/"
end

local function parseQuery(path)
  local params = {}
  local q = path:match("%?(.*)$") or ""
  for k, v in q:gmatch("([^&=]+)=([^&=]+)") do params[k] = v end
  return params
end

------------------------------------------------------------
-- Route Handler
------------------------------------------------------------

local function handleHttpPath(method, rawPath, body)
  local p = cleanPath(rawPath)
  local params = parseQuery(rawPath)

  if p == "/next" then
    triggerNextSlide()
    return "OK\n", 200, "text/plain"

  elseif p == "/previous" or p == "/prev" then
    triggerPreviousSlide()
    return "OK\n", 200, "text/plain"

  elseif p == "/focus" then
    local idx = tonumber(params["index"])
    if not idx then return "Bad index", 400, "text/plain" end
    triggerFocusedSlide(idx)
    return "OK\n", 200, "text/plain"

  ----------------------------------------------------------
  -- Timer proxy endpoints
  ----------------------------------------------------------
  elseif p == "/timer/start" then
    proTimerStart()
    return "OK\n", 200, "text/plain"

  elseif p == "/timer/stop-reset" then
    proTimerStopReset()
    return "OK\n", 200, "text/plain"

  ----------------------------------------------------------
  -- Presentation / slide endpoints
  ----------------------------------------------------------
  elseif p == "/active-presentation" then
    return fetchFullPresentationJSON(), 200, "application/json"

  elseif p == "/slide-index" then
    local ok, status, b = pcall(function()
      return hs.http.get(proRemote.PROPRESENTER_SLIDE_INDEX, { ["accept"]="application/json" })
    end)
    return b or "{}", 200, "application/json"

  elseif p == "/thumbnail" then
    return fetchThumbnail(params.uuid, tonumber(params.index))

  elseif p == "/current-base" then
    refreshPresentationBase()
    local out = string.format('{"mode":"%s","base_url":"%s"}', currentMode(), proRemote.PROPRESENTER_PRESENTATION_BASE)
    return out, 200, "application/json"

  elseif p == "/health" then
    return "OK", 200, "text/plain"

  ----------------------------------------------------------
  -- Service logos list (existing)
  ----------------------------------------------------------
  elseif p == "/service_logos" then
    local out = { items = getServiceLogosList() }
    return jsonResponse(out), 200, "application/json"

  ----------------------------------------------------------
  -- NEW: Macros list + trigger
  ----------------------------------------------------------
  elseif p == "/macros" then
    local out = { items = getMacrosList() }
    return jsonResponse(out), 200, "application/json"

  elseif p == "/macro" then
    if method ~= "POST" then
      return "Method Not Allowed", 405, "text/plain"
    end

    local obj = decodeJson(body or "")
    if type(obj) ~= "table" then
      return jsonResponse({ ok=false, error="bad_json" }), 400, "application/json"
    end

    -- Accept either { name: "..." } or { macro_name: "..." }
    local macroName = trim(obj.name or obj.macro_name or "")
    if macroName == "" then
      return jsonResponse({ ok=false, error="missing_macro_name" }), 400, "application/json"
    end

    if not macroNameAllowed(macroName) then
      return jsonResponse({ ok=false, error="macro_not_in_list", name=macroName }), 400, "application/json"
    end

    local ok = proTriggerMacroByName(macroName)
    if not ok then
      return jsonResponse({ ok=false, error="trigger_failed" }), 500, "application/json"
    end

    return jsonResponse({ ok=true, name=macroName }), 200, "application/json"

  ----------------------------------------------------------
  -- Presets (existing)
  ----------------------------------------------------------
  elseif p == "/preset" then
    if method ~= "POST" then
      return "Method Not Allowed", 405, "text/plain"
    end

    local obj = decodeJson(body or "")
    if type(obj) ~= "table" then
      return jsonResponse({ ok=false, error="bad_json" }), 400, "application/json"
    end

    local preset = tostring(obj.preset or ""):lower()
    local serviceLogoUuid = tostring(obj.service_logo_uuid or "")
    local doSafeClear = toBoolish(params["safeclear"])
    local delay = tonumber(proRemote.SAFECLEAR_DELAY_SEC) or 0.5

    if preset == "stream_beginning" then
      proTriggerPresentationUUID(proRemote.PRESET_STARTING_ANNOUNCEMENTS_UUID)
      obsSetSceneWithTransitionPolicy("Stream Start")
      return jsonResponse({ ok=true }), 200, "application/json"

    elseif preset == "camera" then
      proTriggerPresentationUUID(proRemote.PRESET_PTZ_CAMERA_UUID)
      obsSetSceneWithTransitionPolicy("PTZ Camera")

      if doSafeClear then
        proTriggerPresentationUUIDAfter(proRemote.PRESET_BLANK_PREVIEW_UUID, delay)
      end

      return jsonResponse({ ok=true, safeclear=doSafeClear }), 200, "application/json"

    elseif preset == "show_slides" then
      proClearAnnouncementsLayer()
      obsSetSceneWithTransitionPolicy("ProPresenter Input")
      return jsonResponse({ ok=true }), 200, "application/json"

    elseif preset == "service_logo" then
      if serviceLogoUuid == "" then
        return jsonResponse({ ok=false, error="missing_service_logo_uuid" }), 400, "application/json"
      end

      proTriggerPresentationUUID(serviceLogoUuid)
      obsSetSceneWithTransitionPolicy("Audience Camera")

      if doSafeClear then
        proTriggerPresentationUUIDAfter(proRemote.PRESET_BLANK_PREVIEW_UUID, delay)
      end

      return jsonResponse({ ok=true, safeclear=doSafeClear }), 200, "application/json"

    elseif preset == "testimonies" then
      if serviceLogoUuid == "" then
        return jsonResponse({ ok=false, error="missing_service_logo_uuid" }), 400, "application/json"
      end
      proTriggerPresentationUUID(serviceLogoUuid)
      obsSetSceneWithTransitionPolicy("Testimonies")
      return jsonResponse({ ok=true }), 200, "application/json"

    elseif preset == "ending_stream" then
      proTriggerPresentationUUID(proRemote.PRESET_ENDING_ANNOUNCEMENTS_UUID)
      obsSetSceneWithTransitionPolicy("Thanks Screen")
      return jsonResponse({ ok=true }), 200, "application/json"

    elseif preset == "safely_clear_slide" then
      proTriggerPresentationUUID(proRemote.PRESET_BLANK_PREVIEW_UUID)
      return jsonResponse({ ok=true }), 200, "application/json"

    elseif preset == "nsc_setup" then
      proClearAnnouncementsLayer()
      proTriggerPresentationUUID(proRemote.PRESET_IMAC_SCREEN_UUID)
      return jsonResponse({ ok=true }), 200, "application/json"

    else
      return jsonResponse({ ok=false, error="unknown_preset" }), 400, "application/json"
    end
  end

  return "Not found", 404, "text/plain"
end

------------------------------------------------------------
-- HTTP server
------------------------------------------------------------

local function httpCallback(method, path, headers, body)
  local h = {
    ["Access-Control-Allow-Origin"]  = "*",
    ["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS",
    ["Access-Control-Allow-Headers"] = "Content-Type",
  }

  if method == "OPTIONS" then return "", 204, h end

  local ok, bodyData, status, contentType = pcall(handleHttpPath, method, path, body)
  if not ok then
    return "Internal error\n", 500, h
  end

  h["Content-Type"] = contentType
  return bodyData, status, h
end

if proRemote.server then proRemote.server:stop() end
proRemote.server = hs.httpserver.new(false, false)
proRemote.server:setPort(proRemote.HTTP_SERVER_PORT)
proRemote.server:setInterface(proRemote.HTTP_SERVER_INTERFACE)
proRemote.server:setCallback(httpCallback)
proRemote.server:start()

------------------------------------------------------------
-- STARTUP
------------------------------------------------------------

startBibleTimer()
startClickerListener()

bridgeStart()
bridgeWatchdogStart()

hs.alert.show("ProPresenter Remote/OBS Remote Control Ready")0