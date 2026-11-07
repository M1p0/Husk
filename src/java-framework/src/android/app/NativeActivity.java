package android.app;

import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.res.AssetManager;
import android.content.res.Configuration;
import android.os.Build;
import android.os.Bundle;
import android.view.*;
import java.io.File;

/** Drives the NDK activity entry point and callbacks from the real Java activity lifecycle. */
public class NativeActivity extends Activity implements SurfaceHolder.Callback2, InputQueue.Callback,
        ViewTreeObserver.OnGlobalLayoutListener {
    public static final String META_DATA_LIB_NAME = "android.app.lib_name";
    public static final String META_DATA_FUNC_NAME = "android.app.func_name";
    private static final String SAVED_STATE = "android:native_state";
    private long mNativeHandle;
    private SurfaceHolder mSurface;
    private InputQueue mQueue;
    private View mContent;
    private final int[] mLocation = new int[2];
    private final android.graphics.Rect mContentRect = new android.graphics.Rect();
    private boolean mDestroyed;

    private native long huskLoadNativeCode(String library, String entry, String internal, String external,
            String obb, int sdk, AssetManager assets, byte[] state);
    private native void unloadNativeCode(long handle);
    private native void onStartNative(long handle);
    private native void onResumeNative(long handle);
    private native void onPauseNative(long handle);
    private native void onStopNative(long handle);
    private native byte[] onSaveInstanceStateNative(long handle);
    private native void onConfigurationChangedNative(long handle);
    private native void onLowMemoryNative(long handle);
    private native void onWindowFocusChangedNative(long handle, boolean focus);
    private native void onSurfaceCreatedNative(long handle, Surface surface);
    private native void onSurfaceChangedNative(long handle, Surface surface, int format, int width, int height);
    private native void onSurfaceRedrawNeededNative(long handle, Surface surface);
    private native void onSurfaceDestroyedNative(long handle);
    private native void onInputQueueCreatedNative(long handle, long queue);
    private native void onInputQueueDestroyedNative(long handle, long queue);
    private native void onContentRectChangedNative(long handle, int x, int y, int width, int height);
    private native boolean huskDispatchTouch(long handle, MotionEvent event);
    private native boolean huskDispatchKey(long handle, KeyEvent event);

    private static String path(File file) { return file == null ? null : file.getAbsolutePath(); }
    @Override protected void onCreate(Bundle state) {
        getWindow().takeSurface(this);
        getWindow().takeInputQueue(this);
        mContent = new View(this);
        mContent.setFocusable(true);
        mContent.setFocusableInTouchMode(true);
        setContentView(mContent);
        mContent.getViewTreeObserver().addOnGlobalLayoutListener(this);
        String library = "main", entry = "ANativeActivity_onCreate";
        try {
            ActivityInfo info = getPackageManager().getActivityInfo(getComponentName(), PackageManager.GET_META_DATA);
            if (info.metaData != null) {
                library = info.metaData.getString(META_DATA_LIB_NAME, library);
                entry = info.metaData.getString(META_DATA_FUNC_NAME, entry);
            }
        } catch (PackageManager.NameNotFoundException error) {
            throw new IllegalStateException("NativeActivity metadata unavailable", error);
        }
        mNativeHandle = huskLoadNativeCode(library, entry, path(getFilesDir()), path(getExternalFilesDir(null)),
                path(getObbDir()), Build.VERSION.SDK_INT, getAssets(), state == null ? null : state.getByteArray(SAVED_STATE));
        super.onCreate(state);
    }
    @Override protected void onStart() { super.onStart(); if (mNativeHandle != 0) onStartNative(mNativeHandle); }
    @Override protected void onResume() { super.onResume(); if (mNativeHandle != 0) onResumeNative(mNativeHandle); }
    @Override protected void onPause() { super.onPause(); if (mNativeHandle != 0) onPauseNative(mNativeHandle); }
    @Override protected void onStop() { super.onStop(); if (mNativeHandle != 0) onStopNative(mNativeHandle); }
    @Override protected void onSaveInstanceState(Bundle state) {
        super.onSaveInstanceState(state);
        if (mNativeHandle != 0) { byte[] bytes = onSaveInstanceStateNative(mNativeHandle); if (bytes != null) state.putByteArray(SAVED_STATE, bytes); }
    }
    @Override protected void onDestroy() {
        mDestroyed = true;
        if (mContent != null) mContent.getViewTreeObserver().removeOnGlobalLayoutListener(this);
        if (mNativeHandle != 0) {
            if (mSurface != null) onSurfaceDestroyedNative(mNativeHandle);
            if (mQueue != null) onInputQueueDestroyedNative(mNativeHandle, mQueue.getNativePtr());
            mSurface = null; mQueue = null;
            long handle = mNativeHandle; mNativeHandle = 0;
            unloadNativeCode(handle);
        }
        super.onDestroy();
    }
    @Override public void onConfigurationChanged(Configuration config) { super.onConfigurationChanged(config); if (mNativeHandle != 0) onConfigurationChangedNative(mNativeHandle); }
    @Override public void onLowMemory() { super.onLowMemory(); if (mNativeHandle != 0) onLowMemoryNative(mNativeHandle); }
    @Override public void onWindowFocusChanged(boolean focus) { super.onWindowFocusChanged(focus); if (mNativeHandle != 0) onWindowFocusChangedNative(mNativeHandle, focus); }
    @Override public void surfaceCreated(SurfaceHolder holder) { if (!mDestroyed && mNativeHandle != 0) { mSurface = holder; onSurfaceCreatedNative(mNativeHandle, holder.getSurface()); } }
    @Override public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) { if (!mDestroyed && mNativeHandle != 0) { mSurface = holder; onSurfaceChangedNative(mNativeHandle, holder.getSurface(), format, width, height); } }
    @Override public void surfaceRedrawNeeded(SurfaceHolder holder) { if (!mDestroyed && mNativeHandle != 0) onSurfaceRedrawNeededNative(mNativeHandle, holder.getSurface()); }
    @Override public void surfaceDestroyed(SurfaceHolder holder) { mSurface = null; if (!mDestroyed && mNativeHandle != 0) onSurfaceDestroyedNative(mNativeHandle); }
    @Override public void onInputQueueCreated(InputQueue queue) { if (!mDestroyed && mNativeHandle != 0) { mQueue = queue; onInputQueueCreatedNative(mNativeHandle, queue.getNativePtr()); } }
    @Override public void onInputQueueDestroyed(InputQueue queue) { if (!mDestroyed && mNativeHandle != 0) onInputQueueDestroyedNative(mNativeHandle, queue.getNativePtr()); mQueue = null; }
    @Override public void onGlobalLayout() {
        if (mDestroyed || mNativeHandle == 0 || mContent == null) return;
        mContent.getLocationInWindow(mLocation);
        int x = mLocation[0], y = mLocation[1], width = mContent.getWidth(), height = mContent.getHeight();
        if (mContentRect.left != x || mContentRect.top != y || mContentRect.width() != width || mContentRect.height() != height) {
            mContentRect.set(x, y, x + width, y + height);
            onContentRectChangedNative(mNativeHandle, x, y, width, height);
        }
    }
    @Override public boolean onTouchEvent(MotionEvent event) { return mNativeHandle != 0 && huskDispatchTouch(mNativeHandle, event) || super.onTouchEvent(event); }
    @Override public boolean dispatchKeyEvent(KeyEvent event) { return mNativeHandle != 0 && huskDispatchKey(mNativeHandle, event) || super.dispatchKeyEvent(event); }
}
