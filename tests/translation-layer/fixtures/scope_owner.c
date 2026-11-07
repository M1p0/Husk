/* SPDX-License-Identifier: GPL-2.0-or-later */
typedef unsigned long size_t;
static unsigned char arena[128];
static int released;
void *_Znwm(size_t size) { return size <= sizeof(arena) ? arena : (void *)0; }
void _ZdlPv(void *pointer) { released = pointer == arena ? 1 : -1; }
int protected_value(void) { return 99; }
extern int child_release(void *);
extern void *child_allocate(void);
extern int child_protected(void);
int scope_owner_test(void)
{
    released = 0;
    int result = child_release(arena);
    return result == 0 && released == 1 && child_allocate() == arena && child_protected() == 17;
}
