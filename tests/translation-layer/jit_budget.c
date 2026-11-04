/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "husk-tl-xmem.h"
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

void tl_log_line(const char *fmt, ...) { (void)fmt; }

int main(int argc, char **argv)
{
    assert(argc == 2);
    const char *path = argv[1], *id = "12345678-1234-1234-1234-123456789ABC";
    husk_jit_budget_watch(path, id);
    assert(husk_jit_budget_region_bytes() == 0);  /* Query must not prewarm. */
    assert(!tl_xmem_alloc(TL_XMEM_PAGE, NULL, NULL)); /* Missing JIT is not capacity exhaustion. */
    assert(access(path, F_OK) != 0);
    char err[256] = "";
    assert(tl_xmem_open(4 * TL_XMEM_PAGE, err, sizeof(err)));
    assert(husk_jit_budget_region_bytes() == 4 * TL_XMEM_PAGE);
    assert(tl_xmem_alloc(TL_XMEM_PAGE, NULL, NULL));
    assert(access(path, F_OK) != 0);
    assert(!tl_xmem_alloc(4 * TL_XMEM_PAGE, NULL, NULL));
    /* Read immediately: the failed allocation must not return before its evidence has been committed. */
    FILE *f = fopen(path, "r");
    assert(f);
    char json[512];
    assert(fgets(json, sizeof(json), f));
    fclose(f);
    assert(strstr(json, id));
    assert(strstr(json, "\"capacityBytes\":65536"));
    assert(strstr(json, "\"usedBytes\":16384"));
    assert(strstr(json, "\"requestedBytes\":65536"));
    husk_jit_budget_progress();
    assert(access(path, F_OK) != 0); /* A recovered optional failure must not be blamed for a later crash. */
    husk_jit_budget_watch(NULL, NULL);
    assert(!tl_xmem_alloc(4 * TL_XMEM_PAGE, NULL, NULL));
    assert(access(path, F_OK) != 0);
    husk_jit_budget_watch(path, "bad\"id");
    assert(!tl_xmem_alloc(4 * TL_XMEM_PAGE, NULL, NULL));
    assert(access(path, F_OK) != 0);
    assert(!tl_xmem_alloc(SIZE_MAX, NULL, NULL));
    puts("PASS: real code-region exhaustion writes durable evidence; absent JIT and disabled watches do not");
    return 0;
}
