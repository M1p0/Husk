/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "husk-tl-ld.h"
#include "husk-tl-xmem.h"
#include "husk-tl-codewrite.h"
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

void tl_log_line(const char *fmt, ...) { va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); fputc('\n', stderr); va_end(ap); }
void *tl_bionic_find(const char *);
long tl_linux_syscall(long, long, long, long, long, long, long);
static tl_lib *a, *b;
static atomic_int arrived;
static uintptr_t seen[8];
static uintptr_t (*generated_get)(void);
static void *generated_worker(void *unused)
{
    (void)unused;
    uintptr_t tp = generated_get();
    assert(tp && *(uintptr_t *)tp == tp);
    assert(((uint64_t *)tp)[5] == UINT64_C(0xdeadbeefcafebabe));
    uintptr_t (*get)(void) = tl_ld_sym(a, "tp0");
    assert(get() == tp);
    return NULL;
}
static void check_generated_tls(void)
{
    tl_codewrite_enable();
    uint8_t *rx, *rw;
    assert(tl_xmem_alloc(TL_XMEM_PAGE, &rx, &rw));
    ((volatile uint32_t *)rx)[16] = 0x00005c60u;
    ((volatile uint8_t *)rx)[64 + 3] = 0;
    assert(((uint32_t *)rx)[16] == 0x00005c60u); /* marker-shaped data is not an instruction we rewrote */
    /* A packer writes into an already executable allocation, a byte at a
     * time, without a later mprotect. Also change just the MRS destination
     * after its first execution: the marker must not destroy the other bytes. */
    const uint32_t code[] = {0xd53bd040u, 0xaa0003e0u, 0xd65f03c0u};
    for (size_t i = 0; i < sizeof(code); i++) ((volatile uint8_t *)rx)[i] = ((const uint8_t *)code)[i];
    generated_get = (void *)rx;
    pthread_t t;
    assert(!pthread_create(&t, NULL, generated_worker, NULL)); assert(!pthread_join(t, NULL));
    ((volatile uint8_t *)rx)[0] = 0x41; /* MRS x1, TPIDR_EL0 */
    ((volatile uint32_t *)rx)[1] = 0xaa0103e0u; /* MOV x0, x1 */
    assert(!pthread_create(&t, NULL, generated_worker, NULL)); assert(!pthread_join(t, NULL));
    /* The other producer path writes RW memory, then makes it RX. Restoring
     * write access must recover the original instruction before a byte edit. */
    void *(*map)(void *, size_t, int, int, int, off_t) = tl_bionic_find("mmap");
    int (*protect)(void *, size_t, int) = tl_bionic_find("mprotect");
    int (*unmap)(void *, size_t) = tl_bionic_find("munmap");
    size_t page = (size_t)getpagesize();
    uint32_t *memory = map(NULL, page, 3, 0x22, -1, 0); assert(memory != MAP_FAILED);
    memory[0] = 0x00005c60u;
    assert(!protect(memory, page, 3)); assert(memory[0] == 0x00005c60u); /* ordinary data */
    memcpy(memory, code, sizeof(code));
    assert(!protect(memory, page, 5)); generated_get = (void *)memory;
    assert(!pthread_create(&t, NULL, generated_worker, NULL)); assert(!pthread_join(t, NULL));
    assert(!protect(memory, page, 1)); /* an intermediate read-only mapping keeps marker ownership */
    assert(!protect(memory, page, 3)); assert(memory[0] == code[0]);
    ((uint8_t *)memory)[0] = 0x41; memory[1] = 0xaa0103e0u;
    assert(!protect(memory, page, 5));
    assert(!pthread_create(&t, NULL, generated_worker, NULL)); assert(!pthread_join(t, NULL));
    assert(!unmap(memory, page));
    uint32_t *reused = map(memory, page, 3, 0x32, -1, 0); assert(reused == memory);
    reused[0] = 0x00005c60u;
    assert(!protect(reused, page, 3)); assert(reused[0] == 0x00005c60u);
    assert(!unmap(reused, page));
    puts("PASS: generated TLS on cold threads, byte-wise RWX emission, partial instruction edits and RW/RX transitions");
}
static void *worker(void *arg)
{
    unsigned id = (unsigned)(uintptr_t)arg;
    uint64_t regs[40] = {0};
    void (*check)(uint64_t *) = tl_ld_sym(a, "check_regs"); assert(check); check(regs);
    for (int n = 0; n < 17; n++) assert(regs[n] == (uint64_t)(0x100 + n));
    assert(regs[28] == 0x128 && (regs[30] & 0x40000000));
    for (int n = 0; n < 16; n++) assert(((uint8_t *)regs)[256 + n] == 0x5a);
    for (int n = 0; n < 16; n++) assert(((uint8_t *)regs)[272 + n] == 0xa5);
    for (int n = 0; n < 16; n++) assert(((uint8_t *)regs)[288 + n] == 0x3c);
    uintptr_t tp = regs[17]; assert(tp && *(uintptr_t *)tp == tp); seen[id] = tp;
    for (int lib = 0; lib < 2; lib++) for (int n = 0; n < 6; n++) {
        const char *names[] = {"tp0", "tp16", "tp17", "tp18", "tp28", "tpflags"};
        uintptr_t (*get)(void) = tl_ld_sym(lib ? b : a, names[n]); assert(get && get() == tp);
    }
    uintptr_t (*read_tls_a)(void) = tl_ld_sym(a, "read_tlsdesc");
    uintptr_t (*read_tls_b)(void) = tl_ld_sym(b, "read_tlsdesc");
    void (*write_tls_a)(uintptr_t) = tl_ld_sym(a, "write_tlsdesc");
    assert(read_tls_a && read_tls_b && write_tls_a);
    assert(read_tls_a() == 0x12345678 && read_tls_b() == 0x12345678);
    write_tls_a(0x20000 + id);
    assert(read_tls_a() == 0x20000 + id && read_tls_b() == 0x12345678);
    uintptr_t (*read_g)(void) = tl_ld_sym(b, "read_g");
    void (*write_g)(uintptr_t) = tl_ld_sym(a, "write_g");
    assert(read_g() == 0); write_g(0x10000 + id);
    atomic_fetch_add(&arrived, 1);
    while (atomic_load(&arrived) != 8) usleep(100);
    assert(read_g() == 0x10000 + id && read_tls_a() == 0x20000 + id);
    /* A raw syscall must not run printf/libc on a 4 KiB Go-style stack. */
    size_t page = (size_t)getpagesize();
    uint8_t *memory = mmap(NULL, page * 3, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0); assert(memory != MAP_FAILED);
    assert(!mprotect(memory + page, page, PROT_READ | PROT_WRITE));
    for (int n = 0; n < 1024; n++) memory[page + n] = 0x76;
    long (*small)(void *) = tl_ld_sym(a, "small_stack_syscall"); assert(small);
    assert(small(memory + page + 4096) == -38);
    for (int n = 0; n < 1024; n++) assert(memory[page + n] == 0x76);
    munmap(memory, page * 3);
    return NULL;
}
int main(int argc, char **argv)
{
    assert(argc == 3); char error[160]; assert(tl_xmem_open(64u << 20, error, sizeof(error)));
    a = tl_ld_load(argv[1]); b = tl_ld_load(argv[2]); assert(a && b);
    uint32_t (*literal_mrs)(void) = tl_ld_sym(a, "literal_mrs"), (*literal_svc)(void) = tl_ld_sym(a, "literal_svc");
    if (!literal_mrs || !literal_svc || literal_mrs() != 0xd53bd040u || literal_svc() != 0xd4000001u) {
        fprintf(stderr, "FAIL: ARM instruction literals were rewritten as executable code\n"); return 1;
    }
    uint32_t (*literal_data)(void) = tl_ld_sym(a, "literal_data");
    assert(literal_data && literal_data() == 0x1234cafeu);
    pthread_t threads[8];
    for (unsigned i = 0; i < 8; i++) assert(!pthread_create(&threads[i], NULL, worker, (void *)(uintptr_t)i));
    for (unsigned i = 0; i < 8; i++) assert(!pthread_join(threads[i], NULL));
    for (unsigned i = 0; i < 8; i++) for (unsigned j = i + 1; j < 8; j++) assert(seen[i] != seen[j]);
    check_generated_tls();
    int (*eventfd_create)(unsigned, int) = tl_bionic_find("eventfd"); assert(eventfd_create);
    int fd = eventfd_create(0, 0x800); assert(fd >= 0); uint64_t value = 7, received = 0;
    assert(tl_linux_syscall(fd, (long)&value, 8, 0, 0, 0, 64) == 8);
    assert(tl_linux_syscall(fd, (long)&received, 8, 0, 0, 0, 63) == 8 && received == value);
    assert(tl_linux_syscall(fd, 0, 0, 0, 0, 0, 57) == 0);
    puts("PASS: per-thread/cross-library Go TLS and TLSDESC; cold MRS register/vector/NZCV preservation; small-stack syscalls; raw eventfd IO");
}
