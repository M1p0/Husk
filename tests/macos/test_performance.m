/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Use the actual titlebar action, metrics sampler and window lifecycle. */
#define main husk_app_host_main
#include "../../tools/macos/HuskAppHost.m"
#undef main
#include <assert.h>

static uint64_t rendered_frames;
static int terminations;
@interface PerformanceTestApplication : NSApplication
@end
@implementation PerformanceTestApplication
- (void)terminate:(id)sender { (void)sender; terminations++; }
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
static uint64_t stub_frames(void) { return rendered_frames; }

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
        assert(argc == 2);
        NSString *output = [NSString stringWithUTF8String:argv[1]];
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
            .ended=stub_ended, .frames=stub_frames, .resize=stub_resize};
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
        /* Burn one core using the main thread; actual CPU accounting must read
         * near 100%, independently of the machine's logical processor count. */
        [panel refresh];
        double until = NSProcessInfo.processInfo.systemUptime + .3;
        while (NSProcessInfo.processInfo.systemUptime < until) {}
        [panel refresh];
        assert(panel.values[@"cpu"].doubleValue > 60 && panel.values[@"cpu"].doubleValue < 150);
        fprintf(stdout, "PASS: live process sampling (one-core CPU %s, footprint %s, threads %s)\n",
            panel.values[@"cpu"].stringValue.UTF8String, panel.values[@"footprint"].stringValue.UTF8String,
            panel.values[@"threads"].stringValue.UTF8String);
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
        puts("PASS: closing metrics preserves app; reopening resets sampling; main close still terminates");
    }
    return 0;
}
