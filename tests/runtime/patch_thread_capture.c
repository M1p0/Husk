/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "husk-runtime-patch-v1.h"
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
static const husk_runtime_patch_api_v1 *api;
static void *expected_entry;
static atomic_int recognized;
void husk_patch_test_capture(const husk_runtime_patch_api_v1 *value,void *entry)
{ api=value;expected_entry=entry; }
int husk_patch_test_thread_count(void) { return atomic_load(&recognized); }
static int inspect_create(pthread_t *out,const pthread_attr_t *attr,void *(*start)(void *),void *argument)
{
    void *entry=NULL;
    if (api && api->guest_thread_entry(start,argument,&entry)) {
        assert(entry==expected_entry);atomic_fetch_add(&recognized,1);
        entry=(void *)0x1234;
        assert(!api->guest_thread_entry(start,(void *)1,&entry) && entry==(void *)0x1234);
    }
    return pthread_create(out,attr,start,argument);
}
__attribute__((used, section("__DATA,__interpose,interposing")))
static struct { const void *replacement,*original; } binding={(void *)inspect_create,(void *)pthread_create};
