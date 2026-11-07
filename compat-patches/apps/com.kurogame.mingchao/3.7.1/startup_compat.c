/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Explicit, version-pinned experiments. Each feature requires its own flag. */
#define _DARWIN_C_SOURCE
#include "target_guard.h"

static void *runtime_handle;
static char runtime_path[4096];
static bool use_code, use_v8, use_vulkan;
static void *(*guest_dlopen)(const char *, int);
static void (*egl_offscreen)(bool);
static const char *(*library_name)(const void *);
static void *(*library_base)(const void *);
static void *(*library_sym)(void *, const char *);
static long (*view_delta)(void);
static bool (*write_code)(const void *, const void *, size_t);
static bool (*retranslate)(void *, uint64_t, size_t);

static bool enabled(const char *name)
{
    const char *value = getenv(name);
    return value && !strcmp(value, "1");
}

static void *open_guest(const char *path, int flags)
{
    /* ANGLE's capability-probing context must not own Vulkan's drawable. */
    if (path && !strcmp(path, "libvulkan.so") && egl_offscreen) egl_offscreen(true);
    return guest_dlopen(path, flags);
}

static bool v8_flags(void *lib)
{
    uintptr_t data = (uintptr_t)library_base(lib) + view_delta();
    int *page = (int *)(data + 0x12dae3d0u);
    unsigned char *freeze = (unsigned char *)(data + 0x12dae557u);
    unsigned char *jitless = (unsigned char *)(data + 0x12dae080u);
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

static void on_library_init(void *lib)
{
    const char *name = library_name(lib);
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
        functions[i] = library_sym(lib, symbols[i]);
        if (!functions[i] || !read_memory((char *)functions[i] + view_delta(), actual, 8) ||
            memcmp(actual, expected[i], 8)) {
            fprintf(stderr, "patch[mingchao-startup]: Vulkan symbol precondition rejected\n");
            return;
        }
    }
    if (use_code) {
        unsigned char bytes[64];
        const void *original = (char *)library_base(lib) + view_delta() + 0x11b2cda0u;
        if (!retranslate || !read_memory(original, bytes, sizeof(bytes)) ||
            !memory_matches(bytes, sizeof(bytes), "359f0b03f0113791d4fe25466df85e85b67489097193f1cd86f21cca47bd78bd") ||
            !retranslate(lib, 0x11b20000u, 0x6e000u)) {
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
        for (int i = 0; i < 2; i++) if (!write_code(functions[i], code, sizeof(code))) {
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
    if (!file_matches(path, TARGET_RUNTIME_SHA256)) {
        fprintf(stderr, "patch[mingchao-startup]: Runtime fingerprint rejected\n");
        return result;
    }
    if (vulkan) {
        const char *driver = getenv("HUSK_PATCH_MINGCHAO_MOLTENVK");
        if (!driver || !file_matches(driver, "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f")) {
            fprintf(stderr, "patch[mingchao-startup]: MoltenVK 1.4.2 fingerprint rejected\n");
            return result;
        }
        void (*configure)(const char *, const char *, int) = dlsym(result, "tl_vk_configure");
        bool (*available)(void) = dlsym(result, "tl_vk_available");
        void *(*find)(const char *) = dlsym(result, "tl_bionic_find");
        void (*interpose)(const char *, void *) = dlsym(result, "tl_ld_interpose");
        egl_offscreen = dlsym(result, "tl_egl_offscreen_windows");
        if (!configure || !available || !find || !interpose || !egl_offscreen) return result;
        configure(driver, NULL, 0);
        if (!available()) return result;
        guest_dlopen = find("dlopen");
        if (!guest_dlopen) return result;
        interpose("dlopen", open_guest);
    }
    void (*on_init)(void (*)(void *)) = dlsym(result, "tl_ld_on_init");
    library_name = dlsym(result, "tl_ld_lib_name");
    library_base = dlsym(result, "tl_ld_lib_base");
    library_sym = dlsym(result, "tl_ld_sym");
    view_delta = dlsym(result, "tl_xmem_delta");
    write_code = dlsym(result, "tl_ld_write_code");
    retranslate = dlsym(result, "tl_ld_retranslate_range");
    if (!on_init || !library_name || !library_base || !library_sym || !view_delta || !write_code || (code && !retranslate)) return result;
    runtime_handle = result;
    snprintf(runtime_path, sizeof(runtime_path), "%s", path);
    use_code = code; use_v8 = v8; use_vulkan = vulkan;
    on_init(on_library_init);
    return result;
}

__attribute__((used, section("__DATA,__interpose,interposing")))
static struct { const void *replacement, *original; } binding = {(void *)open_runtime, (void *)dlopen};
