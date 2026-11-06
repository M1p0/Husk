/* SPDX-License-Identifier: GPL-2.0-or-later */
/* One AppKit process per installed APK. Only the versioned public ABI is used. */
#import <AppKit/AppKit.h>
#import <QuartzCore/CAMetalLayer.h>
#import <QuartzCore/CATransaction.h>
#import <Metal/Metal.h>
#include <dlfcn.h>
#include <pthread.h>
#include <mach/mach.h>
#include <math.h>
#include "../../src/runtime-api/husk-runtime-app-v1.h"
#include "runtime-bundle.h"
#include "HuskPerformance.h"

static const husk_runtime_api_v1 *runtime_api;
static const husk_runtime_app_api_v1 *app_api;
static husk_runtime_handle *runtime_handle;
static NSString *runtime_root, *state_root, *generation, *app_id, *frame_root;
static NSDictionary *options, *app_config;
static int runtime_lease = -1, instance_lease = -1;
static BOOL stopping, running;
static BOOL owns_instance, app_api_valid;
static BOOL background_activity;
static CGFloat surface_scale;
static NSInteger window_number;

static NSString *str(const char *s) { return s ? [NSString stringWithUTF8String:s] : @""; }
static void log_line(void *userdata, const char *line)
{
    (void)userdata;
    fprintf(stderr, "%s\n", line);
    fflush(stderr);
}
static void json_file(NSString *path, NSDictionary *value)
{
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingPrettyPrinted error:NULL];
    [data writeToFile:path options:NSDataWritingAtomic error:NULL];
}
static NSDictionary *read_json(NSString *path)
{
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (!data || data.length > 8 * 1024 * 1024) return nil;
    id value = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
    return [value isKindOfClass:NSDictionary.class] ? value : nil;
}
static BOOL mkdir_private(NSString *path)
{
    return [[NSFileManager defaultManager] createDirectoryAtPath:path withIntermediateDirectories:YES
        attributes:@{NSFilePosixPermissions:@0700} error:NULL];
}
static void status(NSString *error)
{
    if (!owns_instance) return;
    task_vm_info_data_t vm = {0};
    mach_msg_type_number_t n = TASK_VM_INFO_COUNT;
    task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vm, &n);
    NSMutableDictionary *value = [@{@"schema":@1, @"pid":@(getpid()), @"app_id":app_id ?: @"",
        @"generation":generation ?: @"", @"runtime":runtime_root ?: @"",
        @"state":@(app_api_valid ? app_api->state() : HUSK_RUNTIME_APP_IDLE),
        @"window_number":@(window_number),
        @"frames":@(app_api_valid ? app_api->frames() : 0), @"footprint_bytes":@(vm.phys_footprint),
        @"background_execution":@"macos", @"app_nap_prevention":@(background_activity),
        @"hard_memory_limit":@"unsupported"} mutableCopy];
    if (error) value[@"error"] = error;
    json_file([state_root stringByAppendingPathComponent:@"status.json"], value);
}
static void fail(NSString *message)
{
    fprintf(stderr, "Husk App: %s\n", message.UTF8String);
    status(message);
    dispatch_async(dispatch_get_main_queue(), ^{
        if (!options[@"--quit-after"]) {
            NSAlert *alert = [NSAlert new];
            alert.messageText = @"无法启动此应用";
            alert.informativeText = message;
            [alert runModal];
        }
        fflush(NULL);
        _exit(1);
    });
}
static int startup_error(NSString *message, int code)
{
    fprintf(stderr, "Husk App: %s\n", message.UTF8String);
    if (state_root) status(message);
    if (!options[@"--quit-after"]) {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        [NSApp activateIgnoringOtherApps:YES];
        NSAlert *alert = [NSAlert new];
        alert.messageText = @"无法打开此应用";
        alert.informativeText = message;
        [alert runModal];
    }
    return code;
}

@interface HuskScreen : NSView <NSTextInputClient>
@property NSMutableAttributedString *marked;
@property BOOL dragging;
@property NSPoint scrollPosition;
@property BOOL scrolling;
@end
@implementation HuskScreen
- (BOOL)isFlipped { return YES; }
- (BOOL)acceptsFirstResponder { return YES; }
- (BOOL)acceptsFirstMouse:(NSEvent *)event { (void)event; return YES; }
- (BOOL)isOpaque { return NO; }
- (CALayer *)makeBackingLayer { return [CAMetalLayer layer]; }
- (NSView *)hitTest:(NSPoint)point
{
    NSPoint local = [self convertPoint:point fromView:self.superview];
    if (app_api && app_api->web_hit(local.x, local.y)) return nil;
    return [super hitTest:point];
}
- (NSPoint)point:(NSEvent *)event { return [self convertPoint:event.locationInWindow fromView:nil]; }
- (void)send:(int)phase point:(NSPoint)p
{
    if (running && !stopping) app_api->touch(phase, 0, p.x * surface_scale, p.y * surface_scale);
}
- (void)mouseDown:(NSEvent *)event
{
    [self.window makeFirstResponder:self];
    self.dragging = YES; [self send:0 point:[self point:event]];
}
- (void)mouseDragged:(NSEvent *)event { if (self.dragging) [self send:1 point:[self point:event]]; }
- (void)mouseUp:(NSEvent *)event { if (self.dragging) [self send:2 point:[self point:event]]; self.dragging = NO; }
- (void)rightMouseDown:(NSEvent *)event { (void)event; if (running) app_api->back(); }
- (void)scrollWheel:(NSEvent *)event
{
    if (!running || stopping) return;
    NSPoint p = [self point:event];
    if (!self.scrolling) { self.scrollPosition = p; self.scrolling = YES; [self send:0 point:p]; }
    NSPoint next = self.scrollPosition;
    CGFloat factor = event.hasPreciseScrollingDeltas ? 1 : 10;
    next.x += event.scrollingDeltaX * factor;
    next.y += event.scrollingDeltaY * factor;
    next.x = MAX(1, MIN(self.bounds.size.width - 1, next.x));
    next.y = MAX(1, MIN(self.bounds.size.height - 1, next.y));
    self.scrollPosition = next; [self send:1 point:next];
    [NSObject cancelPreviousPerformRequestsWithTarget:self selector:@selector(finishScroll) object:nil];
    [self performSelector:@selector(finishScroll) withObject:nil afterDelay:0.12];
}
- (void)finishScroll { if (self.scrolling) [self send:2 point:self.scrollPosition]; self.scrolling = NO; }
- (void)keyDown:(NSEvent *)event
{
    if (!running) return;
    if (event.keyCode == 53) { app_api->back(); return; }
    [self interpretKeyEvents:@[event]];
}
- (BOOL)performKeyEquivalent:(NSEvent *)event
{
    if (!(event.modifierFlags & NSEventModifierFlagCommand) || !running) return [super performKeyEquivalent:event];
    NSString *key = event.charactersIgnoringModifiers.lowercaseString;
    if ([key isEqual:@"v"]) { NSString *value = [NSPasteboard.generalPasteboard stringForType:NSPasteboardTypeString]; if (value) app_api->text(value.UTF8String); return YES; }
    if ([key isEqual:@"a"]) { app_api->text_command(HUSK_RUNTIME_TEXT_SELECT_ALL); return YES; }
    if ([key isEqual:@"c"]) { app_api->text_command(HUSK_RUNTIME_TEXT_COPY); return YES; }
    if ([key isEqual:@"x"]) { app_api->text_command(HUSK_RUNTIME_TEXT_CUT); return YES; }
    if ([key isEqual:@"["]) { app_api->back(); return YES; }
    return [super performKeyEquivalent:event];
}
- (void)insertText:(id)value replacementRange:(NSRange)range
{
    (void)range;
    NSString *text = [value isKindOfClass:NSAttributedString.class] ? [value string] : value;
    self.marked = nil;
    if (running && text.length) app_api->text(text.UTF8String);
}
- (void)doCommandBySelector:(SEL)selector
{
    if (!running) return;
    if (selector == @selector(deleteBackward:)) app_api->text_delete();
    else if (selector == @selector(insertNewline:)) app_api->text_action();
    else {
        int code = selector == @selector(moveLeft:) ? 21 : selector == @selector(moveRight:) ? 22 :
            selector == @selector(moveUp:) ? 19 : selector == @selector(moveDown:) ? 20 :
            selector == @selector(insertTab:) ? 61 : selector == @selector(deleteForward:) ? 112 : 0;
        if (code) { app_api->key(code, 1); app_api->key(code, 0); }
    }
}
- (BOOL)hasMarkedText { return self.marked.length > 0; }
- (NSRange)markedRange { return self.hasMarkedText ? NSMakeRange(0, self.marked.length) : NSMakeRange(NSNotFound, 0); }
- (NSRange)selectedRange { return NSMakeRange(self.marked.length, 0); }
- (void)setMarkedText:(id)value selectedRange:(NSRange)selection replacementRange:(NSRange)range
{
    (void)selection; (void)range;
    self.marked = [value isKindOfClass:NSAttributedString.class] ? [value mutableCopy] : [[NSMutableAttributedString alloc] initWithString:value];
}
- (void)unmarkText { self.marked = nil; }
- (NSArray *)validAttributesForMarkedText { return @[]; }
- (NSAttributedString *)attributedSubstringForProposedRange:(NSRange)range actualRange:(NSRangePointer)actual
{ (void)range; if (actual) *actual = NSMakeRange(NSNotFound, 0); return nil; }
- (NSUInteger)characterIndexForPoint:(NSPoint)point { (void)point; return NSNotFound; }
- (NSRect)firstRectForCharacterRange:(NSRange)range actualRange:(NSRangePointer)actual
{
    if (actual) *actual = range;
    return [self.window convertRectToScreen:[self convertRect:NSMakeRect(12, 30, 1, 20) toView:nil]];
}
@end

@interface HuskContainer : NSView
@end
@implementation HuskContainer
- (BOOL)isFlipped { return YES; }
@end

/* Metal, native WebViews and input all use the window's current point bounds.
 * The runtime receives a new pixel size at the original Android density. */
@interface HuskViewport : HuskContainer
@property HuskContainer *canvas;
@property (copy) void (^onResize)(void);
- (instancetype)initWithSurfaceSize:(NSSize)size;
@end
@implementation HuskViewport
- (instancetype)initWithSurfaceSize:(NSSize)size
{
    self = [super initWithFrame:NSMakeRect(0, 0, size.width, size.height)];
    if (self) {
        self.canvas = [[HuskContainer alloc] initWithFrame:self.bounds];
        self.canvas.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
        [self addSubview:self.canvas];
    }
    return self;
}
- (void)resizeSubviewsWithOldSize:(NSSize)oldSize
{
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    [super resizeSubviewsWithOldSize:oldSize];
    if (self.onResize) self.onResize();
    [CATransaction commit];
}
@end

@interface HuskApp : NSObject <NSApplicationDelegate, NSWindowDelegate>
@property NSWindow *window;
@property HuskScreen *screen;
@property HuskContainer *web;
@property NSTimer *timer;
@property unsigned long long controlOffset;
@property NSString *controlRemainder;
@property id executionActivity;
@property NSSize runtimeSize;
@property HuskPerformance *performance;
- (void)resizeSurface;
- (void)updatePerformanceTitlebar;
- (void)showPerformance:(id)sender;
- (void)back:(id)sender;
- (void)capture:(NSString *)path;
- (void)control:(NSDictionary *)command;
@end
static HuskApp *host;

static void keyboard(void *u, int32_t show, int32_t type, int32_t ime)
{
    (void)u; (void)type; (void)ime;
    dispatch_async(dispatch_get_main_queue(), ^{ if (show) [host.window makeFirstResponder:host.screen]; });
}
static void set_clipboard(void *u, const char *text)
{
    (void)u; NSString *value = str(text);
    dispatch_async(dispatch_get_main_queue(), ^{ [NSPasteboard.generalPasteboard clearContents]; [NSPasteboard.generalPasteboard setString:value forType:NSPasteboardTypeString]; });
}
static size_t get_clipboard(void *u, char *buffer, size_t capacity)
{
    (void)u; __block NSString *value;
    void (^read)(void) = ^{ value = [NSPasteboard.generalPasteboard stringForType:NSPasteboardTypeString]; };
    if ([NSThread isMainThread]) read(); else dispatch_sync(dispatch_get_main_queue(), read);
    if (!value) return 0;
    size_t needed = strlen(value.UTF8String) + 1;
    if (buffer && capacity >= needed) memcpy(buffer, value.UTF8String, needed);
    return needed;
}
static void open_url(void *u, const char *url)
{
    (void)u; NSURL *value = [NSURL URLWithString:str(url)];
    if (![@[@"https", @"http", @"mailto"] containsObject:value.scheme.lowercaseString]) return;
    dispatch_async(dispatch_get_main_queue(), ^{ [NSWorkspace.sharedWorkspace openURL:value]; });
}
static void share_text(void *u, const char *text)
{
    (void)u; NSString *value = str(text);
    dispatch_async(dispatch_get_main_queue(), ^{ [[[NSSharingServicePicker alloc] initWithItems:@[value]] showRelativeToRect:host.screen.bounds ofView:host.screen preferredEdge:NSMaxYEdge]; });
}
static void orientation(void *u, int32_t value) { (void)u; fprintf(stderr, "host: requested Android orientation %d\n", value); }
static void vibrate(void *u, int32_t value) { (void)u; (void)value; }
static void pick_documents(void *u, const char *mime, int32_t multiple)
{
    (void)u; (void)mime;
    dispatch_async(dispatch_get_main_queue(), ^{
        NSOpenPanel *panel = [NSOpenPanel openPanel];
        panel.canChooseDirectories = NO; panel.canChooseFiles = YES; panel.allowsMultipleSelection = multiple != 0;
        [panel beginSheetModalForWindow:host.window completionHandler:^(NSModalResponse response) {
            NSMutableArray<NSString *> *picked = [NSMutableArray array];
            if (response == NSModalResponseOK) {
                NSString *imports = [state_root stringByAppendingPathComponent:@"data/imports"];
                mkdir_private(imports);
                for (NSURL *url in panel.URLs) {
                    NSString *destination = [imports stringByAppendingPathComponent:[NSString stringWithFormat:@"%@-%@", NSUUID.UUID.UUIDString, url.lastPathComponent]];
                    if ([NSFileManager.defaultManager copyItemAtPath:url.path toPath:destination error:NULL]) [picked addObject:destination];
                }
            }
            const char **paths = calloc(MAX(picked.count, 1), sizeof(char *));
            for (NSUInteger i = 0; i < picked.count; i++) paths[i] = picked[i].fileSystemRepresentation;
            if (!stopping) app_api->documents_picked(paths, (int32_t)picked.count);
            free(paths);
        }];
    });
}

static void *start_app(void *unused)
{
    (void)unused;
    @autoreleasepool {
        char error[1024] = {0};
        NSString *package = [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"package"];
        NSString *apk = [package stringByAppendingPathComponent:app_config[@"apk_file"]];
        NSString *graphics = [runtime_root stringByAppendingPathComponent:@"resources/angle"];
        husk_runtime_app_config_v1 config = {.struct_size=sizeof(config), .abi_version=1,
            .apk_path=apk.fileSystemRepresentation,
            .angle_egl=[graphics stringByAppendingPathComponent:@"libEGL.dylib"].fileSystemRepresentation,
            .angle_gles=[graphics stringByAppendingPathComponent:@"libGLESv2.dylib"].fileSystemRepresentation,
            .width=[app_config[@"width"] intValue], .height=[app_config[@"height"] intValue],
            .density=[app_config[@"density"] floatValue], .metal_layer=(__bridge void *)host.screen.layer,
            .frame_dir=frame_root.fileSystemRepresentation, .frame_every=-1,
            .night_mode=[NSApp.effectiveAppearance.name containsString:@"Dark"],
            .show_keyboard=keyboard, .set_clipboard=set_clipboard, .get_clipboard=get_clipboard,
            .open_url=open_url, .share=share_text, .set_orientation=orientation,
            .vibrate=vibrate, .pick_documents=pick_documents};
        int32_t result = app_api->start(runtime_handle, &config, error, sizeof(error));
        NSString *failureMessage = str(error);
        dispatch_async(dispatch_get_main_queue(), ^{
            if (result) { fail(failureMessage); return; }
            running = YES;
            [host resizeSurface];
            host.window.title = app_config[@"display_name"];
            [host updatePerformanceTitlebar];
            status(nil);
        });
    }
    return NULL;
}

@implementation HuskApp
- (void)updatePerformanceTitlebar
{
    if (!self.window.titlebarAccessoryViewControllers.count) return;
    CGFloat titleWidth = [self.window.title sizeWithAttributes:@{NSFontAttributeName:[NSFont titleBarFontOfSize:0]}].width;
    /* AppKit can draw its title underneath a right accessory in narrow windows. */
    self.window.titleVisibility = self.window.frame.size.width < titleWidth + 170 ? NSWindowTitleHidden : NSWindowTitleVisible;
}
- (void)showPerformance:(id)sender
{
    (void)sender;
    if (stopping) return;
    if (!self.performance) {
        self.performance = [[HuskPerformance alloc] initWithName:app_config[@"display_name"]];
        __weak HuskApp *weakSelf = self;
        self.performance.readRendering = ^{
            BOOL active = running && !stopping;
            NSSize pixels = ((CAMetalLayer *)weakSelf.screen.layer).drawableSize;
            return (HuskRenderMetrics){active ? app_api->frames() : 0, pixels, active};
        };
    }
    [self.performance showForWindow:self.window];
}
- (void)resizeSurface
{
    [self updatePerformanceTitlebar];
    NSSize points = self.screen.bounds.size;
    NSSize pixels = NSMakeSize(round(points.width * surface_scale), round(points.height * surface_scale));
    if (pixels.width <= 0 || pixels.height <= 0) return;
    ((CAMetalLayer *)self.screen.layer).drawableSize = pixels;
    if (!running || stopping || NSEqualSizes(pixels, self.runtimeSize)) return;
    char error[512] = {0};
    if (app_api->resize((int32_t)pixels.width, (int32_t)pixels.height, error, sizeof(error))) {
        fail(str(error)); return;
    }
    self.runtimeSize = pixels;
}
- (void)applicationDidFinishLaunching:(NSNotification *)notification
{
    (void)notification;
    CGFloat width = [app_config[@"width"] doubleValue] / surface_scale;
    CGFloat height = [app_config[@"height"] doubleValue] / surface_scale;
    self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, width, height)
        styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
        backing:NSBackingStoreBuffered defer:NO];
    self.window.contentMinSize = NSMakeSize(MIN(width, 180), MIN(height, 240));
    self.window.contentMaxSize = NSMakeSize(16384 / surface_scale, 16384 / surface_scale);
    self.runtimeSize = NSMakeSize([app_config[@"width"] intValue], [app_config[@"height"] intValue]);
    self.window.title = [app_config[@"display_name"] stringByAppendingString:@" · 启动中"];
    self.window.delegate = self;
    self.window.releasedWhenClosed = NO;
    self.window.backgroundColor = NSColor.blackColor;
    NSRect bounds = NSMakeRect(0, 0, width, height);
    HuskViewport *root = [[HuskViewport alloc] initWithSurfaceSize:bounds.size];
    self.web = [[HuskContainer alloc] initWithFrame:bounds];
    self.screen = [[HuskScreen alloc] initWithFrame:bounds];
    self.web.autoresizingMask = self.screen.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.screen.wantsLayer = YES;
    CAMetalLayer *metal = (CAMetalLayer *)self.screen.layer;
    metal.device = MTLCreateSystemDefaultDevice(); metal.pixelFormat = MTLPixelFormatBGRA8Unorm;
    metal.framebufferOnly = NO; metal.contentsScale = surface_scale;
    metal.drawableSize = CGSizeMake([app_config[@"width"] intValue], [app_config[@"height"] intValue]);
    [root.canvas addSubview:self.web]; [root.canvas addSubview:self.screen];
    __weak HuskApp *weakSelf = self;
    root.onResize = ^{ [weakSelf resizeSurface]; };
    self.window.contentView = root;
    NSButton *performance = [NSButton buttonWithTitle:@"性能" target:self action:@selector(showPerformance:)];
    performance.bezelStyle = NSBezelStyleTexturedRounded;
    performance.controlSize = NSControlSizeSmall;
    performance.font = [NSFont systemFontOfSize:NSFont.smallSystemFontSize];
    performance.image = [NSImage imageWithSystemSymbolName:@"waveform.path.ecg" accessibilityDescription:nil];
    performance.imagePosition = NSImageLeft;
    performance.toolTip = @"查看此 App 的实时性能指标";
    [performance setAccessibilityLabel:@"显示性能指标"];
    performance.frame = NSMakeRect(0, 0, 64, 22);
    performance.autoresizingMask = NSViewMinYMargin | NSViewMaxYMargin;
    NSTitlebarAccessoryViewController *accessory = [NSTitlebarAccessoryViewController new];
    accessory.layoutAttribute = NSLayoutAttributeRight;
    accessory.view = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 72, 22)];
    [accessory.view addSubview:performance];
    [self.window addTitlebarAccessoryViewController:accessory];
    [self updatePerformanceTitlebar];
    char error[512] = {0};
    if (app_api->attach_web((__bridge void *)self.screen, (__bridge void *)self.web, surface_scale, 1, error, sizeof(error))) { fail(str(error)); return; }
    [self.window center]; [self.window makeKeyAndOrderFront:nil];
    window_number = self.window.windowNumber;
    [self.window makeFirstResponder:self.screen];
    if (!options[@"--no-activate"]) [NSApp activateIgnoringOtherApps:YES];
    self.executionActivity = [NSProcessInfo.processInfo beginActivityWithOptions:NSActivityUserInitiatedAllowingIdleSystemSleep
        reason:app_config[@"display_name"]];
    background_activity = self.executionActivity != nil;
    self.timer = [NSTimer scheduledTimerWithTimeInterval:0.25 target:self selector:@selector(tick:) userInfo:nil repeats:YES];
    pthread_attr_t attr;
    if (pthread_attr_init(&attr)) { fail(@"无法配置 Java 工作线程"); return; }
    if (pthread_attr_setstacksize(&attr, HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES)) {
        pthread_attr_destroy(&attr); fail(@"无法设置 32 MiB Java 工作线程栈"); return;
    }
    pthread_t thread;
    int rc = pthread_create(&thread, &attr, start_app, NULL);
    pthread_attr_destroy(&attr);
    if (rc) { fail(@"无法创建 Java 工作线程"); return; }
    pthread_detach(thread);
    if (options[@"--quit-after"]) dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)([options[@"--quit-after"] doubleValue] * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{ [NSApp terminate:nil]; });
}
- (void)tick:(NSTimer *)timer
{
    (void)timer;
    status(nil);
    if (running && app_api->ended() && !stopping) { [NSApp terminate:nil]; return; }
    NSString *path = options[@"--control"];
    if (!path) return;
    NSFileHandle *file = [NSFileHandle fileHandleForReadingAtPath:path];
    if (!file) return;
    @try {
        [file seekToFileOffset:self.controlOffset];
        NSData *data = [file readDataUpToLength:64 * 1024 error:NULL];
        self.controlOffset += data.length;
        NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        NSString *all = [(self.controlRemainder ?: @"") stringByAppendingString:text ?: @""];
        NSArray *lines = [all componentsSeparatedByString:@"\n"];
        self.controlRemainder = lines.lastObject;
        for (NSUInteger i = 0; i + 1 < lines.count; i++) {
            NSData *line = [lines[i] dataUsingEncoding:NSUTF8StringEncoding];
            NSDictionary *command = [NSJSONSerialization JSONObjectWithData:line options:0 error:NULL];
            if ([command isKindOfClass:NSDictionary.class]) [self control:command];
        }
    } @catch (NSException *exception) { fprintf(stderr, "control: %s\n", exception.reason.UTF8String); }
    [file closeFile];
}
- (void)control:(NSDictionary *)command
{
    NSString *kind = command[@"action"];
    if ([kind isEqual:@"quit"]) { [NSApp terminate:nil]; return; }
    if ([kind isEqual:@"shot"]) { [self capture:command[@"path"]]; return; }
    if ([kind isEqual:@"performance"]) {
        if (command[@"visible"] && ![command[@"visible"] boolValue]) [self.performance close];
        else [self showPerformance:nil];
        return;
    }
    if ([kind isEqual:@"resize"]) {
        double width = [command[@"width"] doubleValue], height = [command[@"height"] doubleValue];
        NSSize minimum = self.window.contentMinSize, maximum = self.window.contentMaxSize;
        if (width >= minimum.width && height >= minimum.height && width <= maximum.width && height <= maximum.height)
            [self.window setContentSize:NSMakeSize(width, height)];
        return;
    }
    if (!running || stopping) return;
    fprintf(stderr, "host control: %s\n", kind.UTF8String);
    if ([kind isEqual:@"native_click"]) {
        NSPoint local = NSMakePoint([command[@"x"] doubleValue] / surface_scale,
                                   [command[@"y"] doubleValue] / surface_scale);
        NSPoint location = [self.screen convertPoint:local toView:nil];
        [self.window makeKeyAndOrderFront:nil]; [NSApp activateIgnoringOtherApps:YES];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_MSEC * 150), dispatch_get_main_queue(), ^{
            if (stopping) return;
            NSEvent *event = [NSEvent mouseEventWithType:NSEventTypeLeftMouseDown location:location modifierFlags:0
                timestamp:NSProcessInfo.processInfo.systemUptime windowNumber:self.window.windowNumber context:nil eventNumber:0 clickCount:1 pressure:1];
            [self.window sendEvent:event];
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_MSEC * 60), dispatch_get_main_queue(), ^{
                if (stopping) return;
                NSEvent *up = [NSEvent mouseEventWithType:NSEventTypeLeftMouseUp location:location modifierFlags:0
                    timestamp:NSProcessInfo.processInfo.systemUptime windowNumber:self.window.windowNumber context:nil eventNumber:0 clickCount:1 pressure:0];
                [self.window sendEvent:up];
            });
        });
    }
    else if ([kind isEqual:@"native_text"]) {
        id responder = self.window.firstResponder;
        if ([responder respondsToSelector:@selector(insertText:replacementRange:)])
            [(id<NSTextInputClient>)responder insertText:command[@"text"] replacementRange:NSMakeRange(NSNotFound, 0)];
    }
    else if ([kind isEqual:@"native_submit"]) [self.window.firstResponder doCommandBySelector:@selector(insertNewline:)];
    else if ([kind isEqual:@"select_all"]) app_api->text_command(HUSK_RUNTIME_TEXT_SELECT_ALL);
    else if ([kind isEqual:@"text"]) app_api->text([command[@"text"] UTF8String]);
    else if ([kind isEqual:@"delete"]) app_api->text_delete();
    else if ([kind isEqual:@"submit"]) app_api->text_action();
    else if ([kind isEqual:@"back"]) app_api->back();
    else if ([kind isEqual:@"pause"]) app_api->pause([command[@"paused"] boolValue]);
    else if ([kind isEqual:@"tap"] || [kind isEqual:@"swipe"]) {
        float x = [command[@"x"] floatValue], y = [command[@"y"] floatValue];
        app_api->touch(0, 0, x, y);
        float x2 = command[@"x2"] ? [command[@"x2"] floatValue] : x;
        float y2 = command[@"y2"] ? [command[@"y2"] floatValue] : y;
        double duration = command[@"seconds"] ? [command[@"seconds"] doubleValue] : 0.08;
        duration = MAX(0.03, MIN(duration, 5));
        int steps = [kind isEqual:@"swipe"] ? MAX(2, (int)(duration * 60)) : 1;
        for (int i = 1; i <= steps; i++) {
            int step = i;
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, duration * i / steps * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
                if (running && !stopping) app_api->touch(step == steps ? 2 : 1, 0, x + (x2 - x) * step / steps, y + (y2 - y) * step / steps);
            });
        }
    }
}
- (void)capture:(NSString *)path
{
    if (!path.isAbsolutePath) return;
    NSImage *image = [[NSImage alloc] initWithContentsOfFile:[frame_root stringByAppendingPathComponent:@"latest.bmp"]];
    if (!image) return;
    NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithData:image.TIFFRepresentation];
    [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:path atomically:YES];
}
- (void)back:(id)sender { (void)sender; if (running) app_api->back(); }
- (void)windowWillClose:(NSNotification *)notification
{ if (notification.object == self.window) [NSApp terminate:nil]; }
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender { (void)sender; return YES; }
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender
{
    (void)sender;
    if (stopping) return NSTerminateCancel;
    stopping = YES; [self.timer invalidate];
    [self.performance close];
    [self.window orderOut:nil];
    if (self.executionActivity) { [NSProcessInfo.processInfo endActivity:self.executionActivity]; self.executionActivity = nil; }
    background_activity = NO;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        char error[512] = {0};
        int result = app_api ? app_api->stop(error, sizeof(error)) : 0;
        if (result) fprintf(stderr, "host: stop returned %d: %s; terminating own process\n", result, error);
        status(result ? str(error) : nil);
        /* Process exit is the final lifetime boundary, including a failed
         * guest drain. Keep the regular main run loop alive for WebKit's
         * synchronous cleanup; do not enter AppKit's pending-termination loop.
         * The OS closes our windows, mappings and leases. Never execute guest
         * destructors or unload a live image during host exit. */
        fflush(NULL);
        _exit(0);
    });
    return NSTerminateCancel;
}
@end

int main(int argc, char **argv)
{
    @autoreleasepool {
        NSMutableDictionary *args = [NSMutableDictionary dictionary];
        NSSet *keys = [NSSet setWithArray:@[@"--runtime", @"--state-root", @"--control", @"--frame-dir", @"--quit-after"]];
        for (int i = 1; i < argc; i++) {
            NSString *key = str(argv[i]);
            if ([key isEqual:@"--no-activate"]) { args[key] = @YES; continue; }
            if (![keys containsObject:key] || i + 1 >= argc || args[key]) { fprintf(stderr, "invalid launcher argument\n"); return 2; }
            args[key] = str(argv[++i]);
        }
        options = args;
        app_config = read_json([NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"husk-app.json"]);
        app_id = NSBundle.mainBundle.bundleIdentifier;
        if (![app_config[@"schema"] isEqual:@1] || ![app_config[@"bundle_id"] isEqual:app_id] ||
            ![app_config[@"apk_file"] isKindOfClass:NSString.class] || !safe_relative(app_config[@"apk_file"])) {
            return startup_error(@"应用安装信息缺失或已损坏，请重新安装此应用。", 2);
        }
        generation = NSUUID.UUID.UUIDString;
        state_root = args[@"--state-root"] ?: [[NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/Husk/Apps"] stringByAppendingPathComponent:app_id];
        if (!state_root.isAbsolutePath || !mkdir_private(state_root)) return startup_error(@"无法创建此应用的数据目录，请检查目录权限和可用磁盘空间。", 2);
        state_root = canonical(state_root);
        for (NSString *name in @[@"data", @"cache", @"config", @"logs"]) if (!mkdir_private([state_root stringByAppendingPathComponent:name])) return startup_error(@"无法创建应用的数据或日志目录。", 2);
        instance_lease = open([state_root stringByAppendingPathComponent:@"instance.lock"].fileSystemRepresentation, O_CREAT|O_RDWR|O_NOFOLLOW|O_CLOEXEC, 0600);
        if (instance_lease < 0 || flock(instance_lease, LOCK_EX | LOCK_NB)) return startup_error(@"此应用已经在运行。请先关闭现有窗口，再重新打开。", 2);
        owns_instance = YES;
        NSString *log = [[state_root stringByAppendingPathComponent:@"logs"] stringByAppendingPathComponent:[generation stringByAppendingString:@".log"]];
        int fd = open(log.fileSystemRepresentation, O_CREAT|O_APPEND|O_WRONLY|O_CLOEXEC, 0600);
        if (fd >= 0) { dup2(fd, STDERR_FILENO); dup2(fd, STDOUT_FILENO); close(fd); }
        fprintf(stderr, "host: app=%s pid=%d generation=%s\n", app_id.UTF8String, getpid(), generation.UTF8String);
        frame_root = args[@"--frame-dir"];
        if (frame_root && (!frame_root.isAbsolutePath || !mkdir_private(frame_root))) return startup_error(@"无法创建指定的测试截图目录。", 2);
        NSString *store = canonical(app_config[@"runtime_store"]);
        NSDictionary *selection = store ? read_json([store stringByAppendingPathComponent:@"default.json"]) : nil;
        NSString *version = selection[@"build_id"];
        runtime_root = args[@"--runtime"] ? canonical(args[@"--runtime"]) :
            ([version isKindOfClass:NSString.class] && safe_relative(version) && ![version containsString:@"/"] ? canonical([store stringByAppendingPathComponent:version]) : nil);
        if (!runtime_root) return startup_error(@"未找到可用的 Husk 运行库。请先构建并选择兼容版本，再重新打开此应用。", 2);
        NSString *lease_dir = [runtime_root.stringByDeletingLastPathComponent stringByAppendingPathComponent:@".leases"];
        NSString *lease = [lease_dir stringByAppendingPathComponent:[runtime_root.lastPathComponent stringByAppendingString:@".lock"]];
        runtime_lease = directory(lease_dir) ? open(lease.fileSystemRepresentation, O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK) : -1;
        if (runtime_lease < 0 || !same_lock(runtime_lease, lease) || flock(runtime_lease, LOCK_SH|LOCK_NB) || !same_lock(runtime_lease, lease)) return startup_error(@"当前运行库正在维护或版本文件不完整，请稍后重试或重新选择运行库。", 2);
        NSString *error;
        NSDictionary *manifest = validate_bundle(runtime_root, &error);
        if (!manifest || ![manifest[@"profile"] isEqual:@"macos-app"] || !same_lock(runtime_lease, lease)) return startup_error([NSString stringWithFormat:@"运行库校验失败，请重新选择完整的 macOS 应用运行库。\n%@", error ?: @"版本类型或文件持有不匹配"], 2);
        NSString *package = [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"package"];
        NSString *apk = [package stringByAppendingPathComponent:app_config[@"apk_file"]];
        if (!safe_file(package, app_config[@"apk_file"]) || ![digest(apk) isEqual:app_config[@"apk_sha256"]]) return startup_error(@"应用内的 APK 已损坏或被替换，请从原始 APK 重新安装。", 2);
        NSDictionary *settings = read_json([store stringByAppendingPathComponent:@"settings.json"]);
        NSNumber *jit = settings[@"jit_megabytes"] ?: @512;
        if (![jit isKindOfClass:NSNumber.class] || jit.doubleValue != jit.unsignedLongLongValue || jit.intValue < 2 || jit.intValue > 1024) return startup_error(@"手动 JIT 设置无效，请填写 2–1024 MiB 之间的整数后重新打开应用。", 2);
        void *library = dlopen([runtime_root stringByAppendingPathComponent:@"lib/libHuskRuntime.dylib"].fileSystemRepresentation, RTLD_NOW|RTLD_LOCAL);
        if (!library) return startup_error([NSString stringWithFormat:@"无法加载当前运行库，请重新选择兼容版本。\n%@", str(dlerror())], 3);
        const husk_runtime_api_v1 *(*getter)(uint32_t,uint32_t) = dlsym(library, "husk_runtime_get_api");
        const husk_runtime_app_api_v1 *(*app_getter)(uint32_t,uint32_t) = dlsym(library, "husk_runtime_get_app_api");
        runtime_api = getter ? getter(1, sizeof(*runtime_api)) : NULL;
        app_api = app_getter ? app_getter(1, sizeof(*app_api)) : NULL;
        if (!runtime_api || !app_api || runtime_api->struct_size < sizeof(*runtime_api) || app_api->struct_size < sizeof(*app_api) ||
            runtime_api->abi_version != 1 || app_api->abi_version != 1 ||
            !runtime_api->get_info || !runtime_api->configure || !runtime_api->get_context || !runtime_api->close ||
            !app_api->start || !app_api->stop || !app_api->state || !app_api->attach_web || !app_api->web_hit ||
            !app_api->touch || !app_api->key || !app_api->back || !app_api->text || !app_api->text_delete ||
            !app_api->text_action || !app_api->text_command || !app_api->keyboard_closed || !app_api->pause || !app_api->set_insets ||
            !app_api->documents_picked || !app_api->frames || !app_api->ended || !app_api->resize) return startup_error(@"当前运行库缺少此应用外壳需要的接口（包括窗口尺寸更新），请先选择新版兼容运行库。", 3);
        const husk_runtime_info_v1 *info = runtime_api->get_info();
        if (!info || info->struct_size < sizeof(*info) || info->abi_version != 1 ||
            ![str(info->host_target) isEqual:@"macos-arm64"] ||
            ![str(info->build_id) isEqual:manifest[@"build_id"]] || !(info->capabilities & HUSK_RUNTIME_CAP_APK_LAUNCH)) return startup_error(@"运行库的平台或版本信息不匹配，无法启动此应用。", 3);
        app_api_valid = YES;
        husk_runtime_context_v1 context = {.struct_size=sizeof(context), .abi_version=1,
            .app_id=app_id.UTF8String, .generation_id=generation.UTF8String, .runtime_root=runtime_root.fileSystemRepresentation,
            .package_root=package.fileSystemRepresentation,
            .data_root=[state_root stringByAppendingPathComponent:@"data"].fileSystemRepresentation,
            .cache_root=[state_root stringByAppendingPathComponent:@"cache"].fileSystemRepresentation,
            .config_root=[state_root stringByAppendingPathComponent:@"config"].fileSystemRepresentation,
            .manual_jit_bytes=jit.unsignedLongLongValue << 20,
            .java_worker_stack_bytes=HUSK_RUNTIME_JAVA_WORKER_STACK_BYTES, .log=log_line};
        char message[1024] = {0};
        if (runtime_api->configure(&context, &runtime_handle, message, sizeof(message))) return startup_error([NSString stringWithFormat:@"无法配置此应用的数据和运行库。\n%@", str(message)], 3);
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        surface_scale = [app_config[@"density"] doubleValue];
        if (surface_scale <= 0 || surface_scale > 4) return startup_error(@"应用显示尺寸设置无效，请重新安装此应用。", 2);
        NSMenu *menu = [NSMenu new];
        NSMenuItem *app_item = [NSMenuItem new]; [menu addItem:app_item];
        NSMenu *app_menu = [NSMenu new];
        [app_menu addItemWithTitle:@"返回" action:@selector(back:) keyEquivalent:@"["];
        [app_menu addItemWithTitle:@"显示性能指标" action:@selector(showPerformance:) keyEquivalent:@"i"];
        [app_menu addItem:NSMenuItem.separatorItem];
        [app_menu addItemWithTitle:@"退出" action:@selector(terminate:) keyEquivalent:@"q"];
        app_item.submenu = app_menu; NSApp.mainMenu = menu;
        host = [HuskApp new]; NSApp.delegate = host;
        [NSApp run];
        return 0;
    }
}
