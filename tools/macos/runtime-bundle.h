/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Shared by the lightweight probe and AppKit launcher. No Runtime internals. */
#pragma once
#import <Foundation/Foundation.h>
#include <CommonCrypto/CommonDigest.h>
#include <mach-o/loader.h>
#include <mach/machine.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static bool directory(NSString *path)
{
    struct stat st;
    return lstat(path.fileSystemRepresentation, &st) == 0 && S_ISDIR(st.st_mode);
}
static NSString *canonical(NSString *path)
{
    if (!path.isAbsolutePath) return nil;
    char *resolved = realpath(path.fileSystemRepresentation, NULL);
    if (!resolved) return nil;
    NSString *result = [[NSFileManager defaultManager] stringWithFileSystemRepresentation:resolved length:strlen(resolved)];
    free(resolved);
    return result;
}
static inline double monotonic_seconds(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static bool safe_relative(NSString *path)
{
    if (!path.length || path.isAbsolutePath || [path containsString:@"\\"]) return false;
    for (NSString *part in [path componentsSeparatedByString:@"/"])
        if (!part.length || [part isEqual:@"."] || [part isEqual:@".."]) return false;
    return true;
}
static bool safe_file(NSString *root, NSString *relative)
{
    if (!safe_relative(relative)) return false;
    NSArray<NSString *> *parts = [relative componentsSeparatedByString:@"/"];
    NSString *path = root;
    for (NSUInteger i = 0; i < parts.count; i++) {
        path = [path stringByAppendingPathComponent:parts[i]];
        struct stat st;
        if (lstat(path.fileSystemRepresentation, &st)) return false;
        if (i + 1 == parts.count) {
            if (!S_ISREG(st.st_mode) || st.st_nlink != 1) return false;
        } else if (!S_ISDIR(st.st_mode)) return false;
    }
    return true;
}
static NSString *digest(NSString *path)
{
    int fd = open(path.fileSystemRepresentation, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return nil;
    CC_SHA256_CTX ctx;
    CC_SHA256_Init(&ctx);
    uint8_t buffer[64 * 1024], result[CC_SHA256_DIGEST_LENGTH];
    ssize_t n;
    while ((n = read(fd, buffer, sizeof(buffer))) > 0) CC_SHA256_Update(&ctx, buffer, (CC_LONG)n);
    close(fd);
    if (n < 0) return nil;
    CC_SHA256_Final(result, &ctx);
    NSMutableString *hex = [NSMutableString stringWithCapacity:64];
    for (size_t i = 0; i < sizeof(result); i++) [hex appendFormat:@"%02x", result[i]];
    return hex;
}
static NSData *read_manifest(NSString *path)
{
    const size_t limit = 8 * 1024 * 1024;
    int fd = open(path.fileSystemRepresentation, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return nil;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_nlink != 1 ||
        st.st_size <= 0 || (uint64_t)st.st_size > limit) { close(fd); return nil; }
    NSMutableData *data = [NSMutableData data];
    uint8_t buffer[64 * 1024];
    ssize_t n;
    while ((n = read(fd, buffer, sizeof(buffer))) != 0) {
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 || (size_t)n > limit - data.length) { close(fd); return nil; }
        [data appendBytes:buffer length:(NSUInteger)n];
    }
    close(fd);
    return data;
}
static bool macos_library(NSString *path)
{
    NSData *data = [NSData dataWithContentsOfFile:path options:NSDataReadingMappedIfSafe error:NULL];
    if (data.length < sizeof(struct mach_header_64)) return false;
    const struct mach_header_64 *h = data.bytes;
    if (h->magic != MH_MAGIC_64 || h->cputype != CPU_TYPE_ARM64 || h->filetype != MH_DYLIB) return false;
    if (h->sizeofcmds > data.length - sizeof(*h)) return false;
    const uint8_t *p = (const uint8_t *)data.bytes + sizeof(*h);
    const uint8_t *end = p + h->sizeofcmds;
    bool macos = false;
    for (uint32_t i = 0; i < h->ncmds; i++) {
        if ((size_t)(end - p) < sizeof(struct load_command)) return false;
        const struct load_command *command = (const void *)p;
        if (command->cmdsize < sizeof(*command) || command->cmdsize % 8 || command->cmdsize > (size_t)(end - p)) return false;
        if (command->cmd == LC_BUILD_VERSION) {
            if (command->cmdsize < sizeof(struct build_version_command)) return false;
            macos = ((const struct build_version_command *)p)->platform == PLATFORM_MACOS;
        }
        p += command->cmdsize;
    }
    return macos;
}
static NSDictionary *validate_bundle(NSString *root, NSString **error)
{
    if (!safe_file(root, @"manifest.json")) { *error = @"manifest must be a regular, unlinked file"; return nil; }
    NSData *manifest_data = read_manifest([root stringByAppendingPathComponent:@"manifest.json"]);
    if (!manifest_data.length) {
        *error = @"manifest unreadable, empty or too large"; return nil;
    }
    NSDictionary *manifest = [NSJSONSerialization JSONObjectWithData:manifest_data options:0 error:NULL];
    if (![manifest isKindOfClass:NSDictionary.class] ||
        ![manifest[@"schema"] isEqual:@1] || ![manifest[@"abi_version"] isEqual:@1] ||
        ![manifest[@"host_target"] isEqual:@"macos-arm64"] ||
        (![manifest[@"profile"] isEqual:@"headless-bootstrap"] && ![manifest[@"profile"] isEqual:@"macos-app"]) ||
        ![manifest[@"build_id"] isEqual:root.lastPathComponent]) {
        *error = @"manifest schema, ABI, target, profile or build ID mismatch"; return nil;
    }
    NSDictionary *artifacts = manifest[@"artifacts"];
    if (![artifacts isKindOfClass:NSDictionary.class] || !artifacts.count) {
        *error = @"manifest has no artifacts"; return nil;
    }
    for (NSString *required in @[@"lib/libHuskRuntime.dylib", @"resources/java-runtime/husk-framework.dex", @"resources/cacert.pem"])
        if (!artifacts[required]) { *error = [@"missing required artifact: " stringByAppendingString:required]; return nil; }
    if ([manifest[@"profile"] isEqual:@"macos-app"])
        for (NSString *required in @[@"resources/angle/libEGL.dylib", @"resources/angle/libGLESv2.dylib", @"include/husk-runtime-app-v1.h"])
            if (!artifacts[required]) { *error = [@"missing app artifact: " stringByAppendingString:required]; return nil; }
    for (id key in artifacts) {
        id value = artifacts[key];
        if (![key isKindOfClass:NSString.class] || ![value isKindOfClass:NSString.class] ||
            [value length] != 64 || !safe_file(root, key) ||
            ![digest([root stringByAppendingPathComponent:key]) isEqual:value]) {
            *error = [NSString stringWithFormat:@"artifact missing, unsafe or hash mismatch: %@", key]; return nil;
        }
    }
    NSMutableSet *found = [NSMutableSet set];
    NSDirectoryEnumerator *enumerator = [[NSFileManager defaultManager] enumeratorAtPath:root];
    NSString *relative;
    while ((relative = [enumerator nextObject])) {
        struct stat st;
        if (lstat([root stringByAppendingPathComponent:relative].fileSystemRepresentation, &st)) {
            *error = @"artifact enumeration failed"; return nil;
        }
        if (S_ISDIR(st.st_mode)) continue;
        if (!safe_file(root, relative)) { *error = @"bundle contains a symlink or non-regular artifact"; return nil; }
        if (![relative isEqual:@"manifest.json"]) [found addObject:relative];
    }
    if (![found isEqualToSet:[NSSet setWithArray:artifacts.allKeys]]) {
        *error = @"manifest and actual artifact set differ"; return nil;
    }
    if (!macos_library([root stringByAppendingPathComponent:@"lib/libHuskRuntime.dylib"])) {
        *error = @"Runtime is not an arm64 macOS dylib"; return nil;
    }
    if ([manifest[@"profile"] isEqual:@"macos-app"])
        for (NSString *relative in @[@"resources/angle/libEGL.dylib", @"resources/angle/libGLESv2.dylib"])
            if (!macos_library([root stringByAppendingPathComponent:relative])) {
                *error = @"ANGLE must be an arm64 macOS dylib pair"; return nil;
            }
    return manifest;
}
static bool same_lock(int fd, NSString *path)
{
    struct stat held, named;
    return fstat(fd, &held) == 0 && lstat(path.fileSystemRepresentation, &named) == 0 &&
        S_ISREG(held.st_mode) && S_ISREG(named.st_mode) &&
        held.st_nlink == 1 && named.st_nlink == 1 &&
        held.st_dev == named.st_dev && held.st_ino == named.st_ino;
}
