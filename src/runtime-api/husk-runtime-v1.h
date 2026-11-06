/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HUSK_RUNTIME_V1_H
#define HUSK_RUNTIME_V1_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define HUSK_RUNTIME_ABI_V1 UINT32_C(1)
#define HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES (UINT64_C(32) << 20)

typedef enum husk_runtime_status {
    HUSK_RUNTIME_OK = 0,
    HUSK_RUNTIME_INVALID_ARGUMENT = 1,
    HUSK_RUNTIME_UNSUPPORTED_ABI = 2,
    HUSK_RUNTIME_INVALID_PATH = 3,
    HUSK_RUNTIME_PATH_OVERLAP = 4,
    HUSK_RUNTIME_INVALID_STATE = 5,
    HUSK_RUNTIME_NO_MEMORY = 6
} husk_runtime_status;

typedef enum husk_runtime_capability_state {
    HUSK_RUNTIME_CAPABILITY_UNSUPPORTED = 0,
    HUSK_RUNTIME_CAPABILITY_OBSERVED = 1,
    HUSK_RUNTIME_CAPABILITY_SIMULATED = 2,
    HUSK_RUNTIME_CAPABILITY_ENFORCED_AND_VERIFIED = 3
} husk_runtime_capability_state;

#define HUSK_RUNTIME_CAP_CONFIGURE (UINT64_C(1) << 0)
#define HUSK_RUNTIME_CAP_NATIVE_CORE_LINKED (UINT64_C(1) << 1)
/* Advertised only by a complete application profile. */
#define HUSK_RUNTIME_CAP_APK_LAUNCH (UINT64_C(1) << 2)

typedef struct husk_runtime_info_v1 {
    uint32_t struct_size;
    uint32_t abi_version;
    const char *build_id;
    const char *host_target;
    uint64_t capabilities;
    uint32_t hard_memory_limit_state;
    uint32_t reserved;
} husk_runtime_info_v1;

/* The message is borrowed for the duration of the callback. The callback may
 * execute on runtime threads, must return promptly and must not throw or call
 * configure/close. Its userdata must remain valid until close has returned. */
typedef void (*husk_runtime_log_v1)(void *userdata, const char *utf8);

typedef struct husk_runtime_context_v1 {
    uint32_t struct_size;
    uint32_t abi_version;
    const char *app_id;
    const char *generation_id;
    const char *runtime_root;
    const char *package_root;
    const char *data_root;
    const char *cache_root;
    const char *config_root;
    /* Recorded exactly, including zero; configure neither supplies a default
     * nor allocates executable memory. */
    uint64_t manual_jit_bytes;
    /* Zero selects the existing 32 MiB setting; other nonzero values must
     * equal HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES. No memory is allocated here. */
    uint64_t java_worker_stack_bytes;
    husk_runtime_log_v1 log;
    void *log_userdata;
} husk_runtime_context_v1;

typedef struct husk_runtime_handle husk_runtime_handle;

typedef struct husk_runtime_api_v1 {
    uint32_t struct_size;
    uint32_t abi_version;
    /* Static, borrowed information, valid while the library remains loaded. */
    const husk_runtime_info_v1 *(*get_info)(void);
    /* All strings are copied. Every root must be an absolute existing
     * directory; canonical runtime/package roots may not overlap any writable
     * root. Writable roots may be nested. At most one handle is active in this
     * process. Validation does not grant a sandbox or enforce memory limits.
     * An error message is written when error_capacity > 0 and error != NULL. */
    int32_t (*configure)(const husk_runtime_context_v1 *context,
                         husk_runtime_handle **out_handle,
                         char *error, size_t error_capacity);
    /* Borrowed canonical view, valid until close; NULL for an inactive handle.
     * Callers must coordinate view use with close and never reuse a closed
     * handle, even after another configure. No private runtime layout is public. */
    const husk_runtime_context_v1 *(*get_context)(const husk_runtime_handle *handle);
    /* Releases configuration, not a running APK. Waits for in-flight log
     * callbacks. Thread cancellation is deferred until cleanup and unlocking
     * are complete, then the caller's previous cancellation state is restored.
     * The handle and its view become invalid on success. */
    int32_t (*close)(husk_runtime_handle *handle, char *error, size_t error_capacity);
} husk_runtime_api_v1;

/* Stable bootstrap entry point. Only ABI 1 and a caller table size at
 * least sizeof(husk_runtime_api_v1) are accepted. NULL means incompatible.
 * This table configures a process without initializing JIT, Java or an APK.
 * Application profiles expose a separate versioned app getter. */
__attribute__((visibility("default")))
const husk_runtime_api_v1 *husk_runtime_get_api(uint32_t requested_abi,
                                             uint32_t caller_table_size);

#ifdef __cplusplus
}
#endif
#endif
