"""Trace one stage case step by step (scratch)."""
import sys
from dataclasses import replace
sys.path[:0]=["Production_Hub_App","."]
import testing.harness.stage_simulation as sim
from testing.harness.ptz_tracking import run_scenario, build_geometry, ptz_view_box, reference_rect_to_ptz_image
from production_hub.tracking.ptz_geometry import PtzMotorPose
count,kind,speed,direction,profile,dt=int(sys.argv[1]),sys.argv[2],float(sys.argv[3]),int(sys.argv[4]),sys.argv[5],float(sys.argv[6])
every=int(sys.argv[7]) if len(sys.argv)>7 else 1
sim.DT=dt
case,_=sim.make_case("t",count=count,speed=speed,direction=direction,kind=kind)
case=replace(case,motion_easing_enabled=True,tracking_refinements_enabled=True)
vl=profile in ('video-lag','delayed','dropout','bad-box'); ml=profile in ('motor-lag','delayed','dropout','bad-box')
eff=sim.ObservationEffects(latency=.25 if vl else 0,dropout=profile=='dropout',oversized=profile=='bad-box')
cam=lambda pose:sim.DelayedPanasonic(pose,delay=.15 if ml else 0,response=.12 if ml else 0)
rec=run_scenario(case,decision_interval=dt,camera_factory=cam,observation_transform=eff)
g=build_geometry()
r=sim.evaluate(rec,case,dt=dt); print({k:v for k,v in r.items() if k!='series'})
for n,(s,truth) in enumerate(zip(rec.steps,case.all_steps)):
    if n%every: continue
    view=ptz_view_box(g,PtzMotorPose(s.pan,s.tilt,s.zoom))
    tr=" ".join(f"cx{reference_rect_to_ptz_image(p.rect,view).center[0]:.2f}/top{reference_rect_to_ptz_image(p.rect,view).y:.2f}" for p in truth)
    det=" ".join(f"{d[0]:.2f}-{d[0]+d[2]:.2f}" for d in s.ptz_detections)
    print(f"{n*dt:5.2f} truth[{tr}] det[{det}] pose {s.pan},{s.tilt},{s.zoom} spd {s.pan_speed},{s.tilt_speed},{s.zoom_speed} {s.tracking_source} want {s.desired_pose} {s.decision_state}")
