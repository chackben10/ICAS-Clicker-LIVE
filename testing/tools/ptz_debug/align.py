import json,sys,numpy as np
from datetime import datetime
def ts(s): return datetime.fromisoformat(s).timestamp()
mot=np.array(json.load(open(sys.argv[1])))
rows=[json.loads(l) for l in open(sys.argv[2])]
start=ts(sys.argv[3]); end=ts(sys.argv[4])
T=[];P=[]
for r in rows:
    a=r.get("automation") or {}; ap=a.get("actualPose")
    if ap: T.append(ts(r["wall_utc"])); P.append(ap["pan"])
T=np.array(T);P=np.array(P,float)
# pan speed (units/s), smoothed over 0.5 s
grid=np.arange(T[0],T[-1],0.05); pg=np.interp(grid,T,P)
v=np.abs(np.gradient(pg,0.05)); k=np.ones(10)/10; v=np.convolve(v,k,"same")
dx=np.abs(mot[:,0]); dx=np.convolve(dx,np.ones(5)/5,"same")
N=len(dx); idx=np.arange(N)
def score(t0,dt,sel=slice(None)):
    t=t0+idx[sel]*dt; vv=np.interp(t,grid,v,left=np.nan,right=np.nan); m=~np.isnan(vv)
    if m.sum()<100: return -1
    return np.corrcoef(dx[sel][m],vv[m])[0,1]
nominal=(end-start)/N
best=max(((score(start+o,nominal*s),o,s) for o in np.arange(-3,3,0.05) for s in np.arange(0.97,1.03,0.002)))
print(f"global fit: r={best[0]:.3f} offset={best[1]:+.2f}s dt={nominal*best[2]*1000:.2f}ms (nominal {nominal*1000:.2f}ms)")
t0=start+best[1]; dt=nominal*best[2]
# local residual offsets in 600-frame windows
for w0 in range(0,N-600,600):
    sel=slice(w0,w0+600)
    r,o=max((score(t0+o,dt,sel),o) for o in np.arange(-1.5,1.5,0.05))
    print(f"  frames {w0:5}-{w0+600:5} ({datetime.utcfromtimestamp(t0+w0*dt).strftime('%H:%M:%S')}): local r={r:.2f} residual {o:+.2f}s")
json.dump({"t0":t0,"dt":dt},open(sys.argv[5],"w"))
