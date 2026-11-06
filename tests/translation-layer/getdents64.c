/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Exercise Go's raw Linux directory-reading path against actual host entries. */
#include "husk-tl-bionic.h"
#include <assert.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

void tl_log_line(const char *fmt, ...) { (void)fmt; }
long tl_linux_syscall(long, long, long, long, long, long, long);
void tl_set_cacerts_dir(const char *);
static int (*g_open)(const char *, int, unsigned);
static int (*g_close)(int);
static long (*g_getdents)(int, void *, size_t);
static long (*g_seek)(int, long, int);

static long raw(long nr, long a, long b, long c)
{
    return tl_linux_syscall(a, b, c, 0, 0, 0, nr);
}

static long read_entries(int fd, void *p, size_t size, bool syscall)
{
    if (syscall) return raw(61, fd, (long)p, size);
    long result = g_getdents(fd, p, size);
    return result < 0 ? -*tl_guest_errno_ptr() : result;
}

static uint16_t record_size(const void *p)
{
    uint16_t size; memcpy(&size, (const char *)p + 16, 2); return size;
}
static int64_t record_position(const void *p)
{
    int64_t pos; memcpy(&pos, (const char *)p + 8, 8); return pos;
}

typedef struct { char *name; uint64_t ino; uint8_t type; bool seen; } expected_entry;

static void compare_directory(const char *guest_path, const char *host_path, size_t capacity, bool syscall)
{
    DIR *host = opendir(host_path); assert(host);
    expected_entry *expected = NULL; size_t count = 0;
    struct dirent *entry;
    while ((entry = readdir(host))) {
        expected = realloc(expected, (count + 1) * sizeof(*expected)); assert(expected);
        expected[count++] = (expected_entry){ strdup(entry->d_name), entry->d_ino, entry->d_type, false };
    }
    closedir(host);
    int fd = syscall ? (int)tl_linux_syscall(-100, (long)guest_path, 0x90000, 0, 0, 0, 56)
                     : g_open(guest_path, 0x90000, 0); /* Linux O_DIRECTORY | O_CLOEXEC */
    assert(fd >= 0);
    uint8_t storage[8194]; size_t found = 0, calls = 0;
    bool large_retry = false;
    for (;;) {
        assert(++calls < count * 3 + 10);
        size_t size = large_retry ? 8192 : capacity;
        memset(storage, 0xa5, sizeof(storage));
        uint8_t *buffer = storage + 1; /* deliberately unaligned */
        long n = read_entries(fd, buffer, size, syscall);
        assert(storage[0] == 0xa5 && buffer[size] == 0xa5);
        if (n == -22) { assert(!large_retry); large_retry = true; continue; }
        large_retry = false;
        assert(n >= 0 && (size_t)n <= size);
        if (!n) break;
        for (size_t offset = 0; offset < (size_t)n; ) {
            uint8_t *record = buffer + offset;
            uint16_t length = record_size(record);
            assert(length >= 24 && !(length & 7) && offset + length <= (size_t)n);
            const char *name = (const char *)record + 19;
            assert(memchr(name, 0, length - 19));
            uint64_t ino; memcpy(&ino, record, 8);
            size_t index;
            for (index = 0; index < count && strcmp(expected[index].name, name); index++);
            assert(index < count && !expected[index].seen);
            assert(ino == expected[index].ino && record[18] == expected[index].type);
            assert(record_position(record) == (int64_t)found + 1);
            expected[index].seen = true; found++;
            offset += length;
        }
    }
    assert(found == count);
    assert(read_entries(fd, storage, sizeof(storage), syscall) == 0);
    assert(g_seek(fd, 0, SEEK_SET) == 0);
    assert(read_entries(fd, storage, sizeof(storage), syscall) > 0);
    assert(g_close(fd) == 0);
    for (size_t i = 0; i < count; i++) free(expected[i].name);
    free(expected);
}

static int host_fd_count(void)
{
    DIR *d = opendir("/dev/fd"); assert(d);
    int count = 0; while (readdir(d)) count++;
    closedir(d); return count;
}

static void aliases_and_errors(const char *path)
{
    int (*guest_dup)(int) = tl_bionic_find("dup");
    int (*guest_dup2)(int, int) = tl_bionic_find("dup2");
    int (*guest_fcntl)(int, int, long) = tl_bionic_find("fcntl");
    assert(guest_dup && guest_dup2 && guest_fcntl);
    uint8_t data[8192];
    assert(raw(61, -1, (long)data, sizeof(data)) == -9);
    int file = open("/dev/null", O_RDONLY); assert(file >= 0);
    assert(raw(61, file, (long)data, sizeof(data)) == -20);
    assert(g_close(file) == 0);
    int baseline = host_fd_count();
    for (int iteration = 0; iteration < 25; iteration++) {
        int fd = g_open(path, 0x90000, 0); assert(fd >= 0);
        /* Duplicating before the first read must share the later cursor too. */
        int alias = iteration % 2 ? guest_dup(fd) : guest_fcntl(fd, 1030, 0);
        assert(alias >= 0);
        assert(raw(61, fd, (long)data, 1) == -22);
        assert(g_getdents(fd, data, 1) == -1 && *tl_guest_errno_ptr() == 22);
        assert(raw(61, fd, 0, sizeof(data)) == -14);
        assert(raw(61, fd, (long)data, 24) == 24);
        assert(record_position(data) == 1);
        assert(raw(62, alias, 0, SEEK_CUR) == 1);
        assert(g_close(fd) == 0);
        assert(raw(61, alias, (long)data, 24) == 24);
        assert(record_position(data) == 2);
        char second[32]; snprintf(second, sizeof(second), "%s", data + 19);
        assert(raw(62, alias, 1, SEEK_SET) == 1);
        assert(raw(61, alias, (long)data, 24) == 24 && !strcmp((char *)data + 19, second));
        int replacement = g_open(path, 0x90000, 0); assert(replacement >= 0);
        assert(raw(61, replacement, (long)data, 24) == 24);
        assert(guest_dup2(alias, replacement) == replacement);
        assert(g_seek(replacement, 0, SEEK_CUR) == 2);
        assert(g_close(alias) == 0);
        assert(g_seek(replacement, 0, SEEK_SET) == 0);
        assert(raw(61, replacement, (long)data, 24) == 24 && record_position(data) == 1);
        assert(g_close(replacement) == 0);
        assert(raw(61, replacement, (long)data, sizeof(data)) == -9);
    }
    assert(host_fd_count() == baseline);
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    g_open = tl_bionic_find("open"); g_close = tl_bionic_find("close");
    g_getdents = tl_bionic_find("getdents64"); g_seek = tl_bionic_find("lseek");
    assert(g_open && g_close && g_getdents && g_seek);
    assert(tl_bionic_find("__getdents64") == (void *)g_getdents);
    char fixture[2048], child[4096], long_name[256];
    const char *temporary = getenv("TMPDIR"); assert(temporary && temporary[0] == '/');
    assert(snprintf(fixture, sizeof(fixture), "%s/getdents-fixture-XXXXXX", temporary) < sizeof(fixture));
    assert(mkdtemp(fixture));
    memset(long_name, 'x', 255); long_name[255] = 0;
    const char *names[] = { "a", "abc", "abcd", "a-certificate-name.0", long_name };
    for (size_t i = 0; i < sizeof(names) / sizeof(*names); i++) {
        snprintf(child, sizeof(child), "%s/%s", fixture, names[i]);
        int fd = open(child, O_CREAT | O_EXCL | O_WRONLY, 0600); assert(fd >= 0); close(fd);
    }
    snprintf(child, sizeof(child), "%s/folder", fixture); assert(!mkdir(child, 0700));
    snprintf(child, sizeof(child), "%s/link", fixture); assert(!symlink("a", child));
    const size_t capacities[] = {24, 32, 40, 64, 128, 280, 8192};
    for (size_t i = 0; i < sizeof(capacities) / sizeof(*capacities); i++)
        for (int syscall = 0; syscall < 2; syscall++) compare_directory(fixture, fixture, capacities[i], syscall);
    aliases_and_errors(fixture);
    tl_set_cacerts_dir(argv[1]);
    compare_directory("/system/etc/security/cacerts", argv[1], 8192, true);
    compare_directory("/apex/com.android.conscrypt/cacerts", argv[1], 64, false);
    DIR *certs = opendir(argv[1]); assert(certs);
    struct dirent *entry;
    while ((entry = readdir(certs)) && entry->d_name[0] == '.');
    assert(entry);
    snprintf(child, sizeof(child), "/system/etc/security/cacerts/%s", entry->d_name);
    int cert = (int)tl_linux_syscall(-100, (long)child, 0, 0, 0, 0, 56); assert(cert >= 0);
    char pem[64] = {0}; assert(raw(63, cert, (long)pem, sizeof(pem) - 1) > 0);
    assert(strstr(pem, "-----BEGIN CERTIFICATE-----")); assert(!g_close(cert));
    closedir(certs); tl_set_cacerts_dir(NULL);
    for (size_t i = 0; i < sizeof(names) / sizeof(*names); i++) {
        snprintf(child, sizeof(child), "%s/%s", fixture, names[i]); assert(!unlink(child));
    }
    snprintf(child, sizeof(child), "%s/link", fixture); assert(!unlink(child));
    snprintf(child, sizeof(child), "%s/folder", fixture); assert(!rmdir(child));
    assert(!rmdir(fixture));
    puts("PASS: raw/libc getdents64, Linux records, tiny/unaligned buffers, EOF/rewind, dup/dup2/fcntl, errors, fd cleanup and bundled CA enumeration/read");
}
