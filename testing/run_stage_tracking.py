#!/usr/bin/env python3
"""Run stage-specific acceptance gates. Exit 1 = behavior failure; 2 = preflight error.

This never opens the app, sends PTZ commands to hardware, or changes profiles.
"""
from __future__ import annotations
import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from testing.harness.stage_corpus import CORPUS,load_manifest,score_clip,sha256


def replay(clips, *, video, cache, speed=1, fixture_root=CORPUS):
    import cv2
    from PySide6.QtGui import QImage
    from production_hub.tracking.apple_vision import AppleVisionPersonDetector
    from production_hub.tracking.association import SubjectAssociator
    detector=AppleVisionPersonDetector()
    output=[]
    def rect(b):return [b.x,b.y,b.width,b.height]
    for clip in clips:
        rows=[];associator=SubjectAssociator();capture=None
        if video:
            path=cache/(clip['id']+'.mp4')
            if not path.is_file() or sha256(path)!=clip['media_sha256']:
                raise ValueError(f"Missing or changed video: {path}. Fetch/verify corpus; no skip allowed.")
            capture=cv2.VideoCapture(str(path))
            times=[i*speed/4 for i in range(int(clip['duration_seconds']*4/speed))]
            times=sorted(set(times+[f['t'] for f in clip['annotations']]))
        else:
            times=[f['t'] for f in clip['annotations']]
        try:
            for t in times:
                if capture is not None:
                    capture.set(cv2.CAP_PROP_POS_MSEC,t*1000);ok,bgr=capture.read()
                    if not ok:raise ValueError(f"Undecodable frame {clip['id']}:{t}")
                    rgb=cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB);h,w=rgb.shape[:2]
                    image=QImage(rgb.data,w,h,rgb.strides[0],QImage.Format.Format_RGB888).copy()
                else:
                    frame=next(f for f in clip['annotations'] if f['t']==t)
                    image=QImage(str(fixture_root/frame['image']))
                    if image.isNull():raise ValueError('Undecodable image fixture')
                started=time.perf_counter();candidates=detector.detect(image)
                row={'t':t,'latency_ms':(time.perf_counter()-started)*1000,'detections':[{'bounds':rect(c.bounds),'confidence':c.confidence} for c in candidates]}
                if video:
                    tracks=associator.update(candidates,observed_monotonic=100+t/speed)
                    row['tracks']=[{'bounds':rect(s.bounds),'id':s.track_id,'last_seen':s.last_seen_monotonic-100} for s in tracks if s.last_seen_monotonic>=100+t/speed-.01]
                rows.append(row)
        finally:
            if capture is not None:capture.release()
        output.append((clip,rows))
        print(f"Replayed {clip['id']} at {speed}x: {len(rows)} frames",flush=True)
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=['source','installed'],default='source')
    parser.add_argument('--bundle',type=Path,default=Path('/Applications/Production Hub.app'))
    parser.add_argument('--lane',choices=['pixels','video','motion','all'],default='all')
    parser.add_argument('--counts',default='1,2,3')
    parser.add_argument('--cache',type=Path,default=ROOT/'testing/benchmarks/outputs/stage-corpus')
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    report={'schema':1,'run_utc':datetime.now(timezone.utc).isoformat(),'harness_sha256':{p.name:sha256(p) for p in [Path(__file__), ROOT/'testing/harness/ptz_tracking.py',ROOT/'testing/harness/stage_simulation.py',ROOT/'testing/harness/stage_corpus.py',ROOT/'testing/harness/installed_tracking.py']},'physical_camera_test':False,'environment':{'python':sys.version,'platform':platform.platform()},'results':[]}
    exit_code=2
    try:
        counts=tuple(int(c) for c in args.counts.split(','))
        if not counts or any(c not in (1,2,3) for c in counts):raise ValueError('Counts must be 1,2,3')
        if args.backend=='installed' and counts!=(1,):raise ValueError('Installed-release acceptance is solo only; use --counts 1')
        if args.backend=='installed':
            from testing.harness.installed_tracking import use_installed_release
            report['implementation']=use_installed_release(args.bundle)
        else:
            sys.path.insert(0,str(ROOT/'Production_Hub_App'))
            files=sorted((ROOT/'Production_Hub_App/production_hub/tracking').glob('*.py'))
            report['implementation']={'kind':'source-dev-code-offline','module_sha256':{p.name:sha256(p) for p in files},'dev_refinements':True}
        manifest=load_manifest();report['manifest_sha256']=sha256(CORPUS/'manifest.json')
        report['requested_counts']=counts;report['lane']=args.lane
        report['thresholds']=manifest['thresholds']
        report['interpretation']='Video scores only independently annotated instants. Motion cases use assumed camera calibration and delay, not measured hardware response. A pass does not authorize release.'
        clips=[c for c in manifest['clips'] if c['target_count'] in counts]
        if args.lane in ('pixels','video','all'):
            videos=args.lane in ('video','all')
            for speed in ((1,2,4) if videos else (1,)):
                for clip,rows in replay(clips,video=videos,cache=args.cache,speed=speed):
                    result=score_clip(clip,rows,manifest['thresholds']);result['lane']='video' if videos else 'pixels'
                    result['speed_multiplier']=speed
                    result['identity_evaluated']=videos
                    result['evaluated_frames']=len(clip['annotations']);result['processed_frames']=len(rows)
                    result['source_url']=clip['url']+'&t='+str(clip['start_seconds'])
                    result['observations']=rows
                    report['results'].append(result)
        if args.lane in ('motion','all'):
            from testing.harness.stage_simulation import run_matrix
            results=run_matrix(counts=counts,dev=args.backend=='source')
            for result in results:result['lane']='modeled-motion'
            report['results'].extend(results)
        failed=sum(not r['passed'] for r in report['results'])
        report['summary']={'cases':len(report['results']),'passed':len(report['results'])-failed,'failed':failed}
        report['status']='failed' if failed else 'passed';exit_code=int(bool(failed))
    except Exception as exc:
        import traceback
        report['status']='preflight_or_execution_error';report['error']=str(exc)
        report['traceback']=traceback.format_exc()
        print(report['error'],file=sys.stderr)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],**report.get('summary',{}),'report':str(args.report)}),flush=True)
    return exit_code

if __name__=='__main__':raise SystemExit(main())
