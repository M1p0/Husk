/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "husk-tl-session.h"
#include <pthread.h>
#include <stdatomic.h>
#include <assert.h>

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_bool stopping;
static unsigned calls, workers;
static _Thread_local bool cleanup;

bool tl_session_stopping(void) { return !cleanup && atomic_load(&stopping); }
void tl_session_allow_cleanup(bool allow) { cleanup = allow; }
bool tl_session_enter(void)
{
    pthread_mutex_lock(&lock);
    bool ok = cleanup || !atomic_load(&stopping);
    if (ok) calls++;
    pthread_mutex_unlock(&lock);
    return ok;
}
void tl_session_leave(void)
{
    pthread_mutex_lock(&lock); assert(calls); calls--; pthread_mutex_unlock(&lock);
}
bool tl_session_worker_add(void)
{
    pthread_mutex_lock(&lock);
    bool ok = !atomic_load(&stopping);
    if (ok) workers++;
    pthread_mutex_unlock(&lock);
    return ok;
}
void tl_session_worker_done(void)
{
    pthread_mutex_lock(&lock); assert(workers); workers--; pthread_mutex_unlock(&lock);
}
void tl_session_request_stop(void)
{
    pthread_mutex_lock(&lock); atomic_store(&stopping, true); pthread_mutex_unlock(&lock);
}
bool tl_session_drained(void)
{
    pthread_mutex_lock(&lock); bool ok = calls == 0 && workers == 0; pthread_mutex_unlock(&lock); return ok;
}
bool tl_session_restart(void)
{
    pthread_mutex_lock(&lock);
    bool ok = calls == 0 && workers == 0;
    if (ok) atomic_store(&stopping, false);
    pthread_mutex_unlock(&lock);
    return ok;
}
