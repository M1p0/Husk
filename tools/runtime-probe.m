/* SPDX-License-Identifier: GPL-2.0-or-later */
/* M1 development launcher: one immutable Runtime per new process, no APK execution. */
#import <Foundation/Foundation.h>
#include "../src/runtime-api/husk-runtime-v1.h"
#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <mach-o/loader.h>
#include <mach/machine.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static void report(NSDictionary *value)
{
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:NULL];
    fwrite(data.bytes, 1, data.length, stdout);
    fputc('\n', stdout);
    fflush(stdout);
}
static int failure(NSString *message, int code)
{
    report(@{@"ok": @NO, @"error": message ?: @"unknown failure", @"pid": @(getpid())});
    return code;
}
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
static double monotonic_seconds(void)
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
    NSData *manifest_data = [NSData dataWithContentsOfFile:[root stringByAppendingPathComponent:@"manifest.json"]];
    if (!manifest_data.length || manifest_data.length > 8 * 1024 * 1024) {
        *error = @"manifest unreadable, empty or too large"; return nil;
    }
    NSDictionary *manifest = [NSJSONSerialization JSONObjectWithData:manifest_data options:0 error:NULL];
    if (![manifest isKindOfClass:NSDictionary.class] ||
        ![manifest[@"schema"] isEqual:@1] || ![manifest[@"abi_version"] isEqual:@1] ||
        ![manifest[@"host_target"] isEqual:@"macos-arm64"] ||
        ![manifest[@"profile"] isEqual:@"headless-bootstrap"] ||
        ![manifest[@"build_id"] isEqual:root.lastPathComponent]) {
        *error = @"manifest schema, ABI, target, profile or build ID mismatch"; return nil;
    }
    NSDictionary *artifacts = manifest[@"artifacts"];
    if (![artifacts isKindOfClass:NSDictionary.class] || !artifacts.count) {
        *error = @"manifest has no artifacts"; return nil;
    }
    for (NSString *required in @[@"lib/libHuskRuntime.dylib", @"resources/java-runtime/husk-framework.dex", @"resources/cacert.pem"])
        if (!artifacts[required]) { *error = [@"missing required artifact: " stringByAppendingString:required]; return nil; }
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
static void host_log(void *userdata, const char *text)
{
    (void)userdata;
    fprintf(stderr, "runtime: %s\n", text);
}
static NSString *utf8(const char *value)
{
    return value ? [NSString stringWithUTF8String:value] : nil;
}
static bool unsigned_number(NSString *text, unsigned long long *value)
{
    if (!text.length || [text rangeOfCharacterFromSet:NSCharacterSet.decimalDigitCharacterSet.invertedSet].location != NSNotFound) return false;
    errno = 0;
    char *end;
    *value = strtoull(text.UTF8String, &end, 10);
    return !errno && !*end;
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        NSMutableDictionary<NSString *, NSString *> *args = [NSMutableDictionary dictionary];
        NSSet *keys = [NSSet setWithArray:@[@"--runtime", @"--package", @"--data", @"--cache", @"--config",
            @"--app-id", @"--generation", @"--jit-mib", @"--hold-seconds", @"--requested-abi", @"--table-size", @"--lease-timeout-ms"]];
        for (int i = 1; i < argc; i += 2) {
            NSString *key = utf8(argv[i]);
            if (![keys containsObject:key] || i + 1 >= argc || args[key])
                return failure(@"arguments must be unique known --key value pairs", 2);
            args[key] = utf8(argv[i + 1]);
        }
        for (NSString *key in @[@"--runtime", @"--package", @"--data", @"--cache", @"--config", @"--app-id", @"--generation", @"--jit-mib"])
            if (!args[key].length) return failure([@"missing argument " stringByAppendingString:key], 2);
        unsigned long long jit, hold = 0, requested = 1, table_size = sizeof(husk_runtime_api_v1), timeout = 1000;
        if (!unsigned_number(args[@"--jit-mib"], &jit) || !jit || jit > UINT64_MAX / (1u << 20) ||
            (args[@"--hold-seconds"] && !unsigned_number(args[@"--hold-seconds"], &hold)) || hold > 3600 ||
            (args[@"--requested-abi"] && !unsigned_number(args[@"--requested-abi"], &requested)) || requested > UINT32_MAX ||
            (args[@"--table-size"] && !unsigned_number(args[@"--table-size"], &table_size)) || table_size > UINT32_MAX ||
            (args[@"--lease-timeout-ms"] && !unsigned_number(args[@"--lease-timeout-ms"], &timeout)) || timeout > 10000)
            return failure(@"invalid numeric argument", 2);
        NSString *root = canonical(args[@"--runtime"]);
        if (!root || !directory(root)) return failure(@"Runtime root must be an absolute existing directory", 2);
        NSString *lease_dir = [root.stringByDeletingLastPathComponent stringByAppendingPathComponent:@".leases"];
        if (!directory(lease_dir)) return failure(@"missing or unsafe lease directory", 2);
        NSString *lease = [lease_dir stringByAppendingPathComponent:[root.lastPathComponent stringByAppendingString:@".lock"]];
        int fd = open(lease.fileSystemRepresentation, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
        if (fd < 0 || !same_lock(fd, lease)) {
            if (fd >= 0) close(fd);
            return failure(@"missing or unsafe stable lease file", 2);
        }
        double deadline = monotonic_seconds() + timeout / 1000.0;
        bool announced_wait = false;
        while (flock(fd, LOCK_SH | LOCK_NB)) {
            if ((errno != EWOULDBLOCK && errno != EAGAIN) || monotonic_seconds() >= deadline) {
                close(fd); return failure(@"version lease unavailable or timed out", 2);
            }
            if (!announced_wait) { fputs("waiting for version lease\n", stderr); fflush(stderr); announced_wait = true; }
            usleep(10000);
        }
        if (!same_lock(fd, lease)) { close(fd); return failure(@"lease object replaced during acquisition", 2); }
        NSString *error;
        NSDictionary *manifest = validate_bundle(root, &error);
        if (!manifest || !same_lock(fd, lease)) {
            close(fd); return failure(error ?: @"lease object replaced during validation", 2);
        }
        NSString *library = [root stringByAppendingPathComponent:@"lib/libHuskRuntime.dylib"];
        void *image = dlopen(library.fileSystemRepresentation, RTLD_NOW | RTLD_LOCAL);
        if (!image) { close(fd); return failure(utf8(dlerror()), 3); }
        /* Runtime images are never hot-unloaded; all subsequent failure paths exit this process. */
        const husk_runtime_api_v1 *(*get_api)(uint32_t, uint32_t) = dlsym(image, "husk_runtime_get_api");
        const husk_runtime_api_v1 *api = get_api ? get_api((uint32_t)requested, (uint32_t)table_size) : NULL;
        if (!api || api->struct_size < sizeof(*api) || api->abi_version != 1 ||
            !api->get_info || !api->configure || !api->get_context || !api->close)
            return failure(@"Runtime ABI or table size incompatible", 3);
        const husk_runtime_info_v1 *info = api->get_info();
        if (!info || info->struct_size < sizeof(*info) || info->abi_version != 1 ||
            ![utf8(info->build_id) isEqual:manifest[@"build_id"]] ||
            ![utf8(info->host_target) isEqual:@"macos-arm64"] ||
            !(info->capabilities & HUSK_RUNTIME_CAP_NATIVE_CORE_LINKED) ||
            (info->capabilities & HUSK_RUNTIME_CAP_APK_LAUNCH) ||
            info->hard_memory_limit_state != HUSK_RUNTIME_CAPABILITY_UNSUPPORTED)
            return failure(@"loaded Runtime identity or profile does not match manifest", 3);
        NSString *values[] = {args[@"--app-id"], args[@"--generation"], root, args[@"--package"],
            args[@"--data"], args[@"--cache"], args[@"--config"]};
        char *copies[7] = {0};
        for (size_t i = 0; i < 7; i++) {
            copies[i] = strdup(values[i].UTF8String);
            if (!copies[i]) return failure(@"cannot allocate launch context", 4);
        }
        husk_runtime_context_v1 context = {
            .struct_size = sizeof(context), .abi_version = 1,
            .app_id = copies[0], .generation_id = copies[1], .runtime_root = copies[2],
            .package_root = copies[3], .data_root = copies[4], .cache_root = copies[5], .config_root = copies[6],
            .manual_jit_bytes = jit << 20, .java_worker_stack_bytes = HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES,
            .log = host_log
        };
        char message[512] = {0};
        husk_runtime_handle *handle = NULL;
        int32_t status = api->configure(&context, &handle, message, sizeof(message));
        for (size_t i = 0; i < 7; i++) { memset(copies[i], 'x', strlen(copies[i])); free(copies[i]); }
        if (status != HUSK_RUNTIME_OK) return failure(utf8(message), 4);
        const husk_runtime_context_v1 *view = api->get_context(handle);
        if (!view || ![utf8(view->app_id) isEqual:values[0]] ||
            ![utf8(view->generation_id) isEqual:values[1]] ||
            ![utf8(view->runtime_root) isEqual:root] ||
            view->manual_jit_bytes != (jit << 20) ||
            view->java_worker_stack_bytes != HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES)
            return failure(@"Runtime did not preserve its owned context", 4);
        husk_runtime_handle *duplicate = NULL;
        if (api->configure(view, &duplicate, message, sizeof(message)) != HUSK_RUNTIME_INVALID_STATE || duplicate)
            return failure(@"Runtime accepted a second active App context", 4);
        report(@{@"ok": @YES, @"pid": @(getpid()), @"build_id": utf8(info->build_id),
            @"host_target": utf8(info->host_target), @"profile": @"headless-bootstrap",
            @"app_id": utf8(view->app_id), @"generation_id": utf8(view->generation_id),
            @"runtime_root": utf8(view->runtime_root), @"package_root": utf8(view->package_root),
            @"data_root": utf8(view->data_root), @"cache_root": utf8(view->cache_root), @"config_root": utf8(view->config_root),
            @"manual_jit_bytes": @(view->manual_jit_bytes), @"java_worker_stack_bytes": @(view->java_worker_stack_bytes),
            @"hard_memory_limit": @"unsupported", @"apk_execution": @"not-implemented", @"context_copied": @YES});
        double until = monotonic_seconds() + hold;
        while (monotonic_seconds() < until) usleep(10000);
        if (api->close(handle, message, sizeof(message)) != HUSK_RUNTIME_OK)
            return failure(utf8(message), 4);
        close(fd);
        return 0;
    }
}
