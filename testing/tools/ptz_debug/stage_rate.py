"""Run the stage motion matrix at a chosen decision interval (scratch tool)."""
import sys, json, collections
from dataclasses import replace
sys.path[:0] = ["Production_Hub_App", "."]
import testing.harness.stage_simulation as sim
from testing.harness.ptz_tracking import run_scenario

def run(dt, counts):
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
                    cam=lambda pose:sim.DelayedPanasonic(pose,delay=.15 if ml else 0,response=.12 if ml else 0)
                    rec=run_scenario(case,decision_interval=dt,camera_factory=cam,observation_transform=eff)
                    r=sim.evaluate(rec,case,dt=dt); r.pop('series',None)
                    out.append({'name':name,'count':count,'kind':kind,'profile':profile,**r})
    return out

if __name__=="__main__":
    dt=float(sys.argv[1]); counts=tuple(int(c) for c in sys.argv[2].split(",")); path=sys.argv[3]
    res=run(dt,counts); json.dump(res,open(path,"w"))
    by=collections.defaultdict(lambda:[0,0]); reasons=collections.Counter()
    for c in res:
        by[(c['count'],c['kind'])][0]+=c['passed']; by[(c['count'],c['kind'])][1]+=1
        for f in c['failures']: reasons[f]+=1
    print(f"dt={dt}: {sum(c['passed'] for c in res)}/{len(res)} passed")
    print("  "+"  ".join(f"{k[0]}p-{k[1][:8]} {v[0]}/{v[1]}" for k,v in sorted(by.items())))
    print("  reasons:", dict(reasons))
