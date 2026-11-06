/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Exercise the public app ABI in a fresh process without booting an APK. */
#import <Foundation/Foundation.h>
#include "husk-runtime-app-v1.h"
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

#define REQUIRE(c) do { if (!(c)) { fprintf(stderr, "FAIL %d: %s\n", __LINE__, #c); exit(1); } } while (0)
static const husk_runtime_app_api_v1 *api;
static husk_runtime_handle *handle;
static husk_runtime_app_config_v1 config;
static BOOL supports_apps;

static void *worker(void *unused)
{
    (void)unused;
    @autoreleasepool {
        char error[512] = {0};
        REQUIRE(api->start(handle, NULL, error, sizeof(error)) == HUSK_RUNTIME_INVALID_ARGUMENT);
        REQUIRE(api->state() == HUSK_RUNTIME_APP_IDLE);
        int result = api->start(handle, &config, error, sizeof(error));
        if (supports_apps) {
            /* The APK points outside package_root. Reject before any JIT or
             * guest native constructor, and disallow retry in this process. */
            REQUIRE(result == HUSK_RUNTIME_INVALID_PATH);
            REQUIRE(strstr(error, "package_root") != NULL);
            REQUIRE(api->state() == HUSK_RUNTIME_APP_FAILED);
            REQUIRE(api->start(handle, &config, error, sizeof(error)) == HUSK_RUNTIME_INVALID_STATE);
            REQUIRE(api->stop(error, sizeof(error)) == HUSK_RUNTIME_OK);
            REQUIRE(api->state() == HUSK_RUNTIME_APP_STOPPED);
            REQUIRE(api->stop(error, sizeof(error)) == HUSK_RUNTIME_OK);
        } else {
            REQUIRE(result == HUSK_RUNTIME_INVALID_STATE);
            REQUIRE(strstr(error, "profile") != NULL);
            REQUIRE(api->state() == HUSK_RUNTIME_APP_IDLE);
        }
    }
    return NULL;
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        REQUIRE(argc == 2);
        void *image = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        REQUIRE(image != NULL);
        const husk_runtime_api_v1 *(*base_getter)(uint32_t,uint32_t) = dlsym(image, "husk_runtime_get_api");
        const husk_runtime_app_api_v1 *(*getter)(uint32_t,uint32_t) = dlsym(image, "husk_runtime_get_app_api");
        REQUIRE(getter && base_getter);
        REQUIRE(getter(0, sizeof(*api)) == NULL);
        REQUIRE(getter(2, sizeof(*api)) == NULL);
        REQUIRE(getter(1, sizeof(*api) - 1) == NULL);
        api = getter(1, sizeof(*api));
        REQUIRE(api && api->abi_version == 1 && api->struct_size >= sizeof(*api));
        const husk_runtime_api_v1 *base = base_getter(1, sizeof(husk_runtime_api_v1));
        supports_apps = !!(base->get_info()->capabilities & HUSK_RUNTIME_CAP_APK_LAUNCH);
        char temp[] = "/tmp/husk-app-abi-XXXXXX";
        REQUIRE(mkdtemp(temp));
        NSString *root = [NSString stringWithUTF8String:temp];
        NSMutableArray<NSString *> *dirs = [NSMutableArray array];
        for (NSString *name in @[@"runtime", @"package", @"data", @"cache", @"config"]) {
            NSString *path = [root stringByAppendingPathComponent:name];
            REQUIRE([NSFileManager.defaultManager createDirectoryAtPath:path withIntermediateDirectories:NO attributes:nil error:NULL]);
            [dirs addObject:path];
        }
        NSString *outside = [root stringByAppendingPathComponent:@"outside.apk"];
        REQUIRE([[@"not an APK" dataUsingEncoding:NSUTF8StringEncoding] writeToFile:outside atomically:YES]);
        husk_runtime_context_v1 context = {.struct_size=sizeof(context), .abi_version=1,
            .app_id="dev.husk.app-abi", .generation_id="app-abi",
            .runtime_root=dirs[0].fileSystemRepresentation, .package_root=dirs[1].fileSystemRepresentation,
            .data_root=dirs[2].fileSystemRepresentation, .cache_root=dirs[3].fileSystemRepresentation,
            .config_root=dirs[4].fileSystemRepresentation, .manual_jit_bytes=512ull<<20,
            .java_worker_stack_bytes=HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES};
        char error[512];
        REQUIRE(base->configure(&context, &handle, error, sizeof(error)) == HUSK_RUNTIME_OK);
        config = (husk_runtime_app_config_v1){.struct_size=sizeof(config), .abi_version=1,
            .apk_path=outside.fileSystemRepresentation, .angle_egl=outside.fileSystemRepresentation,
            .angle_gles=outside.fileSystemRepresentation, .width=720, .height=1280, .density=2};
        REQUIRE(api->state() == HUSK_RUNTIME_APP_IDLE);
        REQUIRE(api->start(handle, &config, error, sizeof(error)) == HUSK_RUNTIME_INVALID_STATE);
        REQUIRE(api->stop(error, sizeof(error)) == HUSK_RUNTIME_INVALID_STATE);
        REQUIRE(api->state() == HUSK_RUNTIME_APP_IDLE);
        REQUIRE(api->attach_web(NULL, NULL, 1, 1, error, sizeof(error)) == HUSK_RUNTIME_INVALID_ARGUMENT);
        api->touch(0, 0, 1, 1); api->key(4, 1); api->back(); api->text("idle");
        api->text_delete(); api->text_action(); api->text_command(HUSK_RUNTIME_TEXT_SELECT_ALL); api->keyboard_closed(); api->pause(1);
        REQUIRE(api->frames() == 0 && !api->ended() && !api->web_hit(1, 1));
        pthread_t thread;
        REQUIRE(pthread_create(&thread, NULL, worker, NULL) == 0);
        REQUIRE(pthread_join(thread, NULL) == 0);
        REQUIRE(base->close(handle, error, sizeof(error)) == HUSK_RUNTIME_OK);
        REQUIRE([NSFileManager.defaultManager removeItemAtPath:root error:NULL]);
        puts("PASS: app ABI negotiation, worker contract, pre-guest rejection and one-attempt lifecycle");
    }
    return 0;
}
