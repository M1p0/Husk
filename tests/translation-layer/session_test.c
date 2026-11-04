/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "husk-tl-session.h"
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <unistd.h>
#include <stdio.h>

static atomic_bool entered, release_worker;
static void *worker(void *unused)
{
    (void)unused;
    assert(tl_session_enter());
    atomic_store(&entered, true);
    while (!tl_session_stopping()) usleep(1000);
    tl_session_leave();
    /* A thread's cleanup still references app objects after its Java call returns. */
    while (!atomic_load(&release_worker)) usleep(1000);
    tl_session_worker_done();
    return NULL;
}
int main(void)
{
    for (int cycle = 0; cycle < 20; cycle++) {
        assert(tl_session_restart());
        atomic_store(&entered, false); atomic_store(&release_worker, false);
        assert(tl_session_worker_add());
        pthread_t thread; assert(pthread_create(&thread, NULL, worker, NULL) == 0);
        while (!atomic_load(&entered)) usleep(1000);
        tl_session_request_stop();
        assert(!tl_session_enter());
        assert(!tl_session_worker_add());
        assert(!tl_session_drained());
        assert(!tl_session_restart());
        atomic_store(&release_worker, true); pthread_join(thread, NULL);
        assert(tl_session_drained());
        tl_session_allow_cleanup(true);
        assert(!tl_session_stopping()); assert(tl_session_enter());
        assert(!tl_session_drained()); tl_session_leave();
        tl_session_allow_cleanup(false);
        assert(tl_session_stopping());
    }
    puts("PASS: 20 close/reopen cycles; live calls and worker cleanup prevent unsafe reset");
}
