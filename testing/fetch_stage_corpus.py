#!/usr/bin/env python3
"""Fetch only the pinned short excerpts. Never rewrite annotations or checksums."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from testing.harness.stage_corpus import load_manifest,sha256


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,default=ROOT/'testing/benchmarks/outputs/stage-corpus')
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args();manifest=load_manifest();args.cache.mkdir(parents=True,exist_ok=True)
    failures=[]
    for clip in manifest['clips']:
        path=args.cache/(clip['id']+'.mp4')
        if path.is_file() and sha256(path)==clip['media_sha256']:
            print(f"Verified {clip['id']}");continue
        if args.verify_only:
            failures.append(clip['id']);continue
        import imageio_ffmpeg
        import yt_dlp
        options={'quiet':True,'skip_download':True,'js_runtimes':{'node':{}}}
        with yt_dlp.YoutubeDL(options) as ydl:
            info=ydl.extract_info(clip['url'],download=False)
        published=info.get('release_date') or info.get('upload_date')
        if published != clip['published']:
            raise ValueError(f"Source publication date changed for {clip['id']}; review required")
        formats=[f for f in info['formats'] if f['format_id']=='136']
        if not formats:raise ValueError('Pinned 720p source format is unavailable; review required')
        candidate=path.with_suffix('.candidate.mp4')
        result=subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error',
            '-ss',str(clip['start_seconds']),'-i',formats[0]['url'],
            '-t',str(clip['duration_seconds']),'-an','-vf','scale=960:-2',
            '-c:v','libx264','-preset','fast','-crf','20','-y',str(candidate),
        ],capture_output=True)
        # ffmpeg stderr can contain expiring signed media URLs; do not persist it.
        if result.returncode:raise RuntimeError(f"Excerpt download failed: {clip['id']}")
        if sha256(candidate)!=clip['media_sha256']:
            failures.append(clip['id'])
            print(f"Checksum differs for {clip['id']}; retained candidate for review. Expected media was not replaced.")
        else:
            os.replace(candidate,path);print(f"Fetched and verified {clip['id']}")
    if failures:
        print('Preflight failed: '+', '.join(failures),file=sys.stderr);return 2
    return 0

if __name__=='__main__':raise SystemExit(main())
