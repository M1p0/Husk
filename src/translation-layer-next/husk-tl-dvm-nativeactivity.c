/* SPDX-License-Identifier: GPL-2.0-or-later */
/* NativeActivity's NDK ABI, driven by the interpreted framework's lifecycle.
 * No engine detection or synthetic Java activity is involved. */
#define _DARWIN_C_SOURCE
#include "husk-tl-dvm-internal.h"
#include "husk-tl-bionic.h"
#include "husk-tl-ld.h"
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct na_activity na_activity;
typedef struct {
    void (*start)(na_activity *), (*resume)(na_activity *);
    void *(*save)(na_activity *, size_t *);
    void (*pause)(na_activity *), (*stop)(na_activity *), (*destroy)(na_activity *);
    void (*focus)(na_activity *, int);
    void (*window_create)(na_activity *, void *), (*window_resize)(na_activity *, void *);
    void (*window_redraw)(na_activity *, void *), (*window_destroy)(na_activity *, void *);
    void (*input_create)(na_activity *, void *), (*input_destroy)(na_activity *, void *);
    void (*content)(na_activity *, const int32_t *);
    void (*configuration)(na_activity *), (*low_memory)(na_activity *);
} na_callbacks;
struct na_activity {
    na_callbacks *callbacks;
    void *vm, *env, *clazz;
    const char *internal, *external;
    int32_t sdk;
    void *instance, *assets;
    const char *obb;
};
_Static_assert(offsetof(na_activity, instance) == 56 && sizeof(na_activity) == 80, "64-bit NDK activity layout");
_Static_assert(offsetof(na_callbacks, window_destroy) == 80 && sizeof(na_callbacks) == 128, "NDK callback order");

typedef struct na_state {
    na_activity activity;
    na_callbacks callbacks;
    jobj *owner, *assets, *surface;
    void *window, *input;
    int width, height;
    uint64_t handle;
    struct na_state *next;
} na_state;
/* Java dispatches activity callbacks on its main thread. Handles are identities,
 * not exposed heap pointers, so stale Java calls cannot dereference freed state. */
static na_state *states;
static uint64_t next_handle;
extern bool tl_dvm_load_library(const char *);
extern bool tl_a32_is_fn(const void *);
extern void tl_nwindow_release(void *);
extern void tl_inq_touch(int, int, float, float);
extern void tl_inq_key(int, int, int, int);

static na_state *find_state(jobj *owner, uint64_t handle)
{
    for (na_state *s = states; s; s = s->next) if (s->owner == owner && s->handle == handle) return s;
    return NULL;
}
static void drop_window(na_state *s)
{
    if (s->window && s->activity.callbacks->window_destroy) s->activity.callbacks->window_destroy(&s->activity, s->window);
    tl_nwindow_release(s->window); s->window = NULL;
    tl_jni_unref(s->surface); s->surface = NULL;
    s->width = s->height = 0;
}
static void release_state(na_state *s, bool callbacks)
{
    if (callbacks) {
        drop_window(s);
        if (s->input && s->activity.callbacks->input_destroy) s->activity.callbacks->input_destroy(&s->activity, s->input);
        s->input = NULL;
        if (s->activity.callbacks->destroy) s->activity.callbacks->destroy(&s->activity);
    } else {
        tl_nwindow_release(s->window); tl_jni_unref(s->surface);
    }
    free((void *)s->activity.internal); free((void *)s->activity.external); free((void *)s->activity.obb);
    tl_jni_unref(s->assets); tl_jni_unref(s->owner); free(s);
}
/* Session shutdown has already drained native/Java threads. Do not re-enter
 * canceled guest code, but release host ownership before the next app starts. */
void tl_dvm_nativeactivity_reset(void)
{
    while (states) { na_state *s = states; states = s->next; release_state(s, false); }
}

#define NAT(name) static bool name(jobj *self, const jvalue *a, jvalue *ret)
NAT(load_code)
{
    const char *library = tl_jni_string(a[0].l), *entry = tl_jni_string(a[1].l);
    if (!library || !entry) return dvm_throw("java/lang/NullPointerException", "NativeActivity library/entry");
    char name[512];
    if (snprintf(name, sizeof(name), "lib%s.so", library) >= (int)sizeof(name))
        return dvm_throw("java/lang/UnsatisfiedLinkError", "NativeActivity library name too long");
    if (!tl_dvm_load_library(name)) return dvm_throw("java/lang/UnsatisfiedLinkError", "NativeActivity cannot load %s", name);
    if (tl_jni_pending()) return false;
    tl_lib *lib = tl_ld_find_lib(name);
    void (*create)(na_activity *, void *, size_t) = lib ? tl_ld_sym(lib, entry) : NULL;
    if (!create) return dvm_throw("java/lang/UnsatisfiedLinkError", "NativeActivity entry %s missing in %s", entry, name);
    if (tl_a32_is_fn((void *)create)) return dvm_throw("java/lang/UnsupportedOperationException", "NativeActivity Java bridge requires a 64-bit native entry");
    na_state *s = calloc(1, sizeof(*s));
    if (!s) return dvm_out_of_memory(sizeof(*s));
    s->owner = tl_jni_ref(self); s->assets = tl_jni_ref(a[6].l);
    s->activity.callbacks = &s->callbacks;
    s->activity.vm = tl_jni_vm(); s->activity.env = tl_jni_env(); s->activity.clazz = self;
    s->activity.internal = strdup(tl_jni_string(a[2].l) ? tl_jni_string(a[2].l) : "");
    s->activity.external = strdup(tl_jni_string(a[3].l) ? tl_jni_string(a[3].l) : "");
    s->activity.obb = strdup(tl_jni_string(a[4].l) ? tl_jni_string(a[4].l) : "");
    if (!s->activity.internal || !s->activity.external || !s->activity.obb) { release_state(s, false); return dvm_out_of_memory(1); }
    s->activity.sdk = a[5].i;
    void *(*assets)(void *, void *) = tl_bionic_find("AAssetManager_fromJava");
    s->activity.assets = assets ? assets(s->activity.env, s->assets) : NULL;
    jobj *saved = a[7].l;
    void *bytes = saved && saved->kind == TL_K_PRIM_ARRAY ? saved->arr.data : NULL;
    size_t size = bytes ? saved->arr.len : 0;
    s->handle = ++next_handle; s->next = states; states = s;
    tl_log_line("nativeactivity: %s %s, Java owner %s", name, entry, tl_jni_class_name(self));
    create(&s->activity, bytes, size);
    if (tl_jni_pending()) {
        states = s->next; release_state(s, true); return false;
    }
    ret->j = (int64_t)s->handle;
    return true;
}
NAT(unload_code)
{
    (void)ret;
    for (na_state **p = &states; *p; p = &(*p)->next) if ((*p)->handle == (uint64_t)a[0].j && (*p)->owner == self) {
        na_state *s = *p; *p = s->next; release_state(s, true); break;
    }
    return !tl_jni_pending();
}
#define LIFECYCLE(name, field) NAT(name) { (void)ret; na_state *s = find_state(self, a[0].j); if (s && s->activity.callbacks->field) s->activity.callbacks->field(&s->activity); return !tl_jni_pending(); }
LIFECYCLE(start, start) LIFECYCLE(resume, resume) LIFECYCLE(pause, pause) LIFECYCLE(stop, stop)
LIFECYCLE(configuration, configuration) LIFECYCLE(low_memory, low_memory)
NAT(focus)
{
    (void)ret; na_state *s = find_state(self, a[0].j);
    if (s && s->activity.callbacks->focus) s->activity.callbacks->focus(&s->activity, a[1].z ? 1 : 0);
    return !tl_jni_pending();
}
static bool set_surface(na_state *s, jobj *surface, int width, int height)
{
    if (surface != s->surface) {
        drop_window(s);
        void *(*from)(void *, void *) = tl_bionic_find("ANativeWindow_fromSurface");
        s->window = surface && from ? from(tl_jni_env(), surface) : NULL;
        s->surface = tl_jni_ref(surface);
        if (s->window && s->activity.callbacks->window_create) s->activity.callbacks->window_create(&s->activity, s->window);
    } else if (s->window && (s->width != width || s->height != height) && s->width && s->height) {
        if (s->activity.callbacks->window_resize) s->activity.callbacks->window_resize(&s->activity, s->window);
    }
    s->width = width; s->height = height;
    return !tl_jni_pending();
}
NAT(surface_create) { (void)ret; na_state *s = find_state(self, a[0].j); return !s || set_surface(s, a[1].l, 0, 0); }
NAT(surface_change) { (void)ret; na_state *s = find_state(self, a[0].j); return !s || set_surface(s, a[1].l, a[3].i, a[4].i); }
NAT(surface_destroy) { (void)ret; na_state *s = find_state(self, a[0].j); if (s) drop_window(s); return !tl_jni_pending(); }
NAT(surface_redraw)
{
    (void)ret; na_state *s = find_state(self, a[0].j);
    if (s && s->window && s->activity.callbacks->window_redraw) s->activity.callbacks->window_redraw(&s->activity, s->window);
    return !tl_jni_pending();
}
NAT(input_create)
{
    (void)ret; na_state *s = find_state(self, a[0].j);
    if (s && s->input != (void *)(uintptr_t)a[1].j) {
        if (s->input && s->activity.callbacks->input_destroy) s->activity.callbacks->input_destroy(&s->activity, s->input);
        s->input = (void *)(uintptr_t)a[1].j;
        if (s->input && s->activity.callbacks->input_create) s->activity.callbacks->input_create(&s->activity, s->input);
    }
    return !tl_jni_pending();
}
NAT(input_destroy)
{
    (void)ret; na_state *s = find_state(self, a[0].j);
    if (s && s->input == (void *)(uintptr_t)a[1].j) {
        if (s->input && s->activity.callbacks->input_destroy) s->activity.callbacks->input_destroy(&s->activity, s->input);
        s->input = NULL;
    }
    return !tl_jni_pending();
}
NAT(content_rect)
{
    (void)ret; na_state *s = find_state(self, a[0].j);
    if (s && s->activity.callbacks->content) {
        int32_t rect[4] = {a[1].i, a[2].i, a[1].i + a[3].i, a[2].i + a[4].i};
        s->activity.callbacks->content(&s->activity, rect);
    }
    return !tl_jni_pending();
}
NAT(save_state)
{
    na_state *s = find_state(self, a[0].j); ret->l = NULL;
    if (!s || !s->activity.callbacks->save) return true;
    size_t size = 0; void *bytes = s->activity.callbacks->save(&s->activity, &size);
    bool ok = true;
    if (bytes && size) {
        if (size > INT32_MAX) ok = dvm_out_of_memory(size);
        else { ret->l = tl_jni_new_prim_array('B', (uint32_t)size); if (ret->l) memcpy(((jobj *)ret->l)->arr.data, bytes, size); else ok = false; }
    }
    void (*guest_free)(void *) = tl_bionic_find("free"); if (bytes && guest_free) guest_free(bytes);
    return ok && !tl_jni_pending();
}
NAT(touch)
{
    na_state *s = find_state(self, a[0].j); ret->z = false;
    if (!s || !s->input || !a[1].l) return true;
    jobj *e = a[1].l;
    int action = tl_jni_call(e, "getActionMasked", "()I", NULL).i;
    int count = tl_jni_call(e, "getPointerCount", "()I", NULL).i;
    int index = tl_jni_call(e, "getActionIndex", "()I", NULL).i;
    for (int i = 0; i < count; i++) if (action == 2 || action == 3 || i == index) {
        jvalue arg = {.i = i}; int id = tl_jni_call(e, "getPointerId", "(I)I", &arg).i;
        float x = tl_jni_call(e, "getX", "(I)F", &arg).f, y = tl_jni_call(e, "getY", "(I)F", &arg).f;
        int phase = action == 0 || action == 5 ? 0 : action == 1 || action == 6 ? 2 : action == 3 ? 3 : 1;
        tl_inq_touch(phase, id, x, y);
    }
    ret->z = true; return !tl_jni_pending();
}
NAT(key)
{
    na_state *s = find_state(self, a[0].j); ret->z = false;
    if (!s || !s->input || !a[1].l) return true;
    jobj *e = a[1].l;
    int action = tl_jni_call(e, "getAction", "()I", NULL).i, code = tl_jni_call(e, "getKeyCode", "()I", NULL).i;
    int source = tl_jni_call(e, "getSource", "()I", NULL).i, device = tl_jni_call(e, "getDeviceId", "()I", NULL).i;
    tl_inq_key(device, source, action, code); ret->z = true; return !tl_jni_pending();
}
NAT(input_pointer) { (void)a; ret->j = (int64_t)(uintptr_t)self; return true; }

dvm_native_fn tl_nativeactivity_native(const char *name, const char *sig)
{
    static const struct { const char *name, *sig; dvm_native_fn fn; } methods[] = {
        {"huskLoadNativeCode", "(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;ILandroid/content/res/AssetManager;[B)J", load_code},
        {"unloadNativeCode", "(J)V", unload_code}, {"onStartNative", "(J)V", start}, {"onResumeNative", "(J)V", resume},
        {"onPauseNative", "(J)V", pause}, {"onStopNative", "(J)V", stop}, {"onSaveInstanceStateNative", "(J)[B", save_state},
        {"onConfigurationChangedNative", "(J)V", configuration}, {"onLowMemoryNative", "(J)V", low_memory},
        {"onWindowFocusChangedNative", "(JZ)V", focus}, {"onSurfaceCreatedNative", "(JLandroid/view/Surface;)V", surface_create},
        {"onSurfaceChangedNative", "(JLandroid/view/Surface;III)V", surface_change}, {"onSurfaceDestroyedNative", "(J)V", surface_destroy},
        {"onSurfaceRedrawNeededNative", "(JLandroid/view/Surface;)V", surface_redraw}, {"onContentRectChangedNative", "(JIIII)V", content_rect},
        {"onInputQueueCreatedNative", "(JJ)V", input_create}, {"onInputQueueDestroyedNative", "(JJ)V", input_destroy},
        {"huskDispatchTouch", "(JLandroid/view/MotionEvent;)Z", touch}, {"huskDispatchKey", "(JLandroid/view/KeyEvent;)Z", key},
        {"huskInputPointer", "()J", input_pointer}, {NULL, NULL, NULL}
    };
    for (int i = 0; methods[i].name; i++) if (!strcmp(name, methods[i].name) && !strcmp(sig, methods[i].sig)) return methods[i].fn;
    return NULL;
}
