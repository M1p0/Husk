/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "../runtime-api/husk-runtime-v1.h"

#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#if !defined(__APPLE__) || (!defined(__arm64__) && !defined(__aarch64__))
#error "The M1 runtime adapter currently targets Apple Silicon macOS only."
#endif
#include <TargetConditionals.h>
#if !TARGET_OS_OSX
#error "Build the M1 adapter for macOS, not an iOS or Simulator target."
#endif

#ifndef HUSK_RUNTIME_BUILD_ID
#define HUSK_RUNTIME_BUILD_ID "development"
#endif
#ifndef HUSK_RUNTIME_ENABLE_APPS
#define HUSK_RUNTIME_ENABLE_APPS 0
#endif

struct husk_runtime_handle {
    husk_runtime_context_v1 context;
};

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_callbacks_drained = PTHREAD_COND_INITIALIZER;
static husk_runtime_handle *g_active;
static bool g_closing;
static size_t g_callbacks;
static _Thread_local unsigned g_callback_depth;

static const husk_runtime_info_v1 g_info = {
    .struct_size = sizeof(husk_runtime_info_v1),
    .abi_version = HUSK_RUNTIME_ABI_V1,
    .build_id = HUSK_RUNTIME_BUILD_ID,
    .host_target = "macos-arm64",
    .capabilities = HUSK_RUNTIME_CAP_CONFIGURE | HUSK_RUNTIME_CAP_NATIVE_CORE_LINKED |
        (HUSK_RUNTIME_ENABLE_APPS ? HUSK_RUNTIME_CAP_APK_LAUNCH : 0),
    .hard_memory_limit_state = HUSK_RUNTIME_CAPABILITY_UNSUPPORTED,
    .reserved = 0
};

static int32_t failure(int32_t code, char *error, size_t capacity, const char *format, ...)
{
    if (error && capacity) {
        va_list ap;
        va_start(ap, format);
        vsnprintf(error, capacity, format, ap);
        va_end(ap);
    }
    return code;
}

static void clear_error(char *error, size_t capacity)
{
    if (error && capacity) error[0] = '\0';
}

static void discard(husk_runtime_handle *handle)
{
    if (!handle) return;
    free((void *)handle->context.app_id);
    free((void *)handle->context.generation_id);
    free((void *)handle->context.runtime_root);
    free((void *)handle->context.package_root);
    free((void *)handle->context.data_root);
    free((void *)handle->context.cache_root);
    free((void *)handle->context.config_root);
    free(handle);
}

static bool identifier_valid(const char *text)
{
    /* These identifiers are metadata, never interpolated into file paths. */
    return text && text[0] && strnlen(text, 1025) <= 1024;
}

static int32_t canonical_directory(const char *input, const char *name,
                                   const char **output, char *error, size_t capacity)
{
    if (!input || input[0] != '/' || strnlen(input, PATH_MAX) == PATH_MAX)
        return failure(HUSK_RUNTIME_INVALID_PATH, error, capacity,
                       "%s must be an absolute existing directory", name);
    errno = 0;
    char *canonical = realpath(input, NULL);
    if (!canonical) {
        int saved = errno;
        return failure(saved == ENOMEM ? HUSK_RUNTIME_NO_MEMORY : HUSK_RUNTIME_INVALID_PATH,
                       error, capacity, "%s cannot be resolved: %s", name, strerror(saved));
    }
    struct stat info;
    if (stat(canonical, &info) != 0) {
        int saved = errno;
        free(canonical);
        return failure(HUSK_RUNTIME_INVALID_PATH, error, capacity,
                       "%s cannot be inspected: %s", name, strerror(saved));
    }
    if (!S_ISDIR(info.st_mode)) {
        free(canonical);
        return failure(HUSK_RUNTIME_INVALID_PATH, error, capacity,
                       "%s is not a directory", name);
    }
    *output = canonical;
    return HUSK_RUNTIME_OK;
}

static bool ancestor_or_equal(const char *a, const char *b)
{
    size_t n = strlen(a);
    return !strcmp(a, b) || (n == 1 && a[0] == '/')
        || (!strncmp(a, b, n) && b[n] == '/');
}

static bool overlap(const char *a, const char *b)
{
    return ancestor_or_equal(a, b) || ancestor_or_equal(b, a);
}

static const husk_runtime_info_v1 *runtime_info(void)
{
    return &g_info;
}

static int32_t runtime_configure(const husk_runtime_context_v1 *context,
                                 husk_runtime_handle **out_handle,
                                 char *error, size_t capacity)
{
    clear_error(error, capacity);
    if (!out_handle)
        return failure(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, "out_handle is required");
    *out_handle = NULL;
    if (!context || context->struct_size < sizeof(husk_runtime_context_v1))
        return failure(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, "context is missing or too small");
    if (context->abi_version != HUSK_RUNTIME_ABI_V1)
        return failure(HUSK_RUNTIME_UNSUPPORTED_ABI, error, capacity, "context ABI is unsupported");
    if (!identifier_valid(context->app_id) || !identifier_valid(context->generation_id))
        return failure(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity,
                       "app_id and generation_id must contain 1 to 1024 bytes");
    if (context->java_worker_stack_bytes
        && context->java_worker_stack_bytes != HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES)
        return failure(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity,
                       "Java worker stack must retain the 32 MiB setting");

    pthread_mutex_lock(&g_lock);
    bool busy = g_active || g_closing;
    pthread_mutex_unlock(&g_lock);
    if (busy)
        return failure(HUSK_RUNTIME_INVALID_STATE, error, capacity,
                       "this process already has an active configuration");

    husk_runtime_handle *candidate = calloc(1, sizeof(*candidate));
    if (!candidate)
        return failure(HUSK_RUNTIME_NO_MEMORY, error, capacity, "cannot allocate configuration");
    candidate->context.struct_size = sizeof(husk_runtime_context_v1);
    candidate->context.abi_version = HUSK_RUNTIME_ABI_V1;
    candidate->context.app_id = strdup(context->app_id);
    candidate->context.generation_id = strdup(context->generation_id);
    if (!candidate->context.app_id || !candidate->context.generation_id) {
        discard(candidate);
        return failure(HUSK_RUNTIME_NO_MEMORY, error, capacity, "cannot copy application identity");
    }

    const char *inputs[] = { context->runtime_root, context->package_root,
                            context->data_root, context->cache_root, context->config_root };
    const char *names[] = { "runtime_root", "package_root", "data_root", "cache_root", "config_root" };
    const char **outputs[] = { &candidate->context.runtime_root, &candidate->context.package_root,
                              &candidate->context.data_root, &candidate->context.cache_root,
                              &candidate->context.config_root };
    for (size_t i = 0; i < sizeof(inputs) / sizeof(inputs[0]); i++) {
        int32_t status = canonical_directory(inputs[i], names[i], outputs[i], error, capacity);
        if (status != HUSK_RUNTIME_OK) {
            discard(candidate);
            return status;
        }
    }
    for (size_t read = 0; read < 2; read++) {
        for (size_t write = 2; write < 5; write++) {
            if (overlap(*outputs[read], *outputs[write])) {
                discard(candidate);
                return failure(HUSK_RUNTIME_PATH_OVERLAP, error, capacity,
                               "%s overlaps %s after path canonicalization", names[read], names[write]);
            }
        }
    }
    candidate->context.manual_jit_bytes = context->manual_jit_bytes;
    candidate->context.java_worker_stack_bytes = HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES;
    candidate->context.log = context->log;
    candidate->context.log_userdata = context->log_userdata;

    /* Validation is side-effect free. Two concurrent configurations may both
     * validate, but only one can publish a handle. The loser owns no state. */
    pthread_mutex_lock(&g_lock);
    if (g_active || g_closing) {
        pthread_mutex_unlock(&g_lock);
        discard(candidate);
        return failure(HUSK_RUNTIME_INVALID_STATE, error, capacity,
                       "another configuration became active");
    }
    g_active = candidate;
    pthread_mutex_unlock(&g_lock);
    *out_handle = candidate;
    return HUSK_RUNTIME_OK;
}

static const husk_runtime_context_v1 *runtime_context(const husk_runtime_handle *handle)
{
    pthread_mutex_lock(&g_lock);
    const husk_runtime_context_v1 *view =
        handle && handle == g_active && !g_closing ? &g_active->context : NULL;
    pthread_mutex_unlock(&g_lock);
    return view;
}

static int32_t runtime_close(husk_runtime_handle *handle, char *error, size_t capacity)
{
    clear_error(error, capacity);
    if (!handle)
        return failure(HUSK_RUNTIME_INVALID_ARGUMENT, error, capacity, "handle is required");
    if (g_callback_depth)
        return failure(HUSK_RUNTIME_INVALID_STATE, error, capacity,
                       "close cannot be called from a log callback");
    /* A canceled pthread_cond_wait reacquires its mutex before unwinding.
     * Keep close non-cancelable while it owns the lock or closing transition;
     * pending cancellation can be delivered only after resources are released. */
    int previous_cancel_state;
    int cancel_status = pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &previous_cancel_state);
    if (cancel_status)
        return failure(HUSK_RUNTIME_INVALID_STATE, error, capacity,
                       "cannot guard close against thread cancellation: %s", strerror(cancel_status));
    pthread_mutex_lock(&g_lock);
    if (handle != g_active || g_closing) {
        pthread_mutex_unlock(&g_lock);
        int32_t result = failure(HUSK_RUNTIME_INVALID_STATE, error, capacity, "handle is not active");
        pthread_setcancelstate(previous_cancel_state, NULL);
        return result;
    }
    g_closing = true;
    while (g_callbacks) pthread_cond_wait(&g_callbacks_drained, &g_lock);
    g_active = NULL;
    /* Free while configure is excluded, so a successful close fully releases
     * its owned configuration before another one can become active. */
    discard(handle);
    g_closing = false;
    pthread_mutex_unlock(&g_lock);
    pthread_setcancelstate(previous_cancel_state, NULL);
    return HUSK_RUNTIME_OK;
}

static const husk_runtime_api_v1 g_api = {
    .struct_size = sizeof(husk_runtime_api_v1),
    .abi_version = HUSK_RUNTIME_ABI_V1,
    .get_info = runtime_info,
    .configure = runtime_configure,
    .get_context = runtime_context,
    .close = runtime_close
};

const husk_runtime_api_v1 *husk_runtime_get_api(uint32_t requested_abi,
                                             uint32_t caller_table_size)
{
    if (requested_abi != HUSK_RUNTIME_ABI_V1 || caller_table_size < sizeof(g_api)) return NULL;
    return &g_api;
}

/* Existing core sources use this internal logger. The dynamic library owns
 * its definition; the launcher does not need to export private tl_* symbols. */
struct log_cleanup {
    char *allocated;
};

static void finish_log_callback(void *pointer)
{
    struct log_cleanup *cleanup = pointer;
    free(cleanup->allocated);
    g_callback_depth--;
    pthread_mutex_lock(&g_lock);
    g_callbacks--;
    if (!g_callbacks) pthread_cond_broadcast(&g_callbacks_drained);
    pthread_mutex_unlock(&g_lock);
}

void tl_log_line(const char *format, ...)
{
    if (!format) return;
    char small[1024] = {0};
    char *allocated = NULL;
    const char *message = small;
    va_list ap, again;
    va_start(ap, format);
    va_copy(again, ap);
    int length = vsnprintf(small, sizeof(small), format, ap);
    va_end(ap);
    if (length < 0) {
        message = "runtime log formatting failed";
    } else if ((size_t)length >= sizeof(small) && length < (64 << 10)) {
        allocated = malloc((size_t)length + 1);
        if (allocated) {
            vsnprintf(allocated, (size_t)length + 1, format, again);
            message = allocated;
        }
    }
    va_end(again);

    husk_runtime_log_v1 callback = NULL;
    void *userdata = NULL;
    pthread_mutex_lock(&g_lock);
    if (g_active && !g_closing && !g_callback_depth && g_active->context.log) {
        callback = g_active->context.log;
        userdata = g_active->context.log_userdata;
        g_callbacks++;
    }
    pthread_mutex_unlock(&g_lock);
    if (callback) {
        struct log_cleanup cleanup = { .allocated = allocated };
        g_callback_depth++;
        pthread_cleanup_push(finish_log_callback, &cleanup);
        callback(userdata, message);
        pthread_cleanup_pop(1);
    } else {
        flockfile(stderr);
        fprintf(stderr, "%s\n", message);
        funlockfile(stderr);
        free(allocated);
    }
}

/* The console bridge's existing weak hook asks whether an APK is active.
 * M1 has no APK execution API and therefore always reports no active APK. */
const char *husk_native_loaded_apk(void)
{
    return NULL;
}
