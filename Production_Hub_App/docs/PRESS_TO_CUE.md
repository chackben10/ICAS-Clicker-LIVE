# Press-to-cue: measuring and fixing cue lag

Three unrelated problems were once investigated as one, and the fix for the
first was making the second worse. They are separated here, and each is small.

| Problem | Actual cause | Where it is fixed |
| --- | --- | --- |
| Bible verse reference does not appear | The song themes' Fullscreen slides have no `REFERENCE` element; the Bible theme's does | Add that element to the song themes (ProPresenter, no code) |
| Multi-second lag between press and screen | Machine-wide memory starvation and background software, not ProPresenter's own size | Free the machine; see Phase 4 |
| Permissions needing re-approval | Ad-hoc code signatures name one exact build | A persistent signing identity |

## The number that counts

Press-to-cue is the CGEventTap timestamp of a physical clicker press to the
ProPresenter status-stream push reporting the resulting slide. It spans
Production Hub's dispatch, the HTTP round-trip, **and ProPresenter's own
processing**, which is where the multi-second stalls live.

It is not Hub-to-ProPresenter HTTP latency. That number was ~11.5 ms in a
60-minute soak while slides took seconds to appear, because it stops at the
`200 OK` and the slow part happens after. Never report one as the other.

Measurement is always on and costs one subtraction per press. Read it at:

```bash
curl -s localhost:17777/admin/performance
```

`pressToCue` gives p50/p95/max over the retained window, plus `unmatched`
(presses that produced no slide) and `unsolicited_cues` (slides nobody pressed
for — the operator advancing from ProPresenter directly). `propresenter` gives
the resident-size curve from a 30-second sampler.

### What the automated measurement does not cover

A status-stream push is not photons on a screen. **Once per phase** — not
continuously — film the operator display and the audience output together on a
phone at 240 fps, click for thirty seconds, and count frames. That closes the
gap between "ProPresenter acknowledged the cue" and "the congregation saw it".

## Phase 4 — where the lag actually comes from

Measured on the production machine while it was, in the operator's words,
"unbelievably slow". The earlier claim in this document — that ProPresenter
grows to 7.58 GB and that this is the lag — did not hold up.

| | Measured |
| --- | --- |
| Total RAM | 16.0 GB |
| Free | **68 MB** |
| Swap used | **6.26 GB of 7.17 GB** |
| **ProPresenter RSS** | **536 MB** |

ProPresenter was not bloated. The **machine** was starved, so every allocation
went through the compressor or to disk. Competing for it at that moment:

| Process | CPU | RSS |
| --- | --- | --- |
| ProPresenter | 76% | 536 MB |
| **UVCAssistant** | **69%** | 24 MB |
| WindowServer | 40% | 123 MB |
| Production Hub | 39% | 734 MB |
| Chrome Remote Desktop | 37% | 240 MB |
| DisplayLink Manager | 35% | 36 MB |

plus a `contactsd` / `AddressBookManager` burst at ~95% combined.

### UVCAssistant: ProPresenter's idle capture cards

`lsof` shows ProPresenter holding **Cam Link 4K** and **Elgato HD60 X**, while
`GET /v1/status/layers` reports `video_input: false` — the input is captured
continuously but never shown. That capture is what UVCAssistant is serving, and
it survives a Production Hub restart, so it is not Production Hub's doing.

**Remove or disable any Video Input in ProPresenter that is not actually being
shown.** That is most of a core, for nothing.

### Before a service

1. Quit Chrome Remote Desktop, Claude, and any editor. During this session those
   three were ~1.4 GB and ~90% of a core between them.
2. Unplug the DisplayLink adapter if it is not feeding a real output; it
   composites in software and drives WindowServer's cost.
3. Sign out of iCloud Contacts on this machine, and exclude the ProPresenter
   media and library folders from Spotlight (`mds_stores` held 406 MB).
4. Reboot. Two days of uptime with swap 87% full does not recover on its own.
5. Then audit ProPresenter's own content: H.264/H.265, ≤50 Mbps, encoded at
   output resolution; no 4K files feeding 1080p outputs; drop NDI outputs and
   screen destinations nothing consumes.

Use `detailed_snapshot()` in `core/metrics/propresenter_resources.py` for a
`vmmap` breakdown if a case needs sending to Renewed Vision.

### Production Hub's own cost

Profiled with `sample` while it sat at 39% CPU. Subtree-summed, the OpenCV and
Qt work was minor — SIFT 3.5% of a core, `cv::remap` 1.1%, Qt painting 3.5%.
**NDI receive/decode and USB capture dominated**, which is configuration, not
code:

- Lowering `preferred_fps` and the local capture resolution had **no
  measurable effect** on UVCAssistant (72% before, 67–70% after — noise), and
  both were reverted.
- `audience_highest_bandwidth` off made the Click-to-Frame overlay look wrong
  and was reverted. Full bandwidth stays.
- The change that was kept is `match_device_capture_format`: Production Hub no
  longer calls `setCameraFormat` on the capture card. ProPresenter holds the
  same Cam Link 4K — the configured `ptz_device_id` decodes to
  `0x2400000fd900a1`, the card's Unique ID — and a UVC device streams one
  format for all its clients. Forcing one reconfigures a card another
  application is reading, and makes macOS convert for whichever client lost.
  Production Hub already throttles to `preview_fps` itself, so it now consumes
  the device's own format and uses fewer of those frames. Correct on the
  merits; no CPU win demonstrated.

Measured again with the Click-to-Frame window open — the state that matters —
Production Hub sits at **~41% CPU / 710 MB, essentially unchanged** from the
39% / 734 MB baseline. An earlier "39% → 8.3%" figure in this document was
taken with the Camera page closed and was not like-for-like.

What changed is *where* the work happens. The Qt main thread went from 6.7% to
3% busy, and the OpenCV and painting work this session targeted now totals about
4% of a core (remap 0.6%, blendLinear 0.6%, SIFT 1.3%, QPainter 1.7%). The
remaining ~36% is the NDI receive path and Python interpreter overhead, which
these changes never touched.

The lesson is worth keeping: the compositor and the relocalizer were never where
Production Hub's CPU went. Moving the blend off the main thread is still right —
it is a responsiveness fix, not a throughput one — but the throughput lever is
the video pipeline, and the machine-level wins (display refresh, the DisplayLink
display, ProPresenter's NDI outputs) are an order of magnitude larger than
anything available inside Production Hub.

The relocalizer was also optimised in code: the live-frame SIFT budget is capped
separately from the reference (capping both thinned the reference landmarks from
335 to 91 and weakened the homography), the mutual-ratio reverse match runs over
surviving candidates instead of the whole live set, and the CLAHE object is no
longer rebuilt per frame. A pass went from 47 ms to 28 ms.

## Where ProPresenter's CPU actually goes

Profiled directly with `sample` on the ProPresenter process, 8 seconds, on the
production machine. Threads parked in `accept`/`mach_msg` excluded.

| Thread | Busy |
| --- | --- |
| `ndiVideoOutput:Fullscreen Feed` | **98% of a core** |
| `ndiVideoOutput:Props Feed` | **96% of a core** |
| `ndiVideoOutput:LowerThirds Feed` | **41% of a core** |

**~2.3 cores go to encoding three NDI outputs**, inside
`ssNDIOutput::ProcessOutput()` and libndi. During the measurement the program
output was a black Blank slide and only ~2 Mbps was leaving the machine, so this
is the cost of the render-and-encode pipeline running per frame, not of the data
volume. Real content will not be cheaper.

All three feeds have a live receiver on `192.168.1.156`, so none is idle waste.

### Output topology, from `Configuration/Workspace`

| Screen | NDI feed | Format |
| --- | --- | --- |
| Fullscreen-Display (physical) | — | **1080p60** |
| Stage Screen (physical) | — | **1080p60** |
| Fullscreen Output | Fullscreen Feed | 1080p30 |
| Props Output | Props Feed | 1080p30 |
| LowerThirds Output | LowerThirds Feed | 1920x330p30 |
| Operator Screen | — | 740x1080 |

The NDI feeds are already at 30. The two physical screens are at 60.

### Levers, in measured order

1. **Reduce the number or size of NDI outputs (~1 core each).** They cannot
   simply be switched off -- the stream machine consumes all three -- but ask
   whether it needs Props and LowerThirds as *separate* feeds rather than one
   composited Fullscreen feed. The Props Output is a full 1920x1080 screen; if
   props only occupy part of the frame, a smaller Props screen costs
   proportionally less.
2. **Set Fullscreen-Display and Stage Screen to 1080p30.** This is a
   ProPresenter screen setting, not a macOS refresh change. Motion backgrounds
   are typically 30fps and lyrics do not need 60, so this halves the render work
   for two of the six targets.
3. **Get the DisplayLink display onto a native output.** A DisplayLink dock
   (USB vendor 0x17e9) is attached, so one display is composited in software and
   pushed over USB with no GPU scanout path. DisplayLink Manager sits at ~29% of
   a core and inflates WindowServer, which was at ~40%. Moving that display to
   HDMI/DisplayPort or the Blackmagic device reclaims both.
4. **Pin the capture cards' format.** UVCAssistant runs at ~68% of a core
   serving Cam Link 4K and Elgato HD60 X. Both are needed during a service, but
   they offer up to 3840x2160@60 (~498 Mpx/s) and ProPresenter will negotiate
   the highest mode on offer. Pinning both to 1920x1080@30 (~62 Mpx/s) is 4-8x
   less work for the same picture.
5. **Free the machine.** 16 GB total with ~85 MB free and swap at 6.2 of 7.2 GB
   during measurement. Every item above also reduces memory pressure. Restart
   before a service; two days of uptime does not recover on its own.

Chrome Remote Desktop (~40% of a core) is only present during remote support and
is not a service-time concern.

## Phase 3 — the Bible verse

Everything below was read from the live ProPresenter API and the theme files on
disk, not inferred. Earlier explanations in this document were wrong and have
been removed.

### How Looks and themes actually relate

A Look is not a theme override applied on top of a slide. A Look assigns, per
screen, **which theme's slide format that screen uses** — Fullscreen, Lower
Third, or Blank. `GET /v1/looks` shows this: each Look lists five screens, each
naming a theme-slide UUID that `GET /v1/theme/<name>` resolves.

### Why the reference disappears

The verse data is fine. During a reproduction, `GET /v1/status/slide` returned:

```
"John 3:16\rFor God so loved the world…\rതന്റെ ഏകജാതനായ പുത്രനിൽ…"
```

The reference is the first line. `look/current` reported **"Bible"**, with all
five screens on ICAS Bible theme slides. So neither the data nor the Look was
wrong.

The cause is in the themes. Every theme's Fullscreen slide was dumped:

| Theme | Fullscreen slide | Lower Third slide |
| --- | --- | --- |
| ICAS Bible | **has a `REFERENCE` element** | has `REFERENCE` |
| ICAS Malayalam Song | **no `REFERENCE` element** | has `REFERENCE` |
| ICAS English | **no `REFERENCE` element** | has `REFERENCE` |

When a verse is generated while a Song Look is active, the fullscreen output
renders through the song theme's Fullscreen slide, which **has nowhere to put
the reference**. The lower thirds look right because those slide formats do
have the element.

### The fix

Add a `REFERENCE` text element to the Fullscreen slide of **ICAS Malayalam
Song** and **ICAS English**, positioned and styled to match the one in **ICAS
Bible → Fullscreen**.

This needs no code and no change to the Song Looks' theming, so the lower-thirds
formatting that depends on it is untouched. A verse generated under a Song Look
then renders its reference correctly from the first frame.

### Why Production Hub cannot repair this after the fact

Re-rendering the verse under the correct theme would fix it, but a dynamic Bible
cue is **not addressable through the API**:

- `GET /v1/presentation/slide_index` returns `{"presentation_index": null}`.
- `GET /v1/presentation/active` returns the Bible group but with **no UUID**.
- The binary exposes no cue-trigger route; `/v1/presentation/active/0/trigger`
  hits the *underlying song* instead (verified — it cleared the verse and
  restored the Song Look).

So there is no re-render to trigger. The Look guard still sets the Bible Look
promptly, which is worth having, but the reference element is the actual fix.

## What not to do

- **No observers of ProPresenter's UI.** Accessibility observers, mouse taps,
  and UI polling all make synchronous cross-process calls served by the thread
  that renders cues. The deleted `bible_cue_corrector.py` re-walked the toolbar
  subtree twice a second, reading four attributes on up to eighty elements —
  hundreds of blocking round-trips per second into the thread it was meant to
  help.
- **No Swift rewrite.** Production Hub sits at 6–10% while ProPresenter sits at
  72–90%. A rewrite would spend months without touching the slow process. The
  one real Hub hot path — Click-to-Frame's `cv2.remap` and `cv2.blendLinear` —
  needed a worker thread, not a language, and now has one.
- **No more hour-long API-latency soaks.** That measures the wrong variable and
  never reached the failure state: ProPresenter sat at 1.02 GB throughout,
  against 7.58 GB in real use. Continuous press-to-cue logging during real
  services replaces it.
- **No fix accepted without a measurement under load.** "283 tests pass" said
  nothing about whether Sunday felt better.

## Phase 5 — verification

Run the full realistic load: audio playing, motion background running, a prop
rendered, Click-to-Frame open. Then run it again with PTZ tracking armed — a
different CPU profile, and a separate acceptance condition.

Exercise the real paths: fresh Bible entries from both song contexts, **physical
clicker presses** (software-injected keys never reach the event tap, so that
test is meaningless), Look changes, Auto Show on.

### Done means

- The first Bible verse after any song Look renders correctly on the first
  click, 10 times out of 10, from both Malayalam and English contexts.
- Press-to-cue p95 < 250 ms and max < 1 s across a full service with audio,
  motion, a prop, and Click-to-Frame open — and again with tracking armed.
- Production Hub stays under 15% CPU with Click-to-Frame open and tracking off,
  and under 60% with tracking armed.
- ProPresenter finishes a three-hour service under 3 GB resident, zero swap.
- Every physical clicker press across a full service produces exactly one slide
  action, verified from Hub logs — no injected keys.
- Ten consecutive rebuilds produce zero new macOS permission prompts.
