/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HUSK_RUNTIME_APP_V1_H
#define HUSK_RUNTIME_APP_V1_H

#include "husk-runtime-v1.h"

#ifdef __cplusplus
extern "C" {
#endif

#define HUSK_RUNTIME_APP_ABI_V1 UINT32_C(1)

typedef enum husk_runtime_text_command_v1 {
    HUSK_RUNTIME_TEXT_SELECT_ALL = 1,
    HUSK_RUNTIME_TEXT_COPY = 2,
    HUSK_RUNTIME_TEXT_CUT = 3
} husk_runtime_text_command_v1;

typedef enum husk_runtime_app_state_v1 {
    HUSK_RUNTIME_APP_IDLE = 0,
    HUSK_RUNTIME_APP_STARTING = 1,
    HUSK_RUNTIME_APP_RUNNING = 2,
    HUSK_RUNTIME_APP_FAILED = 3,
    HUSK_RUNTIME_APP_STOPPING = 4,
    HUSK_RUNTIME_APP_STOPPED = 5
} husk_runtime_app_state_v1;

/* Strings are borrowed only during callbacks. Callbacks can arrive on runtime
 * workers and must marshal AppKit/UI work to the main thread. userdata and host
 * views/layers must survive until stop completes, or until process exit if stop
 * fails. The host must keep its main run loop alive during start and stop. */
typedef struct husk_runtime_app_config_v1 {
    uint32_t struct_size;
    uint32_t abi_version;
    const char *apk_path;          /* absolute regular file inside package_root */
    const char *angle_egl;         /* actual macOS host libraries */
    const char *angle_gles;
    int32_t width;                 /* surface pixels */
    int32_t height;
    float density;                /* pixels per Android dp */
    int32_t night_mode;
    void *metal_layer;             /* borrowed CAMetalLayer; NULL for offscreen */
    const char *frame_dir;         /* optional existing private writable dir */
    int32_t frame_every;           /* with frame_dir: 0 defaults to 60; -1 latest-only each frame */
    int32_t insets[4];             /* left/top/right/bottom, surface pixels */
    void *userdata;
    void (*show_keyboard)(void *, int32_t show, int32_t input_type, int32_t ime_options);
    void (*set_clipboard)(void *, const char *utf8);
    /* Return required bytes INCLUDING NUL; zero means no text. The first call
     * uses buffer=NULL, capacity=0. No host allocation crosses the ABI. */
    size_t (*get_clipboard)(void *, char *buffer, size_t capacity);
    void (*share)(void *, const char *utf8);
    void (*open_url)(void *, const char *url);
    void (*set_orientation)(void *, int32_t android_orientation);
    void (*pick_documents)(void *, const char *mime_types, int32_t multiple);
    void (*vibrate)(void *, int32_t milliseconds);
} husk_runtime_app_config_v1;

typedef struct husk_runtime_app_api_v1 {
    uint32_t struct_size;
    uint32_t abi_version;
    /* Call from a worker with an existing configured Runtime handle whose
     * get_info advertises HUSK_RUNTIME_CAP_APK_LAUNCH. Keep that handle and its
     * logging userdata alive until application process exit. All
     * config strings are copied. One start attempt per process, including
     * failure. This does not provide a sandbox or system memory hard limit. */
    int32_t (*start)(husk_runtime_handle *, const husk_runtime_app_config_v1 *,
                     char *error, size_t error_capacity);
    /* Call from a worker; does not unload the Runtime or permit another APK. */
    int32_t (*stop)(char *error, size_t error_capacity);
    int32_t (*state)(void);
    void (*touch)(int32_t phase, int32_t pointer_id, float x, float y);
    void (*key)(int32_t android_keycode, int32_t down);
    void (*back)(void);
    void (*text)(const char *utf8);
    void (*text_delete)(void);
    void (*text_action)(void);
    void (*text_command)(int32_t command); /* standard Android context menu action */
    void (*keyboard_closed)(void);
    void (*pause)(int32_t paused);
    void (*set_insets)(int32_t left, int32_t top, int32_t right,
                       int32_t bottom, int32_t keyboard_bottom);
    void (*documents_picked)(const char *const *paths, int32_t count);
    uint64_t (*frames)(void);
    int32_t (*ended)(void);
    /* Main-thread only. screen_view and container are borrowed NSView pointers.
     * The host retains both through stop/process exit. Coordinates are points. */
    int32_t (*attach_web)(void *screen_view, void *container, float scale,
                         int32_t fresh, char *error, size_t error_capacity);
    int32_t (*web_hit)(float x, float y);
    int32_t (*web_parent_scroll_axes)(float x, float y);
} husk_runtime_app_api_v1;

/* Freeze this prefix when adding OPTIONAL entries in a future build. Returning
 * a larger static table must keep older ABI-1 hosts able to use this prefix. */
#define HUSK_RUNTIME_APP_V1_REQUIRED_SIZE ((uint32_t)(offsetof(husk_runtime_app_api_v1, web_parent_scroll_axes) + sizeof(((husk_runtime_app_api_v1 *)0)->web_parent_scroll_axes)))

__attribute__((visibility("default")))
const husk_runtime_app_api_v1 *husk_runtime_get_app_api(uint32_t requested_abi,
                                                     uint32_t caller_table_size);

#ifdef __cplusplus
}
#endif
#endif
