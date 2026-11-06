/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Host-only metrics. No guest calls, GPU readbacks or per-thread Mach ports. */
#import <AppKit/AppKit.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <libproc.h>
#include <math.h>

typedef struct {
    uint64_t frames;
    NSSize pixels;
    BOOL running;
} HuskRenderMetrics;

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

static NSString *husk_performance_bytes(uint64_t bytes)
{
    double mib = bytes / (1024.0 * 1024.0);
    return mib >= 1024 ? [NSString stringWithFormat:@"%.2f GiB", mib / 1024] :
                        [NSString stringWithFormat:@"%.1f MiB", mib];
}

@interface HuskPerformance : NSWindowController <NSWindowDelegate> {
    HuskPerformanceCounter _cpu, _frames;
    double _secondsPerTick;
}
@property (copy) HuskRenderMetrics (^readRendering)(void);
@property NSTimer *timer;
@property NSMutableDictionary<NSString *, NSTextField *> *values;
- (instancetype)initWithName:(NSString *)name;
- (void)showForWindow:(NSWindow *)parent;
- (void)refresh;
@end

@implementation HuskPerformance
- (instancetype)initWithName:(NSString *)name
{
    NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 360, 408)
        styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        backing:NSBackingStoreBuffered defer:NO];
    self = [super initWithWindow:window];
    if (!self) return nil;
    window.title = [@"性能 · " stringByAppendingString:name];
    window.releasedWhenClosed = NO;
    window.delegate = self;
    window.tabbingMode = NSWindowTabbingModeDisallowed;
    window.collectionBehavior = NSWindowCollectionBehaviorFullScreenAuxiliary;
    mach_timebase_info_data_t timebase;
    mach_timebase_info(&timebase);
    _secondsPerTick = (double)timebase.numer / timebase.denom / 1e9;
    self.values = [NSMutableDictionary dictionary];

    NSStackView *stack = [NSStackView new];
    stack.orientation = NSUserInterfaceLayoutOrientationVertical;
    stack.alignment = NSLayoutAttributeLeading;
    stack.spacing = 12;
    stack.translatesAutoresizingMaskIntoConstraints = NO;
    [window.contentView addSubview:stack];
    [NSLayoutConstraint activateConstraints:@[
        [stack.leadingAnchor constraintEqualToAnchor:window.contentView.leadingAnchor constant:20],
        [stack.trailingAnchor constraintEqualToAnchor:window.contentView.trailingAnchor constant:-20],
        [stack.topAnchor constraintEqualToAnchor:window.contentView.topAnchor constant:20]]];
    NSTextField *heading = [NSTextField labelWithString:@"实时性能"];
    heading.font = [NSFont systemFontOfSize:18 weight:NSFontWeightSemibold];
    [stack addArrangedSubview:heading];
    NSTextField *identity = [NSTextField labelWithString:[NSString stringWithFormat:@"%@ · PID %d", name, getpid()]];
    identity.textColor = NSColor.secondaryLabelColor;
    identity.lineBreakMode = NSLineBreakByTruncatingTail;
    [stack addArrangedSubview:identity];
    [identity.widthAnchor constraintEqualToAnchor:stack.widthAnchor].active = YES;

    NSArray *rows = @[@[@"cpu", @"CPU 使用率"], @[@"fps", @"渲染 FPS"],
        @[@"footprint", @"内存占用"], @[@"resident", @"驻留内存"],
        @[@"threads", @"线程数"], @[@"size", @"渲染尺寸"], @[@"frames", @"累计渲染帧"]];
    for (NSArray *row in rows) {
        NSTextField *label = [NSTextField labelWithString:row[1]];
        label.textColor = NSColor.secondaryLabelColor;
        NSTextField *value = [NSTextField labelWithString:@"—"];
        value.font = [NSFont monospacedDigitSystemFontOfSize:13 weight:NSFontWeightMedium];
        value.alignment = NSTextAlignmentRight;
        [value setContentCompressionResistancePriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
        NSView *spacer = [NSView new];
        [spacer setContentHuggingPriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
        NSStackView *line = [NSStackView stackViewWithViews:@[label, spacer, value]];
        line.orientation = NSUserInterfaceLayoutOrientationHorizontal;
        [stack addArrangedSubview:line];
        [line.widthAnchor constraintEqualToAnchor:stack.widthAnchor].active = YES;
        self.values[row[0]] = value;
    }
    self.values[@"footprint"].toolTip = @"系统记录的当前进程 physical footprint，含压缩内存等记账。";
    self.values[@"resident"].toolTip = @"当前进程驻留在物理内存中的页。";
    NSTextField *note = [NSTextField wrappingLabelWithString:
        @"每秒更新，仅统计当前 App 进程，不含 WebKit 子进程。\nCPU 100% = 一个核心。FPS 为渲染提交速率，静止时可为 0，不含原生 WebView。"];
    note.font = [NSFont systemFontOfSize:11];
    note.textColor = NSColor.secondaryLabelColor;
    [stack addArrangedSubview:note];
    [note.widthAnchor constraintEqualToAnchor:stack.widthAnchor].active = YES;
    [note.bottomAnchor constraintLessThanOrEqualToAnchor:window.contentView.bottomAnchor constant:-16].active = YES;
    return self;
}
- (void)showForWindow:(NSWindow *)parent
{
    if (!self.timer) {
        _cpu = (HuskPerformanceCounter){0};
        _frames = (HuskPerformanceCounter){0};
        [self refresh];
        __weak HuskPerformance *weakSelf = self;
        self.timer = [NSTimer timerWithTimeInterval:1 repeats:YES block:^(NSTimer *timer) {
            (void)timer; [weakSelf refresh];
        }];
        self.timer.tolerance = 0.1;
        [NSRunLoop.mainRunLoop addTimer:self.timer forMode:NSRunLoopCommonModes];
        NSRect frame = self.window.frame, available = (parent.screen ?: NSScreen.mainScreen).visibleFrame;
        CGFloat x = NSMaxX(parent.frame) + 12;
        if (x + frame.size.width > NSMaxX(available)) x = NSMinX(parent.frame) - frame.size.width - 12;
        x = MAX(NSMinX(available), MIN(x, NSMaxX(available) - frame.size.width));
        CGFloat y = MAX(NSMinY(available), MIN(NSMaxY(parent.frame), NSMaxY(available)) - frame.size.height);
        [self.window setFrameTopLeftPoint:NSMakePoint(x, y + frame.size.height)];
    }
    [self showWindow:nil];
    [self.window makeKeyAndOrderFront:nil];
}
- (void)refresh
{
    double now = NSProcessInfo.processInfo.systemUptime;
    task_absolutetime_info_data_t cpu = {0};
    mach_msg_type_number_t count = TASK_ABSOLUTETIME_INFO_COUNT;
    BOOL cpuOK = task_info(mach_task_self(), TASK_ABSOLUTETIME_INFO, (task_info_t)&cpu, &count) == KERN_SUCCESS;
    double cpuRate = husk_performance_rate(&_cpu, now, cpu.total_user + cpu.total_system, cpuOK);
    self.values[@"cpu"].stringValue = isfinite(cpuRate) ?
        [NSString stringWithFormat:@"%.1f%%", cpuRate * _secondsPerTick * 100] : @"—";

    task_vm_info_data_t vm = {0};
    count = TASK_VM_INFO_REV1_COUNT;
    BOOL vmOK = task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vm, &count) == KERN_SUCCESS && count >= TASK_VM_INFO_REV1_COUNT;
    self.values[@"footprint"].stringValue = vmOK ? husk_performance_bytes(vm.phys_footprint) : @"—";
    self.values[@"resident"].stringValue = vmOK ? husk_performance_bytes(vm.resident_size) : @"—";
    struct proc_taskinfo task = {0};
    BOOL taskOK = proc_pidinfo(getpid(), PROC_PIDTASKINFO, 0, &task, sizeof(task)) == sizeof(task);
    self.values[@"threads"].stringValue = taskOK ? [NSString stringWithFormat:@"%d", task.pti_threadnum] : @"—";

    HuskRenderMetrics render = self.readRendering ? self.readRendering() : (HuskRenderMetrics){0};
    double fps = husk_performance_rate(&_frames, now, render.frames, render.running);
    self.values[@"fps"].stringValue = isfinite(fps) ? [NSString stringWithFormat:@"%.1f", fps] : @"—";
    self.values[@"frames"].stringValue = render.running ? [NSString stringWithFormat:@"%llu", (unsigned long long)render.frames] : @"—";
    self.values[@"size"].stringValue = render.pixels.width > 0 && render.pixels.height > 0 ?
        [NSString stringWithFormat:@"%.0f × %.0f px", render.pixels.width, render.pixels.height] : @"—";
}
- (void)windowWillClose:(NSNotification *)notification
{
    (void)notification;
    [self.timer invalidate];
    self.timer = nil;
}
- (void)dealloc { [_timer invalidate]; }
@end
