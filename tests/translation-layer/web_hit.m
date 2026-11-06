/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Native half of the Android-control-over-WebView touch regression.
 * Run from the repository root on macOS:
 * clang -fobjc-arc -Wno-deprecated-declarations -Isrc/translation-layer-next \
 *   -Isrc/translation-layer tests/translation-layer/web_hit.m \
 *   -Wl,-undefined,dynamic_lookup -framework Foundation -framework WebKit \
 *   -framework AppKit -framework QuartzCore -o build/web-hit-test
 * build/web-hit-test
 */
#include "../../src/translation-layer-next/husk-tl-dvm-web.m"
#include <assert.h>

int main(void)
{
    g_scale = 2;
    int32_t bounds[] = { 0, 120, 96, 216 };
    jobj array = {0};
    array.kind = TL_K_PRIM_ARRAY;
    array.arr.data = bounds;
    array.arr.len = 4;
    jvalue a[9] = {0}, ret = {0};
    a[0].i = 1; a[1].i = 0; a[2].i = 120;
    a[3].i = 804; a[4].i = 1600; a[5].i = 1; a[6].i = 1; a[7].l = &array;
    assert(W_frame(NULL, a, &ret));
    assert(!tl_web_hit(20, 80));    /* close button remains with Android */
    assert(tl_web_hit(150, 80));    /* the rest of the page is interactive */
    assert(tl_web_hit(20, 108));    /* half-open bounds */
    assert(!tl_web_hit(20, 50));    /* outside the page */
    assert(tl_web_parent_scroll_axes(150, 80) == 0); /* standalone page keeps WebKit scrolling */
    a[8].i = 2;
    assert(W_frame(NULL, a, &ret));
    assert(tl_web_parent_scroll_axes(150, 80) == 2); /* vertical Android list owns vertical drags */
    assert(tl_web_parent_scroll_axes(20, 80) == 0);  /* an Android control is not a page gesture */
    assert(tl_web_parent_scroll_axes(20, 50) == 0);
    a[8].i = 3;
    a[7].l = NULL;
    assert(W_frame(NULL, a, &ret));
    assert(tl_web_hit(20, 80));     /* hidden/removed button no longer blocks */
    assert(tl_web_parent_scroll_axes(20, 80) == 3);
    float rectangles[4] = {0};
    assert(tl_web_visible_rects(rectangles, 1) == 1);
    assert(rectangles[0] == 0 && rectangles[1] == 120 && rectangles[2] == 804 && rectangles[3] == 1600);
    a[6].i = 0;
    assert(W_frame(NULL, a, &ret));
    assert(!tl_web_hit(150, 80));   /* another window owns the touch */
    assert(tl_web_parent_scroll_axes(150, 80) == 0);
    assert(tl_web_visible_rects(rectangles, 1) == 1); /* visible but not touchable still needs a native hole */
    a[5].i = 0;
    assert(W_frame(NULL, a, &ret));
    assert(tl_web_visible_rects(rectangles, 1) == 0);
    forget_hits(1);
    assert(g_nhits == 0);
    assert(tl_web_parent_scroll_axes(150, 80) == 0);
    puts("PASS: Android button exclusion, parent scroll axes, standalone page, scale, boundaries and removal");
    return 0;
}
