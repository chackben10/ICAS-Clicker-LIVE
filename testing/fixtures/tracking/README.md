# Tracking regression fixtures

`dual_person_live_detections.json` contains 100 anonymized PTZ detection frames
from the September 13, 2026 two-person walk-apart/return test. It contains only
normalized rectangles and short-lived detector IDs, without images, calibration,
camera addresses, credentials, or personal identifiers.

The replay exercises the production identity lock against actual detector ID
changes, upper-body/full-body changes, and edge losses. It uses synthetic
reference geometry, so it checks identity retention rather than reproducing the
physical camera's exact motion. The simulated camera scenarios additionally
exercise the complete automation loop, including motor commands, separation,
regrouping, detection dropouts, crossing, and retirement.

Run all of these without camera hardware:

```sh
python3 testing/run_end_to_end.py --mode fast
QT_QPA_PLATFORM=offscreen testing/.venv/bin/python -m pytest testing/tests/desktop/test_click_to_frame_highlights.py -q
```

The desktop tests render synthetic subject pixels through translated/sheared
panorama maps and the actual click-to-frame crop, then check that highlights
coincide with those pixels. They also test the floating window's mapper wiring,
raw-feed fallback, missing/stale observations, tint opacity, and stroke width.

`ptz_single_subject_golden.json` preserves the existing single-person behavior.
It must not be regenerated to conceal a regression. Synthetic motor response is
not a measurement of real Panasonic zoom speed; final tuning still needs a live
staging check.

`test_single_subject_easing.py` separately enables the Dev-only motion experiment
and tests small left/right steps, a brief duck and return, and a larger move
through the closed-loop simulator. It checks speed taper, settled framing, and
immediate halt. Routine pan/tilt slowdowns also taper during group tracking and calibrated
reacquisition, with tests at 2, 4, and 8 decisions per second. Direction reversals
brake promptly; manual/disarm/loss stops and zoom retain their existing behavior. The golden fixture continues to cover the default (experiment off)
controller; no baseline is replaced by enabling the experiment.

`test_tracking_catch_up_and_portrait.py` enables the additional Dev-only pursuit
and two-person framing changes. It reproduces fast horizontal/vertical walks
through the real supervisor with simulated motors, verifies stronger edge
catch-up without changing the small-correction response or zoom commands, and
checks separation/regrouping, a missed detection, and stable portrait scale
when lower bodies leave the image. Approximate rectangles from the supplied
reference photo check optical framing tolerance; they are not a detector replay
or proof that the physical camera matches the photo. The runtime flags do not
write into the shared production profile.
