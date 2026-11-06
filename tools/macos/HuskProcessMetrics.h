/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Read-only host process accounting. No task ports or guest calls. */
#import <Foundation/Foundation.h>
#include <libproc.h>
#include <sys/resource.h>
#include <sys/proc.h>
#include <dlfcn.h>
#include <errno.h>
#include <math.h>
#include <mach/mach_time.h>
#include <unistd.h>

typedef struct {
    double time;
    uint64_t value;
    BOOL valid;
} HuskPerformanceCounter;

/* A missing sample, counter reset or non-positive interval starts a new baseline. */
static double husk_performance_rate(HuskPerformanceCounter *previous, double now,
                                    uint64_t value, BOOL available)
{
    double rate = NAN;
    if (available && previous->valid && now > previous->time && value >= previous->value)
        rate = (double)(value - previous->value) / (now - previous->time);
    *previous = (HuskPerformanceCounter){now, value, available};
    return rate;
}

@interface HuskProcessMetrics : NSObject
@property pid_t pid, parent, responsible;
@property uint64_t started, footprint, resident;
@property NSString *name;
@property int threads;
@property BOOL usageAvailable, threadsAvailable;
@property double cpuPercent;
@property HuskPerformanceCounter cpu;
@end
@implementation HuskProcessMetrics
@end

/* Follow only edges down from this host, never its responsible ancestor (which
 * can be a terminal/IDE shared by unrelated apps). Names are not ownership. */
static NSArray<HuskProcessMetrics *> *husk_performance_owned_processes(
    NSDictionary<NSNumber *, HuskProcessMetrics *> *processes, pid_t root)
{
    HuskProcessMetrics *host = processes[@(root)];
    if (!host) return @[];
    NSMutableSet<NSNumber *> *owned = [NSMutableSet setWithObject:@(root)];
    BOOL changed;
    do {
        changed = NO;
        for (NSNumber *key in processes) {
            if ([owned containsObject:key]) continue;
            HuskProcessMetrics *process = processes[key];
            if (process.started < host.started) continue;
            for (NSNumber *owner in @[@(process.parent), @(process.responsible)]) {
                if ([owned containsObject:owner] && processes[owner].started <= process.started) {
                    [owned addObject:key];
                    changed = YES;
                    break;
                }
            }
        }
    } while (changed);
    NSArray *keys = [owned.allObjects sortedArrayUsingComparator:^NSComparisonResult(NSNumber *a, NSNumber *b) {
        if ([a isEqual:b]) return NSOrderedSame;
        if (a.intValue == root) return NSOrderedAscending;
        if (b.intValue == root) return NSOrderedDescending;
        return [a compare:b];
    }];
    NSMutableArray *result = [NSMutableArray arrayWithCapacity:keys.count];
    for (NSNumber *key in keys) [result addObject:processes[key]];
    return result;
}

static uint64_t husk_performance_start(struct proc_bsdinfo info)
{
    return info.pbi_start_tvsec * 1000000 + info.pbi_start_tvusec;
}

static void husk_performance_process_cpu(HuskProcessMetrics *sample, HuskProcessMetrics *previous,
                                          double now, uint64_t ticks)
{
    static double secondsPerTick;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        mach_timebase_info_data_t timebase;
        mach_timebase_info(&timebase);
        secondsPerTick = (double)timebase.numer / timebase.denom / 1e9;
    });
    HuskPerformanceCounter counter = {0};
    if (previous && previous.started == sample.started) counter = previous.cpu;
    sample.cpuPercent = husk_performance_rate(&counter, now, ticks, sample.usageAvailable) * secondsPerTick * 100;
    sample.cpu = counter;
}

@interface HuskProcessSampler : NSObject
@property NSDictionary<NSNumber *, HuskProcessMetrics *> *previous;
@property (readonly) BOOL discoveryComplete;
- (NSArray<HuskProcessMetrics *> *)sample;
- (void)reset;
@end

@implementation HuskProcessSampler
- (void)reset { self.previous = @{}; }
- (NSArray<HuskProcessMetrics *> *)sample
{
    /* WebKit XPC helpers usually have launchd as their Unix parent. Resolve the
     * optional responsibility SPI at runtime; never fall back to name matching.
     * If it disappears, retain PPID accounting and report incomplete discovery. */
    static pid_t (*responsiblePID)(pid_t);
    static dispatch_once_t once;
    dispatch_once(&once, ^{ responsiblePID = dlsym(RTLD_DEFAULT, "responsibility_get_pid_responsible_for_pid"); });
    /* A command-line launch may share its responsible ancestor with unrelated
     * apps. Never count that ancestor's helpers; explicitly mark the gap. */
    _discoveryComplete = responsiblePID && responsiblePID(getpid()) == getpid();
    int size = proc_listpids(PROC_UID_ONLY, geteuid(), NULL, 0);
    NSMutableData *storage = nil;
    int length = -1;
    for (int attempt = 0; size > 0 && attempt < 3; attempt++) {
        size += 256 * (int)sizeof(pid_t);
        storage = [NSMutableData dataWithLength:size];
        length = proc_listpids(PROC_UID_ONLY, geteuid(), storage.mutableBytes, size);
        if (length < size) break;
        size *= 2;
    }
    if (length <= 0 || length >= (int)storage.length) _discoveryComplete = NO;
    NSMutableDictionary<NSNumber *, HuskProcessMetrics *> *processes = [NSMutableDictionary dictionary];
    const pid_t *pids = storage.bytes;
    /* Explicitly include self even when enumeration fails or races with spawn. */
    for (int i = -1; i < MAX(0, length) / (int)sizeof(pid_t); i++) {
        pid_t pid = i < 0 ? getpid() : pids[i];
        if (pid <= 0 || processes[@(pid)]) continue;
        struct proc_bsdinfo info = {0};
        if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info)) != sizeof(info)) {
            if (errno == EPERM || errno == EACCES) _discoveryComplete = NO;
            continue;
        }
        if (info.pbi_uid != geteuid() || info.pbi_status == SZOMB) continue;
        HuskProcessMetrics *process = [HuskProcessMetrics new];
        process.pid = pid;
        process.parent = info.pbi_ppid;
        process.responsible = responsiblePID ? responsiblePID(pid) : -1;
        process.started = husk_performance_start(info);
        process.cpuPercent = NAN;
        process.name = [[NSString alloc] initWithBytes:info.pbi_name length:strnlen(info.pbi_name, sizeof(info.pbi_name)) encoding:NSUTF8StringEncoding];
        if (!process.name.length)
            process.name = [[NSString alloc] initWithBytes:info.pbi_comm length:strnlen(info.pbi_comm, sizeof(info.pbi_comm)) encoding:NSUTF8StringEncoding] ?: @"进程";
        processes[@(pid)] = process;
    }
    NSMutableArray *result = [NSMutableArray array];
    NSMutableDictionary *next = [NSMutableDictionary dictionary];
    for (HuskProcessMetrics *process in husk_performance_owned_processes(processes, getpid())) {
        struct rusage_info_v0 usage = {0};
        process.usageAvailable = proc_pid_rusage(process.pid, RUSAGE_INFO_V0, (rusage_info_t *)&usage) == 0;
        double now = NSProcessInfo.processInfo.systemUptime;
        struct proc_taskinfo task = {0};
        process.threadsAvailable = proc_pidinfo(process.pid, PROC_PIDTASKINFO, 0, &task, sizeof(task)) == sizeof(task);
        /* Discard exited/reused PIDs rather than displaying stale metrics or
         * carrying CPU history to a different process with the same PID. */
        struct proc_bsdinfo check = {0};
        if (proc_pidinfo(process.pid, PROC_PIDTBSDINFO, 0, &check, sizeof(check)) != sizeof(check)) {
            if (errno == ESRCH) continue;
            process.usageAvailable = NO;
            process.threadsAvailable = NO;
            _discoveryComplete = NO;
        } else if (check.pbi_status == SZOMB || husk_performance_start(check) != process.started) continue;
        process.footprint = usage.ri_phys_footprint;
        process.resident = usage.ri_resident_size;
        process.threads = task.pti_threadnum;
        /* rusage CPU time uses Mach absolute ticks (including on Apple Silicon).
         * Do not add ri_child_*: those account for already-exited children. */
        husk_performance_process_cpu(process, self.previous[@(process.pid)], now, usage.ri_user_time + usage.ri_system_time);
        next[@(process.pid)] = process;
        [result addObject:process];
    }
    self.previous = next;
    return result;
}
@end
