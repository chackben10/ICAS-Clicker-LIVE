"""Run the stage motion matrix at a chosen decision interval (scratch tool)."""
import sys, json, collections
from dataclasses import replace
sys.path[:0] = ["Production_Hub_App", "."]
import testing.harness.stage_simulation as sim
from testing.harness.ptz_tracking import run_scenario

import testing.harness.ptz_tracking as harness
from production_hub.video.models import VideoSourceKey

# Adaptive mode: the automation asks the (fake) tracker for a PTZ rate; the
# PTZ detections then refresh only that often, as the live tracker would.
harness._ScriptedTracking.set_analysis_rate = lambda self, source, fps: setattr(self, "_rate", fps)


class AdaptiveObservation:
    def __init__(self, inner):
        self.inner = inner; self.last = None; self.held = None; self.fast_steps = 0; self.steps = 0
    def __call__(self, video, now, index):
        self.inner(video, now, index)
        rate = getattr(video.tracking, "_rate", None) or 4.0
        self.steps += 1; self.fast_steps += rate > 4.0
        if self.last is not None and now - self.last < 1.0 / rate - 1e-6:
            video.tracking.values[VideoSourceKey.PTZ] = self.held
        else:
            self.last = now; self.held = video.tracking.values[VideoSourceKey.PTZ]


def run(dt, counts, adaptive=False):
    sim.DT = dt
    out = []
    for count in counts:
        specs=[('still',0),('walk',.5),('walk',1.5),('walk',3.),('walk',5.),('reverse',3.),('duck',1.)]
        if count>1: specs.append(('separate_regroup',4.))
        for kind,speed in specs:
            for direction in ((-1,1) if kind in ('walk','reverse') else (1,)):
                for profile in ('ideal','video-lag','motor-lag','delayed','dropout','bad-box'):
                    name=f'{count}p-{kind}-{speed:g}-{direction:+d}-{profile}'
                    case,_=sim.make_case(name,count=count,speed=speed,direction=direction,kind=kind)
                    case=replace(case,motion_easing_enabled=True,tracking_refinements_enabled=True)
                    vl=profile in ('video-lag','delayed','dropout','bad-box'); ml=profile in ('motor-lag','delayed','dropout','bad-box')
                    eff=sim.ObservationEffects(latency=.25 if vl else 0,dropout=profile=='dropout',oversized=profile=='bad-box')
                    if adaptive: eff=AdaptiveObservation(eff)
                    cam=lambda pose:sim.DelayedPanasonic(pose,delay=.15 if ml else 0,response=.12 if ml else 0)
                    rec=run_scenario(case,decision_interval=dt,camera_factory=cam,observation_transform=eff)
                    r=sim.evaluate(rec,case,dt=dt); r.pop('series',None)
                    tail=rec.steps[-round(2/dt):]; zs=[t.zoom for t in tail]; mv=[b-a for a,b in zip(zs,zs[1:]) if b!=a]
                    r['tail_zoom_moves']=mv; r['tail_zoom_monotonic']=all(m>0 for m in mv) or all(m<0 for m in mv)
                    if adaptive: r['fast_fraction']=eff.fast_steps/max(1,eff.steps)
                    out.append({'name':name,'count':count,'kind':kind,'profile':profile,**r})
    return out

if __name__=="__main__":
    adaptive=sys.argv[1]=="adaptive"
    dt=1/12 if adaptive else float(sys.argv[1]); counts=tuple(int(c) for c in sys.argv[2].split(",")); path=sys.argv[3]
    res=run(dt,counts,adaptive); json.dump(res,open(path,"w"))
    if adaptive: print("time at fast rate: %.0f%%" % (100*sum(c['fast_fraction'] for c in res)/len(res)))
    by=collections.defaultdict(lambda:[0,0]); reasons=collections.Counter()
    for c in res:
        by[(c['count'],c['kind'])][0]+=c['passed']; by[(c['count'],c['kind'])][1]+=1
        for f in c['failures']: reasons[f]+=1
    print(f"dt={dt}: {sum(c['passed'] for c in res)}/{len(res)} passed")
    print("  "+"  ".join(f"{k[0]}p-{k[1][:8]} {v[0]}/{v[1]}" for k,v in sorted(by.items())))
    print("  reasons:", dict(reasons))
