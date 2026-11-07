#!/usr/bin/env python3
"""Compare advertised extensions with the selected real ANGLE driver."""
from pathlib import Path
import argparse
import ctypes
import json
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    task_directory("gl-capabilities-test")
    runtime = args.runtime.resolve(strict=True)
    out = temporary_path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pointer, integer, unsigned = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
    library = ctypes.CDLL(str(runtime / "lib/libHuskRuntime.dylib"))
    library.tl_egl_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, integer]
    library.tl_egl_init.restype = ctypes.c_bool
    angle = runtime / "resources/angle"
    if not library.tl_egl_init(bytes(angle / "libEGL.dylib"), bytes(angle / "libGLESv2.dylib"), None, 0):
        raise RuntimeError("ANGLE did not initialize")
    # Exercise the existing shader-compatibility mode too: it cannot invent extensions.
    library.tl_egl_es3_claim.argtypes = [integer]
    library.tl_egl_es3_claim(1)
    library.tl_egl_resolve.argtypes = [ctypes.c_char_p]
    library.tl_egl_resolve.restype = pointer

    def function(name, result, *arguments):
        address = library.tl_egl_resolve(name.encode())
        if not address:
            raise RuntimeError("unresolved graphics entry: " + name)
        return ctypes.CFUNCTYPE(result, *arguments)(address)

    get_display = function("eglGetDisplay", pointer, pointer)
    initialize = function("eglInitialize", unsigned, pointer, ctypes.POINTER(integer), ctypes.POINTER(integer))
    choose = function("eglChooseConfig", unsigned, pointer, ctypes.POINTER(integer), ctypes.POINTER(pointer), integer, ctypes.POINTER(integer))
    create_context = function("eglCreateContext", pointer, pointer, pointer, pointer, ctypes.POINTER(integer))
    create_surface = function("eglCreatePbufferSurface", pointer, pointer, pointer, ctypes.POINTER(integer))
    make_current = function("eglMakeCurrent", unsigned, pointer, pointer, pointer, pointer)
    display = get_display(None)
    context = surface = None
    major, minor = integer(), integer()
    if not initialize(display, ctypes.byref(major), ctypes.byref(minor)):
        raise RuntimeError("EGL display initialization failed")
    try:
        config, count = pointer(), integer()
        attributes = (integer * 7)(0x3033, 1, 0x3040, 0x40, 0x3024, 8, 0x3038)
        if not choose(display, attributes, ctypes.byref(config), 1, ctypes.byref(count)) or count.value < 1:
            raise RuntimeError("no GLES 3 pbuffer configuration")
        context = create_context(display, config, None, (integer * 3)(0x3098, 3, 0x3038))
        surface = create_surface(display, config, (integer * 5)(0x3057, 2, 0x3056, 2, 0x3038))
        if not context or not surface or not make_current(display, surface, surface, context):
            raise RuntimeError("GLES context could not become current")
        driver = ctypes.CDLL(str(angle / "libGLESv2.dylib"))
        driver.glGetString.argtypes = [unsigned]
        driver.glGetString.restype = ctypes.c_char_p
        wrapped = function("glGetString", ctypes.c_char_p, unsigned)
        real = driver.glGetString(0x1f03).decode().split()
        advertised = wrapped(0x1f03).decode().split()
        report = {
            "driver_version": driver.glGetString(0x1f02).decode(),
            "reported_version": wrapped(0x1f02).decode(),
            "driver_texture_buffer_extensions": [e for e in real if "texture_buffer" in e],
            "reported_texture_buffer_extensions": [e for e in advertised if "texture_buffer" in e],
            "invented_extensions": sorted(set(advertised) - set(real)),
        }
    finally:
        make_current(display, None, None, None)
        if surface:
            function("eglDestroySurface", unsigned, pointer, pointer)(display, surface)
        if context:
            function("eglDestroyContext", unsigned, pointer, pointer)(display, context)
        function("eglTerminate", unsigned, pointer)(display)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["invented_extensions"]:
        raise RuntimeError("shader compatibility invented unsupported driver extensions")
    print("PASS: advertised GL extensions are implemented by the selected real ANGLE driver")


if __name__ == "__main__":
    main()
