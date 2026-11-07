/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Per-app launch data; no game names, offsets or SDK behavior. */
#pragma once

static bool profile_identifier(NSString *value)
{
    if (![value isKindOfClass:NSString.class] || !value.length || value.length > 96) return false;
    return [value rangeOfString:@"^[A-Za-z0-9][A-Za-z0-9._-]*$" options:NSRegularExpressionSearch].location != NSNotFound;
}
static bool profile_environment_key(NSString *key)
{
    if (![key isKindOfClass:NSString.class]) return false;
    return [key isEqual:@"TL_ORIGINAL_CODE_VIEW"] ||
        [key rangeOfString:@"^HUSK_PATCH_[A-Z0-9_]+$" options:NSRegularExpressionSearch].location != NSNotFound;
}
static bool profile_artifact(NSDictionary *artifact)
{
    if (![artifact isKindOfClass:NSDictionary.class]) return false;
    NSString *path = artifact[@"path"], *hash = artifact[@"sha256"];
    if (![path isKindOfClass:NSString.class] || !path.isAbsolutePath ||
        ![hash isKindOfClass:NSString.class] || hash.length != 64) return false;
    if ([path containsString:@":"] || [path rangeOfString:[NSString stringWithFormat:@"%C", (unichar)0]].location != NSNotFound) return false;
    return safe_file(path.stringByDeletingLastPathComponent, path.lastPathComponent) && [digest(path) isEqual:hash];
}
static bool profile_requirements(id value)
{
    if (![value isKindOfClass:NSDictionary.class]) return false;
    NSNumber *abi=value[@"patch_abi"], *caps=value[@"capabilities"];
    return [abi isEqual:@1] && [caps isKindOfClass:NSNumber.class] &&
        caps.doubleValue==caps.unsignedLongLongValue && caps.longLongValue>=0;
}

static NSDictionary *launch_profile(NSString *state, NSDictionary *installed, NSString **error)
{
    NSString *path = [state stringByAppendingPathComponent:@"config/launch.json"];
    if (![NSFileManager.defaultManager fileExistsAtPath:path]) return nil;
    if (!safe_file(path.stringByDeletingLastPathComponent, path.lastPathComponent)) { *error = @"应用运行配置文件不安全。"; return nil; }
    NSDictionary *profile = read_json(path);
    NSString *runtime = profile[@"runtime"], *graphics = profile[@"graphics"], *pack = profile[@"compatibility"];
    if (![profile[@"schema"] isEqual:@1] || ![runtime isEqual:@"default"] || !profile_identifier(pack) ||
        ![@[@"auto", @"opengles", @"vulkan"] containsObject:graphics] || ![profile[@"apk_sha256"] isEqual:installed[@"apk_sha256"]]) {
        *error = @"应用运行配置与当前 APK 不匹配，请在安装器中重新保存。"; return nil;
    }
    NSDictionary *execution = profile[@"execution"];
    if ([pack isEqual:@"none"]) {
        if (execution || [graphics isEqual:@"vulkan"]) { *error = @"所选运行配置没有提供 Vulkan 兼容模块。"; return nil; }
        return profile;
    }
    if (![execution isKindOfClass:NSDictionary.class] || !profile_requirements(execution[@"runtime_requirements"])) {
        *error=@"补丁接口版本或能力要求无效，请重新导入兼容包。";return nil;
    }
    if (![execution isKindOfClass:NSDictionary.class] || !profile_artifact(execution[@"manifest"])) {
        *error = @"兼容包缺失或指纹已变化，请重新导入并选择。"; return nil;
    }
    NSDictionary *manifest = read_json(execution[@"manifest"][@"path"]);
    NSDictionary *target = manifest[@"target"];
    if (![manifest[@"schema"] isEqual:@1] || ![target isKindOfClass:NSDictionary.class] ||
        ![manifest[@"runtime"] isKindOfClass:NSDictionary.class] ||
        ![manifest[@"id"] isEqual:pack] || ![manifest[@"runtime"] isEqual:execution[@"runtime_requirements"]] ||
        ![manifest[@"graphics"] isKindOfClass:NSArray.class] || ![manifest[@"graphics"] containsObject:graphics]) {
        *error = @"兼容包与所选 Runtime 或图形后端不匹配。"; return nil;
    }
    for (NSString *key in @[@"android_package", @"android_version_name", @"apk_sha256"])
        if (![target[key] isEqual:installed[key]]) { *error = @"兼容包不适用于当前 APK 的包名、版本或指纹。"; return nil; }
    NSArray *modules = execution[@"modules"], *resources = execution[@"resources"];
    NSDictionary *environment = execution[@"environment"];
    if (![modules isKindOfClass:NSArray.class] || !modules.count || modules.count > 16 ||
        ![resources isKindOfClass:NSArray.class] || resources.count > 32 ||
        ![environment isKindOfClass:NSDictionary.class] || environment.count > 32) {
        *error = @"兼容包的模块或环境配置无效。"; return nil;
    }
    for (NSDictionary *artifact in [modules arrayByAddingObjectsFromArray:resources])
        if (!profile_artifact(artifact)) { *error = @"兼容模块或驱动的文件指纹不匹配。"; return nil; }
    for (NSString *key in environment) {
        NSString *value = environment[key];
        if (!profile_environment_key(key) || ![value isKindOfClass:NSString.class] || value.length > 4096 ||
            [value rangeOfString:[NSString stringWithFormat:@"%C", (unichar)0]].location != NSNotFound) {
            *error = @"兼容包包含不支持的环境配置。"; return nil;
        }
    }
    return profile;
}

static bool activate_launch_profile(NSDictionary *profile, NSString *runtime, int argc, char **argv, BOOL reexecuted, NSString **error)
{
    NSDictionary *execution = profile[@"execution"];
    if (!execution) return true;
    NSString *library = [runtime stringByAppendingPathComponent:@"lib/libHuskRuntime.dylib"];
    NSDictionary *required = execution[@"runtime_requirements"];
    NSDictionary *actual = read_json([runtime stringByAppendingPathComponent:@"manifest.json"])[@"patch_api"];
    if (![actual isKindOfClass:NSDictionary.class] || ![required[@"patch_abi"] isKindOfClass:NSNumber.class] || ![required[@"capabilities"] isKindOfClass:NSNumber.class] ||
        ![actual[@"abi_version"] isEqual:required[@"patch_abi"]] ||
        ([actual[@"capabilities"] unsignedLongLongValue] & [required[@"capabilities"] unsignedLongLongValue]) != [required[@"capabilities"] unsignedLongLongValue]) {
        *error = @"全局运行库不支持兼容包的补丁接口版本或能力。"; return false;
    }
    NSMutableArray *paths = [NSMutableArray new];
    for (NSDictionary *module in execution[@"modules"]) [paths addObject:module[@"path"]];
    NSString *injected = [paths componentsJoinedByString:@":"];
    NSDictionary *environment = execution[@"environment"];
    NSString *token = [NSString stringWithFormat:@"%@:%@:%@", profile[@"apk_sha256"], profile[@"compatibility"], digest(library)];
    NSDictionary *current = NSProcessInfo.processInfo.environment;
    BOOL matches = [current[@"HUSK_COMPAT_LAUNCH_TOKEN"] isEqual:token] && [current[@"DYLD_INSERT_LIBRARIES"] isEqual:injected];
    for (NSString *key in environment) if (![current[key] isEqual:environment[key]]) matches = NO;
    if (matches) return true;
    if (reexecuted) { *error = @"进程未能加载所选兼容模块，请重新保存配置后启动。"; return false; }
    for (NSString *key in current) if (profile_environment_key(key)) unsetenv(key.UTF8String);
    for (NSString *key in environment) setenv(key.UTF8String, [environment[key] UTF8String], 1);
    setenv("DYLD_INSERT_LIBRARIES", injected.UTF8String, 1);
    setenv("HUSK_COMPAT_LAUNCH_TOKEN", token.UTF8String, 1);
    char **arguments = calloc((size_t)argc + 2, sizeof(char *));
    if (!arguments) { *error = @"无法准备兼容启动参数。"; return false; }
    for (int i = 0; i < argc; i++) arguments[i] = argv[i];
    arguments[argc] = "--compat-exec";
    /* Keep this installation locked through exec, then adopt the same verified
     * descriptor on reentry. Otherwise another configure could race the restart. */
    if (fcntl(instance_lease, F_SETFD, 0)) { free(arguments); *error = @"无法持有应用启动锁。"; return false; }
    setenv("HUSK_COMPAT_INSTANCE_FD", [[NSString stringWithFormat:@"%d", instance_lease] UTF8String], 1);
    fprintf(stderr, "host: applying compatibility profile %s, graphics=%s\n", [profile[@"compatibility"] UTF8String], [profile[@"graphics"] UTF8String]);
    fflush(NULL);
    execv(NSBundle.mainBundle.executablePath.fileSystemRepresentation, arguments);
    fcntl(instance_lease, F_SETFD, FD_CLOEXEC);
    free(arguments);
    *error = @"兼容启动重启失败。";
    return false;
}

static bool profile_api_compatible(NSDictionary *profile, void *library)
{
    NSDictionary *required = profile[@"execution"][@"runtime_requirements"];
    if (!required) return true;
    husk_runtime_patch_getter_v1 getter=dlsym(library,"husk_runtime_get_patch_api");
    const husk_runtime_patch_api_v1 *api=getter?getter([required[@"patch_abi"] unsignedIntValue],sizeof(*api)):NULL;
    uint64_t caps=[required[@"capabilities"] unsignedLongLongValue];
    return api && api->abi_version==[required[@"patch_abi"] unsignedIntValue] &&
        api->struct_size>=sizeof(*api) && (api->capabilities & caps)==caps;
}
