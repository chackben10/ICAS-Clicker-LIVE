#!/usr/bin/env node
import http from "http";
import { URL } from "url";
import OBSWebSocket from "obs-websocket-js";

const CONFIG = {
  httpHost: "127.0.0.1",
  httpPort: 17777,

  obsHost: "192.168.1.156",
  obsPort: 4455,
  obsPassword: "", // auth disabled

  // Defaults; can be overridden per-request with query params
  sceneName: "ProPresenter Slides",
  sourceCamera: "PTZ Camera",
  sourceAnn: "Audience Camera",

  // Scene that Hammerspoon will inspect/control by sceneItemId
  lookSceneName: "ProPresenter Input",
};

const obs = new OBSWebSocket();

// --- in-memory state / debug ---
const state = {
  connected: false,
  identified: false,
  lastConnectAttemptAt: 0,
  lastConnectedAt: 0,
  lastError: "",
  desired: "none", // none | ann | cam
  lastApplied: "unknown",
  cache: new Map(), // key: `${sceneName}|${sourceName}` -> sceneItemId, for old /set endpoint
  lastSceneList: null,
  lastSceneItems: new Map(), // sceneName -> items array
};

// Simple timestamped ring log
const LOG_MAX = 400;
const logs = [];
function log(msg) {
  const line = `${new Date().toISOString()}  ${msg}`;
  logs.push(line);
  if (logs.length > LOG_MAX) logs.shift();
  console.log(line);
}

// --- OBS connect loop ---
let connectTimer = null;
async function connectOBS() {
  state.lastConnectAttemptAt = Date.now();
  const url = `ws://${CONFIG.obsHost}:${CONFIG.obsPort}`;
  try {
    log(`OBS connect -> ${url}`);
    await obs.connect(url, CONFIG.obsPassword ? CONFIG.obsPassword : undefined);

    state.connected = true;
    state.identified = true;
    state.lastConnectedAt = Date.now();
    state.lastError = "";
    state.cache.clear();

    log("OBS connected+identified");
  } catch (e) {
    state.connected = false;
    state.identified = false;
    state.lastError = String(e?.message || e);
    log(`OBS connect FAIL: ${state.lastError}`);
    scheduleReconnect();
    return;
  }

  // if OBS disconnects later
  obs.once("ConnectionClosed", () => {
    state.connected = false;
    state.identified = false;
    state.cache.clear();
    log("OBS disconnected (ConnectionClosed)");
    scheduleReconnect();
  });

  // After reconnect, re-apply desired state quickly
  try {
    await applyDesired(state.desired);
  } catch (e) {
    log(`applyDesired after connect failed: ${String(e?.message || e)}`);
  }
}

function scheduleReconnect() {
  if (connectTimer) return;
  connectTimer = setTimeout(async () => {
    connectTimer = null;
    await connectOBS();
  }, 750);
}

// --- OBS helpers ---
async function getSceneItemId(sceneName, sourceName) {
  const key = `${sceneName}|${sourceName}`;
  if (state.cache.has(key)) return state.cache.get(key);

  const { sceneItemId } = await obs.call("GetSceneItemId", { sceneName, sourceName });
  state.cache.set(key, sceneItemId);
  log(`cache sceneItemId: ${key} = ${sceneItemId}`);
  return sceneItemId;
}

async function setEnabled(sceneName, sourceName, enabled) {
  const id = await getSceneItemId(sceneName, sourceName);
  await setSceneItemEnabledById(sceneName, id, enabled);
  log(`SetSceneItemEnabled: scene="${sceneName}" source="${sourceName}" id=${id} enabled=${!!enabled}`);
}

async function setSceneItemEnabledById(sceneName, sceneItemId, enabled) {
  const id = Number(sceneItemId);
  if (!Number.isFinite(id)) throw new Error(`Bad sceneItemId: ${sceneItemId}`);

  await obs.call("SetSceneItemEnabled", {
    sceneName,
    sceneItemId: id,
    sceneItemEnabled: !!enabled,
  });
}

async function applyDesired(desired, overrides = {}) {
  const sceneName = overrides.sceneName || CONFIG.sceneName;
  const sourceCamera = overrides.sourceCamera || CONFIG.sourceCamera;
  const sourceAnn = overrides.sourceAnn || CONFIG.sourceAnn;

  if (!state.identified) throw new Error("OBS not identified");

  // Idempotent
  if (
    desired === state.lastApplied &&
    sceneName === CONFIG.sceneName &&
    sourceCamera === CONFIG.sourceCamera &&
    sourceAnn === CONFIG.sourceAnn
  ) {
    return;
  }

  log(`Apply desired="${desired}" scene="${sceneName}"`);

  if (desired === "none") {
    await setEnabled(sceneName, sourceAnn, false);
    await setEnabled(sceneName, sourceCamera, false);
  } else if (desired === "ann") {
    await setEnabled(sceneName, sourceAnn, true);
    await setEnabled(sceneName, sourceCamera, false);
  } else if (desired === "cam") {
    await setEnabled(sceneName, sourceAnn, false);
    await setEnabled(sceneName, sourceCamera, true);
  } else {
    throw new Error(`bad desired: ${desired}`);
  }

  state.lastApplied = desired;
}

async function setTransition(transitionName, durationMs) {
  if (!transitionName) return;

  await obs.call("SetCurrentSceneTransition", { transitionName });
  log(`SetCurrentSceneTransition: ${transitionName}`);

  if (durationMs != null && !Number.isNaN(Number(durationMs))) {
    try {
      await obs.call("SetCurrentSceneTransitionDuration", { transitionDuration: Number(durationMs) });
      log(`SetCurrentSceneTransitionDuration: ${Number(durationMs)}ms`);
    } catch (e) {
      // Non-fatal in some setups
      log(`SetCurrentSceneTransitionDuration failed: ${String(e?.message || e)}`);
    }
  }
}

async function setProgramScene(sceneName) {
  if (!sceneName) throw new Error("Missing scene name");
  await obs.call("SetCurrentProgramScene", { sceneName });
  log(`SetCurrentProgramScene: ${sceneName}`);
}

async function getCurrentProgramScene() {
  if (!state.identified) throw new Error("OBS not identified");
  const data = await obs.call("GetCurrentProgramScene");
  return data;
}

function normalizeSceneItem(raw) {
  return {
    sceneItemId: raw.sceneItemId,
    itemId: raw.sceneItemId,
    sceneItemIndex: raw.sceneItemIndex,
    sourceName: raw.sourceName,
    sourceUuid: raw.sourceUuid,
    sourceType: raw.sourceType,
    inputKind: raw.inputKind,
    isGroup: raw.isGroup,
    sceneItemEnabled: raw.sceneItemEnabled,
    sceneItemLocked: raw.sceneItemLocked,
    sceneItemBlendMode: raw.sceneItemBlendMode,
    sceneItemTransform: raw.sceneItemTransform,
  };
}

async function getSceneItems(sceneName = CONFIG.lookSceneName) {
  if (!state.identified) throw new Error("OBS not identified");
  const data = await obs.call("GetSceneItemList", { sceneName });
  const items = (data.sceneItems || []).map(normalizeSceneItem);
  state.lastSceneItems.set(sceneName, items);
  return items;
}

async function applySceneItemVisibility(sceneName, payload) {
  if (!state.identified) throw new Error("OBS not identified");
  if (!sceneName) throw new Error("Missing scene name");

  const operations = [];

  for (const id of payload.show || []) {
    operations.push({ sceneItemId: Number(id), enabled: true });
  }
  for (const id of payload.hide || []) {
    operations.push({ sceneItemId: Number(id), enabled: false });
  }
  for (const item of payload.items || []) {
    const id = Number(item.sceneItemId ?? item.id ?? item.scene_item_id);
    const enabled = item.enabled ?? item.sceneItemEnabled;
    operations.push({ sceneItemId: id, enabled: !!enabled });
  }

  const cleanOps = operations.filter(op => Number.isFinite(op.sceneItemId));
  for (const op of cleanOps) {
    await setSceneItemEnabledById(sceneName, op.sceneItemId, op.enabled);
    log(`SetSceneItemEnabled: scene="${sceneName}" id=${op.sceneItemId} enabled=${op.enabled}`);
  }

  return cleanOps;
}

// Optional: list scenes + items
async function refreshSceneData() {
  if (!state.identified) throw new Error("OBS not identified");
  const sceneList = await obs.call("GetSceneList");
  state.lastSceneList = sceneList;

  state.lastSceneItems.clear();
  for (const s of sceneList.scenes || []) {
    try {
      const items = await getSceneItems(s.sceneName);
      state.lastSceneItems.set(s.sceneName, items);
    } catch (e) {
      // ignore per-scene failures
    }
  }
}

function parseJsonBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", chunk => {
      data += chunk;
      if (data.length > 1_000_000) {
        req.destroy();
        reject(new Error("Request body too large"));
      }
    });
    req.on("end", () => {
      if (!data.trim()) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(data));
      } catch (e) {
        reject(new Error(`Bad JSON: ${String(e?.message || e)}`));
      }
    });
    req.on("error", reject);
  });
}

function sceneItemsToText(sceneName, items) {
  const lines = [];
  lines.push("------------------------------------------------------------");
  lines.push(`OBS scene items for: ${sceneName}`);
  lines.push("Use sceneItemId in the Hammerspoon proRemote.OBS_LOOK_RULES dictionary.");
  lines.push("------------------------------------------------------------");
  for (const item of items) {
    lines.push(
      `sceneItemId=${item.sceneItemId} | index=${item.sceneItemIndex ?? ""} | enabled=${item.sceneItemEnabled}` +
      ` | sourceName=${item.sourceName ?? ""} | sourceType=${item.sourceType ?? ""} | sourceUuid=${item.sourceUuid ?? ""}`
    );
  }
  lines.push("------------------------------------------------------------");
  return `${lines.join("\n")}\n`;
}

// --- HTTP server ---
function json(res, code, obj) {
  const body = JSON.stringify(obj, null, 2);
  res.writeHead(code, {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
  });
  res.end(body);
}

function text(res, code, body) {
  res.writeHead(code, {
    "Content-Type": "text/plain; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
  });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    });
    res.end();
    return;
  }

  const u = new URL(req.url, `http://${req.headers.host}`);
  const path = u.pathname;

  try {
    if (path === "/health") {
      json(res, 200, {
        ok: true,
        obs: {
          connected: state.connected,
          identified: state.identified,
          lastError: state.lastError,
        },
        desired: state.desired,
        lastApplied: state.lastApplied,
      });
      return;
    }

    if (path === "/scene/current") {
      if (!state.identified) {
        json(res, 503, { ok: false, error: "OBS not identified", lastError: state.lastError });
        return;
      }
      try {
        const data = await getCurrentProgramScene();
        json(res, 200, data);
      } catch (e) {
        const msg = String(e?.message || e);
        state.lastError = msg;
        log(`/scene/current failed: ${msg}`);
        json(res, 500, { ok: false, error: msg });
      }
      return;
    }

    if (path === "/scene/set") {
      // GET /scene/set?name=Scene%20Name&transition=Cut&duration=500
      const name = (u.searchParams.get("name") || "").trim();
      const transition = (u.searchParams.get("transition") || "").trim();
      const durationRaw = u.searchParams.get("duration");
      const durationMs =
        durationRaw === null || String(durationRaw).trim() === "" ? null : Number(durationRaw);

      if (!name) {
        json(res, 400, { ok: false, error: "missing name" });
        return;
      }
      if (!state.identified) {
        json(res, 503, { ok: false, error: "OBS not identified", lastError: state.lastError });
        return;
      }

      try {
        if (transition) await setTransition(transition, durationMs);
        await setProgramScene(name);

        json(res, 200, {
          ok: true,
          scene: name,
          transition: transition || null,
          durationMs,
        });
      } catch (e) {
        const msg = String(e?.message || e);
        state.lastError = msg;
        log(`scene/set failed: ${msg}`);
        json(res, 500, { ok: false, error: msg });
      }
      return;
    }

    if (path === "/scene/items") {
      const sceneName = (u.searchParams.get("scene") || CONFIG.lookSceneName).trim();
      if (!state.identified) {
        json(res, 503, { ok: false, error: "OBS not identified", lastError: state.lastError });
        return;
      }
      const items = await getSceneItems(sceneName);
      json(res, 200, { ok: true, sceneName, items });
      return;
    }

    if (path === "/scene/items/text") {
      const sceneName = (u.searchParams.get("scene") || CONFIG.lookSceneName).trim();
      if (!state.identified) {
        text(res, 503, `OBS not identified: ${state.lastError}\n`);
        return;
      }
      const items = await getSceneItems(sceneName);
      text(res, 200, sceneItemsToText(sceneName, items));
      return;
    }

    if (path === "/scene/items/apply") {
      if (req.method !== "POST") {
        json(res, 405, { ok: false, error: "Method Not Allowed" });
        return;
      }
      if (!state.identified) {
        json(res, 503, { ok: false, error: "OBS not identified", lastError: state.lastError });
        return;
      }
      const payload = await parseJsonBody(req);
      const sceneName = (payload.sceneName || payload.scene || CONFIG.lookSceneName).trim();
      const applied = await applySceneItemVisibility(sceneName, payload);
      json(res, 200, { ok: true, sceneName, applied });
      return;
    }

    if (path === "/set") {
      // GET /set?mode=ann|cam|none&scene=...&srcAnn=...&srcCam=...
      const mode = (u.searchParams.get("mode") || "").toLowerCase();
      const overrides = {
        sceneName: u.searchParams.get("scene") || undefined,
        sourceAnn: u.searchParams.get("srcAnn") || undefined,
        sourceCamera: u.searchParams.get("srcCam") || undefined,
      };

      if (!["none", "ann", "cam"].includes(mode)) {
        text(res, 400, "mode must be none|ann|cam\n");
        return;
      }

      state.desired = mode;
      await applyDesired(mode, overrides);
      text(res, 200, "OK\n");
      return;
    }

    if (path === "/debug") {
      // GET /debug?refresh=1
      const refresh = u.searchParams.get("refresh") === "1";
      if (refresh && state.identified) {
        await refreshSceneData();
      }

      const cacheObj = {};
      for (const [k, v] of state.cache.entries()) cacheObj[k] = v;

      const scenes = state.lastSceneList?.scenes?.map(s => s.sceneName) || null;

      // include items only if we’ve refreshed or fetched specific scenes
      const sceneItems = {};
      for (const [sceneName, items] of state.lastSceneItems.entries()) {
        sceneItems[sceneName] = items;
      }

      json(res, 200, {
        config: CONFIG,
        obs: {
          connected: state.connected,
          identified: state.identified,
          lastConnectAttemptAt: state.lastConnectAttemptAt,
          lastConnectedAt: state.lastConnectedAt,
          lastError: state.lastError,
        },
        desired: state.desired,
        lastApplied: state.lastApplied,
        cache: cacheObj,
        scenes,
        sceneItems: Object.keys(sceneItems).length ? sceneItems : null,
        logs,
      });
      return;
    }

    text(res, 404, "Not found\n");
  } catch (e) {
    const msg = String(e?.message || e);
    state.lastError = msg;
    log(`HTTP handler error: ${msg}`);
    json(res, 500, { ok: false, error: msg });
  }
});

server.listen(CONFIG.httpPort, CONFIG.httpHost, () => {
  log(`HTTP listening on http://${CONFIG.httpHost}:${CONFIG.httpPort}`);
});

// Kick off initial connect
connectOBS().catch(() => scheduleReconnect());
