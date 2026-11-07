/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Independently compiled Android ELF fixture: standard NDK callback layout. */
typedef unsigned long size_t;
typedef struct activity activity;
typedef struct {
    void (*start)(activity *), (*resume)(activity *);
    void *(*save)(activity *, size_t *);
    void (*pause)(activity *), (*stop)(activity *), (*destroy)(activity *);
    void (*focus)(activity *, int);
    void (*created)(activity *, void *), (*resized)(activity *, void *);
    void (*redraw)(activity *, void *), (*destroyed)(activity *, void *);
    void (*input_created)(activity *, void *), (*input_destroyed)(activity *, void *);
    void (*content)(activity *, const int *), (*configuration)(activity *), (*low)(activity *);
} callbacks;
struct activity {
    callbacks *callbacks;
    void *vm, *env, *clazz;
    const char *internal, *external;
    int sdk;
    void *instance, *assets;
    const char *obb;
};
extern void *malloc(size_t);
static char events[256];
static unsigned count;
static void record(activity *a, char event) { if (a->instance != (void *)0x12345678) event = '!'; if (count < 254) { events[count++] = event; events[count] = 0; } }
#define EVENT(name, code) static void name(activity *a) { record(a, code); }
EVENT(start, 'S') EVENT(resume, 'R') EVENT(pause, 'P') EVENT(stop, 'T') EVENT(destroy, 'E')
EVENT(configuration, 'G') EVENT(low, 'M')
static void focus(activity *a, int focus) { record(a, focus ? 'F' : 'f'); }
static void created(activity *a, void *w) { record(a, w ? 'W' : '!'); }
static void resized(activity *a, void *w) { record(a, w ? 'Z' : '!'); }
static void redraw(activity *a, void *w) { record(a, w ? 'V' : '!'); }
static void destroyed(activity *a, void *w) { record(a, w ? 'X' : '!'); }
static void input_created(activity *a, void *q) { record(a, q ? 'I' : '!'); }
static void input_destroyed(activity *a, void *q) { record(a, q ? 'J' : '!'); }
static void content(activity *a, const int *rect) { record(a, rect[0] == 2 && rect[1] == 3 && rect[2] == 42 && rect[3] == 53 ? 'B' : '!'); }
static void *save(activity *a, size_t *size) { record(a, 'Q'); unsigned char *bytes = malloc(3); if (!bytes) { *size = 0; return 0; } bytes[0] = 7; bytes[1] = 8; bytes[2] = 9; *size = 3; return bytes; }
static void create(activity *a, void *saved, size_t size, char which)
{
    a->instance = (void *)0x12345678;
    if (!a->callbacks || !a->vm || !a->env || !a->clazz || !a->internal || !a->external || !a->obb || !a->assets || a->sdk < 21) which = '!';
    if (size && (size != 3 || ((unsigned char *)saved)[0] != 7 || ((unsigned char *)saved)[2] != 9)) which = '!';
    record(a, which);
    callbacks *c = a->callbacks;
    c->start = start; c->resume = resume; c->pause = pause; c->stop = stop; c->destroy = destroy;
    c->focus = focus; c->created = created; c->resized = resized; c->redraw = redraw; c->destroyed = destroyed;
    c->input_created = input_created; c->input_destroyed = input_destroyed;
    c->content = content; c->configuration = configuration; c->low = low; c->save = save;
}
void ANativeActivity_onCreate(activity *a, void *saved, size_t size) { create(a, saved, size, 'D'); }
void FixtureNativeActivity_onCreate(activity *a, void *saved, size_t size) { create(a, saved, size, 'C'); }
void *Java_NativeActivityLifecycleTest_events(void *env, void *cls)
{
    (void)cls;
    void *(*new_string)(void *, const char *) = ((void **)*(void **)env)[167];
    return new_string(env, events);
}
void Java_NativeActivityLifecycleTest_reset(void *env, void *cls) { (void)env; (void)cls; count = 0; events[0] = 0; }
