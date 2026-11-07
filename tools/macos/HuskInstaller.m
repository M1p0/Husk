/* SPDX-License-Identifier: GPL-2.0-or-later */
#import <AppKit/AppKit.h>
#import <CoreServices/CoreServices.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#include <errno.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>

/* Test outputs need not exist yet. Resolve their existing ancestors before
 * checking containment, including symlinks in a parent of a new directory. */
static NSString *testPath(NSString *path) {
    if (!path.isAbsolutePath) return nil;
    NSMutableArray<NSString *> *missing = [NSMutableArray new];
    NSString *prefix = path;
    for (;;) {
        char resolved[PATH_MAX];
        if (realpath(prefix.fileSystemRepresentation, resolved)) {
            NSString *result = @(resolved);
            for (NSString *part in missing.reverseObjectEnumerator) result = [result stringByAppendingPathComponent:part];
            return result.stringByStandardizingPath;
        }
        if (errno != ENOENT) return nil;
        struct stat info;
        if (lstat(prefix.fileSystemRepresentation, &info) == 0) return nil; /* dangling symlink */
        NSString *parent = prefix.stringByDeletingLastPathComponent;
        if ([parent isEqual:prefix]) return nil;
        [missing addObject:prefix.lastPathComponent];
        prefix = parent;
    }
}

static NSDictionary *readJSON(NSString *path) {
    NSData *data = [NSData dataWithContentsOfFile:path];
    id value = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] : nil;
    return [value isKindOfClass:NSDictionary.class] ? value : nil;
}
static void printJSON(id value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:NULL];
    if (data) { fwrite(data.bytes, 1, data.length, stdout); fputc('\n', stdout); fflush(stdout); }
}
static NSTextField *label(NSString *text, CGFloat size, BOOL bold) {
    NSTextField *field = [NSTextField wrappingLabelWithString:text];
    field.font = bold ? [NSFont boldSystemFontOfSize:size] : [NSFont systemFontOfSize:size];
    field.selectable = YES;
    return field;
}
static NSButton *button(NSString *title, id target, SEL action) {
    NSButton *value = [NSButton buttonWithTitle:title target:target action:action];
    value.bezelStyle = NSBezelStyleRounded;
    return value;
}
static NSStackView *stack(NSArray<NSView *> *views, NSUserInterfaceLayoutOrientation orientation) {
    NSStackView *value = [NSStackView stackViewWithViews:views];
    value.orientation = orientation; value.spacing = 12;
    value.alignment = orientation == NSUserInterfaceLayoutOrientationVertical ? NSLayoutAttributeLeading : NSLayoutAttributeCenterY;
    return value;
}
static BOOL apkType(UTType *type) { return [type.preferredFilenameExtension.lowercaseString isEqual:@"apk"]; }
@interface InstallerBackground : NSView @end
@implementation InstallerBackground
- (void)drawRect:(NSRect)dirtyRect { [NSColor.windowBackgroundColor setFill]; NSRectFill(dirtyRect); }
@end
static int writeIcon(NSString *path) {
    NSImage *image = [[NSImage alloc] initWithSize:NSMakeSize(1024, 1024)];
    [image lockFocus];
    [[NSColor colorWithSRGBRed:.23 green:.29 blue:.77 alpha:1] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:NSMakeRect(36, 36, 952, 952) xRadius:210 yRadius:210] fill];
    NSImage *symbol = [NSImage imageWithSystemSymbolName:@"shippingbox.fill" accessibilityDescription:nil];
    symbol = [symbol imageWithSymbolConfiguration:[NSImageSymbolConfiguration configurationWithHierarchicalColor:NSColor.whiteColor]];
    [NSGraphicsContext saveGraphicsState];
    [symbol drawInRect:NSMakeRect(232, 300, 560, 560)];
    [NSGraphicsContext restoreGraphicsState];
    [@"APK" drawInRect:NSMakeRect(0, 140, 1024, 130) withAttributes:@{
        NSFontAttributeName:[NSFont boldSystemFontOfSize:112], NSForegroundColorAttributeName:NSColor.whiteColor,
        NSParagraphStyleAttributeName:({ NSMutableParagraphStyle *p = [NSMutableParagraphStyle new]; p.alignment = NSTextAlignmentCenter; p; })}];
    [image unlockFocus];
    NSBitmapImageRep *rep = [NSBitmapImageRep imageRepWithData:image.TIFFRepresentation];
    return [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:path atomically:NO] ? 0 : 1;
}

@interface Installer : NSObject <NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate>
@property NSDictionary *config;
@property NSWindow *window;
@property NSTableView *table;
@property NSSearchField *search;
@property NSTextField *summary, *message, *nameLabel, *packageLabel, *versionLabel, *pathLabel;
@property NSImageView *icon;
@property NSButton *installButton, *cancelButton, *launchButton, *removeButton, *revealButton, *dataButton;
@property NSPopUpButton *graphicsChoice, *compatibilityChoice;
@property NSButton *importCompatibility;
@property NSStackView *configurationView;
@property NSDictionary *launchOptions;
@property NSString *optionsApplication;
@property NSProgressIndicator *progress;
@property NSArray<NSDictionary *> *apps, *visible;
@property NSMutableArray<NSString *> *queue;
@property NSDictionary *pending;
@property NSString *selectedPath, *testRoot, *testControl;
@property BOOL busy, noActivate;
@property NSUInteger controlOffset;
@property NSTimer *timer;
@property NSAlert *uninstallAlert;
@property pid_t lastLaunchedPID;
@end

@implementation Installer
- (NSDictionary *)selected {
    for (NSDictionary *row in self.apps) if ([row[@"application"] isEqual:self.selectedPath]) return row;
    return nil;
}
- (NSString *)resource:(NSString *)path { return [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:path]; }
- (void)showMessage:(NSString *)text { self.message.stringValue = text ?: @""; [self saveTestState]; }
- (void)backend:(NSArray<NSString *> *)args done:(void (^)(id, NSString *))done {
    self.busy = YES; [self.progress startAnimation:nil]; [self updateDetails];
    NSDictionary *cfg = self.config;
    NSString *engine = [self resource:@"engine/tools/macos/install_apk.py"];
    NSMutableDictionary *environment = NSProcessInfo.processInfo.environment.mutableCopy;
    environment[@"PYTHONDONTWRITEBYTECODE"] = @"1";
    if (![environment[@"HUSK_TASK_TMPDIR"] length] && ![environment[@"HUSK_TASK_ROOT"] length] &&
        [cfg[@"task_root"] isKindOfClass:NSString.class]) environment[@"HUSK_TASK_ROOT"] = cfg[@"task_root"];
    environment[@"HUSK_APPS_ROOTS"] = [[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:cfg[@"scan_roots"] ?: @[] options:0 error:NULL] encoding:NSUTF8StringEncoding];
    NSString *state = self.testRoot ?: [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/Husk/Installer"];
    environment[@"HUSK_APPS_REGISTRY"] = [state stringByAppendingPathComponent:@"registry.json"];
    if (self.testRoot) environment[@"HUSK_APPS_DATA_BASE"] = [self.testRoot stringByAppendingPathComponent:@"app-data"];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        @autoreleasepool {
            NSTask *task = [NSTask new]; NSPipe *pipe = [NSPipe pipe];
            task.executableURL = [NSURL fileURLWithPath:cfg[@"python"]];
            task.arguments = [@[@"-B", engine] arrayByAddingObjectsFromArray:args];
            task.environment = environment; task.standardOutput = pipe; task.standardError = pipe;
            NSError *error; id value; NSString *issue;
            if (![task launchAndReturnError:&error]) issue = error.localizedDescription;
            else {
                NSData *output = [pipe.fileHandleForReading readDataToEndOfFile]; [task waitUntilExit];
                NSString *text = [[NSString alloc] initWithData:output encoding:NSUTF8StringEncoding] ?: @"";
                if (task.terminationStatus) issue = text.length ? text : @"安装操作未完成，请重试。";
                else { value = [NSJSONSerialization JSONObjectWithData:output options:NSJSONReadingFragmentsAllowed error:&error]; if (!value) issue = error.localizedDescription; }
            }
            dispatch_async(dispatch_get_main_queue(), ^{
                self.busy = NO; [self.progress stopAnimation:nil];
                done(value, issue); [self updateDetails]; [self saveTestState];
            });
        }
    });
}
- (void)applicationDidFinishLaunching:(NSNotification *)note {
    (void)note;
    self.queue = self.queue ?: [NSMutableArray new];
    self.apps = @[]; self.visible = @[];
    self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1040, 850)
        styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
        backing:NSBackingStoreBuffered defer:NO];
    self.window.title = @"Husk APK 安装器"; self.window.minSize = NSMakeSize(900, 790); self.window.releasedWhenClosed = NO;
    self.window.contentView = [[InstallerBackground alloc] initWithFrame:self.window.contentView.bounds];
    NSTextField *title = label(@"APK 应用", 28, YES);
    self.summary = label(@"正在读取已安装应用…", 13, NO); self.summary.textColor = NSColor.secondaryLabelColor;
    NSButton *choose = button(@"安装 APK…", self, @selector(chooseAPK:));
    choose.keyEquivalent = @"o";
    NSButton *refresh = button(@"刷新", self, @selector(refresh:));
    NSButton *folder = button(@"添加应用目录…", self, @selector(addDirectory:));
    NSButton *association = button(@"设为 APK 默认安装器", self, @selector(setDefault:));
    self.search = [[NSSearchField alloc] initWithFrame:NSZeroRect]; self.search.placeholderString = @"搜索名称或包名"; self.search.delegate = self;
    [self.search.widthAnchor constraintEqualToConstant:220].active = YES;
    NSView *space = [NSView new]; [space setContentHuggingPriority:1 forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *toolbar = stack(@[choose, refresh, folder, space, self.search], NSUserInterfaceLayoutOrientationHorizontal);
    self.table = [NSTableView new]; self.table.rowHeight = 64; self.table.headerView = nil;
    self.table.usesAlternatingRowBackgroundColors = YES; self.table.delegate = self; self.table.dataSource = self;
    NSTableColumn *column = [[NSTableColumn alloc] initWithIdentifier:@"application"]; column.width = 560;
    [self.table addTableColumn:column]; self.table.target = self; self.table.doubleAction = @selector(launch:);
    NSScrollView *scroll = [NSScrollView new]; scroll.hasVerticalScroller = YES; scroll.borderType = NSBezelBorder; scroll.documentView = self.table;
    self.icon = [[NSImageView alloc] initWithFrame:NSMakeRect(0, 0, 64, 64)];
    [self.icon.widthAnchor constraintEqualToConstant:64].active = YES; [self.icon.heightAnchor constraintEqualToConstant:64].active = YES;
    self.nameLabel = label(@"选择一个应用", 21, YES);
    self.packageLabel = label(@"左侧显示所有已发现的 APK 应用。", 12, NO); self.packageLabel.textColor = NSColor.secondaryLabelColor;
    self.versionLabel = label(@"", 13, NO);
    self.pathLabel = label(@"", 12, NO); self.pathLabel.textColor = NSColor.secondaryLabelColor;
    self.installButton = button(@"安装", self, @selector(install:)); self.installButton.bezelColor = NSColor.controlAccentColor;
    self.cancelButton = button(@"取消", self, @selector(cancelInstall:));
    self.launchButton = button(@"打开应用", self, @selector(launch:));
    self.revealButton = button(@"在 Finder 中显示", self, @selector(reveal:));
    self.dataButton = button(@"打开数据目录", self, @selector(revealData:));
    self.removeButton = button(@"卸载…", self, @selector(remove:));
    self.graphicsChoice = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
    self.compatibilityChoice = [[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
    self.compatibilityChoice.target = self; self.compatibilityChoice.action = @selector(compatibilityChanged:);
    self.graphicsChoice.target = self; self.graphicsChoice.action = @selector(saveLaunchConfiguration:);
    self.importCompatibility = button(@"导入兼容包…", self, @selector(importCompatibilityPack:));
    self.configurationView = stack(@[label(@"图形后端", 11, NO), self.graphicsChoice,
        label(@"兼容补丁", 11, NO), self.compatibilityChoice], NSUserInterfaceLayoutOrientationVertical);
    self.configurationView.spacing = 4;
    for (NSView *view in @[self.graphicsChoice, self.compatibilityChoice])
        [view.widthAnchor constraintEqualToConstant:292].active = YES;
    NSTextField *hint = label(@"安装后的应用独立运行。退出安装器不会关闭它们。", 12, NO); hint.textColor = NSColor.secondaryLabelColor;
    NSView *verticalSpace = [NSView new]; [verticalSpace setContentHuggingPriority:1 forOrientation:NSLayoutConstraintOrientationVertical];
    NSStackView *detail = stack(@[self.icon, self.nameLabel, self.packageLabel, self.versionLabel, self.pathLabel,
        stack(@[self.installButton, self.cancelButton], NSUserInterfaceLayoutOrientationHorizontal), self.launchButton,
        self.configurationView, self.importCompatibility, self.revealButton, self.dataButton, self.removeButton, verticalSpace, hint, association], NSUserInterfaceLayoutOrientationVertical);
    detail.spacing = 7;
    [detail.widthAnchor constraintEqualToConstant:292].active = YES;
    for (NSTextField *field in @[self.nameLabel, self.packageLabel, self.versionLabel, self.pathLabel, hint]) {
        [field.widthAnchor constraintEqualToAnchor:detail.widthAnchor].active = YES;
        field.preferredMaxLayoutWidth = 292;
    }
    NSStackView *body = stack(@[scroll, detail], NSUserInterfaceLayoutOrientationHorizontal); body.alignment = NSLayoutAttributeTop;
    [scroll.heightAnchor constraintEqualToAnchor:body.heightAnchor].active = YES;
    [detail.heightAnchor constraintEqualToAnchor:body.heightAnchor].active = YES;
    self.progress = [[NSProgressIndicator alloc] initWithFrame:NSZeroRect]; self.progress.style = NSProgressIndicatorStyleSpinning;
    self.progress.displayedWhenStopped = NO; [self.progress.widthAnchor constraintEqualToConstant:18].active = YES;
    self.message = label(@"双击 APK，或点击“安装 APK…”开始。", 12, NO); self.message.textColor = NSColor.secondaryLabelColor;
    NSStackView *footer = stack(@[self.progress, self.message], NSUserInterfaceLayoutOrientationHorizontal);
    self.message.maximumNumberOfLines = 3;
    [self.message.widthAnchor constraintEqualToAnchor:footer.widthAnchor constant:-30].active = YES;
    NSStackView *content = stack(@[title, self.summary, toolbar, body, footer], NSUserInterfaceLayoutOrientationVertical);
    content.translatesAutoresizingMaskIntoConstraints = NO; [self.window.contentView addSubview:content];
    for (NSView *view in @[toolbar, body, footer]) [view.widthAnchor constraintEqualToAnchor:content.widthAnchor].active = YES;
    [NSLayoutConstraint activateConstraints:@[[content.leadingAnchor constraintEqualToAnchor:self.window.contentView.leadingAnchor constant:24],
        [content.trailingAnchor constraintEqualToAnchor:self.window.contentView.trailingAnchor constant:-24],
        [content.topAnchor constraintEqualToAnchor:self.window.contentView.topAnchor constant:24],
        [content.bottomAnchor constraintEqualToAnchor:self.window.contentView.bottomAnchor constant:-20]]];
    NSMenu *menu = [NSMenu new]; NSMenuItem *appMenu = [NSMenuItem new]; [menu addItem:appMenu]; appMenu.submenu = [NSMenu new];
    [appMenu.submenu addItemWithTitle:@"安装 APK…" action:@selector(chooseAPK:) keyEquivalent:@"o"].target = self;
    [appMenu.submenu addItem:NSMenuItem.separatorItem];
    [appMenu.submenu addItemWithTitle:@"退出 Husk APK 安装器" action:@selector(terminate:) keyEquivalent:@"q"];
    NSMenuItem *edit = [[NSMenuItem alloc] initWithTitle:@"编辑" action:NULL keyEquivalent:@""]; edit.submenu = [NSMenu new];
    for (NSArray *item in @[@[@"剪切", @"cut:", @"x"], @[@"复制", @"copy:", @"c"], @[@"粘贴", @"paste:", @"v"], @[@"全选", @"selectAll:", @"a"]])
        [edit.submenu addItemWithTitle:item[0] action:NSSelectorFromString(item[1]) keyEquivalent:item[2]];
    [menu addItem:edit]; NSApp.mainMenu = menu;
    [self.window center]; [self.window makeKeyAndOrderFront:nil]; if (!self.noActivate) [NSApp activateIgnoringOtherApps:YES];
    self.timer = [NSTimer scheduledTimerWithTimeInterval:.5 target:self selector:@selector(tick:) userInfo:nil repeats:YES];
    [self refresh:nil];
}
- (void)refresh:(id)sender {
    (void)sender; if (self.busy) return;
    [self backend:@[@"list", @"--all"] done:^(id value, NSString *error) {
        if (error) [self showMessage:error];
        else { self.apps = value; self.optionsApplication = nil; [self filter]; if (!self.selectedPath && self.apps.count) self.selectedPath = self.apps[0][@"application"]; }
        if (self.queue.count && !self.pending) [self previewNext];
    }];
}
- (void)filter {
    NSString *query = self.search.stringValue;
    self.visible = !query.length ? self.apps : [self.apps filteredArrayUsingPredicate:[NSPredicate predicateWithBlock:^BOOL(NSDictionary *row, NSDictionary *bindings) {
        (void)bindings; return [row[@"display_name"] localizedCaseInsensitiveContainsString:query] || [row[@"android_package"] localizedCaseInsensitiveContainsString:query];
    }]];
    self.summary.stringValue = [NSString stringWithFormat:@"%lu 个已安装应用%@", (unsigned long)self.apps.count, self.queue.count ? @" · 有 APK 等待安装" : @""];
    [self.table reloadData];
    NSUInteger index = [self.visible indexOfObjectPassingTest:^BOOL(NSDictionary *row, NSUInteger i, BOOL *stop) { (void)i; (void)stop; return [row[@"application"] isEqual:self.selectedPath]; }];
    if (index != NSNotFound) [self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:index] byExtendingSelection:NO];
    [self updateDetails];
}
- (void)controlTextDidChange:(NSNotification *)note { (void)note; [self filter]; }
- (NSInteger)numberOfRowsInTableView:(NSTableView *)table { (void)table; return self.visible.count; }
- (NSView *)tableView:(NSTableView *)table viewForTableColumn:(NSTableColumn *)column row:(NSInteger)index {
    (void)table; (void)column; NSDictionary *row = self.visible[index];
    NSView *cell = [NSView new]; NSImageView *image = [NSImageView new]; image.image = [[NSImage alloc] initWithContentsOfFile:row[@"icon"]];
    NSTextField *name = label(row[@"display_name"], 14, YES);
    NSTextField *description = label([NSString stringWithFormat:@"%@  ·  %@", row[@"android_version_name"], row[@"android_package"]], 11, NO); description.textColor = NSColor.secondaryLabelColor;
    for (NSView *view in @[image, name, description]) { view.translatesAutoresizingMaskIntoConstraints = NO; [cell addSubview:view]; }
    [NSLayoutConstraint activateConstraints:@[[image.leadingAnchor constraintEqualToAnchor:cell.leadingAnchor constant:12],
        [image.centerYAnchor constraintEqualToAnchor:cell.centerYAnchor], [image.widthAnchor constraintEqualToConstant:40], [image.heightAnchor constraintEqualToConstant:40],
        [name.leadingAnchor constraintEqualToAnchor:image.trailingAnchor constant:12], [name.topAnchor constraintEqualToAnchor:cell.topAnchor constant:12],
        [name.trailingAnchor constraintEqualToAnchor:cell.trailingAnchor constant:-12],
        [description.leadingAnchor constraintEqualToAnchor:name.leadingAnchor], [description.topAnchor constraintEqualToAnchor:name.bottomAnchor constant:3],
        [description.trailingAnchor constraintEqualToAnchor:name.trailingAnchor]]];
    return cell;
}
- (void)tableViewSelectionDidChange:(NSNotification *)note {
    (void)note;
    if (self.table.selectedRow >= 0 && (NSUInteger)self.table.selectedRow < self.visible.count) {
        self.selectedPath = self.visible[self.table.selectedRow][@"application"];
        if (!self.busy) self.pending = nil;
        [self updateDetails]; [self saveTestState];
    }
}
- (void)updateDetails {
    NSDictionary *row = self.pending ?: self.selected;
    BOOL pending = self.pending != nil, installed = !pending && row != nil;
    self.nameLabel.stringValue = row[pending ? @"label" : @"display_name"] ?: @"选择一个应用";
    self.packageLabel.stringValue = row[pending ? @"package" : @"android_package"] ?: @"左侧显示所有已发现的 APK 应用。";
    self.versionLabel.stringValue = row ? [NSString stringWithFormat:@"版本 %@", row[pending ? @"version_name" : @"android_version_name"]] : @"";
    self.pathLabel.stringValue = row[pending ? @"file" : @"application"] ?: @"";
    self.icon.image = installed ? [[NSImage alloc] initWithContentsOfFile:row[@"icon"]] : [[NSImage alloc] initWithContentsOfFile:[self resource:@"DefaultAPK.png"]];
    self.installButton.hidden = !pending; self.installButton.enabled = pending && !self.busy;
    self.cancelButton.hidden = !pending; self.cancelButton.enabled = pending && !self.busy;
    self.launchButton.hidden = !installed; self.launchButton.enabled = installed && !self.busy;
    self.removeButton.hidden = !installed; self.removeButton.enabled = installed && !self.busy;
    self.revealButton.hidden = !installed; self.dataButton.hidden = !installed;
    self.configurationView.hidden = !installed;
    self.importCompatibility.enabled = !self.busy;
    for (NSPopUpButton *choice in @[self.graphicsChoice, self.compatibilityChoice]) choice.enabled = installed && !self.busy && self.launchOptions != nil;
    if (installed && !self.busy && ![self.optionsApplication isEqual:row[@"application"]]) [self loadLaunchOptions:row];
    [self saveTestState];
}
- (void)selectChoice:(NSPopUpButton *)popup identifier:(NSString *)identifier {
    for (NSMenuItem *item in popup.itemArray) if ([item.representedObject isEqual:identifier]) { [popup selectItem:item]; return; }
}
- (void)fillChoice:(NSPopUpButton *)popup items:(NSArray<NSDictionary *> *)items selected:(NSString *)selected {
    [popup removeAllItems];
    for (NSDictionary *item in items) { [popup addItemWithTitle:item[@"name"]]; popup.lastItem.representedObject = item[@"id"]; }
    [self selectChoice:popup identifier:selected];
}
- (void)fillGraphics:(NSArray<NSString *> *)values selected:(NSString *)selected {
    NSDictionary *titles = @{@"auto":@"自动", @"opengles":@"OpenGL ES", @"vulkan":@"Vulkan"};
    NSMutableArray *items = [NSMutableArray new];
    for (NSString *value in values) if (titles[value]) [items addObject:@{@"id":value, @"name":titles[value]}];
    [self fillChoice:self.graphicsChoice items:items selected:selected];
}
- (void)loadLaunchOptions:(NSDictionary *)row {
    NSString *application = row[@"application"];
    self.optionsApplication = application; self.launchOptions = nil;
    [self backend:@[@"launch-options", @"--application", application] done:^(id value, NSString *error) {
        if (![self.selectedPath isEqual:application] || self.pending) { self.optionsApplication = nil; return; }
        if (error) { [self showMessage:error]; return; }
        self.launchOptions = value;
        NSDictionary *selected = value[@"selected"];
        [self fillChoice:self.compatibilityChoice items:value[@"packs"] selected:selected[@"compatibility"] ?: @"none"];
        NSArray *graphics = @[@"auto", @"opengles"];
        for (NSDictionary *pack in value[@"packs"]) if ([pack[@"id"] isEqual:self.compatibilityChoice.selectedItem.representedObject]) graphics = pack[@"graphics"];
        [self fillGraphics:graphics selected:selected[@"graphics"] ?: @"auto"];
        if (selected[@"configuration_error"]) [self showMessage:@"保存的运行配置已损坏，请重新选择运行选项。"];
    }];
}
- (void)compatibilityChanged:(id)sender {
    (void)sender;
    for (NSDictionary *pack in self.launchOptions[@"packs"]) if ([pack[@"id"] isEqual:self.compatibilityChoice.selectedItem.representedObject]) {
        [self fillGraphics:pack[@"graphics"] selected:pack[@"default_graphics"]];
        break;
    }
    [self saveLaunchConfiguration:sender];
}
- (void)saveLaunchConfiguration:(id)sender {
    (void)sender; NSDictionary *row = self.selected;
    if (!row || self.pending || self.busy || !self.launchOptions) return;
    [self backend:@[@"configure", row[@"application"], @"--runtime", @"default",
        @"--graphics", self.graphicsChoice.selectedItem.representedObject ?: @"auto", @"--compatibility", self.compatibilityChoice.selectedItem.representedObject ?: @"none",
        @"--host", [self resource:@"HuskAppHost"]] done:^(id value, NSString *error) {
        (void)value;
        if (error) { self.optionsApplication = nil; [self showMessage:error]; [self loadLaunchOptions:row]; return; }
        self.optionsApplication = nil;
        [self showMessage:@"运行配置已自动保存，下次打开此应用时生效。"]; [self refresh:nil];
    }];
}
- (void)importCompatibilityPath:(NSString *)path {
    if (self.busy) return;
    NSString *store = self.selected[@"runtime_store"] ?: self.config[@"runtime_store"];
    [self backend:@[@"import-compatibility", path, @"--store", store] done:^(id value, NSString *error) {
        (void)value;
        if (error) { [self showMessage:error]; return; }
        self.optionsApplication = nil;
        [self showMessage:@"兼容包已导入。为应用选择兼容补丁后即自动保存启用。"]; [self updateDetails];
    }];
}
- (void)importCompatibilityPack:(id)sender {
    (void)sender; if (self.busy) return;
    NSOpenPanel *panel = [NSOpenPanel openPanel]; panel.allowedContentTypes = @[UTTypeJSON];
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response) {
        if (response == NSModalResponseOK) [self importCompatibilityPath:panel.URL.path];
    }];
}
- (void)enqueue:(NSArray<NSString *> *)files {
    self.queue = self.queue ?: [NSMutableArray new];
    for (NSString *file in files) {
        NSString *path = file.stringByStandardizingPath;
        if (![path.pathExtension.lowercaseString isEqual:@"apk"]) { [self showMessage:@"请选择 .apk 文件。"]; continue; }
        if (![self.queue containsObject:path] && ![self.pending[@"file"] isEqual:path]) [self.queue addObject:path];
    }
    if (self.window) { [self.window makeKeyAndOrderFront:nil]; if (!self.noActivate) [NSApp activateIgnoringOtherApps:YES]; }
    if (self.window && !self.busy && !self.pending) [self previewNext];
}
- (void)application:(NSApplication *)application openFiles:(NSArray<NSString *> *)files {
    [self enqueue:files]; [application replyToOpenOrPrint:NSApplicationDelegateReplySuccess];
}
- (void)previewNext {
    if (self.busy || self.pending || !self.queue.count) return;
    NSString *path = self.queue.firstObject; [self.queue removeObjectAtIndex:0];
    [self showMessage:@"正在读取 APK 信息…"];
    [self backend:@[@"inspect", path] done:^(id value, NSString *error) {
        if (error) { [self showMessage:error]; [self previewNext]; return; }
        for (NSDictionary *row in self.apps) if ([row[@"android_package"] isEqual:value[@"package"]]) {
            self.selectedPath = row[@"application"]; [self filter];
            [self showMessage:@"此 APK 已安装。可直接打开；更换 APK 前请先卸载，默认保留数据。"];
            [self previewNext]; return;
        }
        self.pending = value;
        [self showMessage:[NSString stringWithFormat:@"安装到 %@%@", self.config[@"applications"], self.queue.count ? @"；安装后继续下一个 APK。" : @""]];
    }];
}
- (void)chooseAPK:(id)sender {
    (void)sender; if (self.busy) return;
    NSOpenPanel *panel = [NSOpenPanel openPanel]; panel.allowedContentTypes = @[[UTType typeWithFilenameExtension:@"apk"] ?: UTTypeData]; panel.allowsMultipleSelection = YES;
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response) {
        if (response == NSModalResponseOK) { self.pending = nil; [self enqueue:[panel.URLs valueForKey:@"path"]]; }
    }];
}
- (void)install:(id)sender {
    (void)sender; if (!self.pending || self.busy) return;
    NSMutableArray *args = [@[@"install", self.pending[@"file"], @"--applications", self.config[@"applications"], @"--runtime-store", self.config[@"runtime_store"], @"--host", [self resource:@"HuskAppHost"]] mutableCopy];
    if (![self.pending[@"has_raster_icon"] boolValue]) [args addObjectsFromArray:@[@"--icon", [self resource:@"DefaultAPK.png"]]];
    [self showMessage:@"正在安装，请稍候…"];
    [self backend:args done:^(id value, NSString *error) {
        if (error) { [self showMessage:error]; return; }
        self.selectedPath = value[@"application"]; self.pending = nil;
        [self showMessage:@"安装完成。可点击“打开应用”，或在 Finder 中双击启动。"];
        [self refresh:nil];
    }];
}
- (void)cancelInstall:(id)sender {
    (void)sender; if (self.busy) return;
    self.pending = nil; [self showMessage:@"已取消安装。"]; [self updateDetails]; [self previewNext];
}
- (void)launch:(id)sender {
    (void)sender; NSDictionary *row = self.selected; if (!row || self.pending || self.busy) return;
    NSWorkspaceOpenConfiguration *configuration = [NSWorkspaceOpenConfiguration configuration];
    if (self.testRoot) {
        configuration.arguments = @[@"--state-root", row[@"data_directory"], @"--quit-after", @"90", @"--no-activate"];
        configuration.environment = NSProcessInfo.processInfo.environment;
        configuration.activates = NO;
    }
    [NSWorkspace.sharedWorkspace openApplicationAtURL:[NSURL fileURLWithPath:row[@"application"]] configuration:configuration completionHandler:^(NSRunningApplication *app, NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{ self.lastLaunchedPID = app.processIdentifier; [self showMessage:error ? error.localizedDescription : @"应用已打开，退出安装器后仍可继续运行。"]; });
    }];
}
- (void)reveal:(id)sender { (void)sender; if (self.selected) [NSWorkspace.sharedWorkspace activateFileViewerSelectingURLs:@[[NSURL fileURLWithPath:self.selected[@"application"]]]]; }
- (void)revealData:(id)sender {
    (void)sender; if (!self.selected) return;
    NSString *path = self.selected[@"data_directory"];
    if ([NSFileManager.defaultManager fileExistsAtPath:path]) [NSWorkspace.sharedWorkspace openURL:[NSURL fileURLWithPath:path]];
    else [self showMessage:@"此应用还没有生成数据，首次启动后即可打开。"];
}
- (void)uninstallSelected:(BOOL)removeData {
    NSDictionary *row = self.selected; if (!row || self.busy) return;
    if (self.testRoot && ![row[@"application"] hasPrefix:[NSProcessInfo.processInfo.environment[@"HUSK_TASK_TMPDIR"] stringByAppendingString:@"/"]]) {
        [self showMessage:@"测试模式不能卸载任务目录之外的应用。"]; return;
    }
    for (NSRunningApplication *app in [NSRunningApplication runningApplicationsWithBundleIdentifier:row[@"bundle_id"]]) {
        if (!app.terminated) { [self showMessage:@"应用正在运行，请先完全退出后再卸载。"]; return; }
    }
    NSMutableArray *args = [@[@"uninstall", row[@"application"]] mutableCopy]; if (removeData) [args addObject:@"--remove-data"];
    [self backend:args done:^(id value, NSString *error) {
        (void)value; if (error) [self showMessage:error];
        else { self.selectedPath = nil; [self showMessage:removeData ? @"应用及其数据已删除。" : @"应用已卸载，数据已保留，重新安装后可继续使用。"]; [self refresh:nil]; }
    }];
}
- (void)remove:(id)sender {
    (void)sender; if (!self.selected || self.busy) return;
    NSAlert *alert = [NSAlert new]; alert.messageText = [NSString stringWithFormat:@"卸载“%@”？", self.selected[@"display_name"]];
    self.uninstallAlert = alert;
    alert.informativeText = @"将删除此 Mac 应用。默认保留数据，重新安装同一应用后可继续使用。原始 APK 文件不会被删除。";
    [alert addButtonWithTitle:@"卸载"]; [alert addButtonWithTitle:@"取消"];
    NSButton *check = [NSButton checkboxWithTitle:@"同时删除应用数据（不可恢复）" target:nil action:NULL]; alert.accessoryView = check;
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) { self.uninstallAlert = nil; if (result == NSAlertFirstButtonReturn) [self uninstallSelected:check.state == NSControlStateValueOn]; [self saveTestState]; }];
}
- (void)addDirectory:(id)sender {
    (void)sender; if (self.busy) return;
    NSOpenPanel *panel = [NSOpenPanel openPanel]; panel.canChooseFiles = NO; panel.canChooseDirectories = YES;
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) {
        if (result == NSModalResponseOK) [self backend:@[@"track", panel.URL.path] done:^(id value, NSString *error) {
            (void)value; if (error) [self showMessage:error]; else [self refresh:nil];
        }];
    }];
}
- (void)setDefault:(id)sender {
    (void)sender;
    NSString *sample = self.pending[@"file"];
    if (!sample) {
        for (NSDictionary *row in self.apps) { sample = [row[@"application"] stringByAppendingPathComponent:[@"Contents/Resources/package" stringByAppendingPathComponent:row[@"apk_file"]]]; if (sample) break; }
    }
    if (!sample) { [self showMessage:@"先选择一个 APK，再点击设置默认安装器。"]; return; }
    UTType *type; [[NSURL fileURLWithPath:sample] getResourceValue:&type forKey:NSURLContentTypeKey error:NULL];
    if (!apkType(type)) { [self showMessage:@"APK 文件类型尚未正确注册，请重新打开安装器后重试。"]; return; }
    [NSWorkspace.sharedWorkspace setDefaultApplicationAtURL:NSBundle.mainBundle.bundleURL toOpenContentTypeOfFileAtURL:[NSURL fileURLWithPath:sample] completionHandler:^(NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{ [self showMessage:error ? error.localizedDescription : @"已设置。以后双击 APK 会打开此安装器。"]; });
    }];
}
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app { (void)app; return !self.busy; }
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)app {
    (void)app;
    if (self.busy) { [self.window makeKeyAndOrderFront:nil]; [self showMessage:@"当前操作完成后即可退出安装器。"]; return NSTerminateCancel; }
    return NSTerminateNow;
}
- (void)applicationDidBecomeActive:(NSNotification *)note { (void)note; if (self.window && !self.busy && !self.pending) [self refresh:nil]; }
- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)visible { (void)sender; (void)visible; [self.window makeKeyAndOrderFront:nil]; return YES; }
- (void)saveTestState {
    if (!self.testRoot || !self.window) return;
    NSDictionary *state = @{@"busy":@(self.busy), @"apps":self.apps ?: @[], @"pending":self.pending ?: NSNull.null,
        @"selected":self.selected ?: NSNull.null, @"message":self.message.stringValue ?: @"", @"pid":@(getpid()), @"queue_count":@(self.queue.count),
        @"uninstall_prompt":@(self.uninstallAlert != nil), @"last_launched_pid":@(self.lastLaunchedPID), @"visible_count":@(self.visible.count),
        @"detail_frame":NSStringFromRect(self.nameLabel.frame), @"install_frame":NSStringFromRect(self.installButton.frame),
        @"detail_title":self.nameLabel.stringValue ?: @"", @"window_number":@(self.window.windowNumber)};
    NSMutableDictionary *all = state.mutableCopy;
    all[@"launch_options"] = self.launchOptions ?: @{};
    all[@"runtime_editable"] = @NO;
    all[@"runtime_selection"] = @"default";
    all[@"graphics_selection"] = self.graphicsChoice.selectedItem.representedObject ?: @"auto";
    all[@"compatibility_selection"] = self.compatibilityChoice.selectedItem.representedObject ?: @"none";
    [[NSJSONSerialization dataWithJSONObject:all options:NSJSONWritingPrettyPrinted error:NULL] writeToFile:[self.testRoot stringByAppendingPathComponent:@"state.json"] atomically:NO];
}
- (void)tick:(NSTimer *)timer {
    (void)timer; if (!self.testControl) return;
    NSData *data = [NSData dataWithContentsOfFile:self.testControl]; if (data.length <= self.controlOffset) return;
    NSData *tail = [data subdataWithRange:NSMakeRange(self.controlOffset, data.length-self.controlOffset)];
    NSString *text = [[NSString alloc] initWithData:tail encoding:NSUTF8StringEncoding];
    NSRange last = [text rangeOfString:@"\n" options:NSBackwardsSearch]; if (last.location == NSNotFound) return;
    NSString *complete = [text substringToIndex:last.location]; self.controlOffset += [[text substringToIndex:last.location+1] lengthOfBytesUsingEncoding:NSUTF8StringEncoding];
    for (NSString *line in [complete componentsSeparatedByString:@"\n"]) {
        NSDictionary *command = [NSJSONSerialization JSONObjectWithData:[line dataUsingEncoding:NSUTF8StringEncoding] options:0 error:NULL];
        if (![command isKindOfClass:NSDictionary.class]) continue;
        NSString *action = command[@"action"];
        if ([action isEqual:@"quit"]) [NSApp terminate:nil];
        else if ([action isEqual:@"install"]) [self.installButton performClick:nil];
        else if ([action isEqual:@"launch"]) [self.launchButton performClick:nil];
        else if ([action isEqual:@"uninstall"]) [self.removeButton performClick:nil];
        else if ([action isEqual:@"confirm-uninstall"]) {
            ((NSButton *)self.uninstallAlert.accessoryView).state = [command[@"remove_data"] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
            [self.uninstallAlert.buttons.firstObject performClick:nil];
        }
        else if ([action isEqual:@"refresh"]) [self refresh:nil];
        else if ([action isEqual:@"import-compatibility"]) [self importCompatibilityPath:command[@"manifest"]];
        else if ([action isEqual:@"configure"]) {
            NSPopUpButton *choice = command[@"graphics"] ? self.graphicsChoice : self.compatibilityChoice;
            NSString *value = command[@"graphics"] ?: command[@"compatibility"] ?: @"none";
            if (!self.busy && choice.enabled) {
                [self selectChoice:choice identifier:value];
                [NSApp sendAction:choice.action to:choice.target from:choice];
            }
        }
        else if ([action isEqual:@"search"]) { self.search.stringValue = command[@"query"] ?: @""; [self filter]; }
        else if ([action isEqual:@"select"]) { self.pending = nil; self.selectedPath = command[@"application"]; [self filter]; }
        else if ([action isEqual:@"render"]) {
            NSString *path = [self.testRoot stringByAppendingPathComponent:@"window.png"];
            [self.window.contentView layoutSubtreeIfNeeded];
            NSBitmapImageRep *rep = [self.window.contentView bitmapImageRepForCachingDisplayInRect:self.window.contentView.bounds];
            [self.window.contentView cacheDisplayInRect:self.window.contentView.bounds toBitmapImageRep:rep];
            [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:path atomically:NO];
        }
        [self saveTestState];
    }
}
@end

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc == 3 && !strcmp(argv[1], "--make-icon")) return writeIcon(@(argv[2]));
        if (argc == 3 && (!strcmp(argv[1], "--register-default") || !strcmp(argv[1], "--query-default"))) {
            BOOL registering = !strcmp(argv[1], "--register-default");
            NSURL *bundle = NSBundle.mainBundle.bundleURL;
            OSStatus registered = registering ? LSRegisterURL((__bridge CFURLRef)bundle, true) : 0;
            UTType *type; [[NSURL fileURLWithPath:@(argv[2])] getResourceValue:&type forKey:NSURLContentTypeKey error:NULL];
            OSStatus status = registering ? (apkType(type) ? LSSetDefaultRoleHandlerForContentType((__bridge CFStringRef)type.identifier, kLSRolesAll, (__bridge CFStringRef)NSBundle.mainBundle.bundleIdentifier) : -1) : 0;
            NSURL *handler = [NSWorkspace.sharedWorkspace URLForApplicationToOpenURL:[NSURL fileURLWithPath:@(argv[2])]];
            printJSON(@{@"registered":@(registered), @"status":@(status), @"type":type.identifier ?: @"", @"handler":handler.path ?: @""});
            return registered || status || ![handler.path isEqual:bundle.path] ? 1 : 0;
        }
        Installer *delegate = [Installer new];
        NSString *task = testPath(NSProcessInfo.processInfo.environment[@"HUSK_TASK_TMPDIR"]);
        for (int i = 1; i < argc; i++) {
            NSString *key = @(argv[i]);
            if ([key isEqual:@"--no-activate"]) { delegate.noActivate = YES; continue; }
            if (([key isEqual:@"--test-root"] || [key isEqual:@"--test-control"]) && i + 1 < argc) {
                NSString *path = testPath(@(argv[++i]));
                if (!task.isAbsolutePath || [task isEqual:@"/"] || !path.isAbsolutePath ||
                    ![path hasPrefix:[task stringByAppendingString:@"/"]]) return 2;
                if ([key isEqual:@"--test-root"]) delegate.testRoot = path; else delegate.testControl = path;
            } else return 2;
        }
        if ((delegate.testRoot != nil) != (delegate.testControl != nil)) return 2;
        if (delegate.testRoot) [NSFileManager.defaultManager createDirectoryAtPath:delegate.testRoot withIntermediateDirectories:YES attributes:nil error:NULL];
        delegate.config = readJSON([NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"installer.json"]);
        if (!delegate.config) return 1;
        [NSApplication sharedApplication]; [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular]; NSApp.delegate = delegate; [NSApp run];
    }
    return 0;
}
