/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Bounded own-process capability probe. No allocation stress or other task. */
#include <mach/mach.h>
#include <stdio.h>

int main(void)
{
    int previous = 0;
    kern_return_t result = task_set_phys_footprint_limit(mach_task_self(), 64, &previous);
    printf("{\"operation\":\"task_set_phys_footprint_limit\",\"target\":\"self\","
           "\"requested_mib\":64,\"kern_return\":%d,\"old_limit_mib\":%d,"
           "\"hard_limit_verified\":false}\n", result, previous);
    /* Even a successful setter is not an enforcement test. This small probe
     * exits without any stress allocation; a later device test needs an
     * explicit effective-limit/termination/auxiliary-process validation. */
    return result == KERN_SUCCESS ? 0 : 2;
}
