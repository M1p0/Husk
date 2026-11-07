/* SPDX-License-Identifier: GPL-2.0-or-later */
/* APK-fingerprinted module on the versioned public patch ABI. */
#define _DARWIN_C_SOURCE
#include "target_guard.h"

static void *runtime_handle;
static char runtime_path[4096];
static bool use_code, use_v8, use_vulkan;
static void *(*guest_dlopen)(const char *, int);
static const husk_runtime_patch_api_v1 *patch;

static bool enabled(const char *name)
{
    const char *value = getenv(name);
    return value && !strcmp(value, "1");
}

static void *open_guest(const char *path, int flags)
{
    /* ANGLE's capability-probing context must not own Vulkan's drawable. */
    if (path && !strcmp(path, "libvulkan.so")) patch->offscreen_gl(1);
    return guest_dlopen(path, flags);
}

static bool v8_flags(void *lib)
{
    int *page = patch->library_data(lib, 0x12dae3d0u, sizeof(int));
    unsigned char *freeze = patch->library_data(lib, 0x12dae557u, 1);
    unsigned char *jitless = patch->library_data(lib, 0x12dae080u, 1);
    int old_page = -1;
    unsigned char old_freeze = 255, old_jitless = 255;
    if (!read_memory(page, &old_page, sizeof(old_page)) || old_page != 0 ||
        !read_memory(freeze, &old_freeze, 1) || old_freeze != 1 ||
        !read_memory(jitless, &old_jitless, 1) || old_jitless != 0) return false;
    /* V8 11.8.172.18: these precede EnforceFlagImplications/Initialize.
     * Its FlagValues object has only 4 KiB alignment; freezing it on this
     * 16 KiB host would protect neighbouring mutable globals too. */
    *page = 16;
    *freeze = 0;
    *jitless = 1;
    return true;
}

static void on_library_init(void *lib, void *userdata)
{
    (void)userdata;
    const char *name = patch->library_name(lib);
    if (!name || strcmp(strrchr(name, '/') ? strrchr(name, '/') + 1 : name, "libUE4.so")) return;
    if (!verify_target(runtime_handle, runtime_path)) {
        fprintf(stderr, "patch[mingchao-startup]: target fingerprints rejected\n");
        return;
    }
    const char *symbols[] = {
        "_ZN12FAndroidMisc15ShouldUseVulkanEv", "_ZN12FAndroidMisc17IsVulkanAvailableEv"
    };
    const unsigned char expected[2][8] = {
        {0xfd,0x7b,0xbc,0xa9,0xf7,0x0b,0x00,0xf9}, {0xff,0x83,0x01,0xd1,0xfd,0x7b,0x02,0xa9}
    };
    void *functions[2] = {0};
    if (use_vulkan) for (int i = 0; i < 2; i++) {
        unsigned char actual[8];
        functions[i] = patch->library_symbol(lib, symbols[i]);
        if (!functions[i] || !read_memory(patch->library_data(lib, (uintptr_t)functions[i]-(uintptr_t)patch->library_base(lib), 8), actual, 8) ||
            memcmp(actual, expected[i], 8)) {
            fprintf(stderr, "patch[mingchao-startup]: Vulkan symbol precondition rejected\n");
            return;
        }
    }
    if (use_code) {
        unsigned char bytes[64];
        const void *original = patch->library_data(lib, 0x11b2cda0u, sizeof(bytes));
        if (!read_memory(original, bytes, sizeof(bytes)) ||
            !memory_matches(bytes, sizeof(bytes), "359f0b03f0113791d4fe25466df85e85b67489097193f1cd86f21cca47bd78bd") ||
            !patch->retranslate(lib, 0x11b20000u, 0x6e000u)) {
            fprintf(stderr, "patch[mingchao-startup]: decrypted Oodle code precondition or translation rejected\n");
            return;
        }
        fprintf(stderr, "patch[mingchao-startup]: Oodle code translated after library initialization\n");
    }
    if (use_v8) {
        if (!v8_flags(lib)) {
            fprintf(stderr, "patch[mingchao-startup]: V8 flag preconditions rejected\n");
            return;
        }
        fprintf(stderr, "patch[mingchao-startup]: V8 configured for 16 KiB pages and interpreter execution\n");
    }
    if (use_vulkan) {
        const unsigned code[] = {0x52800020u, 0xd65f03c0u};
        for (int i = 0; i < 2; i++) if (!patch->write_code(functions[i], code, sizeof(code))) {
            fprintf(stderr, "patch[mingchao-startup]: Vulkan selection write failed\n");
            return;
        }
        fprintf(stderr, "patch[mingchao-startup]: selected mobile Vulkan\n");
    }
}

static void *open_runtime(const char *path, int flags)
{
    void *result = dlopen(path, flags);
    bool code = enabled("HUSK_PATCH_MINGCHAO_UE4_CODE");
    bool v8 = enabled("HUSK_PATCH_MINGCHAO_V8");
    bool vulkan = enabled("HUSK_PATCH_MINGCHAO_VULKAN");
    if (!result || !path || !(code || v8 || vulkan) || !strstr(path, "/libHuskRuntime.dylib")) return result;
    /* RTLD_NOLOAD queries (including thread recognition) reopen the same handle.
     * Initialization and callback registration are once per Runtime instance. */
    if (result == runtime_handle) return result;
    patch=patch_api(result,TARGET_PATCH_CAPABILITIES);
    if (!patch) {
        fprintf(stderr,"patch[mingchao-startup]: patch ABI/capabilities unavailable\n");return result;
    }
    if (code && !patch->enable_code_views()) {
        fprintf(stderr,"patch[mingchao-startup]: code views requested after guest loading\n");return result;
    }
    if (vulkan) {
        const char *driver=getenv("HUSK_PATCH_MINGCHAO_MOLTENVK");
        if (!driver || !file_matches(driver,"aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f") ||
            !patch->configure_vulkan(driver)) return result;
        guest_dlopen=patch->guest_symbol("dlopen");if (!guest_dlopen) return result;
        patch->interpose_guest("dlopen",open_guest);
    }
    runtime_handle = result;
    snprintf(runtime_path, sizeof(runtime_path), "%s", path);
    use_code = code; use_v8 = v8; use_vulkan = vulkan;
    if (!patch->add_init_hook(on_library_init,NULL))
        fprintf(stderr,"patch[mingchao-startup]: initialization hook registration rejected\n");
    return result;
}

__attribute__((used, section("__DATA,__interpose,interposing")))
static struct { const void *replacement, *original; } binding = {(void *)open_runtime, (void *)dlopen};
