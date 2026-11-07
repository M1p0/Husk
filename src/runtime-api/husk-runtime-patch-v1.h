/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HUSK_RUNTIME_PATCH_V1_H
#define HUSK_RUNTIME_PATCH_V1_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
#define HUSK_RUNTIME_PATCH_ABI_V1 UINT32_C(1)
#define HUSK_RUNTIME_PATCH_TARGET UINT64_C(1)
#define HUSK_RUNTIME_PATCH_CODE UINT64_C(2)
#define HUSK_RUNTIME_PATCH_LIBRARY UINT64_C(4)
#define HUSK_RUNTIME_PATCH_THREAD UINT64_C(8)
#define HUSK_RUNTIME_PATCH_VULKAN UINT64_C(16)
typedef void (*husk_runtime_patch_init_v1)(void *library, void *userdata);
/* Tables and opaque library handles remain valid for this Runtime's process
 * lifetime. Modules must stay loaded, return promptly from init callbacks and
 * register before any guest image loads. No Runtime object layout is public.
 * Code APIs are macOS arm64 JIT only. Enable before guest loading; query each
 * image's data address and quiesce the affected threads before code changes.
 * ABI 1 layout/signatures are frozen. A future API can coexist with ABI 1;
 * changing this contract requires a new ABI. Getter rejects unknown ABIs and
 * smaller caller tables. Build IDs, hashes
 * and private symbol addresses are intentionally outside compatibility checks. */
typedef struct husk_runtime_patch_api_v1 {
    uint32_t struct_size, abi_version;
    uint64_t capabilities;
    int32_t (*match_apk)(uint64_t size, const char *apk_sha256,
                         const char *entry, const char *entry_sha256);
    int32_t (*enable_code_views)(void);
    int32_t (*add_init_hook)(husk_runtime_patch_init_v1 callback, void *userdata);
    const char *(*library_name)(void *library);
    void *(*library_base)(void *library);
    void *(*library_symbol)(void *library, const char *name);
    void *(*library_data)(void *library, uint64_t vaddr, size_t bytes);
    int32_t (*write_code)(const void *address, const void *bytes, size_t count);
    int32_t (*retranslate)(void *library, uint64_t vaddr, size_t bytes);
    /* Recognizes this Runtime's host thunk and reads its internal argument;
     * unrelated starts or invalid arguments return zero without changing output. */
    int32_t (*guest_thread_entry)(const void *start, const void *argument, void **entry);
    void *(*guest_symbol)(const char *name);
    void (*interpose_guest)(const char *name, void *replacement);
    int32_t (*configure_vulkan)(const char *driver);
    void (*offscreen_gl)(int32_t enabled);
} husk_runtime_patch_api_v1;
typedef const husk_runtime_patch_api_v1 *(*husk_runtime_patch_getter_v1)(uint32_t, uint32_t);
__attribute__((visibility("default")))
const husk_runtime_patch_api_v1 *husk_runtime_get_patch_api(uint32_t abi, uint32_t caller_size);
#ifdef __cplusplus
}
#endif
#endif
