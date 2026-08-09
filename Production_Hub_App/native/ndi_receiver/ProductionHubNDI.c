#include "ProductionHubNDI.h"

#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Minimal declarations from the public NDI SDK ABI. Keeping these behind this
// bridge prevents Python from depending on native struct layout details.
typedef void *NDIlib_find_instance_t;
typedef void *NDIlib_recv_instance_t;

typedef struct NDIlib_source_t {
    const char *p_ndi_name;
    const char *p_url_address;
} NDIlib_source_t;

typedef struct NDIlib_find_create_t {
    bool show_local_sources;
    const char *p_groups;
    const char *p_extra_ips;
} NDIlib_find_create_t;

typedef struct NDIlib_recv_create_v3_t {
    NDIlib_source_t source_to_connect_to;
    int color_format;
    int bandwidth;
    bool allow_video_fields;
    const char *p_ndi_recv_name;
} NDIlib_recv_create_v3_t;

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

typedef struct NDIlib_recv_performance_t {
    int64_t video_frames;
    int64_t audio_frames;
    int64_t metadata_frames;
} NDIlib_recv_performance_t;

typedef bool (*ndi_initialize_fn)(void);
typedef void (*ndi_destroy_fn)(void);
typedef const char *(*ndi_version_fn)(void);
typedef NDIlib_find_instance_t (*ndi_find_create_v2_fn)(const NDIlib_find_create_t *);
typedef void (*ndi_find_destroy_fn)(NDIlib_find_instance_t);
typedef bool (*ndi_find_wait_fn)(NDIlib_find_instance_t, uint32_t);
typedef const NDIlib_source_t *(*ndi_find_sources_fn)(NDIlib_find_instance_t, uint32_t *);
typedef NDIlib_recv_instance_t (*ndi_recv_create_v3_fn)(const NDIlib_recv_create_v3_t *);
typedef void (*ndi_recv_destroy_fn)(NDIlib_recv_instance_t);
typedef int (*ndi_recv_capture_v3_fn)(
    NDIlib_recv_instance_t,
    NDIlib_video_frame_v2_t *,
    void *,
    void *,
    uint32_t
);
typedef void (*ndi_recv_free_video_v2_fn)(NDIlib_recv_instance_t, const NDIlib_video_frame_v2_t *);
typedef void (*ndi_recv_get_performance_fn)(
    NDIlib_recv_instance_t,
    NDIlib_recv_performance_t *,
    NDIlib_recv_performance_t *
);

enum {
    NDI_FRAME_NONE = 0,
    NDI_FRAME_VIDEO = 1,
    NDI_FRAME_ERROR = 4,
    NDI_COLOR_BGRX_BGRA = 0,
    NDI_BANDWIDTH_LOWEST = 0,
    NDI_BANDWIDTH_HIGHEST = 100,
};

static void *g_library = NULL;
static ndi_initialize_fn g_initialize = NULL;
static ndi_destroy_fn g_destroy = NULL;
static ndi_version_fn g_version = NULL;
static ndi_find_create_v2_fn g_find_create = NULL;
static ndi_find_destroy_fn g_find_destroy = NULL;
static ndi_find_wait_fn g_find_wait = NULL;
static ndi_find_sources_fn g_find_sources = NULL;
static ndi_recv_create_v3_fn g_recv_create = NULL;
static ndi_recv_destroy_fn g_recv_destroy = NULL;
static ndi_recv_capture_v3_fn g_recv_capture = NULL;
static ndi_recv_free_video_v2_fn g_recv_free_video = NULL;
static ndi_recv_get_performance_fn g_recv_performance = NULL;
static char g_error[768] = "NDI runtime has not been loaded.";

static void set_error(const char *message) {
    snprintf(g_error, sizeof(g_error), "%s", message ? message : "Unknown NDI error.");
}

static void *load_symbol(const char *name) {
    void *symbol = dlsym(g_library, name);
    if (!symbol) {
        char message[512];
        snprintf(message, sizeof(message), "NDI runtime is missing symbol %s.", name);
        set_error(message);
    }
    return symbol;
}

static void *try_load_runtime(const char *explicit_path) {
    if (explicit_path && explicit_path[0]) return dlopen(explicit_path, RTLD_NOW | RTLD_LOCAL);

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

bool ph_ndi_initialize(const char *explicit_runtime_path) {
    if (g_library) return true;
    g_library = try_load_runtime(explicit_runtime_path);
    if (!g_library) {
        const char *loader_error = dlerror();
        char message[768];
        snprintf(
            message,
            sizeof(message),
            "Official NDI Runtime 6 was not found.%s%s",
            loader_error ? " Loader error: " : "",
            loader_error ? loader_error : ""
        );
        set_error(message);
        return false;
    }

    g_initialize = (ndi_initialize_fn)load_symbol("NDIlib_initialize");
    g_destroy = (ndi_destroy_fn)load_symbol("NDIlib_destroy");
    g_version = (ndi_version_fn)load_symbol("NDIlib_version");
    g_find_create = (ndi_find_create_v2_fn)load_symbol("NDIlib_find_create_v2");
    g_find_destroy = (ndi_find_destroy_fn)load_symbol("NDIlib_find_destroy");
    g_find_wait = (ndi_find_wait_fn)load_symbol("NDIlib_find_wait_for_sources");
    g_find_sources = (ndi_find_sources_fn)load_symbol("NDIlib_find_get_current_sources");
    g_recv_create = (ndi_recv_create_v3_fn)load_symbol("NDIlib_recv_create_v3");
    g_recv_destroy = (ndi_recv_destroy_fn)load_symbol("NDIlib_recv_destroy");
    g_recv_capture = (ndi_recv_capture_v3_fn)load_symbol("NDIlib_recv_capture_v3");
    g_recv_free_video = (ndi_recv_free_video_v2_fn)load_symbol("NDIlib_recv_free_video_v2");
    g_recv_performance = (ndi_recv_get_performance_fn)load_symbol("NDIlib_recv_get_performance");

    if (!g_initialize || !g_destroy || !g_version || !g_find_create || !g_find_destroy ||
        !g_find_wait || !g_find_sources || !g_recv_create || !g_recv_destroy ||
        !g_recv_capture || !g_recv_free_video || !g_recv_performance) {
        dlclose(g_library);
        g_library = NULL;
        return false;
    }
    if (!g_initialize()) {
        set_error("NDI runtime loaded but initialization failed.");
        dlclose(g_library);
        g_library = NULL;
        return false;
    }
    set_error("");
    return true;
}

void ph_ndi_shutdown(void) {
    if (!g_library) return;
    if (g_destroy) g_destroy();
    dlclose(g_library);
    g_library = NULL;
}

bool ph_ndi_is_loaded(void) { return g_library != NULL; }
const char *ph_ndi_version(void) { return g_library && g_version ? g_version() : "Unavailable"; }
const char *ph_ndi_last_error(void) { return g_error; }

ph_ndi_finder_t ph_ndi_finder_create(void) {
    if (!g_library || !g_find_create) {
        set_error("Cannot create an NDI finder before initializing the runtime.");
        return NULL;
    }
    NDIlib_find_create_t settings = {
        .show_local_sources = true,
        .p_groups = NULL,
        .p_extra_ips = NULL,
    };
    NDIlib_find_instance_t finder = g_find_create(&settings);
    if (!finder) set_error("NDI runtime could not create a source finder.");
    return finder;
}

void ph_ndi_finder_destroy(ph_ndi_finder_t finder) {
    if (finder && g_find_destroy) g_find_destroy((NDIlib_find_instance_t)finder);
}

bool ph_ndi_finder_wait(ph_ndi_finder_t finder, uint32_t timeout_ms) {
    return finder && g_find_wait && g_find_wait((NDIlib_find_instance_t)finder, timeout_ms);
}

uint32_t ph_ndi_finder_source_count(ph_ndi_finder_t finder) {
    if (!finder || !g_find_sources) return 0;
    uint32_t count = 0;
    g_find_sources((NDIlib_find_instance_t)finder, &count);
    return count;
}

bool ph_ndi_finder_source_name(
    ph_ndi_finder_t finder,
    uint32_t index,
    char *destination,
    uint32_t destination_size
) {
    if (!finder || !g_find_sources || !destination || destination_size == 0) return false;
    uint32_t count = 0;
    const NDIlib_source_t *sources = g_find_sources((NDIlib_find_instance_t)finder, &count);
    if (!sources || index >= count || !sources[index].p_ndi_name) return false;
    snprintf(destination, destination_size, "%s", sources[index].p_ndi_name);
    return true;
}

ph_ndi_receiver_t ph_ndi_receiver_create(
    const char *source_name,
    const char *receiver_name,
    bool bandwidth_highest
) {
    if (!g_library || !g_recv_create || !source_name || !source_name[0]) {
        set_error("Cannot create an NDI receiver without a source name.");
        return NULL;
    }
    NDIlib_recv_create_v3_t settings = {
        .source_to_connect_to = {.p_ndi_name = source_name, .p_url_address = NULL},
        .color_format = NDI_COLOR_BGRX_BGRA,
        .bandwidth = bandwidth_highest ? NDI_BANDWIDTH_HIGHEST : NDI_BANDWIDTH_LOWEST,
        .allow_video_fields = false,
        .p_ndi_recv_name = receiver_name,
    };
    NDIlib_recv_instance_t receiver = g_recv_create(&settings);
    if (!receiver) set_error("NDI runtime could not create the video receiver.");
    return receiver;
}

void ph_ndi_receiver_destroy(ph_ndi_receiver_t receiver) {
    if (receiver && g_recv_destroy) g_recv_destroy((NDIlib_recv_instance_t)receiver);
}

int ph_ndi_receiver_capture_video(
    ph_ndi_receiver_t receiver,
    uint32_t timeout_ms,
    ph_ndi_video_frame_t *output
) {
    if (!receiver || !g_recv_capture || !output) return -1;
    memset(output, 0, sizeof(*output));
    NDIlib_video_frame_v2_t *native_frame = calloc(1, sizeof(*native_frame));
    if (!native_frame) {
        set_error("Could not allocate an NDI frame descriptor.");
        return -1;
    }
    int frame_type = g_recv_capture((NDIlib_recv_instance_t)receiver, native_frame, NULL, NULL, timeout_ms);
    if (frame_type != NDI_FRAME_VIDEO) {
        free(native_frame);
        if (frame_type == NDI_FRAME_ERROR) {
            set_error("NDI receiver reported a capture error.");
            return -1;
        }
        return 0;
    }

    output->width = native_frame->xres;
    output->height = native_frame->yres;
    output->fourcc = native_frame->FourCC;
    output->frame_rate_numerator = native_frame->frame_rate_N;
    output->frame_rate_denominator = native_frame->frame_rate_D;
    output->picture_aspect_ratio = native_frame->picture_aspect_ratio;
    output->frame_format_type = native_frame->frame_format_type;
    output->timecode = native_frame->timecode;
    output->data = native_frame->p_data;
    output->line_stride_bytes = native_frame->line_stride_in_bytes;
    output->timestamp = native_frame->timestamp;
    output->private_frame = native_frame;
    return 1;
}

void ph_ndi_receiver_release_video(ph_ndi_receiver_t receiver, ph_ndi_video_frame_t *frame) {
    if (!frame || !frame->private_frame) return;
    NDIlib_video_frame_v2_t *native_frame = (NDIlib_video_frame_v2_t *)frame->private_frame;
    if (receiver && g_recv_free_video) {
        g_recv_free_video((NDIlib_recv_instance_t)receiver, native_frame);
    }
    free(native_frame);
    memset(frame, 0, sizeof(*frame));
}

bool ph_ndi_receiver_performance(ph_ndi_receiver_t receiver, ph_ndi_performance_t *output) {
    if (!receiver || !g_recv_performance || !output) return false;
    NDIlib_recv_performance_t total = {0};
    NDIlib_recv_performance_t dropped = {0};
    g_recv_performance((NDIlib_recv_instance_t)receiver, &total, &dropped);
    output->total_video_frames = total.video_frames;
    output->dropped_video_frames = dropped.video_frames;
    return true;
}
