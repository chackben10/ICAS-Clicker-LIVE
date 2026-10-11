"""Stage-inspired robustness tests, distinct from actual video detector replays.

Motor delay and response are sensitivity assumptions, not measured calibration.
All scoring uses independent subject truth, never the detector's claimed boxes.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
import math
from production_hub.tracking.models import NormalizedRect
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.video.models import VideoSourceKey
from testing.harness.ptz_tracking import (
    SimulatedPanasonic, build_geometry, ptz_view_box, reference_rect_to_ptz_image,
    run_scenario, scenario,
)

DT = .125


@dataclass(frozen=True)
class StagePerson:
    name: str
    x: float = .40
    y: float = .57
    width: float = .012
    height: float = .030
    confidence: float = .92
    seen_by_ptz: bool = True
    seen_by_audience: bool = True
    detector_suffix: int = 0
    @property
    def rect(self):
        return NormalizedRect(self.x-self.width/2,self.y-self.height/2,self.width,self.height)


class DelayedPanasonic(SimulatedPanasonic):
    def __init__(self, pose, *, delay=.15, response=.12, motor_scale=1.0):
        super().__init__(pose)
        self.elapsed=0.;self.delay=delay;self.response=response;self.motor_scale=motor_scale
        self.pending=[];self.requested=[50.,50.,50.]
    async def pan_tilt(self,pan,tilt):
        self.pan_tilt_speed_commands.append((pan,tilt))
        self.pending.append((self.elapsed+self.delay,'pan_tilt',(pan,tilt)))
        return True
    async def zoom(self,zoom):
        self.zoom_speed_commands.append(zoom)
        self.pending.append((self.elapsed+self.delay,'zoom',(zoom,)))
        return True
    async def stop_pan_tilt(self):
        return await self.pan_tilt(50,50)
    async def stop_zoom(self):
        return await self.zoom(50)
    def advance(self, seconds):
        remaining=seconds
        while remaining>1e-9:
            dt=min(.01,remaining)
            ready=[p for p in self.pending if p[0]<=self.elapsed+1e-9]
            self.pending=[p for p in self.pending if p[0]>self.elapsed+1e-9]
            for _,kind,args in ready:
                if kind=='zoom':self.requested[2]=args[0]
                else:self.requested[:2]=args
            future = [p[0]-self.elapsed for p in self.pending if p[0]>self.elapsed+1e-9]
            if future:
                dt=min(dt,min(future))
            alpha=1 if self.response<=0 else 1-math.exp(-dt/self.response)
            for field,target in zip(('pan_speed','tilt_speed','zoom_speed'),self.requested):
                current=getattr(self._state,field)
                setattr(self._state,field,current+(target-current)*alpha)
            super().advance(dt*self.motor_scale)
            self.elapsed+=dt
            remaining-=dt


class ObservationEffects:
    def __init__(self, *, latency=.25, dropout=False, oversized=False):
        self.latency=latency;self.dropout=dropout;self.oversized=oversized
        self.history=[];self.start=None
    def __call__(self, video, now, index):
        if self.start is None:self.start=now
        snapshots={k:v.copy() for k,v in video.tracking.values.items()}
        self.history.append((now,snapshots))
        available=[item for item in self.history if item[0]<=now-self.latency+1e-8]
        chosen=(available[-1] if available else self.history[0])[1]
        for key,snapshot in chosen.items():
            value=snapshot.copy()
            elapsed=now-self.start
            if self.dropout and 4 <= elapsed < 4.5:
                value.subjects=()
            if self.oversized and 4 <= elapsed < 4.5 and key==VideoSourceKey.PTZ:
                value.subjects=tuple(replace(p,bounds=NormalizedRect(.05,.05,.90,.90)) for p in value.subjects)
            video.tracking.values[key]=value
        self.history=[item for item in self.history if item[0]>=now-max(2.,self.latency+1)]


def make_case(name, *, count=1, speed=1., direction=1, kind='walk', zoom=3450, hold=8.):
    """Speeds are body widths/second, not unmeasured stage metres/second."""
    start=[StagePerson(str(i),x=.40+(i-(count-1)/2)*.018) for i in range(count)]
    frames=[]
    move_seconds=3.
    for i in range(round((3+move_seconds+hold)/DT)):
        t=i*DT;progress=min(move_seconds,max(0,t-3))
        distance=direction*speed*start[0].width*progress
        people=[]
        for j,p in enumerate(start):
            x=p.x;y=p.y;height=p.height
            if kind=='walk':x+=distance
            elif kind=='reverse':x+=direction*speed*p.width*(progress if progress<1.5 else 3-progress)
            elif kind=='duck':y+=.008*math.sin(math.pi*progress/3)
            elif kind=='separate_regroup':x+=(j-(count-1)/2)*speed*p.width*math.sin(math.pi*progress/3)
            elif kind!='still':raise ValueError(kind)
            people.append(replace(p,x=x,y=y,height=height))
        frames.append(tuple(people))
    # Same stage coordinates as the legacy calibration, but tight shot at the
    # 3450 zoom region reported in the live failures, not only wide shots.
    initial=PtzMotorPose(round(46000-.40*9000),round(32000+.57*5200),zoom)
    if count>1:
        from testing.harness.ptz_tracking import pose_framing
        initial=pose_framing(start,occupancy=.78)
    return scenario(name,frames,initial_pose=initial,warmup_steps=8),frames


def evaluate(recording, case, *, dt=DT):
    if not recording.armed or len(recording.steps)!=len(case.all_steps):
        return {'passed':False,'failures':['arm_or_incomplete_run']}
    geometry=build_geometry();lost=0.;worst_lost=0.;retained=0;total=0;series=[]
    warmup=case.warmup_steps+round(2/dt)
    for n,(record,truth) in enumerate(zip(recording.steps,case.all_steps)):
        if n<warmup:continue
        view=ptz_view_box(geometry,PtzMotorPose(record.pan,record.tilt,record.zoom))
        visible=True
        for person in truth:
            b=reference_rect_to_ptz_image(person.rect,view)
            # Upper-body visibility + head clearance, independent of emitted detections.
            fraction=b.clamped().area/max(b.area,1e-12)
            okay=fraction>=.90 and b.y>=.015 and b.y<.8 and .015<=b.center[0]<=.985
            retained+=int(okay);total+=1;visible &= okay
        lost=0 if visible else lost+dt;worst_lost=max(worst_lost,lost)
        series.append({'step':n,'all_people_visible':visible,'zoom':record.zoom,'pan':record.pan,'tilt':record.tilt,'command':[record.pan_speed,record.tilt_speed,record.zoom_speed]})
    tail=recording.steps[-round(2/dt):]
    stopped=all(abs(r.pan_speed-50)<=1 and abs(r.tilt_speed-50)<=1 for r in tail)
    settled_zoom=max(r.zoom for r in tail)-min(r.zoom for r in tail)
    tail_pan_range=max(r.pan for r in tail)-min(r.pan for r in tail)
    tail_tilt_range=max(r.tilt for r in tail)-min(r.tilt for r in tail)
    final_view=ptz_view_box(geometry,PtzMotorPose(tail[-1].pan,tail[-1].tilt,tail[-1].zoom))
    final_heights=[p.rect.height/final_view.height for p in case.all_steps[-1]]
    minimum_height=.4 if len(final_heights)==1 else .25
    # Pose drift also catches a latched moving motor after a missing stop.
    failures=[];retention=retained/max(1,total)
    if retention<.98:failures.append('subject_retention_below_98pct')
    if worst_lost>.5+1e-8:failures.append('lost_longer_than_500ms')
    if not stopped or max(tail_pan_range,tail_tilt_range)>15:failures.append('not_settled_after_stop')
    if min(final_heights)<minimum_height:failures.append('final_framing_too_wide')
    if settled_zoom>30:failures.append('zoom_hunting_after_stop')
    return {'passed':not failures,'failures':failures,'retention':retention,'longest_loss_seconds':worst_lost,'settled':stopped,'tail_pan_range':tail_pan_range,'tail_tilt_range':tail_tilt_range,'final_subject_heights':final_heights,'tail_zoom_range':settled_zoom,'series':series}


def run_matrix(*, counts=(1,), dev=False):
    results=[]
    for count in counts:
        specs=[('still',0),('walk',.5),('walk',1.5),('walk',3.),('walk',5.),('reverse',3.),('duck',1.)]
        if count>1:specs.append(('separate_regroup',4.))
        for kind,speed in specs:
            for direction in ((-1,1) if kind in ('walk','reverse') else (1,)):
                for profile in ('ideal','video-lag','motor-lag','delayed','dropout','bad-box'):
                    name=f'{count}p-{kind}-{speed:g}bwps-{direction:+d}-{profile}'
                    case,_=make_case(name,count=count,speed=speed,direction=direction,kind=kind)
                    case=replace(case,motion_easing_enabled=dev,tracking_refinements_enabled=dev)
                    video_lag=profile in ('video-lag','delayed','dropout','bad-box')
                    motor_lag=profile in ('motor-lag','delayed','dropout','bad-box')
                    effects=ObservationEffects(latency=.25 if video_lag else 0,dropout=profile=='dropout',oversized=profile=='bad-box')
                    camera=lambda pose:DelayedPanasonic(pose,delay=.15 if motor_lag else 0,response=.12 if motor_lag else 0)
                    recording=run_scenario(case,decision_interval=DT,camera_factory=camera,observation_transform=effects)
                    results.append({'name':name,'count':count,'kind':kind,'speed_body_widths_per_second':speed,'profile':profile,'model_assumptions':{'observation_latency_s':effects.latency,'command_delay_s':.15 if motor_lag else 0,'motor_response_s':.12 if motor_lag else 0,'initial_zoom':case.initial_pose.zoom},**evaluate(recording,case)})
        for playback in (1,4,16):
            for profile in ('ideal','delayed'):
                name=f'{count}p-footage-{playback}x-{profile}'
                case,evidence=recorded_case(name,count=count,playback=playback)
                case=replace(case,motion_easing_enabled=dev,tracking_refinements_enabled=dev)
                delayed=profile=='delayed'
                effects=ObservationEffects(latency=.25 if delayed else 0)
                camera=lambda pose:DelayedPanasonic(pose,delay=.15 if delayed else 0,response=.12 if delayed else 0)
                recording=run_scenario(case,decision_interval=DT,camera_factory=camera,observation_transform=effects)
                results.append({'name':name,'count':count,'kind':'source-guided-footage',
                                'profile':profile,'evidence':evidence,
                                'model_assumptions':{'observation_latency_s':effects.latency,
                                  'command_delay_s':.15 if delayed else 0,
                                  'motor_response_s':.12 if delayed else 0,
                                  'initial_zoom':case.initial_pose.zoom},
                                **evaluate(recording,case)})
    return results


def recorded_case(name, *, count, playback=1, hold=6.):
    """Convert labelled image trajectories into a local reference-space stress path.

    The initial broadcast crop is held fixed for projection. This tests the
    observed relative body movement/duck; it is not measured physical PTZ pose.
    For solo, the moving person in the August pair is the selected target.
    """
    from testing.harness.stage_corpus import load_manifest
    from testing.harness.ptz_tracking import pose_framing

    if count not in (1, 2, 3) or playback <= 0:
        raise ValueError('Count must be 1, 2, or 3 and playback must be positive')
    clip_id = 'august-dual-walk' if count in (1, 2) else 'may-triple'
    clip = next(c for c in load_manifest()['clips'] if c['id'] == clip_id)
    if count in (1, 2):
        chosen = [a for a in clip['annotations'] if a['t'] in (0, 4, 8)]
        names = ('lectern',) if count == 1 else ('lectern', 'right')
        footprint = .065
    else:
        chosen = [a for a in clip['annotations'] if a['t'] in (4, 8, 12, 16, 24)]
        names = ('left', 'middle', 'right')
        footprint = .09
    if len(chosen) < 3:
        raise ValueError('Insufficient source annotations')
    base_t = chosen[0]['t']
    def head_center(frame, name):
        person = next(p for p in frame['subjects'] if p['id'] == name)
        x, y, w, h = person['head']
        return x + w/2, y + h/2
    baseline = [head_center(chosen[0], name) for name in names]
    first = [StagePerson(name, x=.40+(x-.5)*footprint,
                         y=.57+(y-.25)*footprint,
                         width=.012,height=.030)
             for name,(x,y) in zip(names,baseline)]
    frames=[]
    duration=(chosen[-1]['t']-base_t)/playback
    prelude=2.
    for i in range(round((prelude+duration+hold)/DT)):
        t=max(0,min(chosen[-1]['t']-base_t,(i*DT-prelude)*playback))
        before=chosen[0];after=chosen[-1]
        for left,right in zip(chosen,chosen[1:]):
            if left['t']-base_t <= t <= right['t']-base_t:
                before,after=left,right
                break
        alpha=(t-(before['t']-base_t))/(after['t']-before['t']) if before!=after else 0
        people=[]
        for person in first:
            x0,y0=head_center(before,person.name);x1,y1=head_center(after,person.name)
            ox,oy=baseline[names.index(person.name)]
            x=x0+(x1-x0)*alpha
            y=y0+(y1-y0)*alpha
            people.append(replace(person,
                x=person.x+(x-ox)*footprint,
                y=person.y+(y-oy)*footprint))
        frames.append(tuple(people))
    case=scenario(name,frames,initial_pose=pose_framing(first,occupancy=.78),warmup_steps=8)
    return case,{'clip':clip_id,'url':clip['url']+'&t='+str(clip['start_seconds']+base_t),
                 'annotated_seconds':[f['t'] for f in chosen],
                 'playback_multiplier':playback,
                 'projection_footprint_assumption':footprint,
                 'physical_world_motion_measured':False}
