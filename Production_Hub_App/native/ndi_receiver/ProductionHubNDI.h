#ifndef PRODUCTION_HUB_NDI_H
#define PRODUCTION_HUB_NDI_H

#include <stdbool.h>
#include <stdint.h>

#if defined(__GNUC__)
#define PH_NDI_API __attribute__((visibility("default")))
#else
#define PH_NDI_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef void *ph_ndi_finder_t;
typedef void *ph_ndi_receiver_t;

typedef struct ph_ndi_video_frame_t {
    int width;
    int height;
    int fourcc;
    int frame_rate_numerator;
    int frame_rate_denominator;
    float picture_aspect_ratio;
    int frame_format_type;
    int64_t timecode;
    const uint8_t *data;
    int line_stride_bytes;
    int64_t timestamp;
    void *private_frame;
} ph_ndi_video_frame_t;

typedef struct ph_ndi_performance_t {
    int64_t total_video_frames;
    int64_t dropped_video_frames;
} ph_ndi_performance_t;

// Load an already-installed official NDI runtime. The runtime is not bundled.
PH_NDI_API bool ph_ndi_initialize(const char *explicit_runtime_path);
PH_NDI_API void ph_ndi_shutdown(void);
PH_NDI_API bool ph_ndi_is_loaded(void);
PH_NDI_API const char *ph_ndi_version(void);
PH_NDI_API const char *ph_ndi_last_error(void);

PH_NDI_API ph_ndi_finder_t ph_ndi_finder_create(void);
PH_NDI_API void ph_ndi_finder_destroy(ph_ndi_finder_t finder);
PH_NDI_API bool ph_ndi_finder_wait(ph_ndi_finder_t finder, uint32_t timeout_ms);
PH_NDI_API uint32_t ph_ndi_finder_source_count(ph_ndi_finder_t finder);
PH_NDI_API bool ph_ndi_finder_source_name(
    ph_ndi_finder_t finder,
    uint32_t index,
    char *destination,
    uint32_t destination_size
);

// bandwidth_highest selects full-bandwidth video. False requests the NDI
// preview stream. Video is returned as BGRX/BGRA for direct Qt display.
PH_NDI_API ph_ndi_receiver_t ph_ndi_receiver_create(
    const char *source_name,
    const char *receiver_name,
    bool bandwidth_highest
);
PH_NDI_API void ph_ndi_receiver_destroy(ph_ndi_receiver_t receiver);

// Returns 1 for video, 0 for a timeout/non-video frame, and -1 for an error.
// Every successful frame must be paired with ph_ndi_receiver_release_video.
PH_NDI_API int ph_ndi_receiver_capture_video(
    ph_ndi_receiver_t receiver,
    uint32_t timeout_ms,
    ph_ndi_video_frame_t *output
);
PH_NDI_API void ph_ndi_receiver_release_video(ph_ndi_receiver_t receiver, ph_ndi_video_frame_t *frame);
PH_NDI_API bool ph_ndi_receiver_performance(ph_ndi_receiver_t receiver, ph_ndi_performance_t *output);

#ifdef __cplusplus
}
#endif

#endif
