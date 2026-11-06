/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Host-only metrics. No guest calls, GPU readbacks or per-thread Mach ports. */
#import <AppKit/AppKit.h>
#include "HuskProcessMetrics.h"

typedef struct {
    uint64_t frames;
    NSSize pixels;
    BOOL running;
} HuskRenderMetrics;

static NSString *husk_performance_bytes(uint64_t bytes)
{
    double mib = bytes / (1024.0 * 1024.0);
    return mib >= 1024 ? [NSString stringWithFormat:@"%.2f GiB", mib / 1024] :
                        [NSString stringWithFormat:@"%.1f MiB", mib];
}

@interface HuskPerformance : NSWindowController <NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate> {
    HuskPerformanceCounter _frames;
}
@property (copy) HuskRenderMetrics (^readRendering)(void);
@property NSTimer *timer;
@property NSMutableDictionary<NSString *, NSTextField *> *values;
@property HuskProcessSampler *sampler;
@property NSArray<HuskProcessMetrics *> *processes;
@property NSTableView *processTable;
@property NSTextField *coverage;
- (instancetype)initWithName:(NSString *)name;
- (void)showForWindow:(NSWindow *)parent;
- (void)refresh;
@end

@implementation HuskPerformance
- (instancetype)initWithName:(NSString *)name
{
    NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 760, 650)
        styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
        backing:NSBackingStoreBuffered defer:NO];
    self = [super initWithWindow:window];
    if (!self) return nil;
    window.title = [@"性能 · " stringByAppendingString:name];
    window.releasedWhenClosed = NO;
    window.delegate = self;
    window.tabbingMode = NSWindowTabbingModeDisallowed;
    window.collectionBehavior = NSWindowCollectionBehaviorFullScreenAuxiliary;
    window.contentMinSize = NSMakeSize(720, 600);
    self.sampler = [HuskProcessSampler new];
    self.processes = @[];
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
    NSTextField *heading = [NSTextField labelWithString:@"实时性能 · 合计"];
    heading.font = [NSFont systemFontOfSize:18 weight:NSFontWeightSemibold];
    [stack addArrangedSubview:heading];
    NSTextField *identity = [NSTextField labelWithString:[NSString stringWithFormat:@"%@ · PID %d", name, getpid()]];
    identity.textColor = NSColor.secondaryLabelColor;
    identity.lineBreakMode = NSLineBreakByTruncatingTail;
    [stack addArrangedSubview:identity];
    [identity.widthAnchor constraintEqualToAnchor:stack.widthAnchor].active = YES;

    NSArray *rows = @[@[@"cpu", @"CPU 总使用率"],
        @[@"footprint", @"内存总占用"], @[@"resident", @"驻留内存合计"],
        @[@"threads", @"总线程数"], @[@"processes", @"进程数"],
        @[@"fps", @"渲染 FPS"], @[@"size", @"渲染尺寸"], @[@"frames", @"累计渲染帧"]];
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
    self.values[@"footprint"].toolTip = @"此 App 各进程的 physical footprint 相加，含压缩内存等系统记账；不是去重后的独占物理内存。";
    self.values[@"resident"].toolTip = @"各进程驻留内存相加，共享页可能重复计入。";
    NSTextField *detail = [NSTextField labelWithString:@"各进程明细"];
    detail.font = [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold];
    [stack addArrangedSubview:detail];
    self.processTable = [NSTableView new];
    self.processTable.dataSource = self;
    self.processTable.delegate = self;
    self.processTable.rowHeight = 26;
    self.processTable.usesAlternatingRowBackgroundColors = YES;
    self.processTable.columnAutoresizingStyle = NSTableViewFirstColumnOnlyAutoresizingStyle;
    NSArray *columns = @[@[@"name", @"进程", @220], @[@"pid", @"PID", @64],
        @[@"cpu", @"CPU", @78], @[@"footprint", @"内存占用", @104],
        @[@"resident", @"驻留内存", @104], @[@"threads", @"线程", @60]];
    for (NSArray *spec in columns) {
        NSTableColumn *column = [[NSTableColumn alloc] initWithIdentifier:spec[0]];
        column.title = spec[1];
        column.width = [spec[2] doubleValue];
        column.minWidth = [spec[0] isEqual:@"name"] ? 160 : column.width;
        column.resizingMask = [spec[0] isEqual:@"name"] ? NSTableColumnAutoresizingMask : NSTableColumnNoResizing;
        [self.processTable addTableColumn:column];
    }
    NSScrollView *scroll = [NSScrollView new];
    scroll.hasVerticalScroller = YES;
    scroll.borderType = NSBezelBorder;
    scroll.documentView = self.processTable;
    [stack addArrangedSubview:scroll];
    [scroll.widthAnchor constraintEqualToAnchor:stack.widthAnchor].active = YES;
    [scroll.heightAnchor constraintGreaterThanOrEqualToConstant:150].active = YES;
    [scroll setContentHuggingPriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationVertical];
    self.coverage = [NSTextField wrappingLabelWithString:@""];
    self.coverage.font = [NSFont systemFontOfSize:11];
    self.coverage.textColor = NSColor.secondaryLabelColor;
    [stack addArrangedSubview:self.coverage];
    [self.coverage.widthAnchor constraintEqualToAnchor:stack.widthAnchor].active = YES;
    NSTextField *note = [NSTextField wrappingLabelWithString:
        @"每秒更新，含此 App 的子进程及可确认归属的 WebKit 辅助进程。CPU 100% = 一个核心。\n内存为各进程记账之和；FPS 为渲染提交速率，静止时可为 0，不含原生 WebView。"];
    note.font = [NSFont systemFontOfSize:11];
    note.textColor = NSColor.secondaryLabelColor;
    [stack addArrangedSubview:note];
    [note.widthAnchor constraintEqualToAnchor:stack.widthAnchor].active = YES;
    [note.bottomAnchor constraintEqualToAnchor:window.contentView.bottomAnchor constant:-16].active = YES;
    return self;
}
- (void)showForWindow:(NSWindow *)parent
{
    if (!self.timer) {
        [self.sampler reset];
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
    self.processes = [self.sampler sample];
    double cpu = 0;
    uint64_t footprint = 0, resident = 0;
    int threads = 0;
    NSUInteger cpuCount = 0, memoryCount = 0, threadCount = 0;
    for (HuskProcessMetrics *process in self.processes) {
        if (isfinite(process.cpuPercent)) { cpu += process.cpuPercent; cpuCount++; }
        if (process.usageAvailable) { footprint += process.footprint; resident += process.resident; memoryCount++; }
        if (process.threadsAvailable) { threads += process.threads; threadCount++; }
    }
    NSUInteger count = self.processes.count;
    BOOL discovered = self.sampler.discoveryComplete;
    NSString *cpuPrefix = cpuCount == count && discovered ? @"" : @"≥ ";
    NSString *memoryPrefix = memoryCount == count && discovered ? @"" : @"≥ ";
    NSString *threadPrefix = threadCount == count && discovered ? @"" : @"≥ ";
    self.values[@"cpu"].stringValue = cpuCount ? [NSString stringWithFormat:@"%@%.1f%%", cpuPrefix, cpu] : @"—";
    self.values[@"footprint"].stringValue = memoryCount ? [memoryPrefix stringByAppendingString:husk_performance_bytes(footprint)] : @"—";
    self.values[@"resident"].stringValue = memoryCount ? [memoryPrefix stringByAppendingString:husk_performance_bytes(resident)] : @"—";
    self.values[@"threads"].stringValue = threadCount ? [NSString stringWithFormat:@"%@%d", threadPrefix, threads] : @"—";
    self.values[@"processes"].stringValue = [NSString stringWithFormat:@"%@%lu", discovered ? @"" : @"≥ ", (unsigned long)count];
    self.coverage.stringValue = !discovered ? @"进程归属查询不完整，合计仅包含已确认并读到的部分（≥）。" :
        cpuCount < count || memoryCount < count || threadCount < count ?
        @"部分进程读数尚不可用（—），对应合计仅包含已读部分（≥）。" : @"已汇总下列全部进程。";
    [self.processTable reloadData];

    HuskRenderMetrics render = self.readRendering ? self.readRendering() : (HuskRenderMetrics){0};
    double fps = husk_performance_rate(&_frames, now, render.frames, render.running);
    self.values[@"fps"].stringValue = isfinite(fps) ? [NSString stringWithFormat:@"%.1f", fps] : @"—";
    self.values[@"frames"].stringValue = render.running ? [NSString stringWithFormat:@"%llu", (unsigned long long)render.frames] : @"—";
    self.values[@"size"].stringValue = render.pixels.width > 0 && render.pixels.height > 0 ?
        [NSString stringWithFormat:@"%.0f × %.0f px", render.pixels.width, render.pixels.height] : @"—";
}
- (NSInteger)numberOfRowsInTableView:(NSTableView *)tableView
{
    (void)tableView;
    return self.processes.count;
}
- (NSView *)tableView:(NSTableView *)tableView viewForTableColumn:(NSTableColumn *)column row:(NSInteger)row
{
    NSTextField *cell = [tableView makeViewWithIdentifier:column.identifier owner:self];
    if (!cell) {
        cell = [NSTextField labelWithString:@""];
        cell.identifier = column.identifier;
        cell.lineBreakMode = NSLineBreakByTruncatingTail;
        cell.font = [NSFont monospacedDigitSystemFontOfSize:12 weight:NSFontWeightRegular];
        cell.alignment = [column.identifier isEqual:@"name"] ? NSTextAlignmentLeft : NSTextAlignmentRight;
    }
    HuskProcessMetrics *process = self.processes[row];
    NSString *key = column.identifier;
    if ([key isEqual:@"name"]) cell.stringValue = process.pid == getpid() ? [process.name stringByAppendingString:@"（主进程）"] : process.name;
    else if ([key isEqual:@"pid"]) cell.stringValue = [NSString stringWithFormat:@"%d", process.pid];
    else if ([key isEqual:@"cpu"]) cell.stringValue = isfinite(process.cpuPercent) ? [NSString stringWithFormat:@"%.1f%%", process.cpuPercent] : @"—";
    else if ([key isEqual:@"footprint"]) cell.stringValue = process.usageAvailable ? husk_performance_bytes(process.footprint) : @"—";
    else if ([key isEqual:@"resident"]) cell.stringValue = process.usageAvailable ? husk_performance_bytes(process.resident) : @"—";
    else if ([key isEqual:@"threads"]) cell.stringValue = process.threadsAvailable ? [NSString stringWithFormat:@"%d", process.threads] : @"—";
    cell.toolTip = [NSString stringWithFormat:@"%@ · PID %d · 父进程 %d · 负责进程 %d", process.name, process.pid, process.parent, process.responsible];
    return cell;
}
- (void)windowWillClose:(NSNotification *)notification
{
    (void)notification;
    [self.timer invalidate];
    self.timer = nil;
}
- (void)dealloc { [_timer invalidate]; }
@end
