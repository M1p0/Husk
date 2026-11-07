/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "../runtime-api/husk-runtime-patch-v1.h"
#include "husk-tl-internal.h"
#include "husk-tl-ld.h"
#include <CommonCrypto/CommonDigest.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#ifndef HUSK_RUNTIME_ENABLE_APPS
#define HUSK_RUNTIME_ENABLE_APPS 0
#endif
extern bool tl_bionic_guest_thread_entry(const void *, const void *, void **);
extern void tl_vk_configure(const char *, const char *, int);
extern bool tl_vk_available(void);
extern void tl_egl_offscreen_windows(bool);
extern void *tl_bionic_find(const char *);
static bool hash_matches(const void *bytes, size_t size, const char *expected)
{
    if (!expected || strnlen(expected, 65) != 64) return false;
    CC_SHA256_CTX ctx; unsigned char result[CC_SHA256_DIGEST_LENGTH]; char hex[65];
    CC_SHA256_Init(&ctx);
    for (size_t i=0;i<size;) {
        size_t n=size-i<(1u<<20)?size-i:(1u<<20);
        CC_SHA256_Update(&ctx,(const char *)bytes+i,(CC_LONG)n);i+=n;
    }
    CC_SHA256_Final(result,&ctx);
    for (unsigned i=0;i<sizeof(result);i++) snprintf(hex+i*2,3,"%02x",result[i]);
    return !strcmp(hex,expected);
}
static int32_t match_apk(uint64_t size,const char *hash,const char *entry,const char *entry_hash)
{
    if (!entry || !entry[0] || strnlen(entry,4097)>4096) return 0;
    for (int i=0;i<TL_LD_MAX_APKS;i++) {
        const tl_zip *apk=tl_ld_apk_at(i);if (!apk) break;
        if (apk->size!=size || !hash_matches(apk->map,apk->size,hash)) continue;
        const tl_zip_entry *item=tl_zip_find(apk,entry);
        const uint8_t *bytes=NULL;size_t count=0;bool owned=false;char error[256];
        if (!item || !tl_zip_data(apk,item,16u<<20,&bytes,&count,&owned,error,sizeof(error))) return 0;
        bool ok=hash_matches(bytes,count,entry_hash);if (owned) free((void *)bytes);return ok;
    }
    return 0;
}
static int32_t enable_views(void) { return tl_ld_enable_code_views(); }
static const char *name(void *lib) { return tl_ld_lib_name(lib); }
static void *base(void *lib) { return tl_ld_lib_base(lib); }
static void *symbol(void *lib,const char *key) { return tl_ld_sym(lib,key); }
static void *data(void *lib,uint64_t at,size_t count) { return tl_ld_data_address(lib,at,count); }
static int32_t write_code(const void *at,const void *bytes,size_t count) { return bytes && tl_ld_write_code(at,bytes,count); }
static int32_t retranslate(void *lib,uint64_t at,size_t count) { return tl_ld_retranslate_range(lib,at,count); }
static int32_t thread_entry(const void *start,const void *argument,void **entry) { return tl_bionic_guest_thread_entry(start,argument,entry); }
static int32_t configure_vulkan(const char *driver)
{ if (!driver || driver[0]!='/') return 0;tl_vk_configure(driver,NULL,0);return tl_vk_available(); }
static void offscreen(int32_t value) { tl_egl_offscreen_windows(value!=0); }
static const husk_runtime_patch_api_v1 api={
    .struct_size=sizeof(api),.abi_version=1,.capabilities=15 | (HUSK_RUNTIME_ENABLE_APPS?16:0),
    .match_apk=match_apk,.enable_code_views=enable_views,.add_init_hook=tl_ld_add_init_hook,
    .library_name=name,.library_base=base,.library_symbol=symbol,.library_data=data,
    .write_code=write_code,.retranslate=retranslate,.guest_thread_entry=thread_entry,
    .guest_symbol=tl_bionic_find,.interpose_guest=tl_ld_interpose,
    .configure_vulkan=configure_vulkan,.offscreen_gl=offscreen
};
const husk_runtime_patch_api_v1 *husk_runtime_get_patch_api(uint32_t abi,uint32_t size)
{ return abi==1 && size>=sizeof(api)?&api:NULL; }
