import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import java.lang.reflect.Field;
import sun.misc.Unsafe;

/** Regression for wrap-content dialogs whose content frame has size 0 and weight 1. */
public class WeightedMeasureTest {
    static Unsafe unsafe;
    static class Desired extends View {
        int desired;
        Desired() { super(null); }
        @Override protected void onMeasure(int ws, int hs) {
            setMeasuredDimension(resolveSize(desired, ws), resolveSize(desired, hs));
        }
    }
    static void set(Object o, Class<?> owner, String name, Object value) throws Exception {
        Field f = owner.getDeclaredField(name); f.setAccessible(true); f.set(o, value);
    }
    static void check(boolean vertical, int mode, int desired, int limit, int expected) throws Exception {
        LinearLayout group = (LinearLayout)unsafe.allocateInstance(LinearLayout.class);
        Desired child = (Desired)unsafe.allocateInstance(Desired.class); child.desired = desired;
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(vertical ? 100 : 0, vertical ? 0 : 100, 1);
        set(child, View.class, "mLayoutParams", lp);
        set(group, ViewGroup.class, "mChildren", new View[] { child });
        set(group, ViewGroup.class, "mChildrenCount", 1);
        set(group, LinearLayout.class, "mOrientation", vertical ? LinearLayout.VERTICAL : LinearLayout.HORIZONTAL);
        set(group, LinearLayout.class, "mBaselineAlignedChildIndex", -1);
        set(group, LinearLayout.class, "mBaselineAligned", false);
        int bound = View.MeasureSpec.makeMeasureSpec(limit, mode);
        int other = View.MeasureSpec.makeMeasureSpec(100, View.MeasureSpec.EXACTLY);
        group.measure(vertical ? other : bound, vertical ? bound : other);
        int outer = vertical ? group.getMeasuredHeight() : group.getMeasuredWidth();
        int inner = vertical ? child.getMeasuredHeight() : child.getMeasuredWidth();
        if (outer != expected || inner != expected) throw new AssertionError(
            (vertical ? "vertical" : "horizontal") + " mode=" + mode + " parent=" + outer + " child=" + inner + " expected=" + expected);
    }
    public static void main(String[] args) throws Exception {
        Field f = Unsafe.class.getDeclaredField("theUnsafe"); f.setAccessible(true); unsafe = (Unsafe)f.get(null);
        for (boolean vertical : new boolean[] { true, false }) {
            check(vertical, View.MeasureSpec.AT_MOST, 1200, 800, 800);
            check(vertical, View.MeasureSpec.AT_MOST, 500, 800, 500);
            check(vertical, View.MeasureSpec.EXACTLY, 1200, 800, 800);
            check(vertical, View.MeasureSpec.UNSPECIFIED, 500, 0, 500);
        }
        System.out.println("PASS: weighted content stays within dialog bounds; vertical/horizontal, long/short, bounded/exact/unbounded");
    }
}
