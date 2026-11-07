/* SPDX-License-Identifier: GPL-2.0-or-later */
/* The guest consumes get_crc_table as a packed array of 32-bit words. */
#include "husk-tl-bionic.h"
#include <pthread.h>
#include <stdatomic.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(expr) do { if (!(expr)) { fprintf(stderr, "FAIL: %s:%d: %s\n", __FILE__, __LINE__, #expr); exit(1); } } while (0)
void tl_log_line(const char *fmt, ...) { va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); fputc('\n', stderr); va_end(ap); }

static const uint32_t *(*guest_table)(void);
static const uint32_t *tables[8];
static atomic_int ready;

static void *worker(void *argument)
{
    unsigned index = (unsigned)(uintptr_t)argument;
    atomic_fetch_add(&ready, 1);
    while (atomic_load(&ready) != 8) {}
    const uint32_t *table = tables[index] = guest_table();
    CHECK(table && table[0] == 0 && table[1] == 0x77073096u && table[255] == 0x2d02ef8du);
    const unsigned char message[] = "123456789";
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < sizeof(message) - 1; i++) crc = table[(crc ^ message[i]) & 255] ^ (crc >> 8);
    CHECK((crc ^ UINT32_MAX) == 0xcbf43926u);
    return NULL;
}

int main(void)
{
    guest_table = tl_bionic_find("get_crc_table");
    CHECK(guest_table);
    pthread_t threads[8];
    for (unsigned i = 0; i < 8; i++) CHECK(!pthread_create(&threads[i], NULL, worker, (void *)(uintptr_t)i));
    for (unsigned i = 0; i < 8; i++) CHECK(!pthread_join(threads[i], NULL));
    for (unsigned i = 1; i < 8; i++) CHECK(tables[i] == tables[0]);
    for (unsigned i = 0; i < 256; i++) {
        uint32_t expected = i;
        for (unsigned bit = 0; bit < 8; bit++) expected = (expected >> 1) ^ ((expected & 1) ? 0xedb88320u : 0);
        CHECK(tables[0][i] == expected);
    }
    puts("PASS: Android 32-bit CRC table layout, standard CRC-32 vector and concurrent first use");
}
