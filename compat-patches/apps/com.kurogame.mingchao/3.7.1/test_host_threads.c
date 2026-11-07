/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <pthread.h>
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>

static void *identity(void *value) { return value; }
int main(void)
{
    pthread_t thread; void *result = NULL;
    if (pthread_create(&thread, NULL, identity, (void *)(uintptr_t)0x1234) ||
        pthread_join(thread, &result) || result != (void *)(uintptr_t)0x1234) return 1;
    void *handle = dlopen(NULL, RTLD_LAZY);
    if (!handle || !dlsym(handle, "getpid")) return 2;
    dlclose(handle);
    puts("PASS: unrelated host threads, return value and dlopen/dlsym");
    return 0;
}
