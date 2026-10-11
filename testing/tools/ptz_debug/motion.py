"""Per-frame horizontal image shift of a video (phase correlation)."""
import sys, json, cv2, numpy as np
cap=cv2.VideoCapture(sys.argv[1]); prev=None; out=[]
win=None
while True:
    ok,f=cap.read()
    if not ok: break
    g=cv2.cvtColor(cv2.resize(f,(320,180)),cv2.COLOR_BGR2GRAY).astype(np.float32)
    if win is None: win=cv2.createHanningWindow((320,180),cv2.CV_32F)
    if prev is None: out.append([0.0,0.0,0.0])
    else:
        (dx,dy),resp=cv2.phaseCorrelate(prev,g,win)
        out.append([float(dx),float(dy),float(np.mean(np.abs(g-prev)))])
    prev=g
json.dump(out,open(sys.argv[2],"w")); print(len(out),"frames")
