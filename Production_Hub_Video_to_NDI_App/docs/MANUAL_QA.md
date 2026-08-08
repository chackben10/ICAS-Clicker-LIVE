# Manual QA checklist

Run this checklist on the OBS Mac with the actual Audience Cam capture interface.

## First launch

- Install the official NDI Runtime or NDI Tools.
- Build and install the app with `./scripts/build_app.sh`.
- Launch the app and allow Camera and Local Network access when macOS asks.
- Confirm the green NDI Runtime banner shows a version.
- Click **Refresh Devices** and confirm Audience Cam appears.

## Route setup

- Edit **Audience Cam**, select the Audience capture device, and save.
- Start the route and verify its preview, negotiated format, frame rate, and increasing frame count.
- Test 90°, 180°, and 270° rotation and both flip controls on a route; confirm its preview and OBS receive the same orientation.
- Stop and restart the route three times to catch ownership or shutdown races.
- Unplug Audience Cam, refresh, and confirm it displays as disconnected.
- Try starting its route and confirm the app reports **Missing** rather than crashing.

## OBS and network

- Confirm OBS and the helper can simultaneously open Audience Cam. If that specific driver becomes exclusive, use the helper's NDI output in OBS instead.
- Confirm the source name appears as `COMPUTER NAME (Production Hub - Audience Cam)` or its locally configured equivalent.
- Open the NDI source on the Production Hub Mac and confirm OBS continues viewing its local feed.
- Let the feed run for at least 30 minutes; confirm frame rate remains stable and dropped-frame count does not climb continuously.
- Confirm video latency does not grow over time. A slowly growing dropped count during CPU/network pressure is preferable to accumulating latency.

## Operator behavior

- Enable auto-start for Audience Cam, quit, relaunch, and confirm it starts.
- Close the main window and confirm the sources keep running.
- Use the menu-bar camera icon to stop and restart Audience Cam, then reopen the window.
- Enable **Launch at login**, log out/in, and confirm the helper launches.
- Copy Diagnostics and retain the text if any test fails.
