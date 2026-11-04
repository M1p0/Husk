/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "husk-tl-dvm.h"
#include "husk-tl-jni.h"
#include "husk-tl-ld.h"
#include "husk-tl-session.h"
#include <assert.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

void tl_log_line(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); fputc('\n', stderr); va_end(ap);
}
static atomic_bool done;
static bool ok;
static jvalue returned, marker;
static char database[1024];
static void *run(void *unused) {
    (void)unused;
    jvalue argument = { .l = tl_jni_new_string(database) };
    ok = tl_dvm_call_static("ReopenBootProbe", "marker", "()Ljava/lang/Object;", NULL, &marker)
      && tl_dvm_call_static("ReopenBootProbe", "run", "(Ljava/lang/String;)I", &argument, &returned);
    atomic_store(&done, true);
    return NULL;
}
int main(int argc, char **argv) {
    assert(argc == 3);
    snprintf(database, sizeof(database), "/tmp/husk-reopen-%d.sqlite", getpid()); unlink(database);
    static const char *names[] = { "com.android.art/javalib/core-oj.jar", "com.android.art/javalib/core-libart.jar",
        "com.android.art/javalib/okhttp.jar", "com.android.art/javalib/bouncycastle.jar", "com.android.art/javalib/apache-xml.jar",
        "com.android.i18n/javalib/core-icu4j.jar", "com.android.conscrypt/javalib/conscrypt.jar", "husk-framework.dex" };
    char paths[8][1024], dir[1024]; const char *boot[9];
    for (int i = 0; i < 8; i++) { snprintf(paths[i], sizeof(paths[i]), "%s/%s", argv[1], names[i]); boot[i] = paths[i]; }
    boot[8] = argv[2];
    snprintf(dir, sizeof(dir), "%s/com.android.art", argv[1]); setenv("ANDROID_ART_ROOT", dir, 1);
    snprintf(dir, sizeof(dir), "%s/com.android.i18n", argv[1]); setenv("ANDROID_I18N_ROOT", dir, 1);
    snprintf(dir, sizeof(dir), "%s/com.android.tzdata", argv[1]); setenv("ANDROID_TZDATA_ROOT", dir, 1);
    snprintf(dir, sizeof(dir), "%s/com.android.i18n/lib64", argv[1]); tl_ld_add_search_dir(dir);
    snprintf(dir, sizeof(dir), "%s/com.android.conscrypt/lib64", argv[1]); tl_ld_add_search_dir(dir);
    tl_jni_init(); assert(tl_dvm_start(boot, 9));
    snprintf(dir, sizeof(dir), "%s/com.android.art/lib64", argv[1]); tl_dvm_load_natives(dir);
    void *old_marker = NULL;
    for (int cycle = 0; cycle < 3; cycle++) {
        assert(tl_session_restart()); assert(tl_dvm_initialize_session());
        atomic_store(&done, false); returned.j = 0; marker.j = 0;
        pthread_attr_t attr; pthread_attr_init(&attr); pthread_attr_setstacksize(&attr, 64u << 20);
        pthread_t thread; assert(pthread_create(&thread, &attr, run, NULL) == 0); pthread_attr_destroy(&attr);
        for (int i = 0; i < 3000 && !atomic_load(&done); i++) usleep(1000);
        if (!atomic_load(&done)) { fputs("FAIL: frame callback was stranded on an old looper\n", stderr); tl_session_request_stop(); }
        pthread_join(thread, NULL);
        assert(ok && returned.i == 1 && marker.l && marker.l != old_marker);
        old_marker = marker.l;
        tl_session_request_stop(); assert(tl_session_drained()); assert(tl_dvm_end_session());
        fprintf(stderr, "PASS: session %d created fresh boot singletons/properties, executed its frame callback and released abandoned database locks\n", cycle + 1);
    }
    unlink(database);
}
