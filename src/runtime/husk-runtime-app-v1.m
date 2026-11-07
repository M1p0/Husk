/* SPDX-License-Identifier: GPL-2.0-or-later */
/* macOS host bridge. Guest/framework/engine layouts remain private to this dylib. */
#import <Foundation/Foundation.h>
#include "../runtime-api/husk-runtime-app-v1.h"
#include "../translation-layer-next/husk-tl-dvm-javaapp.h"
#include "../translation-layer-next/husk-tl-dvm.h"
#include "../translation-layer-next/husk-tl-jni.h"
#include "../translation-layer-next/husk-tl-ld.h"
#include "../translation-layer-next/husk-tl-xmem.h"
#include "../translation-layer-next/husk-tl-session.h"
#include "../translation-layer-next/husk-tl-audio.h"
#include "../translation-layer-next/husk-tl-bionic.h"
#include <TargetConditionals.h>
#include <errno.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#if !TARGET_OS_OSX
#error "This application host bridge currently targets macOS."
#endif

void tl_log_line(const char *format, ...);
void tl_hle_set_ca_bundle(const char *path);
void tl_set_cacerts_dir(const char *path);
void tl_set_system_fonts_dir(const char *path);
void tl_set_shared_storage(const char *path);
void tl_web_attach(void *, void *, float, bool);
bool tl_web_hit(float, float);
int tl_web_parent_scroll_axes(float, float);
void tl_web_harness_size(int, int, float);

static atomic_int g_state = HUSK_RUNTIME_APP_IDLE;
static atomic_bool g_guest_ended;
static bool g_dvm_started, g_java_attempted;
/* Owned strings, callback pointers and referenced host objects survive the
 * process's sole execution attempt. No hot restart or library unloading. */
static husk_runtime_app_config_v1 g_host;
static NSString *g_apk, *g_egl, *g_gles, *g_frames, *g_package_name;
static NSString *g_runtime_root, *g_data_root, *g_java_root, *g_system_root;
static NSString *g_framework_res, *g_cacerts, *g_ca_bundle, *g_shared_root;
static NSArray<NSString *> *g_boot_paths;

static int32_t error_result(int32_t status, char *error, size_t capacity, NSString *message)
{
    if (error && capacity) snprintf(error, capacity, "%s", message.UTF8String ?: "runtime failure");
    return status;
}
static int32_t failed(int32_t status, char *error, size_t capacity, NSString *message)
{
    atomic_store(&g_state, HUSK_RUNTIME_APP_FAILED);
    tl_log_line("runtime-app: %s", message.UTF8String ?: "startup failed");
    return error_result(status, error, capacity, message);
}
static NSString *string(const char *value)
{
    return value ? [NSString stringWithUTF8String:value] : nil;
}
static NSString *resolved_path(const char *value, bool want_directory)
{
    if (!value || value[0] != '/' || strnlen(value, PATH_MAX) == PATH_MAX) return nil;
    char *path = realpath(value, NULL);
    if (!path) return nil;
    NSString *result = [[NSFileManager defaultManager] stringWithFileSystemRepresentation:path length:strlen(path)];
    struct stat info;
    bool ok = stat(path, &info) == 0 && (want_directory ? S_ISDIR(info.st_mode) : S_ISREG(info.st_mode));
    free(path);
    return ok ? result : nil;
}
static bool inside(NSString *path, NSString *directory)
{
    return [path isEqual:directory] || [path hasPrefix:[directory stringByAppendingString:@"/"]];
}
static bool readable_file(NSString *path)
{
    struct stat info;
    return stat(path.fileSystemRepresentation, &info) == 0 && S_ISREG(info.st_mode)
        && access(path.fileSystemRepresentation, R_OK) == 0;
}
static bool running(void)
{
    return atomic_load(&g_state) == HUSK_RUNTIME_APP_RUNNING;
}

static void keyboard(int show, int input_type, int ime_options)
{
    if (g_host.show_keyboard) g_host.show_keyboard(g_host.userdata, show, input_type, ime_options);
}
static void set_clipboard(const char *text)
{
    if (g_host.set_clipboard) g_host.set_clipboard(g_host.userdata, text);
}
static char *get_clipboard(void)
{
    if (!g_host.get_clipboard) return NULL;
    size_t count = g_host.get_clipboard(g_host.userdata, NULL, 0);
    if (!count || count > (16u << 20)) return NULL;
    char *text = calloc(1, count);
    if (!text) return NULL;
    size_t actual = g_host.get_clipboard(g_host.userdata, text, count);
    if (!actual || actual > count || !memchr(text, '\0', count)) { free(text); return NULL; }
    return text;
}
static void share(const char *text) { if (g_host.share) g_host.share(g_host.userdata, text); }
static void open_url(const char *url) { if (g_host.open_url) g_host.open_url(g_host.userdata, url); }
static void orientation(int value) { if (g_host.set_orientation) g_host.set_orientation(g_host.userdata, value); }
static void documents(const char *mime, int multiple) { if (g_host.pick_documents) g_host.pick_documents(g_host.userdata, mime, multiple); }
static void vibrate(int milliseconds) { if (g_host.vibrate) g_host.vibrate(g_host.userdata, milliseconds); }
static void guest_exited(int status)
{
    tl_log_line("runtime-app: guest requested exit(%d)", status);
    /* This profile hosts exactly one APK per process. Android's explicit
     * exit is terminal: preserve its status and let the OS release this app's
     * resources. Cancelling unrelated app workers first can make Go's I/O
     * cleanup panic and replace an intentional exit(0) with exit(2). */
    fflush(NULL);
    _exit(status);
}

static int32_t app_start(husk_runtime_handle *handle, const husk_runtime_app_config_v1 *config,
                         char *error, size_t capacity)
{
    @autoreleasepool {
        if (error && capacity) error[0] = '\0';
        if ([NSThread isMainThread])
            return error_result(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"start must run on a worker while the main run loop remains active");
        if (!config || config->struct_size < sizeof(*config) || config->abi_version != HUSK_RUNTIME_APP_ABI_V1)
            return error_result(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"application config ABI or size is invalid");
        const husk_runtime_api_v1 *base = husk_runtime_get_api(HUSK_RUNTIME_ABI_V1, sizeof(husk_runtime_api_v1));
        const husk_runtime_info_v1 *info = base->get_info();
        if (!(info->capabilities & HUSK_RUNTIME_CAP_APK_LAUNCH))
            return error_result(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"this Runtime profile does not support APK execution");
        const husk_runtime_context_v1 *context = base->get_context(handle);
        if (!context)
            return error_result(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"Runtime handle is not active; retain it until application process exit");
        int expected = HUSK_RUNTIME_APP_IDLE;
        if (!atomic_compare_exchange_strong(&g_state, &expected, HUSK_RUNTIME_APP_STARTING))
            return error_result(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"one APK startup attempt is allowed per process");
        if (config->width <= 0 || config->height <= 0 || config->width > 16384 || config->height > 16384
            || !isfinite(config->density) || config->density <= 0 || config->density > 16
            || config->frame_every < -1)
            return failed(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"invalid surface size, density or capture interval");
        for (size_t i = 0; i < 4; i++)
            if (config->insets[i] < 0)
                return failed(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"surface insets must be nonnegative");
        g_runtime_root = [string(context->runtime_root) copy];
        g_data_root = [string(context->data_root) copy];
        NSString *package_root = string(context->package_root);
        g_apk = resolved_path(config->apk_path, false);
        g_egl = resolved_path(config->angle_egl, false);
        g_gles = resolved_path(config->angle_gles, false);
        if (!g_apk || !inside(g_apk, package_root) || !readable_file(g_apk))
            return failed(HUSK_RUNTIME_INVALID_PATH, error, capacity, @"APK must be a readable regular file inside the configured package_root");
        if (!g_egl || !g_gles || !readable_file(g_egl) || !readable_file(g_gles))
            return failed(HUSK_RUNTIME_INVALID_PATH, error, capacity, @"both actual macOS ANGLE libraries are required");
        if (config->frame_dir) {
            g_frames = resolved_path(config->frame_dir, true);
            if (!g_frames || !(inside(g_frames, g_data_root) || inside(g_frames, string(context->cache_root))
                              || inside(g_frames, string(context->config_root))))
                return failed(HUSK_RUNTIME_INVALID_PATH, error, capacity, @"frame_dir must be an existing private writable directory");
        }
        if (!config->metal_layer && !g_frames)
            return failed(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"a Metal layer or private offscreen frame directory is required");

        uint64_t manual = context->manual_jit_bytes;
        if (!manual || manual > SIZE_MAX / 2 || manual % TL_XMEM_PAGE)
            return failed(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"manual JIT size must be nonzero, 16 KiB aligned and addressable");
        if (getenv("TL_XMEM_MIB") || getenv("TL_XMEM_SELF") || getenv("TL_NO_XMEM"))
            return failed(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"TL_XMEM_MIB/TL_XMEM_SELF/TL_NO_XMEM may not override explicit application JIT configuration");
        if (tl_xmem_size() && tl_xmem_size() != manual)
            return failed(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"executable memory was already initialized with a different capacity");

        g_java_root = [g_runtime_root stringByAppendingPathComponent:@"resources/java-runtime"];
        g_system_root = [g_runtime_root stringByAppendingPathComponent:@"resources/android-system"];
        g_framework_res = [g_java_root stringByAppendingPathComponent:@"framework-res.apk"];
        g_cacerts = [g_system_root stringByAppendingPathComponent:@"cacerts"];
        g_ca_bundle = [g_runtime_root stringByAppendingPathComponent:@"resources/cacert.pem"];
        NSArray<NSString *> *relative_boot = @[@"com.android.art/javalib/core-oj.jar",
            @"com.android.art/javalib/core-libart.jar", @"com.android.art/javalib/okhttp.jar",
            @"com.android.art/javalib/bouncycastle.jar", @"com.android.art/javalib/apache-xml.jar",
            @"com.android.i18n/javalib/core-icu4j.jar", @"com.android.conscrypt/javalib/conscrypt.jar",
            @"husk-framework.dex", @"org.apache.http.legacy.jar"];
        NSMutableArray<NSString *> *boot_paths = [NSMutableArray array];
        for (NSString *relative in relative_boot) {
            NSString *path = [g_java_root stringByAppendingPathComponent:relative];
            if (!readable_file(path))
                return failed(HUSK_RUNTIME_INVALID_PATH, error, capacity, [@"missing Runtime boot resource: " stringByAppendingString:relative]);
            [boot_paths addObject:path];
        }
        if (!readable_file(g_framework_res) || !readable_file(g_ca_bundle))
            return failed(HUSK_RUNTIME_INVALID_PATH, error, capacity, @"Runtime framework resources or CA bundle are missing");
        if (![[NSFileManager defaultManager] contentsOfDirectoryAtPath:g_cacerts error:NULL].count)
            return failed(HUSK_RUNTIME_INVALID_PATH, error, capacity, @"Runtime Android trusted certificate directory is missing or empty");
        g_boot_paths = [boot_paths copy];
        char package[512] = {0}, activity[1024] = {0}, application[1024] = {0};
        if (!tl_javaapp_manifest(g_apk.fileSystemRepresentation, package, sizeof(package), activity, sizeof(activity), application, sizeof(application)))
            return failed(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"APK manifest does not define a supported launcher activity");
        g_package_name = [string(package) copy];
        if (!g_package_name.length)
            return failed(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"APK package name is invalid UTF-8 or empty");

        g_host = *config;
        g_host.apk_path = g_apk.fileSystemRepresentation;
        g_host.angle_egl = g_egl.fileSystemRepresentation;
        g_host.angle_gles = g_gles.fileSystemRepresentation;
        g_host.frame_dir = g_frames.fileSystemRepresentation;
        char jit_error[256] = {0};
        husk_jit_budget_set_target_bytes((size_t)manual);
        if (!tl_xmem_open((size_t)manual, jit_error, sizeof(jit_error)) || tl_xmem_size() != manual)
            return failed(HUSK_RUNTIME_NO_MEMORY, error, capacity,
                          [NSString stringWithFormat:@"manual JIT allocation failed or changed size: %s", jit_error]);
        setenv("ANDROID_ART_ROOT", [[g_java_root stringByAppendingPathComponent:@"com.android.art"] fileSystemRepresentation], 1);
        setenv("ANDROID_I18N_ROOT", [[g_java_root stringByAppendingPathComponent:@"com.android.i18n"] fileSystemRepresentation], 1);
        setenv("ANDROID_TZDATA_ROOT", [[g_java_root stringByAppendingPathComponent:@"com.android.tzdata"] fileSystemRepresentation], 1);
        setenv("ANDROID_ROOT", g_system_root.fileSystemRepresentation, 1);
        setenv("ANDROID_DATA", g_data_root.fileSystemRepresentation, 1);
        g_shared_root = [g_data_root stringByAppendingPathComponent:@"sdcard"];
        if (![[NSFileManager defaultManager] createDirectoryAtPath:g_shared_root withIntermediateDirectories:YES attributes:nil error:NULL])
            return failed(HUSK_RUNTIME_INVALID_PATH, error, capacity, @"cannot create private external-storage directory");
        tl_set_shared_storage(g_shared_root.fileSystemRepresentation);
        tl_set_system_fonts_dir(g_system_root.fileSystemRepresentation);
        tl_set_cacerts_dir(g_cacerts.fileSystemRepresentation);
        tl_hle_set_ca_bundle(g_ca_bundle.fileSystemRepresentation);
        tl_guest_exit_hook = guest_exited;
        tl_session_set_cancellable(true);
        tl_ld_set_verbosity(0);
        tl_jni_set_trace(0);
        tl_dvm_set_trace(0);
        const char *boot[9];
        for (size_t i = 0; i < g_boot_paths.count; i++) boot[i] = g_boot_paths[i].fileSystemRepresentation;
        tl_log_line("runtime-app: booting %s with %llu MiB manual JIT", package, (unsigned long long)(manual >> 20));
        tl_jni_init();
        if (!tl_dvm_start(boot, (int)g_boot_paths.count))
            return failed(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"Java Runtime bootstrap failed");
        g_dvm_started = true;
        tl_ld_add_search_dir([[g_java_root stringByAppendingPathComponent:@"com.android.i18n/lib64"] fileSystemRepresentation]);
        tl_ld_add_search_dir([[g_java_root stringByAppendingPathComponent:@"com.android.conscrypt/lib64"] fileSystemRepresentation]);
        if (!tl_dvm_load_natives([[g_java_root stringByAppendingPathComponent:@"com.android.art/lib64"] fileSystemRepresentation]))
            tl_log_line("runtime-app: some libcore native methods were unavailable; continuing with Runtime fallbacks");
        tl_audio_install();
        if (!config->metal_layer) tl_web_harness_size(config->width, config->height, config->density);
        tl_javaapp_config internal = {
            .apk_path = g_apk.fileSystemRepresentation, .data_dir = g_data_root.fileSystemRepresentation,
            .package_name = g_package_name.UTF8String, .width = config->width, .height = config->height,
            .density = config->density, .metal_layer = config->metal_layer,
            .angle_egl = g_egl.fileSystemRepresentation, .angle_gles = g_gles.fileSystemRepresentation,
            .frame_dir = g_frames.fileSystemRepresentation, .frame_every = config->frame_every,
            .framework_res = g_framework_res.fileSystemRepresentation, .night_mode = config->night_mode != 0,
            .show_keyboard = config->show_keyboard ? keyboard : NULL,
            .set_clipboard = config->set_clipboard ? set_clipboard : NULL,
            .get_clipboard = config->get_clipboard ? get_clipboard : NULL,
            .share = config->share ? share : NULL, .open_url = config->open_url ? open_url : NULL,
            .set_orientation = config->set_orientation ? orientation : NULL,
            .pick_documents = config->pick_documents ? documents : NULL,
            .vibrate = config->vibrate ? vibrate : NULL,
            .insets = {config->insets[0], config->insets[1], config->insets[2], config->insets[3]}
        };
        g_java_attempted = true;
        if (!tl_javaapp_start(&internal))
            return failed(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"APK initialization failed; terminate this process after cleanup");
        atomic_store(&g_state, HUSK_RUNTIME_APP_RUNNING);
        tl_log_line("runtime-app: APK started: %s", package);
        return HUSK_RUNTIME_OK;
    }
}

static int32_t app_stop(char *error, size_t capacity)
{
    if (error && capacity) error[0] = '\0';
    if ([NSThread isMainThread])
        return error_result(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"stop must run on a worker while the main run loop remains active");
    int state = atomic_load(&g_state);
    if (state == HUSK_RUNTIME_APP_STOPPED) return HUSK_RUNTIME_OK;
    if (state == HUSK_RUNTIME_APP_IDLE || state == HUSK_RUNTIME_APP_STARTING || state == HUSK_RUNTIME_APP_STOPPING)
        return error_result(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"start and stop must be serialized by the host");
    if (!atomic_compare_exchange_strong(&g_state, &state, HUSK_RUNTIME_APP_STOPPING))
        return error_result(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"application state changed during stop");
    /* This profile always ends its process after stop. Do not make later file
     * operations fail with ECANCELED while native workers finish: Go/SQLite
     * treats that synthetic I/O error as a panic and replaces a requested
     * normal close with exit(2). The session still stops Java entry/loopers;
     * uncooperative native workers are retained until the host exits. */
    tl_session_set_cancellable(false);
    tl_session_request_stop();
    if (g_java_attempted && !tl_javaapp_stop())
        return failed(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"APK workers did not drain; retain resources and terminate the application process");
    if (g_dvm_started && !tl_dvm_end_session())
        return failed(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"Java Runtime did not drain; terminate the application process");
    atomic_store(&g_state, HUSK_RUNTIME_APP_STOPPED);
    return HUSK_RUNTIME_OK;
}
static int32_t app_state(void) { return atomic_load(&g_state); }
static void touch(int32_t phase, int32_t pointer, float x, float y) { if (running() && phase >= 0 && phase <= 3 && isfinite(x) && isfinite(y)) tl_javaapp_touch(phase, pointer, x, y); }
static void key(int32_t code, int32_t down) { if (running()) tl_javaapp_key(code, down != 0); }
static void back(void) { if (running()) tl_javaapp_back(); }
static void text(const char *utf8) { if (running() && utf8) tl_javaapp_text(utf8); }
static void text_delete(void) { if (running()) tl_javaapp_text_delete(); }
static void text_action(void) { if (running()) tl_javaapp_text_action(); }
static void text_command(int32_t command) { if (running() && command >= 1 && command <= 3) tl_javaapp_text_command(command); }
static void keyboard_closed(void) { if (running()) tl_javaapp_keyboard_closed(); }
static void pause_app(int32_t paused) { if (running()) { tl_javaapp_set_paused(paused != 0); tl_audio_set_paused(paused != 0); } }
static void insets(int32_t left, int32_t top, int32_t right, int32_t bottom, int32_t keyboard_bottom) { if (running()) tl_javaapp_set_insets(left, top, right, bottom, keyboard_bottom); }
static void picked(const char *const *paths, int32_t count) { if (running() && count >= 0 && (paths || !count)) tl_javaapp_documents_picked(paths, count); }
static uint64_t frames(void) { return running() ? tl_javaapp_frames() : 0; }
static int32_t ended(void) { return atomic_load(&g_guest_ended) || (running() && tl_javaapp_ended()); }
static int32_t attach_web(void *screen, void *container, float scale, int32_t fresh, char *error, size_t capacity)
{
    if (error && capacity) error[0] = '\0';
    if (![NSThread isMainThread] || !screen || !container || !isfinite(scale) || scale <= 0)
        return error_result(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"web attachment requires main-thread host views and a positive scale");
    tl_web_attach(screen, container, scale, fresh != 0);
    return HUSK_RUNTIME_OK;
}
static int32_t web_hit(float x, float y) { return running() && tl_web_hit(x, y); }
static int32_t web_axes(float x, float y) { return running() ? tl_web_parent_scroll_axes(x, y) : 0; }
static int32_t resize_app(int32_t width, int32_t height, char *error, size_t capacity)
{
    if (error && capacity) error[0] = '\0';
    if (width <= 0 || height <= 0 || width > 16384 || height > 16384)
        return error_result(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, @"invalid surface dimensions");
    if (![NSThread isMainThread] || !running())
        return error_result(HUSK_RUNTIME_INVALID_STATE, error, capacity, @"resize requires a running app and the host main thread");
    tl_javaapp_resize(width, height);
    return HUSK_RUNTIME_OK;
}

static const husk_runtime_app_api_v1 g_app_api = {
    .struct_size = sizeof(husk_runtime_app_api_v1), .abi_version = HUSK_RUNTIME_APP_ABI_V1,
    .start = app_start, .stop = app_stop, .state = app_state,
    .touch = touch, .key = key, .back = back, .text = text, .text_delete = text_delete,
    .text_action = text_action, .text_command = text_command, .keyboard_closed = keyboard_closed, .pause = pause_app,
    .set_insets = insets, .documents_picked = picked, .frames = frames, .ended = ended,
    .attach_web = attach_web, .web_hit = web_hit, .web_parent_scroll_axes = web_axes, .resize = resize_app
};

const husk_runtime_app_api_v1 *husk_runtime_get_app_api(uint32_t requested_abi, uint32_t caller_table_size)
{
    return requested_abi == HUSK_RUNTIME_APP_ABI_V1 && caller_table_size >= HUSK_RUNTIME_APP_V1_REQUIRED_SIZE ? &g_app_api : NULL;
}
