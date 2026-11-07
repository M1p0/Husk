/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Optional SDK compatibility module. It is never a Runtime source input. */
#define _DARWIN_C_SOURCE
#include <CommonCrypto/CommonDigest.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <libkern/OSCacheControl.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "husk-tl-internal.h"
#include "target.h"

/* Entire observed probe, including the unused word following its return. */
static const uint32_t probe[] = {
    0xd10043ffu, 0xd2800008u, 0xf90007e0u, 0xf94007e9u, 0xeb08013fu, 0x1a9f07eau, 0x370000aau, 0xd2800008u,
    0xaa0803e0u, 0x910043ffu, 0xd65f03c0u, 0xf9000000u, 0xf9000401u, 0xf9000802u, 0xf9000c03u, 0xf9001004u,
    0xf9001405u, 0xf9001806u, 0xf9001c07u, 0xf9002008u, 0xf9002409u, 0xf900280au, 0xf9002c0bu, 0xf900300cu,
    0xf900340du, 0xf900380eu, 0xf9003c0fu, 0xf9004010u, 0xf9004411u, 0xf9004812u, 0xf9004c13u, 0xf900500au,
    0xf9005415u, 0xf9005816u, 0xf9005c17u, 0xf9006018u, 0xf9006419u, 0xf900681au, 0xf9006c1bu, 0xf900701cu,
    0xf900741du, 0xf900781eu, 0xf9007c1fu, 0xd10043ffu, 0xf90003e1u, 0xd2800001u, 0xf9008001u, 0xf94003e1u,
    0x910043ffu, 0xd2800000u, 0xd65f03c0u, 0x00000000u,
};

#include "target_guard.h"

static bool matches_probe(void *entry)
{
    uint32_t bytes[sizeof(probe) / 4];
    return read_memory(entry, bytes, sizeof(bytes)) && !memcmp(bytes, probe, sizeof(bytes));
}
static bool patch_probe(void *entry)
{
    if (!matches_probe(entry)) return false;
    size_t page = (size_t)getpagesize(), off = (uintptr_t)entry & (page - 1);
    if (off + sizeof(probe) > page) return false;
    vm_address_t writer = 0; vm_prot_t current, maximum;
    if (vm_remap(mach_task_self(), &writer, page, 0, VM_FLAGS_ANYWHERE, mach_task_self(),
                 (vm_address_t)entry - off, false, &current, &maximum, VM_INHERIT_NONE) != KERN_SUCCESS) return false;
    bool ok = vm_protect(mach_task_self(), writer, page, false, VM_PROT_READ | VM_PROT_WRITE) == KERN_SUCCESS;
    if (ok) {
        const uint32_t epilogue[] = {0x910043ffu, 0xd65f03c0u}; /* add sp, sp, #16; ret */
        memcpy((void *)(writer + off + 0xc8), epilogue, sizeof(epilogue));
        sys_icache_invalidate((char *)entry + 0xc8, sizeof(epilogue));
    }
    vm_deallocate(mach_task_self(), writer, page);
    return ok;
}

static pthread_mutex_t gate = PTHREAD_MUTEX_INITIALIZER;
static int verified;
static int patched_create(pthread_t *thread, const pthread_attr_t *attr, void *(*start)(void *), void *argument)
{
    const char *enabled = getenv("HUSK_PATCH_MINGCHAO_THREAD_PROBE");
    Dl_info image; void *context[2];
    if (enabled && !strcmp(enabled, "1") && dladdr((const void *)start, &image) && image.dli_fname &&
        (uintptr_t)start - (uintptr_t)image.dli_fbase == TARGET_START_THUNK_OFFSET &&
        read_memory(argument, context, sizeof(context)) && matches_probe(context[0])) {
        pthread_mutex_lock(&gate);
        if (!verified) {
            void *runtime = dlopen(image.dli_fname, RTLD_NOLOAD | RTLD_LAZY);
            verified = runtime && verify_target(runtime, image.dli_fname) ? 1 : -1;
            if (runtime) dlclose(runtime);
            fprintf(stderr, "patch[mingchao-thread-probe]: %s\n", verified == 1 ? "APK, SDK and Runtime fingerprints matched" : "target mismatch; refused");
        }
        if (verified == 1)
            fprintf(stderr, "patch[mingchao-thread-probe]: %s entry %p\n", patch_probe(context[0]) ? "applied" : "precondition failed; refused", context[0]);
        pthread_mutex_unlock(&gate);
    }
    return pthread_create(thread, attr, start, argument);
}

/* The public SDK omits dyld-interposing.h. This arm64-only module records
 * its pair in a section of type S_INTERPOSING, as dyld requires. */
__attribute__((used, section("__DATA,__interpose,interposing"))) static struct {
    const void *replacement, *original;
} binding = { (const void *)patched_create, (const void *)pthread_create };

#ifdef HUSK_PATCH_TEST
#include "husk-tl-ld.h"
#include "husk-tl-xmem.h"
#define CHECK(v) do { if (!(v)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #v); exit(1); } } while (0)
__attribute__((naked)) static intptr_t measure_stack(void *fn, void *argument)
{
    __asm__ volatile("stp x29, x30, [sp, #-32]!\nmov x29, sp\nstr x19, [sp, #16]\nmov x19, x0\nmov x0, x1\nblr x19\nmov x0, sp\nsub x0, x0, x29\nmov sp, x29\nldr x19, [sp, #16]\nldp x29, x30, [sp], #32\nret");
}
int main(int argc, char **argv)
{
    CHECK(argc == 3); char error[256]; uint8_t *rx, *rw;
    void *runtime = dlopen(argv[2], RTLD_NOLOAD | RTLD_LAZY); CHECK(runtime);
    CHECK(!verify_target(runtime, argv[0]));
    CHECK(!verify_target(runtime, argv[2])); /* no target APK registered */
    CHECK(tl_ld_add_apk(argv[1])); CHECK(verify_target(runtime, argv[2]));
    CHECK(tl_xmem_open(2u << 20, error, sizeof(error))); CHECK(tl_xmem_alloc(16384, &rx, &rw));
    memcpy(rw, probe, sizeof(probe)); tl_xmem_flush(rx, sizeof(probe)); uint64_t context[64] = {0};
    CHECK(measure_stack(rx, context) == -16); CHECK(patch_probe(rx)); CHECK(measure_stack(rx, context) == 0);
    uint8_t before[sizeof(probe)]; memcpy(before, rw, sizeof(before));
    CHECK(!patch_probe(rx)); CHECK(!memcmp(before, rw, sizeof(before)));
    puts("PASS: pinned APK/SDK/Runtime checks, stack delta -16 to 0, mismatched template refused without writes");
    dlclose(runtime); return 0;
}
#endif
