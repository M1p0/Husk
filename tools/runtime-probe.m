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
#include "macos/runtime-bundle.h"

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
        if (!unsigned_number(args[@"--jit-mib"], &jit) || jit > UINT64_MAX / (1u << 20) ||
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
            (!!(info->capabilities & HUSK_RUNTIME_CAP_APK_LAUNCH) != [manifest[@"profile"] isEqual:@"macos-app"]) ||
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
            @"host_target": utf8(info->host_target), @"profile": manifest[@"profile"],
            @"app_id": utf8(view->app_id), @"generation_id": utf8(view->generation_id),
            @"runtime_root": utf8(view->runtime_root), @"package_root": utf8(view->package_root),
            @"data_root": utf8(view->data_root), @"cache_root": utf8(view->cache_root), @"config_root": utf8(view->config_root),
            @"manual_jit_bytes": @(view->manual_jit_bytes), @"java_worker_stack_bytes": @(view->java_worker_stack_bytes),
            @"hard_memory_limit": @"unsupported", @"apk_execution": (info->capabilities & HUSK_RUNTIME_CAP_APK_LAUNCH) ? @"available-not-exercised" : @"not-implemented", @"context_copied": @YES});
        double until = monotonic_seconds() + hold;
        while (monotonic_seconds() < until) usleep(10000);
        if (api->close(handle, message, sizeof(message)) != HUSK_RUNTIME_OK)
            return failure(utf8(message), 4);
        close(fd);
        return 0;
    }
}
