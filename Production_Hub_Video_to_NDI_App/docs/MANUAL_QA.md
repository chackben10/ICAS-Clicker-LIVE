# Manual QA checklist

Run this checklist on the OBS Mac with the actual Audience Cam and PTZ capture interfaces.

## First launch

- Install the official NDI Runtime or NDI Tools.
- Build and install the app with `./scripts/build_app.sh`.
- Launch the app and allow Camera and Local Network access when macOS asks.
- Confirm the green NDI Runtime banner shows a version.
- Click **Refresh Devices** and confirm both capture interfaces appear.

## Route setup

- Edit **Audience Cam**, select the Audience capture device, and save.
- Edit **PTZ Camera**, select the PTZ capture device, and save.
- Start one route at a time and verify its preview, negotiated format, frame rate, and increasing frame count.
- Start both routes and confirm both remain green.
- Test 90°, 180°, and 270° rotation and both flip controls on a route; confirm its preview and OBS receive the same orientation.
- Stop and restart both routes three times to catch ownership or shutdown races.
- Unplug one inactive device, refresh, and confirm it displays as disconnected.
- Try starting its route and confirm the app reports **Missing** rather than crashing.

## OBS and network

- Remove or disable OBS's direct source for a device before this app opens that same device.
- Add each helper output to OBS using an NDI source from DistroAV.
- Confirm the source names appear as `COMPUTER NAME (Production Hub - Audience Cam)` and `COMPUTER NAME (Production Hub - PTZ Camera)` or their locally configured equivalents.
- Open the same NDI source on the Production Hub Mac and confirm OBS continues receiving it.
- Let both feeds run for at least 30 minutes; confirm frame rate remains stable and dropped-frame count does not climb continuously.
- Confirm video latency does not grow over time. A slowly growing dropped count during CPU/network pressure is preferable to accumulating latency.

## Operator behavior

- Enable auto-start for both routes, quit, relaunch, and confirm they start.
- Close the main window and confirm the sources keep running.
- Reopen the window from the menu-bar camera icon.
- Enable **Launch at login**, log out/in, and confirm the helper launches.
- Copy Diagnostics and retain the text if any test fails.
