#include "CNDIShim.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    if (!ndi_bridge_initialize(NULL)) {
        fprintf(stderr, "NDI initialization failed: %s\n", ndi_bridge_last_error());
        return EXIT_FAILURE;
    }

    printf("NDI runtime: %s\n", ndi_bridge_version());
    ndi_bridge_sender_t sender = ndi_bridge_sender_create("Production Hub - NDI Bridge Smoke Test");
    if (!sender) {
        fprintf(stderr, "NDI sender creation failed: %s\n", ndi_bridge_last_error());
        ndi_bridge_shutdown();
        return EXIT_FAILURE;
    }
    if (ndi_bridge_last_error()[0]) {
        printf("NDI sender note: %s\n", ndi_bridge_last_error());
    }

    uint8_t pixels[16] = {
        0, 0, 255, 255,
        0, 255, 0, 255,
        255, 0, 0, 255,
        255, 255, 255, 255,
    };
    if (!ndi_bridge_sender_send_bgra(sender, pixels, 2, 2, 8, 30, 1, INT64_MAX)) {
        fprintf(stderr, "NDI frame send failed: %s\n", ndi_bridge_last_error());
        ndi_bridge_sender_destroy(sender);
        ndi_bridge_shutdown();
        return EXIT_FAILURE;
    }

    printf("NDI sender smoke test passed; receivers=%d\n", ndi_bridge_sender_connection_count(sender));
    ndi_bridge_sender_destroy(sender);
    ndi_bridge_shutdown();
    return EXIT_SUCCESS;
}
