/* SPDX-License-Identifier: GPL-2.0-or-later */
/* macOS: see run_font_paths.sh. Exercises the production path resolver. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
void tl_set_system_fonts_dir(const char *);
void tl_set_extra_fonts_dir(const char *);
void tl_set_cacerts_dir(const char *);
const char *tl_path_resolve(const char *, char *, size_t);
static void check(const char *path, const char *expected) {
    char buf[2048]; const char *actual = tl_path_resolve(path, buf, sizeof(buf));
    if (strcmp(actual, expected)) { fprintf(stderr, "%s -> %s, expected %s\n", path, actual, expected); exit(1); }
}
int main(void) {
    char root[1024], fonts[1024], extra[1024], file[2048], certs[2048];
    const char *temporary = getenv("TMPDIR"); assert(temporary && temporary[0] == '/');
    assert(snprintf(root, sizeof(root), "%s/husk-font-paths-XXXXXX", temporary) < sizeof(root));
    assert(mkdtemp(root));
    snprintf(fonts, sizeof(fonts), "%s/fonts", root); assert(!mkdir(fonts, 0700));
    snprintf(extra, sizeof(extra), "%s/extra", root); assert(!mkdir(extra, 0700));
    snprintf(file, sizeof(file), "%s/Latin.ttf", fonts); FILE *f = fopen(file,"wb"); assert(f); fclose(f);
    tl_set_system_fonts_dir(root); tl_set_extra_fonts_dir(extra);
    setenv("ANDROID_ROOT", "/runtime/java-data", 1);
    check("/system/fonts/Latin.ttf", file);
    check("/runtime/java-data/fonts/Latin.ttf", file);
    snprintf(file, sizeof(file), "%s/CJK.ttc", extra); f=fopen(file,"wb"); assert(f); fclose(f);
    check("/system/fonts/CJK.ttc", file);
    check("/runtime/java-data/fonts/CJK.ttc", file);
    check("/runtime/java-data/fonts-backup/CJK.ttc", "/runtime/java-data/fonts-backup/CJK.ttc");
    check("/runtime/java-data-other/fonts/CJK.ttc", "/runtime/java-data-other/fonts/CJK.ttc");
    check("/runtime/java-data/files/save", "/runtime/java-data/files/save");
    snprintf(certs, sizeof(certs), "%s/cacerts", root); assert(!mkdir(certs, 0700));
    tl_set_cacerts_dir(certs);
    check("/system/etc/security/cacerts", certs);
    check("/apex/com.android.conscrypt/cacerts", certs);
    check("/runtime/java-data/etc/security/cacerts", certs);
    snprintf(file, sizeof(file), "%s/12345678.0", certs);
    check("/runtime/java-data/etc/security/cacerts/12345678.0", file);
    check("/system/etc/security/cacerts/12345678.0", file);
    check("/apex/com.android.conscrypt/cacerts/12345678.0", file);
    check("/runtime/java-data/etc/security/cacerts-backup/12345678.0", "/runtime/java-data/etc/security/cacerts-backup/12345678.0");
    check("/runtime/java-data-other/etc/security/cacerts", "/runtime/java-data-other/etc/security/cacerts");
    tl_set_cacerts_dir(NULL); rmdir(certs);
    snprintf(file, sizeof(file), "%s/CJK.ttc", extra);
    unlink(file); snprintf(file,sizeof(file),"%s/fonts/CJK.ttc",root);
    check("/runtime/java-data/fonts/CJK.ttc",file);
    setenv("ANDROID_ROOT","/system",1); check("/system/fonts/CJK.ttc",file);
    unsetenv("ANDROID_ROOT"); check("/system/fonts/CJK.ttc",file);
    tl_set_system_fonts_dir(NULL);
    check("/runtime/java-data/fonts/CJK.ttc","/runtime/java-data/fonts/CJK.ttc");
    snprintf(file,sizeof(file),"%s/Latin.ttf",fonts);unlink(file);rmdir(fonts);rmdir(extra);rmdir(root);
    puts("PASS: bundled/downloaded fonts, trusted CA paths, root aliases, boundaries, unrelated data");
}
