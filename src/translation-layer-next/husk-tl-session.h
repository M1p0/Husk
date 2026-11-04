/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HUSK_TL_SESSION_H
#define HUSK_TL_SESSION_H
#include <stdbool.h>

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
#endif
