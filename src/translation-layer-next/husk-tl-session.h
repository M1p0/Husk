/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HUSK_TL_SESSION_H
#define HUSK_TL_SESSION_H
#include <stdbool.h>
#include <poll.h>

/* Cooperative Java shutdown. Never discard DEX while a call or worker still owns it. */
bool tl_session_enter(void);
void tl_session_leave(void);
bool tl_session_worker_add(void);
void tl_session_worker_done(void);
bool tl_session_stopping(void);
void tl_session_request_stop(void);
bool tl_session_drained(void);
bool tl_session_restart(void);
/* Only the teardown thread may run framework reset code while entry is closed. */
void tl_session_allow_cleanup(bool allow);
/* Pin only sockets currently in a blocking guest call. Stop shuts down the pin,
 * never closes an arbitrary descriptor which another thread may have reused. */
typedef struct tl_session_io { int socket; struct tl_session_io *next; } tl_session_io;
bool tl_session_io_begin(tl_session_io *io, int fd);
void tl_session_io_end(tl_session_io *io);
int tl_session_poll(struct pollfd *fds, nfds_t count, int timeout_ms);
void tl_session_counts(unsigned *active_calls, unsigned *active_workers);
#endif
