#ifndef CNDI_SHIM_H
#define CNDI_SHIM_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void *ndi_bridge_sender_t;

// Loads an already-installed official NDI runtime. The runtime is intentionally
// not bundled with this project. Pass NULL to search standard macOS locations.
bool ndi_bridge_initialize(const char *explicit_runtime_path);
void ndi_bridge_shutdown(void);
bool ndi_bridge_is_loaded(void);
const char *ndi_bridge_version(void);
const char *ndi_bridge_last_error(void);

ndi_bridge_sender_t ndi_bridge_sender_create(const char *source_name);
void ndi_bridge_sender_destroy(ndi_bridge_sender_t sender);
int ndi_bridge_sender_connection_count(ndi_bridge_sender_t sender);

// Sends one progressive BGRA frame synchronously. The caller retains ownership
// of the pixels and may reuse them after this function returns.
bool ndi_bridge_sender_send_bgra(
    ndi_bridge_sender_t sender,
    const uint8_t *pixels,
    int width,
    int height,
    int line_stride_bytes,
    int frame_rate_numerator,
    int frame_rate_denominator,
    int64_t timecode
);

#ifdef __cplusplus
}
#endif

#endif
