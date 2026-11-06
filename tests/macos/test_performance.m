/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Use the actual titlebar action, metrics sampler and window lifecycle. */
#define main husk_app_host_main
#include "../../tools/macos/HuskAppHost.m"
#undef main
#include <assert.h>
#include <sys/mman.h>
#import <WebKit/WebKit.h>

static uint64_t rendered_frames;
static int terminations;
static volatile sig_atomic_t workerStop;
static void stop_worker(int signal) { (void)signal; workerStop = 1; }
@interface PerformanceTestApplication : NSApplication
@end
@implementation PerformanceTestApplication
- (void)terminate:(id)sender { (void)sender; terminations++; }
@end

@interface PartialProcessSampler : HuskProcessSampler
@property NSArray<HuskProcessMetrics *> *fixed;
@property BOOL complete;
@end
@implementation PartialProcessSampler
- (NSArray<HuskProcessMetrics *> *)sample { return self.fixed; }
- (BOOL)discoveryComplete { return self.complete; }
@end

static int32_t stub_start(husk_runtime_handle *handle, const husk_runtime_app_config_v1 *config,
                         char *error, size_t capacity)
{ (void)handle; (void)config; (void)error; (void)capacity; return 0; }
static int32_t stub_attach(void *screen, void *container, float scale, int32_t fresh,
                          char *error, size_t capacity)
{ (void)screen; (void)container; (void)scale; (void)fresh; (void)error; (void)capacity; return 0; }
static int32_t stub_resize(int32_t width, int32_t height, char *error, size_t capacity)
{ (void)width; (void)height; (void)error; (void)capacity; return 0; }
static int32_t stub_ended(void) { return 0; }
static int32_t stub_web_hit(float x, float y) { (void)x; (void)y; return 0; }
static uint64_t stub_frames(void) { return rendered_frames; }

static NSTask *worker(pid_t owner, BOOL nested)
{
    NSTask *task = [NSTask new];
    task.executableURL = NSBundle.mainBundle.executableURL;
    task.arguments = @[@"--worker", [NSString stringWithFormat:@"%d", owner], nested ? @"1" : @"0"];
    task.standardOutput = [NSPipe pipe];
    assert([task launchAndReturnError:NULL]);
    NSData *ready = [[task.standardOutput fileHandleForReading] availableData];
    assert(ready.length);
    return task;
}

static HuskProcessMetrics *find_process(HuskPerformance *panel, pid_t pid)
{
    for (HuskProcessMetrics *process in panel.processes) if (process.pid == pid) return process;
    return nil;
}

static HuskProcessMetrics *fake_process(pid_t pid, pid_t parent, pid_t responsible, uint64_t start)
{
    HuskProcessMetrics *process = [HuskProcessMetrics new];
    process.pid = pid; process.parent = parent; process.responsible = responsible; process.started = start;
    return process;
}

static void test_ownership(void)
{
    NSDictionary *processes = @{@10:fake_process(10, 1, 1, 100), @11:fake_process(11, 10, 1, 110),
        @12:fake_process(12, 11, 1, 120), @13:fake_process(13, 1, 10, 130),
        @14:fake_process(14, 1, 13, 140), @15:fake_process(15, 1, 1, 150),
        @16:fake_process(16, 1, 15, 160), @17:fake_process(17, 10, 10, 90),
        @18:fake_process(18, 19, 19, 180), @19:fake_process(19, 18, 18, 190)};
    NSArray *owned = husk_performance_owned_processes(processes, 10);
    assert(([[owned valueForKey:@"pid"] isEqual:@[@10, @11, @12, @13, @14]]));
    assert(husk_performance_owned_processes(processes, 999).count == 0);
    HuskProcessMetrics *before = fake_process(10, 1, 10, 100);
    before.usageAvailable = YES;
    husk_performance_process_cpu(before, nil, 1, 100);
    HuskProcessMetrics *reused = fake_process(10, 1, 10, 200);
    reused.usageAvailable = YES;
    husk_performance_process_cpu(reused, before, 2, 100000000);
    assert(isnan(reused.cpuPercent));
    reused.usageAvailable = NO;
    husk_performance_process_cpu(reused, reused, 3, 0);
    assert(isnan(reused.cpuPercent) && !reused.cpu.valid);
    puts("PASS: descendants, launchd-owned helpers, nested responsibility, foreign apps, cycles and PID reuse");
}

static void run_for(double seconds)
{ [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:seconds]]; }

static void capture(NSWindow *window, NSString *path)
{
    NSView *frame = window.contentView.superview;
    [frame layoutSubtreeIfNeeded];
    NSBitmapImageRep *bitmap = [frame bitmapImageRepForCachingDisplayInRect:frame.bounds];
    [frame cacheDisplayInRect:frame.bounds toBitmapImageRep:bitmap];
    assert([[bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:path atomically:YES]);
}

int main(int argc, const char **argv)
{
    @autoreleasepool {
        setbuf(stdout, NULL);
        setbuf(stderr, NULL);
        if (argc == 4 && strcmp(argv[1], "--worker") == 0) {
            signal(SIGTERM, stop_worker);
            pid_t owner = atoi(argv[2]);
            NSTask *nested = atoi(argv[3]) ? worker(owner, NO) : nil;
            volatile char *pages = mmap(NULL, 32 << 20, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
            assert(pages != MAP_FAILED);
            for (size_t i = 0; i < (32 << 20); i += 4096) pages[i] = (char)i;
            puts("ready"); fflush(stdout);
            double deadline = NSProcessInfo.processInfo.systemUptime + 20;
            while (!workerStop && NSProcessInfo.processInfo.systemUptime < deadline && kill(owner, 0) == 0) {
                double until = NSProcessInfo.processInfo.systemUptime + .03;
                while (NSProcessInfo.processInfo.systemUptime < until) {}
                usleep(10000);
            }
            if (nested.running) { [nested terminate]; [nested waitUntilExit]; }
            munmap((void *)pages, 32 << 20);
            return 0;
        }
        assert(argc == 3);
        NSString *output = [NSString stringWithUTF8String:argv[1]];
        pid_t foreign = atoi(argv[2]);
        test_ownership();
        HuskPerformanceCounter counter = {0};
        assert(isnan(husk_performance_rate(&counter, 1, 100, YES)));
        assert(husk_performance_rate(&counter, 3, 220, YES) == 60);
        assert(husk_performance_rate(&counter, 4, 220, YES) == 0);
        assert(isnan(husk_performance_rate(&counter, 5, 0, YES)));
        assert(husk_performance_rate(&counter, 6, 20, YES) == 20);
        assert(isnan(husk_performance_rate(&counter, 6, 40, YES)));
        assert(isnan(husk_performance_rate(&counter, 7, 0, NO)));
        assert(isnan(husk_performance_rate(&counter, 8, 100, YES)));
        assert([husk_performance_bytes(512ULL << 20) isEqual:@"512.0 MiB"]);
        assert([husk_performance_bytes(2ULL << 30) isEqual:@"2.00 GiB"]);
        puts("PASS: elapsed-time rates, idle frames, counter resets, unavailable samples and memory units");

        [PerformanceTestApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
        static const husk_runtime_app_api_v1 stub = {.start=stub_start, .attach_web=stub_attach,
            .ended=stub_ended, .frames=stub_frames, .resize=stub_resize, .web_hit=stub_web_hit};
        app_api = &stub;
        app_config = @{@"width":@720, @"height":@1280, @"density":@2, @"display_name":@"Husk 性能测试"};
        options = @{@"--no-activate":@YES};
        surface_scale = 2;
        host = [HuskApp new];
        NSApp.delegate = host;
        [host applicationDidFinishLaunching:[NSNotification notificationWithName:NSApplicationDidFinishLaunchingNotification object:NSApp]];
        run_for(.1);
        assert(running && !host.performance);
        assert(host.window.titlebarAccessoryViewControllers.count == 1);
        NSButton *button = (NSButton *)host.window.titlebarAccessoryViewControllers[0].view.subviews[0];
        assert([button isKindOfClass:NSButton.class] && button.target == host);
        assert(NSEqualSizes(host.screen.bounds.size, NSMakeSize(360, 640)));
        [button performClick:nil];
        HuskPerformance *panel = host.performance;
        assert(panel.window.visible && panel.timer.valid);
        assert([panel.values[@"fps"].stringValue isEqual:@"—"]);
        assert([panel.values[@"cpu"].stringValue isEqual:@"—"]);
        assert([panel.values[@"size"].stringValue isEqual:@"720 × 1280 px"]);
        NSTimer *timer = panel.timer;
        [button performClick:nil];
        assert(host.performance == panel && panel.timer == timer);
        rendered_frames = 60;
        run_for(1.2);
        assert(panel.values[@"fps"].doubleValue > 30 && panel.values[@"fps"].doubleValue < 80);
        assert(![panel.values[@"cpu"].stringValue isEqual:@"—"]);
        assert(panel.values[@"threads"].intValue > 0);
        assert(panel.values[@"footprint"].doubleValue > 0);
        assert(panel.values[@"resident"].doubleValue > 0);
        assert(!find_process(panel, foreign));
        /* Burn one core using the main thread; actual CPU accounting must read
         * near 100%, independently of the machine's logical processor count. */
        [panel refresh];
        double until = NSProcessInfo.processInfo.systemUptime + .3;
        while (NSProcessInfo.processInfo.systemUptime < until) {}
        [panel refresh];
        fprintf(stderr, "CPU sample: %s; %s\n", panel.values[@"cpu"].stringValue.UTF8String, panel.coverage.stringValue.UTF8String);
        assert(panel.values[@"cpu"].doubleValue > 60 && panel.values[@"cpu"].doubleValue < 150);
        fprintf(stdout, "PASS: live process sampling (one-core CPU %s, footprint %s, threads %s)\n",
            panel.values[@"cpu"].stringValue.UTF8String, panel.values[@"footprint"].stringValue.UTF8String,
            panel.values[@"threads"].stringValue.UTF8String);
        NSTask *child = worker(getpid(), YES);
        [panel refresh];
        HuskProcessMetrics *childSample = find_process(panel, child.processIdentifier);
        assert(childSample && isnan(childSample.cpuPercent));
        assert([panel.values[@"cpu"].stringValue hasPrefix:@"≥ "]);
        run_for(.4);
        [panel refresh];
        childSample = find_process(panel, child.processIdentifier);
        assert(childSample.cpuPercent > 30 && childSample.footprint >= (32 << 20));
        BOOL grandchild = NO;
        for (HuskProcessMetrics *sample in panel.processes)
            if (sample.parent == child.processIdentifier) grandchild = YES;
        assert(grandchild && !find_process(panel, foreign));
        double sum = 0;
        for (HuskProcessMetrics *sample in panel.processes) sum += sample.cpuPercent;
        assert(fabs(panel.values[@"cpu"].doubleValue - sum) < .11);
        assert(panel.processTable.numberOfRows == (NSInteger)panel.processes.count);
        uint64_t memorySum = 0;
        int threadSum = 0;
        for (HuskProcessMetrics *sample in panel.processes) { memorySum += sample.footprint; threadSum += sample.threads; }
        assert([panel.values[@"footprint"].stringValue isEqual:husk_performance_bytes(memorySum)]);
        assert(panel.values[@"threads"].intValue == threadSum);
        pid_t childPID = child.processIdentifier;
        [child terminate]; [child waitUntilExit];
        [panel refresh];
        assert(!find_process(panel, childPID));
        assert(isfinite(find_process(panel, getpid()).cpuPercent));
        puts("PASS: live child/grandchild CPU and memory, foreign same-binary exclusion, totals and exit without host baseline reset");

        /* Use a real WebKit XPC content process, not a helper with a fake name. */
        WKWebView *web = [[WKWebView alloc] initWithFrame:NSMakeRect(0, 0, 320, 200)];
        [host.window.contentView addSubview:web];
        [web loadHTMLString:@"<html><body>Husk performance WebKit test</body></html>" baseURL:nil];
        BOOL webFound = NO;
        double webDeadline = NSProcessInfo.processInfo.systemUptime + 8;
        while (!webFound && NSProcessInfo.processInfo.systemUptime < webDeadline) {
            run_for(.1);
            [panel refresh];
            for (HuskProcessMetrics *sample in panel.processes)
                if ([sample.name containsString:@"WebContent"]) webFound = YES;
        }
        if (!webFound) {
            HuskProcessMetrics *selfSample = find_process(panel, getpid());
            fprintf(stderr, "WebKit missing: host %d responsible %d; loading %d; URL %s\n", getpid(), selfSample.responsible, web.loading, web.URL.absoluteString.UTF8String);
            pid_t (*responsible)(pid_t) = dlsym(RTLD_DEFAULT, "responsibility_get_pid_responsible_for_pid");
            int capacity = proc_listallpids(NULL, 0) + 256;
            NSMutableData *pids = [NSMutableData dataWithLength:capacity * sizeof(pid_t)];
            int n = proc_listallpids(pids.mutableBytes, (int)pids.length);
            for (int i = 0; i < n; i++) {
                pid_t pid = ((pid_t *)pids.mutableBytes)[i];
                char name[1024] = {0};
                proc_name(pid, name, sizeof(name));
                if (strstr(name, "WebKit")) fprintf(stderr, "WebKit PID %d name %s responsible %d\n", pid, name, responsible ? responsible(pid) : -1);
            }
        }
        assert(webFound && !find_process(panel, foreign));
        puts("PASS: real WebKit content process belongs to this host and appears in process details");
        run_for(.05);
        [panel refresh];
        assert(panel.values[@"fps"].doubleValue == 0);
        [host.window setContentSize:NSMakeSize(760, 640)];
        [panel refresh];
        assert([panel.values[@"size"].stringValue isEqual:@"1520 × 1280 px"]);
        assert([panel.values[@"frames"].stringValue isEqual:@"60"]);
        for (NSString *appearance in @[NSAppearanceNameAqua, NSAppearanceNameDarkAqua]) {
            panel.window.appearance = [NSAppearance appearanceNamed:appearance];
            run_for(.1);
            capture(panel.window, [output stringByAppendingPathComponent:[appearance stringByAppendingString:@".png"]]);
        }
        [panel.window setContentSize:panel.window.contentMinSize];
        run_for(.1);
        capture(panel.window, [output stringByAppendingPathComponent:@"performance-minimum.png"]);
        HuskProcessSampler *realSampler = panel.sampler;
        PartialProcessSampler *partial = [PartialProcessSampler new];
        HuskProcessMetrics *known = fake_process(getpid(), 1, getpid(), 100);
        known.name = @"已读进程"; known.usageAvailable = YES; known.threadsAvailable = YES;
        known.cpuPercent = 25; known.footprint = 32 << 20; known.resident = 48 << 20; known.threads = 3;
        HuskProcessMetrics *unknown = fake_process(999999, 1, getpid(), 200);
        unknown.name = @"暂不可读进程"; unknown.cpuPercent = NAN;
        partial.fixed = @[known, unknown]; partial.complete = YES;
        panel.sampler = partial;
        [panel refresh];
        assert([panel.values[@"cpu"].stringValue isEqual:@"≥ 25.0%"]);
        assert([panel.values[@"footprint"].stringValue isEqual:@"≥ 32.0 MiB"]);
        assert([panel.values[@"threads"].stringValue isEqual:@"≥ 3"]);
        NSTextField *unreadable = (NSTextField *)[panel tableView:panel.processTable viewForTableColumn:[panel.processTable tableColumnWithIdentifier:@"footprint"] row:1];
        assert([unreadable.stringValue isEqual:@"—"]);
        partial.fixed = @[known]; partial.complete = NO;
        [panel refresh];
        assert([panel.values[@"processes"].stringValue isEqual:@"≥ 1"]);
        assert([panel.coverage.stringValue containsString:@"归属查询不完整"]);
        panel.sampler = realSampler;
        [panel refresh];
        puts("PASS: unreadable per-process metrics stay unavailable; partial discovery and totals are clearly marked");
        [host.window setContentSize:NSMakeSize(360, 640)];
        assert(host.window.titleVisibility == NSWindowTitleVisible);
        capture(host.window, [output stringByAppendingPathComponent:@"titlebar.png"]);
        [host.window setContentSize:host.window.contentMinSize];
        run_for(.1);
        assert(!button.hiddenOrHasHiddenAncestor && !NSIsEmptyRect(button.visibleRect));
        assert(host.window.titleVisibility == NSWindowTitleHidden);
        capture(host.window, [output stringByAppendingPathComponent:@"titlebar-minimum.png"]);
        puts("PASS: titlebar action, one window/timer, periodic refresh, idle FPS and resize metrics");

        [panel.window performClose:nil];
        assert(!panel.window.visible && !panel.timer && !timer.valid);
        assert(!stopping && host.window.visible && terminations == 0);
        rendered_frames = 6000;
        [button performClick:nil];
        assert(host.performance == panel && panel.timer.valid && panel.timer != timer);
        assert([panel.values[@"fps"].stringValue isEqual:@"—"]);
        assert([panel.values[@"cpu"].stringValue isEqual:@"—"]);
        assert([panel.values[@"frames"].stringValue isEqual:@"6000"]);
        [host.window performClose:nil];
        assert(terminations == 1);
        [panel close];
        [host.timer invalidate];
        if (host.executionActivity) [NSProcessInfo.processInfo endActivity:host.executionActivity];
        [web removeFromSuperview];
        assert([@"passed\n" writeToFile:[output stringByAppendingPathComponent:@"passed.txt"] atomically:YES encoding:NSUTF8StringEncoding error:NULL]);
        puts("PASS: closing metrics preserves app; reopening resets sampling; main close still terminates");
    }
    return 0;
}
