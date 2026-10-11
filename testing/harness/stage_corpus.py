"""Independent, sparse human-readable ground truth for ICAS stage footage.

No detector outputs are used as expected values. Unlabelled times are not scored.
Missing media/labels are errors, never skips or successes.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from datetime import datetime
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / 'fixtures/tracking/stage'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_manifest(manifest, root=CORPUS):
    start = datetime.fromisoformat(manifest['window']['from']).date()
    end = datetime.fromisoformat(manifest['window']['through']).date()
    ids = set()
    if not manifest['clips']:
        raise ValueError('Empty corpus')
    for clip in manifest['clips']:
        if clip['id'] in ids:
            raise ValueError('Duplicate clip ID')
        ids.add(clip['id'])
        date = datetime.strptime(clip['published'], '%Y%m%d').date()
        if not start <= date <= end:
            raise ValueError('Source outside review window')
        previous = -1
        for frame in clip['annotations']:
            if not previous < frame['t'] < clip['duration_seconds']:
                raise ValueError('Unordered or out-of-range annotation')
            previous = frame['t']
            path = (root / frame['image']).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError('Missing or unsafe fixture path')
            if sha256(path) != frame['sha256']:
                raise ValueError('Fixture checksum mismatch')
            people = frame['subjects']
            if len(people) != clip['target_count'] or len({s['id'] for s in people}) != len(people):
                raise ValueError('Ambiguous target membership')
            for person in people:
                for key in ('body', 'head'):
                    x,y,w,h = person[key]
                    if not all(math.isfinite(v) for v in (x,y,w,h)) or min(x,y)<0 or min(w,h)<=0 or x+w>1.00001 or y+h>1.00001:
                        raise ValueError('Invalid annotation bounds')
        if not clip['annotations']:
            raise ValueError('Clip has no ground truth')
    return manifest


def load_manifest(path=CORPUS / 'manifest.json'):
    path = Path(path)
    return validate_manifest(json.loads(path.read_text()), path.parent)


def contains(rect, x, y, margin=0):
    a,b,w,h = rect
    return a-margin <= x <= a+w+margin and b-margin <= y <= b+h+margin


def iou(a,b):
    x,y,w,h = a; u,v,s,t = b
    overlap = max(0,min(x+w,u+s)-max(x,u))*max(0,min(y+h,v+t)-max(y,v))
    return overlap/max(1e-12,w*h+s*t-overlap)


def match_frame(people, detections, thresholds):
    """Maximum-cardinality one-to-one matching; a union box cannot match twice."""
    pairs = {}
    merged = []
    oversized = []
    for j, detection in enumerate(detections):
        rect = detection['bounds']
        if len(rect)!=4 or not all(math.isfinite(v) for v in rect) or min(rect[2:])<=0:
            raise ValueError('Invalid detector output bounds')
        heads = [p for p in people if contains(rect,p['head'][0]+p['head'][2]/2,p['head'][1]+p['head'][3]/2,thresholds['head_margin'])]
        if heads and all(rect[2] > p['body'][2]*thresholds['maximum_width_ratio'] for p in heads):
            oversized.append(j)
        if len(heads)>1 and rect[2] > max(p['body'][2] for p in heads)*1.7:
            merged.append(j)
        for i, p in enumerate(people):
            overlap = iou(rect,p['body'])
            if p in heads and overlap >= thresholds['minimum_iou'] and rect[2] <= p['body'][2]*thresholds['maximum_width_ratio']:
                pairs[i,j] = overlap
    # At most three intended subjects. Search choices including unmatched.
    best = (-1,-1.0); matches = {}
    for choice in itertools.product(range(-1,len(detections)), repeat=len(people)):
        used = [j for j in choice if j>=0]
        if len(set(used)) != len(used) or any(j>=0 and (i,j) not in pairs for i,j in enumerate(choice)):
            continue
        score = (len(used),sum(pairs[i,j] for i,j in enumerate(choice) if j>=0))
        if score > best:
            best = score; matches = {people[i]['id']:j for i,j in enumerate(choice) if j>=0}
    return {'matches':matches,'missed':[p['id'] for p in people if p['id'] not in matches], 'merged':merged,'oversized':oversized}


def score_clip(clip, rows, thresholds):
    by_time = {round(row['t'],6):row for row in rows}
    if len(by_time) != len(rows):
        raise ValueError('Duplicate result timestamps')
    expected = found = switches = merged = oversized = 0
    last_ids = {}; last_shot = None; details = []
    for frame in clip['annotations']:
        row = by_time.get(round(frame['t'],6))
        if row is None:
            raise ValueError(f"Missing result at {clip['id']}:{frame['t']}")
        result = match_frame(frame['subjects'],row['detections'],thresholds)
        expected += len(frame['subjects']); found += len(result['matches'])
        merged += len(result['merged']); oversized += len(result['oversized'])
        # A broadcast edit is not a physical identity switch; count within shots only.
        if frame['shot'] != last_shot:
            last_ids = {}
        tracked = match_frame(frame['subjects'], row.get('tracks',[]), thresholds)
        for person,j in tracked['matches'].items():
            track_id = row['tracks'][j]['id']
            if person in last_ids and last_ids[person] != track_id:
                switches += 1
            last_ids[person] = track_id
        last_shot = frame['shot']
        details.append({'t':frame['t'],**result})
    recall = found / expected
    failures=[]
    if recall < thresholds['minimum_recall']:failures.append('subject_recall')
    if merged > thresholds['maximum_merged_boxes']:failures.append('merged_subject_box')
    if oversized:failures.append('oversized_subject_box')
    if switches > thresholds['maximum_identity_switches']:failures.append('identity_switch')
    return {'identity_evaluated':all('tracks' in row for row in rows),'clip':clip['id'],'target_count':clip['target_count'],'expected_subject_samples':expected,'found_subject_samples':found,'recall':recall,'merged_boxes':merged,'oversized_boxes':oversized,'identity_switches_at_labelled_instants':switches,'failures':failures,'passed':not failures,'frames':details}
