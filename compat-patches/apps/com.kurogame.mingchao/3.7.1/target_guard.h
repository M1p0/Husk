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
#include "husk-runtime-patch-v1.h"
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

static const husk_runtime_patch_api_v1 *patch_api(void *runtime, uint64_t required)
{
    husk_runtime_patch_getter_v1 getter=dlsym(runtime,"husk_runtime_get_patch_api");
    const husk_runtime_patch_api_v1 *api=getter?getter(1,sizeof(*api)):NULL;
    return api && api->abi_version==1 && api->struct_size>=sizeof(*api) &&
        (api->capabilities & required)==required?api:NULL;
}
static bool verify_target(void *runtime, const char *path)
{
    (void)path;
    const husk_runtime_patch_api_v1 *api=patch_api(runtime,HUSK_RUNTIME_PATCH_TARGET);
    return api && api->match_apk(TARGET_APK_SIZE,TARGET_APK_SHA256,
                                "lib/arm64-v8a/libtprt.so",TARGET_SDK_SHA256);
}
static bool read_memory(const void *address, void *out, size_t bytes)
{
    mach_vm_size_t got = 0;
    return mach_vm_read_overwrite(mach_task_self(), (mach_vm_address_t)address, bytes,
                                 (mach_vm_address_t)out, &got) == KERN_SUCCESS && got == bytes;
}
