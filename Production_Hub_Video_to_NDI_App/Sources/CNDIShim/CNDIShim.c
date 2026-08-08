#include "CNDIShim.h"

#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// These ABI declarations mirror the public NDI SDK sender structures. The app
// resolves all functions dynamically so no proprietary runtime is redistributed.
typedef void *NDIlib_send_instance_t;

typedef struct NDIlib_send_create_t {
    const char *p_ndi_name;
    const char *p_groups;
    bool clock_video;
    bool clock_audio;
} NDIlib_send_create_t;

typedef struct NDIlib_video_frame_v2_t {
    int xres;
    int yres;
    int FourCC;
    int frame_rate_N;
    int frame_rate_D;
    float picture_aspect_ratio;
    int frame_format_type;
    int64_t timecode;
    uint8_t *p_data;
    int line_stride_in_bytes;
    const char *p_metadata;
    int64_t timestamp;
} NDIlib_video_frame_v2_t;

typedef bool (*ndi_initialize_fn)(void);
typedef void (*ndi_destroy_fn)(void);
typedef const char *(*ndi_version_fn)(void);
typedef NDIlib_send_instance_t (*ndi_send_create_fn)(const NDIlib_send_create_t *);
typedef void (*ndi_send_destroy_fn)(NDIlib_send_instance_t);
typedef void (*ndi_send_video_v2_fn)(NDIlib_send_instance_t, const NDIlib_video_frame_v2_t *);
typedef int (*ndi_send_connections_fn)(NDIlib_send_instance_t, uint32_t);

static void *g_library = NULL;
static ndi_initialize_fn g_initialize = NULL;
static ndi_destroy_fn g_destroy = NULL;
static ndi_version_fn g_version = NULL;
static ndi_send_create_fn g_send_create = NULL;
static ndi_send_destroy_fn g_send_destroy = NULL;
static ndi_send_video_v2_fn g_send_video_v2 = NULL;
static ndi_send_connections_fn g_send_connections = NULL;
static char g_error[512] = "NDI runtime has not been loaded.";

static void set_error(const char *message) {
    if (!message) message = "Unknown NDI error.";
    snprintf(g_error, sizeof(g_error), "%s", message);
}

static void *load_symbol(const char *name) {
    void *symbol = dlsym(g_library, name);
    if (!symbol) {
        char buffer[512];
        snprintf(buffer, sizeof(buffer), "NDI runtime is missing symbol %s.", name);
        set_error(buffer);
    }
    return symbol;
}

static void *try_load_runtime(const char *explicit_path) {
    if (explicit_path && explicit_path[0]) {
        return dlopen(explicit_path, RTLD_NOW | RTLD_LOCAL);
    }

    const char *runtime_directory = getenv("NDI_RUNTIME_DIR_V6");
    if (runtime_directory && runtime_directory[0]) {
        char candidate[PATH_MAX];
        snprintf(candidate, sizeof(candidate), "%s/libndi.dylib", runtime_directory);
        void *handle = dlopen(candidate, RTLD_NOW | RTLD_LOCAL);
        if (handle) return handle;
    }

    const char *candidates[] = {
        "libndi.dylib",
        "/usr/local/lib/libndi.dylib",
        "/Library/NDI SDK for Apple/lib/macOS/libndi.dylib",
        NULL,
    };
    for (int index = 0; candidates[index]; ++index) {
        void *handle = dlopen(candidates[index], RTLD_NOW | RTLD_LOCAL);
        if (handle) return handle;
    }
    return NULL;
}

bool ndi_bridge_initialize(const char *explicit_runtime_path) {
    if (g_library) return true;

    g_library = try_load_runtime(explicit_runtime_path);
    if (!g_library) {
        const char *loader_error = dlerror();
        char buffer[512];
        snprintf(
            buffer,
            sizeof(buffer),
            "Official NDI Runtime not found. Install NDI Runtime 6 or set NDI_RUNTIME_DIR_V6.%s%s",
            loader_error ? " Loader error: " : "",
            loader_error ? loader_error : ""
        );
        set_error(buffer);
        return false;
    }

    g_initialize = (ndi_initialize_fn)load_symbol("NDIlib_initialize");
    g_destroy = (ndi_destroy_fn)load_symbol("NDIlib_destroy");
    g_version = (ndi_version_fn)load_symbol("NDIlib_version");
    g_send_create = (ndi_send_create_fn)load_symbol("NDIlib_send_create");
    g_send_destroy = (ndi_send_destroy_fn)load_symbol("NDIlib_send_destroy");
    g_send_video_v2 = (ndi_send_video_v2_fn)load_symbol("NDIlib_send_send_video_v2");
    g_send_connections = (ndi_send_connections_fn)load_symbol("NDIlib_send_get_no_connections");

    if (!g_initialize || !g_destroy || !g_version || !g_send_create ||
        !g_send_destroy || !g_send_video_v2 || !g_send_connections) {
        dlclose(g_library);
        g_library = NULL;
        return false;
    }

    if (!g_initialize()) {
        set_error("NDI Runtime loaded but initialization failed on this computer.");
        dlclose(g_library);
        g_library = NULL;
        return false;
    }

    set_error("");
    return true;
}

void ndi_bridge_shutdown(void) {
    if (!g_library) return;
    if (g_destroy) g_destroy();
    dlclose(g_library);
    g_library = NULL;
    g_initialize = NULL;
    g_destroy = NULL;
    g_version = NULL;
    g_send_create = NULL;
    g_send_destroy = NULL;
    g_send_video_v2 = NULL;
    g_send_connections = NULL;
}

bool ndi_bridge_is_loaded(void) {
    return g_library != NULL;
}

const char *ndi_bridge_version(void) {
    return g_library && g_version ? g_version() : "Unavailable";
}

const char *ndi_bridge_last_error(void) {
    return g_error;
}

ndi_bridge_sender_t ndi_bridge_sender_create(const char *source_name) {
    if (!g_library || !g_send_create || !source_name || !source_name[0]) {
        set_error("Cannot create NDI sender without a loaded runtime and source name.");
        return NULL;
    }

    NDIlib_send_create_t create = {
        .p_ndi_name = source_name,
        .p_groups = NULL,
        .clock_video = true,
        .clock_audio = false,
    };
    NDIlib_send_instance_t sender = g_send_create(&create);
    if (!sender) set_error("NDI Runtime could not create the sender. Check local-network permission and NDI configuration.");
    return sender;
}

void ndi_bridge_sender_destroy(ndi_bridge_sender_t sender) {
    if (sender && g_send_destroy) g_send_destroy(sender);
}

int ndi_bridge_sender_connection_count(ndi_bridge_sender_t sender) {
    if (!sender || !g_send_connections) return 0;
    return g_send_connections(sender, 0);
}

bool ndi_bridge_sender_send_bgra(
    ndi_bridge_sender_t sender,
    const uint8_t *pixels,
    int width,
    int height,
    int line_stride_bytes,
    int frame_rate_numerator,
    int frame_rate_denominator,
    int64_t timecode
) {
    if (!sender || !pixels || !g_send_video_v2 || width <= 0 || height <= 0) return false;

    // NDI_LIB_FOURCC('B', 'G', 'R', 'A') in the public NDI SDK.
    const int NDI_BGRA = ('B') | ('G' << 8) | ('R' << 16) | ('A' << 24);
    const int NDI_PROGRESSIVE = 1;
    NDIlib_video_frame_v2_t frame = {
        .xres = width,
        .yres = height,
        .FourCC = NDI_BGRA,
        .frame_rate_N = frame_rate_numerator > 0 ? frame_rate_numerator : 30000,
        .frame_rate_D = frame_rate_denominator > 0 ? frame_rate_denominator : 1001,
        .picture_aspect_ratio = (float)width / (float)height,
        .frame_format_type = NDI_PROGRESSIVE,
        .timecode = timecode,
        .p_data = (uint8_t *)pixels,
        .line_stride_in_bytes = line_stride_bytes,
        .p_metadata = NULL,
        .timestamp = 0,
    };
    g_send_video_v2(sender, &frame);
    return true;
}
