/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Dynamic ABI tests: compile this file alone, then pass a real Runtime dylib.
 * clang -std=c11 -Wall -Wextra -Werror -Isrc/runtime-api \
 *   tests/runtime/context_api_test.c -o build/runtime-context-test
 * build/runtime-context-test /absolute/path/to/libHuskRuntime.dylib
 */
#include "husk-runtime-v1.h"

#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define REQUIRE(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "FAIL: %s:%d: %s\n", __FILE__, __LINE__, #condition); \
        exit(1); \
    } \
} while (0)

enum { ROOT_RUNTIME, ROOT_PACKAGE, ROOT_DATA, ROOT_CACHE, ROOT_CONFIG, ROOT_COUNT };
static char g_root[PATH_MAX];
static char g_dirs[ROOT_COUNT][PATH_MAX];
static char g_alias[PATH_MAX], g_nested_cache[PATH_MAX], g_file[PATH_MAX];
static const husk_runtime_api_v1 *g_api;

static void cleanup(void)
{
    /* No globbing, recursive removal, shared /tmp paths or process-name kills. */
    if (g_alias[0]) unlink(g_alias);
    if (g_file[0]) unlink(g_file);
    if (g_nested_cache[0]) rmdir(g_nested_cache);
    for (size_t i = 0; i < ROOT_COUNT; i++) if (g_dirs[i][0]) rmdir(g_dirs[i]);
    if (g_root[0]) rmdir(g_root);
}

static void child_path(char *out, size_t capacity, const char *name)
{
    int count = snprintf(out, capacity, "%s/%s", g_root, name);
    REQUIRE(count > 0 && (size_t)count < capacity);
}

static husk_runtime_context_v1 valid_context(void)
{
    return (husk_runtime_context_v1){
        .struct_size = sizeof(husk_runtime_context_v1),
        .abi_version = HUSK_RUNTIME_ABI_V1,
        .app_id = "org.husk.tests.context",
        .generation_id = "context-generation-1",
        .runtime_root = g_dirs[ROOT_RUNTIME],
        .package_root = g_dirs[ROOT_PACKAGE],
        .data_root = g_dirs[ROOT_DATA],
        .cache_root = g_dirs[ROOT_CACHE],
        .config_root = g_dirs[ROOT_CONFIG],
        .manual_jit_bytes = (UINT64_C(73) << 20) + 123,
        .java_worker_stack_bytes = HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES
    };
}

static void close_handle(husk_runtime_handle **handle)
{
    char error[256];
    REQUIRE(*handle != NULL);
    REQUIRE(g_api->close(*handle, error, sizeof(error)) == HUSK_RUNTIME_OK);
    *handle = NULL; /* No API calls use a handle after successful close. */
}

static void prove_idle(void)
{
    husk_runtime_context_v1 context = valid_context();
    husk_runtime_handle *handle = NULL;
    char error[256];
    REQUIRE(g_api->configure(&context, &handle, error, sizeof(error)) == HUSK_RUNTIME_OK);
    REQUIRE(g_api->get_context(handle) != NULL);
    close_handle(&handle);
}

static void expect_failure(const char *name, const husk_runtime_context_v1 *context, int32_t status)
{
    husk_runtime_handle *handle = NULL;
    char error[256] = {0};
    int32_t actual = g_api->configure(context, &handle, error, sizeof(error));
    if (actual != status)
        fprintf(stderr, "%s: status %d, expected %d; error: %s\n", name, actual, status, error);
    REQUIRE(actual == status);
    REQUIRE(handle == NULL);
    REQUIRE(error[0] != '\0');
    prove_idle();
}

static void copied_context_test(void)
{
    char app[] = "org.husk.tests.copied";
    char generation[] = "copied-generation";
    char paths[ROOT_COUNT][PATH_MAX];
    for (size_t i = 0; i < ROOT_COUNT; i++) strcpy(paths[i], g_dirs[i]);
    husk_runtime_context_v1 context = valid_context();
    context.app_id = app;
    context.generation_id = generation;
    context.runtime_root = paths[ROOT_RUNTIME];
    context.package_root = paths[ROOT_PACKAGE];
    context.data_root = paths[ROOT_DATA];
    context.cache_root = paths[ROOT_CACHE];
    context.config_root = paths[ROOT_CONFIG];
    husk_runtime_handle *handle = NULL;
    char error[256];
    REQUIRE(g_api->configure(&context, &handle, error, sizeof(error)) == HUSK_RUNTIME_OK);
    const husk_runtime_context_v1 *view = g_api->get_context(handle);
    REQUIRE(view && view->struct_size == sizeof(*view) && view->abi_version == HUSK_RUNTIME_ABI_V1);
    REQUIRE(view->manual_jit_bytes == context.manual_jit_bytes);
    REQUIRE(view->java_worker_stack_bytes == HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES);
    memset(app, 'x', sizeof(app) - 1);
    memset(generation, 'y', sizeof(generation) - 1);
    for (size_t i = 0; i < ROOT_COUNT; i++) memset(paths[i], 'z', strlen(paths[i]));
    REQUIRE(!strcmp(view->app_id, "org.husk.tests.copied"));
    REQUIRE(!strcmp(view->generation_id, "copied-generation"));
    const char *view_paths[] = { view->runtime_root, view->package_root,
                                view->data_root, view->cache_root, view->config_root };
    for (size_t i = 0; i < ROOT_COUNT; i++) {
        char *canonical = realpath(g_dirs[i], NULL);
        REQUIRE(canonical != NULL);
        REQUIRE(!strcmp(view_paths[i], canonical));
        free(canonical);
    }
    husk_runtime_context_v1 duplicate = valid_context();
    husk_runtime_handle *second = NULL;
    REQUIRE(g_api->configure(&duplicate, &second, error, sizeof(error)) == HUSK_RUNTIME_INVALID_STATE);
    REQUIRE(second == NULL);
    REQUIRE(!strcmp(g_api->get_context(handle)->app_id, "org.husk.tests.copied"));
    close_handle(&handle);
    prove_idle();
    puts("PASS: copied identity/paths, canonical view, exact JIT snapshot and one active configuration");
}

static void validation_test(void)
{
    husk_runtime_context_v1 context;
    expect_failure("null context", NULL, HUSK_RUNTIME_INVALID_ARGUMENT);
    context = valid_context(); context.struct_size = 0;
    expect_failure("small structure", &context, HUSK_RUNTIME_INVALID_ARGUMENT);
    context = valid_context(); context.abi_version = HUSK_RUNTIME_ABI_V1 + 1;
    expect_failure("context ABI", &context, HUSK_RUNTIME_UNSUPPORTED_ABI);
    context = valid_context(); context.app_id = "";
    expect_failure("empty app identity", &context, HUSK_RUNTIME_INVALID_ARGUMENT);
    context = valid_context(); context.generation_id = NULL;
    expect_failure("missing generation", &context, HUSK_RUNTIME_INVALID_ARGUMENT);
    context = valid_context(); context.package_root = "relative/package";
    expect_failure("relative path", &context, HUSK_RUNTIME_INVALID_PATH);
    char missing[PATH_MAX]; child_path(missing, sizeof(missing), "missing");
    context = valid_context(); context.runtime_root = missing;
    expect_failure("missing directory", &context, HUSK_RUNTIME_INVALID_PATH);
    context = valid_context(); context.data_root = g_file;
    expect_failure("regular file", &context, HUSK_RUNTIME_INVALID_PATH);
    context = valid_context(); context.data_root = context.runtime_root;
    expect_failure("equal read/write roots", &context, HUSK_RUNTIME_PATH_OVERLAP);
    context = valid_context(); context.runtime_root = g_root;
    expect_failure("read root contains write roots", &context, HUSK_RUNTIME_PATH_OVERLAP);
    context = valid_context(); context.data_root = g_root;
    expect_failure("write root contains read roots", &context, HUSK_RUNTIME_PATH_OVERLAP);
    context = valid_context(); context.config_root = g_alias;
    expect_failure("symlink aliases read root", &context, HUSK_RUNTIME_PATH_OVERLAP);
    context = valid_context(); context.java_worker_stack_bytes = UINT64_C(64) << 20;
    expect_failure("changed Java stack", &context, HUSK_RUNTIME_INVALID_ARGUMENT);

    char error[256];
    context = valid_context();
    REQUIRE(g_api->configure(&context, NULL, error, sizeof(error)) == HUSK_RUNTIME_INVALID_ARGUMENT);
    prove_idle();
    puts("PASS: ABI, identity, paths, aliases and stack validation leave no active configuration");
}

static void defaults_and_buffers_test(void)
{
    husk_runtime_context_v1 context = valid_context();
    context.manual_jit_bytes = 0; /* An explicit zero quota, never an automatic default. */
    context.java_worker_stack_bytes = 0;
    context.cache_root = g_nested_cache; /* Writable roots may be nested. */
    husk_runtime_handle *handle = NULL;
    char guard[] = { 'L', 'X', 'R' };
    REQUIRE(g_api->configure(&context, &handle, &guard[1], 0) == HUSK_RUNTIME_OK);
    REQUIRE(guard[0] == 'L' && guard[1] == 'X' && guard[2] == 'R');
    const husk_runtime_context_v1 *view = g_api->get_context(handle);
    REQUIRE(view && view->manual_jit_bytes == 0);
    REQUIRE(view->java_worker_stack_bytes == HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES);
    REQUIRE(g_api->close(handle, &guard[1], 1) == HUSK_RUNTIME_OK);
    handle = NULL;
    REQUIRE(guard[0] == 'L' && guard[1] == '\0' && guard[2] == 'R');

    context.abi_version = HUSK_RUNTIME_ABI_V1 + 1;
    guard[1] = 'X';
    REQUIRE(g_api->configure(&context, &handle, &guard[1], 0) == HUSK_RUNTIME_UNSUPPORTED_ABI);
    REQUIRE(guard[0] == 'L' && guard[1] == 'X' && guard[2] == 'R');
    REQUIRE(handle == NULL);
    REQUIRE(g_api->configure(&context, &handle, &guard[1], 1) == HUSK_RUNTIME_UNSUPPORTED_ABI);
    REQUIRE(guard[0] == 'L' && guard[1] == '\0' && guard[2] == 'R');
    REQUIRE(handle == NULL);
    REQUIRE(g_api->configure(&context, &handle, NULL, 128) == HUSK_RUNTIME_UNSUPPORTED_ABI);
    REQUIRE(handle == NULL);
    prove_idle();
    puts("PASS: explicit zero JIT, default 32 MiB stack, nested writable roots and bounded error buffers");
}

enum { CONFIGURE_THREADS = 8 };
struct configure_race {
    pthread_mutex_t lock;
    pthread_cond_t changed;
    bool go;
    husk_runtime_context_v1 context;
    int32_t status[CONFIGURE_THREADS];
    husk_runtime_handle *handle[CONFIGURE_THREADS];
};
struct configure_job { struct configure_race *race; size_t index; };

static void *configure_thread(void *pointer)
{
    struct configure_job *job = pointer;
    struct configure_race *race = job->race;
    pthread_mutex_lock(&race->lock);
    while (!race->go) pthread_cond_wait(&race->changed, &race->lock);
    pthread_mutex_unlock(&race->lock);
    char error[128];
    race->status[job->index] = g_api->configure(&race->context, &race->handle[job->index], error, sizeof(error));
    return NULL;
}

static void concurrent_configure_test(void)
{
    struct configure_race race = {
        .lock = PTHREAD_MUTEX_INITIALIZER,
        .changed = PTHREAD_COND_INITIALIZER,
        .context = valid_context()
    };
    pthread_t threads[CONFIGURE_THREADS];
    struct configure_job jobs[CONFIGURE_THREADS];
    for (size_t i = 0; i < CONFIGURE_THREADS; i++) {
        jobs[i] = (struct configure_job){ .race = &race, .index = i };
        REQUIRE(pthread_create(&threads[i], NULL, configure_thread, &jobs[i]) == 0);
    }
    pthread_mutex_lock(&race.lock);
    race.go = true;
    pthread_cond_broadcast(&race.changed);
    pthread_mutex_unlock(&race.lock);
    size_t winners = 0;
    husk_runtime_handle *winner = NULL;
    for (size_t i = 0; i < CONFIGURE_THREADS; i++) {
        REQUIRE(pthread_join(threads[i], NULL) == 0);
        if (race.status[i] == HUSK_RUNTIME_OK) {
            winners++;
            winner = race.handle[i];
            REQUIRE(winner != NULL);
        } else {
            REQUIRE(race.status[i] == HUSK_RUNTIME_INVALID_STATE);
            REQUIRE(race.handle[i] == NULL);
        }
    }
    REQUIRE(winners == 1);
    close_handle(&winner);
    REQUIRE(pthread_cond_destroy(&race.changed) == 0);
    REQUIRE(pthread_mutex_destroy(&race.lock) == 0);
    prove_idle();
    puts("PASS: concurrent configure publishes exactly one handle and can be configured again after close");
}

int main(int argc, char **argv)
{
    if (argc != 2 && !(argc == 3 && !strcmp(argv[2], "--app-profile"))) {
        fprintf(stderr, "usage: %s /absolute/path/to/Runtime.dylib [--app-profile]\n", argv[0]);
        return 2;
    }
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!library) {
        fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return 1;
    }
    typedef const husk_runtime_api_v1 *(*getter_fn)(uint32_t, uint32_t);
    getter_fn get_api = (getter_fn)dlsym(library, "husk_runtime_get_api");
    REQUIRE(get_api != NULL);
    REQUIRE(get_api(0, sizeof(husk_runtime_api_v1)) == NULL);
    REQUIRE(get_api(HUSK_RUNTIME_ABI_V1 + 1, sizeof(husk_runtime_api_v1)) == NULL);
    REQUIRE(get_api(HUSK_RUNTIME_ABI_V1, 0) == NULL);
    REQUIRE(get_api(HUSK_RUNTIME_ABI_V1, sizeof(husk_runtime_api_v1) - 1) == NULL);
    g_api = get_api(HUSK_RUNTIME_ABI_V1, sizeof(husk_runtime_api_v1));
    REQUIRE(g_api && g_api->struct_size >= sizeof(*g_api) && g_api->abi_version == HUSK_RUNTIME_ABI_V1);
    REQUIRE(get_api(HUSK_RUNTIME_ABI_V1, sizeof(husk_runtime_api_v1) + 128) == g_api);
    REQUIRE(g_api->get_info && g_api->configure && g_api->get_context && g_api->close);
    const husk_runtime_info_v1 *info = g_api->get_info();
    REQUIRE(info && info->abi_version == HUSK_RUNTIME_ABI_V1 && info->struct_size >= sizeof(*info));
    REQUIRE(info->build_id && info->build_id[0]);
    REQUIRE(!strcmp(info->host_target, "macos-arm64"));
    REQUIRE((info->capabilities & HUSK_RUNTIME_CAP_NATIVE_CORE_LINKED) != 0);
    REQUIRE(!!(info->capabilities & HUSK_RUNTIME_CAP_APK_LAUNCH) == (argc == 3));
    REQUIRE(info->hard_memory_limit_state == HUSK_RUNTIME_CAPABILITY_UNSUPPORTED);
    puts("PASS: dynamic entry point, ABI negotiation and profile capabilities");

    const char *temporary = getenv("TMPDIR");
    REQUIRE(temporary && temporary[0] == '/');
    int root_length = snprintf(g_root, sizeof(g_root), "%s/husk-runtime-context-XXXXXX", temporary);
    REQUIRE(root_length > 0 && (size_t)root_length < sizeof(g_root));
    REQUIRE(mkdtemp(g_root) != NULL);
    REQUIRE(atexit(cleanup) == 0);
    const char *names[] = { "runtime", "package", "data", "cache", "config" };
    for (size_t i = 0; i < ROOT_COUNT; i++) {
        child_path(g_dirs[i], sizeof(g_dirs[i]), names[i]);
        REQUIRE(mkdir(g_dirs[i], 0700) == 0);
    }
    child_path(g_alias, sizeof(g_alias), "runtime-alias");
    REQUIRE(symlink(g_dirs[ROOT_RUNTIME], g_alias) == 0);
    int count = snprintf(g_nested_cache, sizeof(g_nested_cache), "%s/nested-cache", g_dirs[ROOT_DATA]);
    REQUIRE(count > 0 && (size_t)count < sizeof(g_nested_cache));
    REQUIRE(mkdir(g_nested_cache, 0700) == 0);
    child_path(g_file, sizeof(g_file), "regular-file");
    FILE *file = fopen(g_file, "wb");
    REQUIRE(file && fclose(file) == 0);

    copied_context_test();
    validation_test();
    defaults_and_buffers_test();
    concurrent_configure_test();
    /* No runtime API or borrowed view is used after this point. */
    g_api = NULL;
    REQUIRE(dlclose(library) == 0);
    puts("PASS: runtime context API tests");
    return 0;
}
