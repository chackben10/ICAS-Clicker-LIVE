------------------------------------------------------------
-- ProPresenter Remote Control via Clicker + HTTP Server
------------------------------------------------------------

proRemote = proRemote or {}

------------------------------------------------------------
-- CONFIG
------------------------------------------------------------

-- Clicker keys
proRemote.nextSlideKey = 69
proRemote.prevSlideKey = 78

-- Base API URLs
proRemote.PROPRESENTER_ACTIVE_BASE  = "http://localhost:49232/v1/presentation/active"
proRemote.PROPRESENTER_FOCUSED_BASE = "http://localhost:49232/v1/presentation/focused"
proRemote.PROPRESENTER_UUID_BASE    = "http://localhost:49232/v1/presentation"
proRemote.PROPRESENTER_SLIDE_INDEX  = "http://localhost:49232/v1/presentation/slide_index"

proRemote.PROPRESENTER_PRESENTATION_BASE = proRemote.PROPRESENTER_ACTIVE_BASE

-- HTTP Server config
proRemote.HTTP_SERVER_PORT      = 1337
proRemote.HTTP_SERVER_INTERFACE = "localhost"

------------------------------------------------------------
-- Utility: Read JSON safely
------------------------------------------------------------

local function decodeJson(str)
    if not str or str == "" then return nil end
    local ok, result = pcall(function() return hs.json.decode(str) end)
    if ok then return result end
    return nil
end

------------------------------------------------------------
-- Auto-select ACTIVE vs FOCUSED based on slide_index
------------------------------------------------------------

local function refreshPresentationBase()
    local ok, status, body = pcall(function()
        return hs.http.get(proRemote.PROPRESENTER_SLIDE_INDEX, {
            ["accept"] = "application/json"
        })
    end)

    -- any error → fallback to focused
    if not ok or status ~= 200 or not body or body == "" then
        proRemote.PROPRESENTER_PRESENTATION_BASE = proRemote.PROPRESENTER_FOCUSED_BASE
        return
    end

    local data = decodeJson(body)
    if not data or not data.presentation_index or not data.presentation_index.index then
        -- null / missing index → no active presentation → use focused
        proRemote.PROPRESENTER_PRESENTATION_BASE = proRemote.PROPRESENTER_FOCUSED_BASE
    else
        -- valid index → active presentation exists
        proRemote.PROPRESENTER_PRESENTATION_BASE = proRemote.PROPRESENTER_ACTIVE_BASE
    end
end

local function currentMode()
    if proRemote.PROPRESENTER_PRESENTATION_BASE == proRemote.PROPRESENTER_ACTIVE_BASE then
        return "active"
    else
        return "focused"
    end
end

------------------------------------------------------------
-- Trigger slide actions
------------------------------------------------------------

local function triggerNextSlide()
    refreshPresentationBase()
    local url = proRemote.PROPRESENTER_PRESENTATION_BASE .. "/next/trigger"
    hs.http.asyncGet(url, {}, function() end)
end

local function triggerPreviousSlide()
    refreshPresentationBase()
    local url = proRemote.PROPRESENTER_PRESENTATION_BASE .. "/previous/trigger"
    hs.http.asyncGet(url, {}, function() end)
end

local function triggerFocusedSlide(index)
    if type(index) ~= "number" or index < 0 then return end
    refreshPresentationBase()
    local url = string.format("%s/%d/trigger",
        proRemote.PROPRESENTER_PRESENTATION_BASE, index)
    hs.http.asyncGet(url, {}, function() end)
end

------------------------------------------------------------
-- Unified Presentation Fetcher (ACTIVE or FOCUSED MODE)
------------------------------------------------------------

local function fetchFullPresentationJSON()
    refreshPresentationBase()

    -- If ACTIVE mode: easy path
    if currentMode() == "active" then
        local ok, status, body = pcall(function()
            return hs.http.get(proRemote.PROPRESENTER_ACTIVE_BASE, {
                ["accept"] = "application/json"
            })
        end)
        if ok and status == 200 and body then return body end
        return '{"error":"cannot fetch active presentation"}'
    end

    -- If FOCUSED mode: get uuid → fetch full presentation by uuid
    local ok1, status1, focusedBody = pcall(function()
        return hs.http.get(proRemote.PROPRESENTER_FOCUSED_BASE, {
            ["accept"] = "application/json"
        })
    end)

    local focused = decodeJson(focusedBody)
    if not ok1 or status1 ~= 200 or not focused or not focused.uuid then
        return '{"error":"cannot fetch focused presentation"}'
    end

    local uuid = focused.uuid
    local fullURL = string.format("%s/%s", proRemote.PROPRESENTER_UUID_BASE, uuid)

    local ok2, status2, fullBody = pcall(function()
        return hs.http.get(fullURL, { ["accept"]="application/json" })
    end)

    if ok2 and status2 == 200 and fullBody then
        return fullBody
    end

    return '{"error":"cannot fetch presentation by uuid"}'
end

------------------------------------------------------------
-- Thumbnail Fetch
------------------------------------------------------------

local function fetchThumbnail(uuid, index)
    if not uuid or not index then
        return "Missing uuid or index", 400, "text/plain; charset=utf-8"
    end

    local url = string.format(
        "%s/%s/thumbnail/%d?quality=800&thumbnail_type=png",
        proRemote.PROPRESENTER_UUID_BASE, uuid, index
    )

    local status, body, headers = hs.http.doRequest(url, "GET", nil, {
        ["Accept"] = "image/png"
    })

    if status ~= 200 or not body then
        return "Error fetching thumbnail", 500, "text/plain; charset=utf-8"
    end

    local contentType = headers["Content-Type"] or "image/png"
    return body, 200, contentType
end

------------------------------------------------------------
-- eventtap for clicker
------------------------------------------------------------

if proRemote.clickerTap then proRemote.clickerTap:stop() end

proRemote.clickerTap = hs.eventtap.new(
    { hs.eventtap.event.types.keyDown },
    function(e)
        local key = e:getKeyCode()
        if key == proRemote.nextSlideKey then
            triggerNextSlide()
        elseif key == proRemote.prevSlideKey then
            triggerPreviousSlide()
        end
        return false
    end
)

proRemote.clickerTap:start()

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
    for k, v in q:gmatch("([^&=]+)=([^&=]+)") do
        params[k] = v
    end
    return params
end

------------------------------------------------------------
-- Route Handler
------------------------------------------------------------

local function handleHttpPath(rawPath)
    local p      = cleanPath(rawPath)
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

    elseif p == "/active-presentation" then
        return fetchFullPresentationJSON(), 200, "application/json"

    elseif p == "/slide-index" then
        local ok, status, body = pcall(function()
            return hs.http.get(proRemote.PROPRESENTER_SLIDE_INDEX, {
                ["accept"]="application/json"
            })
        end)
        return body or "{}", 200, "application/json"

    elseif p == "/thumbnail" then
        return fetchThumbnail(params.uuid, tonumber(params.index))

    elseif p == "/current-base" then
        refreshPresentationBase()
        local out = string.format(
            '{"mode":"%s","base_url":"%s"}',
            currentMode(), proRemote.PROPRESENTER_PRESENTATION_BASE
        )
        return out, 200, "application/json"

    elseif p == "/health" then
        return "OK", 200, "text/plain"
    end

    return "Not found", 404, "text/plain"
end

------------------------------------------------------------
-- HTTP server
------------------------------------------------------------

local function httpCallback(method, path, headers, body)
    local h = {
        ["Access-Control-Allow-Origin"]  = "*",
        ["Access-Control-Allow-Methods"] = "GET, OPTIONS",
        ["Access-Control-Allow-Headers"] = "Content-Type",
    }

    if method == "OPTIONS" then
        return "", 204, h
    end

    local ok, bodyData, status, contentType = pcall(handleHttpPath, path)
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

hs.alert.show("ProPresenter remote ready")
