/* SPDX-License-Identifier: GPL-2.0-or-later */
typedef unsigned long size_t;
static unsigned char foreign_arena[128];
static int bad_release;
__attribute__((weak)) void *_Znwm(size_t size) { return size <= sizeof(foreign_arena) ? foreign_arena : (void *)0; }
__attribute__((weak)) void _ZdlPv(void *pointer) { (void)pointer; bad_release++; }
__attribute__((weak, visibility("protected"))) int protected_value(void) { return 17; }
int child_release(void *pointer) { bad_release = 0; _ZdlPv(pointer); return bad_release; }
void *child_allocate(void) { return _Znwm(32); }
int child_protected(void) { return protected_value(); }
int child_own_test(void)
{
    bad_release = 0;
    void *pointer = _Znwm(32);
    _ZdlPv(pointer);
    return pointer == foreign_arena && bad_release == 1 && protected_value() == 17;
}
