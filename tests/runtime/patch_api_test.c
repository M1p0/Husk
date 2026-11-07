/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "husk-runtime-patch-v1.h"
#include <assert.h>
#include <stdbool.h>
#include <dlfcn.h>
#include <stdio.h>
#include <pthread.h>
static void *thread_body(void *argument) { return argument; }
static void observe(void *library, void *userdata)
{ assert(library); ++*(int *)userdata; }
int main(int argc,char **argv)
{
    assert(argc==3);
    void *runtime=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(runtime);
    husk_runtime_patch_getter_v1 getter=dlsym(runtime,"husk_runtime_get_patch_api");assert(getter);
    assert(!getter(0,sizeof(husk_runtime_patch_api_v1)));
    assert(!getter(2,sizeof(husk_runtime_patch_api_v1)));
    assert(!getter(1,sizeof(husk_runtime_patch_api_v1)-1));
    const husk_runtime_patch_api_v1 *api=getter(1,sizeof(*api));
    for (int i=0;i<32;i++) {
        void *again=dlopen(argv[1],RTLD_NOLOAD|RTLD_LAZY);
        assert(again==runtime);dlclose(again);
    }
    assert(api && api->abi_version==1 && (api->capabilities & 31)==31);
    void (*capture)(const husk_runtime_patch_api_v1 *,void *)=dlsym(RTLD_DEFAULT,"husk_patch_test_capture");
    int (*count)(void)=dlsym(RTLD_DEFAULT,"husk_patch_test_thread_count");
    assert(capture && count);capture(api,(void *)thread_body);
    int (*create)(pthread_t *,const void *,void *(*)(void *),void *)=api->guest_symbol("pthread_create");
    int (*join)(pthread_t,void **)=api->guest_symbol("pthread_join");
    assert(create && join);pthread_t worker;void *answer=NULL;
    assert(!create(&worker,NULL,thread_body,(void *)0x5678));
    assert(!join(worker,&answer) && answer==(void *)0x5678 && count()==1);
    assert(!api->match_apk(0,NULL,NULL,NULL));
    void *entry=(void *)0x1234;
    assert(!api->guest_thread_entry(observe,(void *)1,&entry) && entry==(void *)0x1234);
    assert(!api->library_data(NULL,0,4));
    assert(!api->write_code(NULL,"data",4));
    assert(!api->retranslate(NULL,0,4));
    assert(!api->add_init_hook(NULL,NULL));
    int first=0,second=0;
    assert(api->add_init_hook(observe,&first));
    assert(api->add_init_hook(observe,&second));
    assert(api->enable_code_views());
    /* Private fixture driver only triggers the public opaque-handle callbacks. */
    void *(*load)(const char *)=dlsym(runtime,"tl_ld_load");
    bool (*init)(void *)=dlsym(runtime,"tl_ld_init");assert(load && init);
    void *lib=load(argv[2]);assert(lib && init(lib));
    assert(first==1 && second==1);
    assert(!api->add_init_hook(observe,&first));
    assert(!api->enable_code_views());
    void *function=api->library_symbol(lib,"refreshed");assert(function);
    uint64_t vaddr=(uintptr_t)function-(uintptr_t)api->library_base(lib);
    assert(api->library_name(lib) && api->library_data(lib,vaddr,4));
    assert(!api->library_data(lib,UINT64_MAX,4));
    puts("PASS: patch ABI/size rejection, capabilities, safe invalid inputs, composed hooks and opaque image access");
    return 0;
}
