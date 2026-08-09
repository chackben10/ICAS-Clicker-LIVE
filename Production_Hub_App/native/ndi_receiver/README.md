# Production Hub NDI receiver bridge

This narrow C bridge loads the separately installed official NDI Runtime 6 at
runtime. It does not redistribute the NDI SDK or runtime. Its public interface
keeps the NDI ABI out of Python and exposes only source discovery, video receive,
frame release, and performance counters.

Build it with `python3 scripts/build_native_video.py`. The Production Hub macOS
build script performs that step automatically and packages only this bridge.
