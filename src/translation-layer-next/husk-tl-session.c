/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "husk-tl-session.h"
#include <pthread.h>
#include <stdatomic.h>
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_bool stopping;
static unsigned calls, workers;
static _Thread_local bool cleanup;
static tl_session_io *blocking_io;

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
    pthread_mutex_lock(&lock);
    atomic_store(&stopping, true);
    for (tl_session_io *io = blocking_io; io; io = io->next)
        if (io->socket >= 0) shutdown(io->socket, SHUT_RDWR);
    pthread_mutex_unlock(&lock);
}
bool tl_session_io_begin(tl_session_io *io, int fd)
{
    int saved_errno = errno;
    io->socket = -1; io->next = NULL;
    pthread_mutex_lock(&lock);
    if (!cleanup && atomic_load(&stopping)) {
        pthread_mutex_unlock(&lock); errno = ECANCELED; return false;
    }
    int type; socklen_t size = sizeof(type);
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &size) == 0) {
        io->socket = dup(fd);
        if (io->socket < 0) { pthread_mutex_unlock(&lock); return false; }
    }
    io->next = blocking_io; blocking_io = io;
    pthread_mutex_unlock(&lock);
    errno = saved_errno;
    return true;
}
void tl_session_io_end(tl_session_io *io)
{
    int saved_errno = errno;
    pthread_mutex_lock(&lock);
    tl_session_io **p = &blocking_io;
    while (*p && *p != io) p = &(*p)->next;
    assert(*p == io); *p = io->next;
    if (io->socket >= 0) close(io->socket);
    pthread_mutex_unlock(&lock);
    errno = saved_errno;
}
static int64_t monotonic_ms(void)
{
    struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}
int tl_session_poll(struct pollfd *fds, nfds_t count, int timeout_ms)
{
    int64_t deadline = timeout_ms < 0 ? 0 : monotonic_ms() + timeout_ms;
    for (;;) {
        if (tl_session_stopping()) { errno = ECANCELED; return -1; }
        int wait = 20;
        if (timeout_ms >= 0) {
            int64_t left = deadline - monotonic_ms();
            if (left < wait) wait = left > 0 ? (int)left : 0;
        }
        int result = poll(fds, count, wait);
        if (result != 0 || (timeout_ms >= 0 && monotonic_ms() >= deadline)) return result;
    }
}
void tl_session_counts(unsigned *active_calls, unsigned *active_workers)
{
    pthread_mutex_lock(&lock);
    *active_calls = calls; *active_workers = workers;
    pthread_mutex_unlock(&lock);
}
bool tl_session_drained(void)
{
    pthread_mutex_lock(&lock); bool ok = calls == 0 && workers == 0 && blocking_io == NULL; pthread_mutex_unlock(&lock); return ok;
}
bool tl_session_restart(void)
{
    pthread_mutex_lock(&lock);
    bool ok = calls == 0 && workers == 0 && blocking_io == NULL;
    if (ok) atomic_store(&stopping, false);
    pthread_mutex_unlock(&lock);
    return ok;
}
