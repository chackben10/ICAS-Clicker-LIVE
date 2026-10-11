"""Overlay Production Hub tracking telemetry on the diagnostic recording."""
import bisect, json, sys
from datetime import datetime, timezone
import cv2, numpy as np

ptz_path, aud_path, telemetry, align_path, manifest_path, out_path, index_path = sys.argv[1:8]
def ts(s): return datetime.fromisoformat(s).timestamp()
al = json.load(open(align_path)); man = json.load(open(manifest_path))
t0, dt = al["t0"], al["dt"]
# Residuals from the high-confidence local fits (frame index -> seconds).
anchors = [(300, .10), (900, .35), (1500, .15), (2100, .35), (4500, -.35), (5700, .20)]
ax, ay = zip(*anchors)
def frame_time(i): return t0 + i * dt - float(np.interp(i, ax, ay)) * 0  # pose lags video; keep video clock
rows = [json.loads(l) for l in open(telemetry)]
rows = [r for r in rows if "automation" in r and "tracking" in r]
rt = [ts(r["wall_utc"]) for r in rows]
def row_at(t):
    k = bisect.bisect_left(rt, t)
    k = min(range(max(0, k - 1), min(len(rows), k + 1)), key=lambda j: abs(rt[j] - t))
    return rows[k]
def detections_for(t):
    # The snapshot that best describes frame time t: analysed frame captured at (poll - age).
    best = None
    k = bisect.bisect_left(rt, t)
    for j in range(max(0, k - 8), min(len(rows), k + 8)):
        p = rows[j]["tracking"]["ptz"]
        cap = rt[j] - (p.get("analysisAgeSeconds") or 0)
        if best is None or abs(cap - t) < best[0]: best = (abs(cap - t), p)
    return best[1]

cap = cv2.VideoCapture(ptz_path); aud = cv2.VideoCapture(aud_path)
na = int(aud.get(cv2.CAP_PROP_FRAME_COUNT))
a_start, a_end = ts(man["started_at"]), ts(man["ended_at"]); a_dt = (a_end - a_start) / na
writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), round(1 / dt, 3), (1280, 720))
aj, aframe = -1, None
index = []
font = cv2.FONT_HERSHEY_SIMPLEX
def put(img, text, y, color=(255, 255, 255), scale=0.55, x=12):
    cv2.putText(img, text, (x, y), font, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), font, scale, color, 1, cv2.LINE_AA)
i = 0
while True:
    ok, f = cap.read()
    if not ok: break
    t = frame_time(i)
    r = row_at(t); a = r["automation"]; d = a.get("decision") or {}
    p = detections_for(t)
    H, W = f.shape[:2]
    armed = a.get("armed")
    # centre guides and the pan dead-band (|x-0.5| <= 0.06)
    cv2.line(f, (W // 2, 0), (W // 2, H), (200, 200, 200), 1)
    cv2.rectangle(f, (int(W * .44), 0), (int(W * .56), H), (120, 120, 120), 1)
    off = []
    for s in p["subjects"]:
        b = s["bounds"]; x0, y0 = int(b["x"] * W), int(b["y"] * H); x1, y1 = int((b["x"] + b["width"]) * W), int((b["y"] + b["height"]) * H)
        cx = b["x"] + b["width"] / 2; off.append(abs(cx - .5))
        col = (0, 220, 0) if abs(cx - .5) <= .15 else (0, 200, 255) if abs(cx - .5) <= .30 else (0, 0, 255)
        cv2.rectangle(f, (x0, y0), (x1, y1), col, 2)
        put(f, f"#{s['id']} {s['confidence']:.2f}", max(18, y0 - 6), col, .5, x0 + 2)
    ap = a.get("actualPose") or {}; dp = d.get("desiredPose") or {}
    clock = datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S.%f")[:-5]
    state = f"{a.get('state')} / {a.get('trackingSource') or '-'}"
    scol = (0, 220, 0) if a.get("state") == "armed_tracking" and a.get("trackingSource") == "ptz" else (0, 200, 255) if armed else (180, 180, 180)
    put(f, f"{clock} UTC   frame {i}", 24)
    put(f, state, 48, scol)
    put(f, (a.get("message") or "")[:95], 70, (220, 220, 220), .45)
    put(f, f"pan  {ap.get('pan', '-')}  want {dp.get('pan', '-')}   tilt {ap.get('tilt', '-')}  want {dp.get('tilt', '-')}", H - 44)
    put(f, f"zoom {ap.get('zoom', '-')}  want {dp.get('zoom', '-')}   PTZ analysis {p['analysisFps']:.1f}/s age {(p.get('analysisAgeSeconds') or 0):.2f}s   cmds {a.get('commandsSent')}", H - 20)
    # Audience inset
    want = int((t - a_start) / a_dt)
    while aj < want:
        ok2, fr = aud.read()
        if not ok2: break
        aj += 1; aframe = fr
    if aframe is not None:
        inset = cv2.resize(aframe, (384, 216)).copy()
        for s in r["tracking"]["audience"]["subjects"]:
            b = s["bounds"]
            cv2.rectangle(inset, (int(b["x"] * 384), int(b["y"] * 216)), (int((b["x"] + b["width"]) * 384), int((b["y"] + b["height"]) * 216)), (255, 200, 0), 1)
        f[8:224, W - 392:W - 8] = inset
        cv2.rectangle(f, (W - 392, 8), (W - 8, 224), (255, 255, 255), 1)
    writer.write(f)
    index.append({"frame": i, "t": t, "state": a.get("state"), "source": a.get("trackingSource"), "n": len(p["subjects"]), "max_off": max(off) if off else None, "armed": bool(armed)})
    i += 1
writer.release()
json.dump(index, open(index_path, "w"))
print(i, "frames written")
