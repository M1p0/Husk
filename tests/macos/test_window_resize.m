/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Exercise the real host's AppKit geometry and mouse routing without a guest. */
#define main husk_app_host_main
#include "../../tools/macos/HuskAppHost.m"
#undef main
#include <assert.h>
#include <math.h>

static float touch_x, touch_y;
static int touches, native_clicks;
static int resized_width, resized_height;
static NSView *web_target;

@interface NativeTarget : NSView
@end
@implementation NativeTarget
- (BOOL)acceptsFirstMouse:(NSEvent *)event { (void)event; return YES; }
- (void)mouseDown:(NSEvent *)event { (void)event; native_clicks++; }
@end

static int32_t stub_start(husk_runtime_handle *handle, const husk_runtime_app_config_v1 *config,
                          char *error, size_t capacity)
{ (void)handle; (void)config; (void)error; (void)capacity; return 0; }
static int32_t stub_ended(void) { return 0; }
static void record_touch(int32_t phase, int32_t pointer, float x, float y)
{ (void)pointer; if (phase == 0) { touch_x = x; touch_y = y; touches++; } }
static int32_t web_hit(float x, float y)
{ return NSPointInRect(NSMakePoint(x, y), web_target.frame); }
static int32_t record_resize(int32_t width, int32_t height, char *error, size_t capacity)
{ (void)error; (void)capacity; resized_width = width; resized_height = height; return 0; }
static int32_t attach(void *screen, void *container, float scale, int32_t fresh,
                      char *error, size_t capacity)
{
    (void)screen; (void)scale; (void)fresh; (void)error; (void)capacity;
    web_target = [[NativeTarget alloc] initWithFrame:NSMakeRect(20, 300, 320, 80)];
    [(__bridge NSView *)container addSubview:web_target];
    return 0;
}

/* Calculate physical window positions independently of NSView conversions. */
static NSPoint window_point(NSSize size, CGFloat x, CGFloat y)
{
    return NSMakePoint(x, size.height - y);
}
static void click(NSPoint point)
{
    for (int up = 0; up < 2; up++) {
        NSEvent *event = [NSEvent mouseEventWithType:up ? NSEventTypeLeftMouseUp : NSEventTypeLeftMouseDown
            location:point modifierFlags:0 timestamp:NSProcessInfo.processInfo.systemUptime
            windowNumber:host.window.windowNumber context:nil eventNumber:0 clickCount:1 pressure:up ? 0 : 1];
        [host.window sendEvent:event];
    }
}

int main(void)
{
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
        static const husk_runtime_app_api_v1 stub = {
            .start=stub_start, .ended=stub_ended, .touch=record_touch, .attach_web=attach, .web_hit=web_hit, .resize=record_resize};
        app_api = &stub;
        app_config = @{@"width":@720, @"height":@1280, @"density":@2, @"display_name":@"Husk resize regression"};
        options = @{@"--no-activate":@YES};
        surface_scale = 2;
        host = [HuskApp new];
        [host applicationDidFinishLaunching:[NSNotification notificationWithName:NSApplicationDidFinishLaunchingNotification object:NSApp]];
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
        assert(running);
        assert(host.window.styleMask & NSWindowStyleMaskResizable);
        NSSize sizes[] = {{540, 960}, {800, 640}, {250, 700}, {180, 240}, {360, 640}};
        for (size_t i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
            [host.window setContentSize:sizes[i]];
            [host.window.contentView layoutSubtreeIfNeeded];
            assert(NSEqualSizes(host.screen.bounds.size, sizes[i]));
            assert(NSEqualSizes(host.web.bounds.size, sizes[i]));
            assert(NSEqualRects(host.screen.superview.frame, host.window.contentView.bounds));
            NSSize pixels = NSMakeSize(sizes[i].width * 2, sizes[i].height * 2);
            assert(CGSizeEqualToSize(((CAMetalLayer *)host.screen.layer).drawableSize, pixels));
            assert(resized_width == (int)pixels.width && resized_height == (int)pixels.height);
            web_target.frame = NSMakeRect(10, sizes[i].height / 2, sizes[i].width - 20, 60);
            int before = touches;
            click(window_point(sizes[i], sizes[i].width / 4, sizes[i].height / 4));
            assert(touches == before + 1);
            assert(fabs(touch_x - pixels.width / 4) < 0.1 && fabs(touch_y - pixels.height / 4) < 0.1);
            before = native_clicks;
            click(window_point(sizes[i], sizes[i].width / 2, sizes[i].height / 2 + 20));
            assert(native_clicks == before + 1);
            assert(touches == (int)i + 1);
            fprintf(stdout, "PASS %.0fx%.0f: drawable %.0fx%.0f, runtime resize, Android touch, native overlay\n",
                    sizes[i].width, sizes[i].height, pixels.width, pixels.height);
        }
        [host.timer invalidate];
        [host.window orderOut:nil];
    }
    return 0;
}
