/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include <CommonCrypto/CommonDigest.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "husk-tl-internal.h"
#include "target.h"

static void digest_hex(const unsigned char digest[CC_SHA256_DIGEST_LENGTH], char out[65])
{
    for (unsigned i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) snprintf(out + i * 2, 3, "%02x", digest[i]);
}
static bool memory_matches(const void *bytes, size_t size, const char *expected)
{
    CC_SHA256_CTX ctx; unsigned char digest[CC_SHA256_DIGEST_LENGTH]; char hex[65];
    CC_SHA256_Init(&ctx);
    for (size_t i = 0; i < size;) {
        size_t n = size - i < (1u << 20) ? size - i : (1u << 20);
        CC_SHA256_Update(&ctx, (const char *)bytes + i, (CC_LONG)n); i += n;
    }
    CC_SHA256_Final(digest, &ctx); digest_hex(digest, hex);
    return !strcmp(hex, expected);
}
static bool file_matches(const char *path, const char *expected)
{
    int fd = open(path, O_RDONLY); if (fd < 0) return false;
    unsigned char buffer[65536], digest[CC_SHA256_DIGEST_LENGTH]; char hex[65]; CC_SHA256_CTX ctx;
    CC_SHA256_Init(&ctx); ssize_t n;
    while ((n = read(fd, buffer, sizeof(buffer))) > 0) CC_SHA256_Update(&ctx, buffer, (CC_LONG)n);
    close(fd); if (n < 0) return false;
    CC_SHA256_Final(digest, &ctx); digest_hex(digest, hex);
    return !strcmp(hex, expected);
}

/* The private zip API is used only with the exact pinned Runtime artifact. */
static bool verify_target(void *runtime, const char *path)
{
    if (!file_matches(path, TARGET_RUNTIME_SHA256)) return false;
    const tl_zip *(*apk_at)(int) = dlsym(runtime, "tl_ld_apk_at");
    const tl_zip_entry *(*find)(const tl_zip *, const char *) = dlsym(runtime, "tl_zip_find");
    bool (*data)(const tl_zip *, const tl_zip_entry *, size_t, const uint8_t **, size_t *, bool *, char *, size_t) = dlsym(runtime, "tl_zip_data");
    if (!apk_at || !find || !data) return false;
    for (int i = 0; i < 48; i++) {
        const tl_zip *apk = apk_at(i); if (!apk) break;
        if (apk->size != TARGET_APK_SIZE || !memory_matches(apk->map, apk->size, TARGET_APK_SHA256)) continue;
        const tl_zip_entry *entry = find(apk, "lib/arm64-v8a/libtprt.so");
        const uint8_t *bytes = NULL; size_t size = 0; bool owned = false; char error[256];
        if (!entry || !data(apk, entry, 4u << 20, &bytes, &size, &owned, error, sizeof(error))) return false;
        bool ok = memory_matches(bytes, size, TARGET_SDK_SHA256);
        if (owned) free((void *)bytes);
        return ok;
    }
    return false;
}
static bool read_memory(const void *address, void *out, size_t bytes)
{
    mach_vm_size_t got = 0;
    return mach_vm_read_overwrite(mach_task_self(), (mach_vm_address_t)address, bytes,
                                 (mach_vm_address_t)out, &got) == KERN_SUCCESS && got == bytes;
}
