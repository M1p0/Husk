import android.app.NativeActivity;
import android.content.ComponentName;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.os.Bundle;
import android.os.Looper;
import android.view.*;
import java.io.File;
import java.lang.reflect.Field;
import java.lang.reflect.Method;

/** Real framework Java -> JNI bridge -> independently linked Android callbacks. */
public final class NativeActivityLifecycleTest {
    private static native String events();
    private static native void reset();
    private static void check(boolean value, String message) { if (!value) throw new AssertionError(message + " events=" + events()); }
    static final class Probe extends NativeActivity {
        final String entry;
        Probe(String entry) { this.entry = entry; attachBaseContext(husk.ContextImpl.app()); }
        @Override public ComponentName getComponentName() { return new ComponentName("husk.fixture", Probe.class.getName()); }
        @Override public PackageManager getPackageManager() {
            return new PackageManager() {
                @Override public ActivityInfo getActivityInfo(ComponentName name, int flags) {
                    ActivityInfo info = new ActivityInfo(); info.metaData = new Bundle();
                    info.metaData.putString(META_DATA_LIB_NAME, "native-activity-lifecycle");
                    if (entry != null) info.metaData.putString(META_DATA_FUNC_NAME, entry);
                    return info;
                }
            };
        }
        @Override public File getFilesDir() { return new File(System.getenv("HUSK_NATIVE_TEST_DIR"), "internal"); }
        @Override public File getExternalFilesDir(String type) { return new File(System.getenv("HUSK_NATIVE_TEST_DIR"), "external"); }
        @Override public File getObbDir() { return new File(System.getenv("HUSK_NATIVE_TEST_DIR"), "obb"); }
        void create(Bundle state) { onCreate(state); }
        void start() { onStart(); }
        void resume() { onResume(); }
        void pause() { onPause(); }
        void stop() { onStop(); }
        void save(Bundle state) { onSaveInstanceState(state); }
        void closeNative() { onDestroy(); }
    }
    public static String run() throws Exception {
        Looper.prepareMainLooper();
        System.load(new File(System.getenv("HUSK_NATIVE_TEST_DIR"), "libnative-activity-lifecycle.so").getPath());
        reset();
        Probe activity = new Probe("FixtureNativeActivity_onCreate");
        activity.create(null); activity.start(); activity.resume(); activity.onWindowFocusChanged(true);
        check(events().equals("CSRF"), "metadata entry and lifecycle order");
        InputQueue queue = new InputQueue(); check(queue.getNativePtr() != 0, "opaque input queue identity");
        activity.onInputQueueCreated(queue);
        SurfaceView surface = new SurfaceView(husk.ContextImpl.app()); SurfaceHolder holder = surface.getHolder();
        activity.surfaceCreated(holder); activity.surfaceChanged(holder, 1, 160, 120);
        activity.surfaceChanged(holder, 1, 320, 240); activity.surfaceRedrawNeeded(holder);
        activity.onConfigurationChanged(new Configuration()); activity.onLowMemory();
        Bundle saved = new Bundle(); activity.save(saved);
        check(events().equals("CSRFIWZVGMQ"), "NDK callback layout and window ownership");
        byte[] bytes = saved.getByteArray("android:native_state");
        check(bytes != null && bytes.length == 3 && bytes[0] == 7 && bytes[2] == 9, "native saved bytes copied before free");
        Field handle = NativeActivity.class.getDeclaredField("mNativeHandle"); handle.setAccessible(true);
        long stale = handle.getLong(activity);
        activity.pause(); activity.stop(); activity.closeNative();
        check(events().equals("CSRFIWZVGMQPTXJE"), "destroy window/input before native destroy");
        Method resume = NativeActivity.class.getDeclaredMethod("onResumeNative", long.class); resume.setAccessible(true);
        resume.invoke(activity, stale); activity.surfaceCreated(holder); activity.onLowMemory();
        check(events().equals("CSRFIWZVGMQPTXJE"), "stale handle and late callbacks ignored");
        Probe reopened = new Probe(null); reopened.create(saved); reopened.start(); reopened.resume(); reopened.pause(); reopened.stop(); reopened.closeNative();
        check(events().equals("CSRFIWZVGMQPTXJEDSRPTE"), "new instance uses default entry and restored state");
        boolean missing = false; Probe broken = new Probe("MissingNativeActivityEntry");
        try { broken.create(null); } catch (UnsatisfiedLinkError expected) { missing = true; } finally { broken.closeNative(); }
        check(missing, "missing entry throws instead of silently showing an empty activity");
        windowContract();
        return "PASS: NativeActivity Java lifecycle, Android ELF callbacks, saved state, missing entry, stale handles and close/reopen";
    }
    private static void windowContract() {
        Window window = new Window(husk.ContextImpl.app());
        ViewGroup decor = (ViewGroup) window.getDecorView(); int children = decor.getChildCount();
        SurfaceHolder.Callback2 callback = new SurfaceHolder.Callback2() {
            public void surfaceCreated(SurfaceHolder h) {} public void surfaceChanged(SurfaceHolder h, int f, int w, int y) {}
            public void surfaceDestroyed(SurfaceHolder h) {} public void surfaceRedrawNeeded(SurfaceHolder h) {}
        };
        window.takeSurface(callback); check(decor.getChildCount() == children + 1 && decor.getChildAt(0) instanceof SurfaceView, "window owns a surface behind content");
        window.takeSurface(callback); check(decor.getChildCount() == children + 1, "same surface callback is idempotent");
        window.takeSurface(null); check(decor.getChildCount() == children, "surface takeover can be released for a separate SurfaceView");
        final int[] changes = {0, 0};
        window.takeInputQueue(new InputQueue.Callback() {
            public void onInputQueueCreated(InputQueue q) { changes[0]++; }
            public void onInputQueueDestroyed(InputQueue q) { changes[1]++; }
        });
        window.huskNativeAttached(); window.huskNativeAttached(); window.huskNativeDetached(); window.huskNativeDetached();
        check(changes[0] == 1 && changes[1] == 1, "one input queue ownership per attach");
    }
}
