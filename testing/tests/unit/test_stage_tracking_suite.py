"""Tests of the acceptance oracles, not a claim that tracking passes them.

Behavioral gates run through testing/run_stage_tracking.py and remain red when
current tracking fails. These tests prevent an incorrect harness manufacturing
a pass (or a failure) independently of the implementation under test.
"""
import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from testing.harness.stage_corpus import CORPUS, load_manifest, match_frame, score_clip, validate_manifest
from testing.harness.stage_simulation import (
    DT, DelayedPanasonic, ObservationEffects, evaluate, make_case,
)
from testing.harness.ptz_tracking import (
    SimulatedPanasonic, SimulatedVideo, _detector_snapshot, run_scenario,
)
from production_hub.tracking.models import NormalizedRect
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.video.models import VideoSourceKey


@pytest.fixture
def manifest():
    return load_manifest()


def test_stage_corpus_has_independent_real_images_and_all_subject_counts(manifest):
    assert {c['target_count'] for c in manifest['clips']} == {1,2,3}
    assert len(manifest['clips']) >= 7
    assert sum(len(c['annotations']) for c in manifest['clips']) >= 40
    assert sum(c['duration_seconds'] for c in manifest['clips']) >= 160
    assert all(c['url'].startswith('https://www.youtube.com/watch?v=') for c in manifest['clips'])


@pytest.mark.parametrize('mutation', ['date','time','identity','checksum','path','empty'])
def test_corpus_validation_fails_closed(manifest,mutation):
    m=copy.deepcopy(manifest);c=m['clips'][0];f=c['annotations'][0]
    if mutation=='date':c['published']='20200101'
    elif mutation=='time':f['t']=c['duration_seconds']+1
    elif mutation=='identity':f['subjects'].append(f['subjects'][0])
    elif mutation=='checksum':f['sha256']='0'*64
    elif mutation=='path':f['image']='../../../../README.md'
    elif mutation=='empty':c['annotations']=[]
    with pytest.raises(ValueError):validate_manifest(m)


def perfect_rows(clip):
    return [{'t':f['t'],'detections':[{'bounds':p['body']} for p in f['subjects']],
             'tracks':[{'bounds':p['body'],'id':i} for i,p in enumerate(f['subjects'])]}
            for f in clip['annotations']]


def test_independent_perfect_detections_pass_for_each_clip(manifest):
    for clip in manifest['clips']:
        result=score_clip(clip,perfect_rows(clip),manifest['thresholds'])
        assert result['passed'],(clip['id'],result)


def test_union_box_cannot_count_as_multiple_people(manifest):
    people=[{'id':str(i),'body':[.1+i*.3,.2,.1,.5],'head':[.12+i*.3,.2,.05,.1]} for i in range(3)]
    result=match_frame(people,[{'bounds':[0,0,1,1]}],manifest['thresholds'])
    assert len(result['missed'])==3
    assert result['merged']==[0]
    assert result['oversized']==[0]


def test_duplicate_boxes_cannot_replace_a_missing_person(manifest):
    clip=next(c for c in manifest['clips'] if c['id']=='june-dual')
    rows=perfect_rows(clip)
    for row in rows:row['detections']=[row['detections'][0]]*2
    result=score_clip(clip,rows,manifest['thresholds'])
    assert not result['passed'] and result['recall']==.5


def test_missing_frame_is_error_not_zero_or_skip(manifest):
    clip=manifest['clips'][0]
    with pytest.raises(ValueError,match='Missing result'):
        score_clip(clip,perfect_rows(clip)[:-1],manifest['thresholds'])


def test_identity_switch_within_shot_fails_but_edit_boundary_does_not(manifest):
    clip=next(c for c in manifest['clips'] if c['id']=='september-single')
    rows=perfect_rows(clip)
    for row in rows:
        if row['t']>=12:row['tracks'][0]['id']=9
    assert score_clip(clip,rows,manifest['thresholds'])['identity_switches_at_labelled_instants']==0
    rows[-1]['tracks'][0]['id']=10
    assert 'identity_switch' in score_clip(clip,rows,manifest['thresholds'])['failures']


def test_motor_latency_applies_no_motion_before_command_arrives():
    pose=PtzMotorPose(43000,34000,3000)
    camera=DelayedPanasonic(pose,delay=.2,response=0)
    asyncio.run(camera.pan_tilt(99,1))
    camera.advance(.2)
    assert camera.pose==pose
    camera.advance(.1)
    expected=SimulatedPanasonic(pose,measured_rates=True)
    asyncio.run(expected.pan_tilt(99,1));expected.advance(.1)
    assert camera.pose==expected.pose
    assert camera.pose.pan<pose.pan and camera.pose.tilt>pose.tilt


def test_zero_delay_motor_matches_existing_integrator():
    pose=PtzMotorPose(43000,34000,3000)
    cameras=[DelayedPanasonic(pose,delay=0,response=0),SimulatedPanasonic(pose,measured_rates=True)]
    for camera in cameras:
        asyncio.run(camera.pan_tilt(70,35));asyncio.run(camera.zoom(45));camera.advance(.25)
    # Pan/tilt rates depend on zoom, which changes during the move; the
    # delayed camera integrates in 10 ms steps, so allow rounding-level drift.
    a,b=cameras[0].pose,cameras[1].pose
    assert abs(a.pan-b.pan)<=2 and abs(a.tilt-b.tilt)<=2 and a.zoom==b.zoom


def test_stop_command_stops_latched_motion_after_latency():
    camera=DelayedPanasonic(PtzMotorPose(43000,34000,3000),delay=.15,response=0)
    asyncio.run(camera.pan_tilt(70,65));camera.advance(.5)
    asyncio.run(camera.stop_pan_tilt());camera.advance(.3)
    pose=camera.pose;camera.advance(1)
    assert camera.pose==pose


def test_feedback_delay_keeps_capture_timestamp_and_never_uses_future_frame():
    video=SimulatedVideo();effects=ObservationEffects(latency=.25)
    for i in range(6):
        now=100+i*DT
        snapshot=_detector_snapshot(VideoSourceKey.PTZ,[(1,NormalizedRect(i*.1, .2,.1,.3),.9,5)],now)
        video.tracking.values[VideoSourceKey.PTZ]=snapshot
        effects(video,now,i)
        result=video.tracking.values[VideoSourceKey.PTZ]
        assert result.last_analysis_monotonic==100+max(0,i-2)*DT
        assert result.subjects[0].bounds.x==pytest.approx(max(0,i-2)*.1)


@pytest.mark.parametrize('count',[1,2,3])
def test_speed_units_and_stop_are_independent_of_detection(count):
    case,frames=make_case('fast',count=count,speed=5,kind='walk')
    p0=frames[round(3/DT)][0];p1=frames[round(4/DT)][0]
    assert (p1.x-p0.x)/p0.width==pytest.approx(5)
    assert frames[-1]==frames[-16]
    assert len(case.all_steps)==len(frames)+case.warmup_steps


def test_retention_oracle_rejects_a_frozen_camera_even_with_perfect_claimed_boxes():
    case,_=make_case('walk',speed=5)
    pose=case.initial_pose
    record=SimpleNamespace(pan=pose.pan,tilt=pose.tilt,zoom=pose.zoom,pan_speed=50,tilt_speed=50,zoom_speed=50,ptz_detections=[(.4,.2,.2,.6)])
    recording=SimpleNamespace(armed=True,steps=[record]*len(case.all_steps))
    result=evaluate(recording,case)
    assert 'lost_longer_than_500ms' in result['failures']
    assert result['retention']<.98


def test_framing_oracle_rejects_a_permanently_wide_shot():
    case,_=make_case('still',kind='still')
    pose=case.initial_pose
    record=SimpleNamespace(pan=pose.pan,tilt=pose.tilt,zoom=1500,pan_speed=50,tilt_speed=50,zoom_speed=50)
    result=evaluate(SimpleNamespace(armed=True,steps=[record]*len(case.all_steps)),case)
    assert result['retention']==1
    assert 'final_framing_too_wide' in result['failures']


def test_stationary_positive_control_passes_with_real_supervisor():
    case,_=make_case('stationary-positive-control',kind='still')
    recording=run_scenario(case,decision_interval=DT,camera_factory=lambda pose:DelayedPanasonic(pose,delay=0,response=0))
    assert evaluate(recording,case)['passed']


def test_incomplete_execution_is_not_a_passing_scenario():
    case,_=make_case('still',kind='still')
    assert not evaluate(SimpleNamespace(armed=True,steps=[]),case)['passed']


@pytest.mark.parametrize('bad',[[float('nan'),0,.1,.2],[0,0,-1,.2],[0,0,0,0]])
def test_invalid_detector_output_cannot_pass(manifest,bad):
    with pytest.raises(ValueError,match='Invalid detector'):
        match_frame(manifest['clips'][0]['annotations'][0]['subjects'],[{'bounds':bad}],manifest['thresholds'])


def test_release_loader_refuses_to_mix_already_imported_source():
    from testing.harness.installed_tracking import use_installed_release
    with pytest.raises(RuntimeError,match='before importing'):
        use_installed_release('/Applications/Production Hub.app')


@pytest.mark.parametrize('interval',[0,-.25,float('nan')])
def test_invalid_simulation_clock_is_rejected(interval):
    case,_=make_case('still',kind='still')
    with pytest.raises(ValueError,match='interval'):
        run_scenario(case,decision_interval=interval)


@pytest.mark.parametrize('count',[1,2,3])
def test_footage_paths_are_anchored_to_manual_head_positions(count):
    from testing.harness.stage_simulation import recorded_case
    from testing.harness.stage_corpus import load_manifest
    case,evidence=recorded_case('footage',count=count,playback=4)
    clip=next(c for c in load_manifest()['clips'] if c['id']==evidence['clip'])
    first=next(f for f in clip['annotations'] if f['t']==evidence['annotated_seconds'][0])
    last=next(f for f in clip['annotations'] if f['t']==evidence['annotated_seconds'][-1])
    end=case.steps[-1]
    for person in end:
        start_x=next(p['head'][0]+p['head'][2]/2 for p in first['subjects'] if p['id']==person.name)
        final_x=next(p['head'][0]+p['head'][2]/2 for p in last['subjects'] if p['id']==person.name)
        starting=next(p for p in case.steps[0] if p.name==person.name)
        assert person.x-starting.x==pytest.approx((final_x-start_x)*evidence['projection_footprint_assumption'])
    assert not evidence['physical_world_motion_measured']


def test_footage_acceleration_changes_time_not_path():
    from testing.harness.stage_simulation import recorded_case
    slow,_=recorded_case('slow',count=1,playback=1)
    fast,_=recorded_case('fast',count=1,playback=16)
    assert fast.steps[-1][0].x==pytest.approx(slow.steps[-1][0].x)
    assert len(fast.steps)<len(slow.steps)


def test_stage_camera_moves_the_picture_at_the_measured_aw_he120_speed():
    # Stage cases judge reach against the real camera, which slows pan as it
    # zooms in: the same command must move the *picture* at the measured rate.
    from production_hub.integrations.panasonic_awp import speed_model
    from testing.harness.ptz_tracking import build_geometry
    geometry=build_geometry()
    for zoom in (2238,3500):
        pose=PtzMotorPose(43000,34000,zoom)
        camera=DelayedPanasonic(pose,delay=0,response=0)
        asyncio.run(camera.pan_tilt(76,50));camera.advance(.5)
        span,_=geometry.image_axis_motor_units(pose)
        frames_per_second=(pose.pan-camera.pose.pan)/span/.5
        assert abs(frames_per_second-speed_model.picture_speed('pan',26,zoom))<.02
