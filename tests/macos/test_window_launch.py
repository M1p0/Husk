#!/usr/bin/env python3
"""Check consecutive real-window launches without enabling runtime frame capture.

The supplied state must already have been activated through the real APK UI.
This runner sends no taps or text, changes no database or runtime selection, and
does not pass --runtime or --frame-dir to the host. --runtime is an expectation
for the launch-time default only. Each child gets its own control/evidence files.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import collections
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def png_pixels(path):
    """Decode ImageIO/screencapture RGB(A) PNG rows in top-to-bottom order.

    No Pillow, OCR or GPU readback. Support noninterlaced 8/16-bit RGB(A) and
    grayscale PNG; unsupported encodings fail instead of guessing pixel layout.
    Stored RGB values are compared directly with an explicit channel tolerance.
    """
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("capture is not a PNG")
    offset, compressed, header, profile = 8, bytearray(), None, []
    while offset + 12 <= len(raw):
        size, = struct.unpack_from(">I", raw, offset)
        kind = raw[offset + 4:offset + 8]
        end = offset + 8 + size
        if end + 4 > len(raw):
            raise ValueError("truncated PNG chunk")
        data = raw[offset + 8:end]
        crc, = struct.unpack_from(">I", raw, end)
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            raise ValueError("PNG CRC mismatch")
        if kind == b"IHDR":
            header = struct.unpack(">2I5B", data)
        elif kind == b"IDAT":
            compressed += data
        elif kind in (b"iCCP", b"sRGB", b"gAMA", b"cHRM"):
            profile.append(kind.decode("ascii"))
        offset = end + 4
        if kind == b"IEND":
            break
    if not header:
        raise ValueError("missing PNG header")
    width, height, depth, color, compression, filtering, interlace = header
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if not channels or depth not in (8, 16) or compression or filtering or interlace:
        raise ValueError(f"unsupported PNG encoding: depth={depth}, color={color}, interlace={interlace}")
    if not (0 < width <= 16384 and 0 < height <= 16384):
        raise ValueError("capture dimensions out of range")
    sample_bytes = depth // 8
    bpp, stride = channels * sample_bytes, width * channels * sample_bytes
    data = zlib.decompress(compressed)
    if len(data) != height * (stride + 1):
        raise ValueError("PNG row length mismatch")
    rows, prior, position = [], bytearray(stride), 0
    for _ in range(height):
        method = data[position]
        row = bytearray(data[position + 1:position + 1 + stride])
        position += stride + 1
        if method > 4:
            raise ValueError("invalid PNG row filter")
        for n in range(stride):
            left = row[n - bpp] if n >= bpp else 0
            above = prior[n]
            upper_left = prior[n - bpp] if n >= bpp else 0
            if method == 1:
                predictor = left
            elif method == 2:
                predictor = above
            elif method == 3:
                predictor = (left + above) // 2
            elif method == 4:
                p = left + above - upper_left
                a, b, c = abs(p - left), abs(p - above), abs(p - upper_left)
                predictor = left if a <= b and a <= c else above if b <= c else upper_left
            else:
                predictor = 0
            row[n] = (row[n] + predictor) & 255
        rows.append(row)
        prior = row
    return width, height, channels, sample_bytes, rows, profile


def top_strip(path, config, expected, tolerance):
    width, height, channels, sample, rows, profile = png_pixels(path)
    guest_w, guest_h, density = config["width"], config["height"], config["density"]
    scale = width / (guest_w / density)
    body_height = guest_h / density * scale
    titlebar = height - body_height
    if titlebar < -1 or titlebar > 160 * scale:
        raise ValueError(f"window/body geometry mismatch: PNG={width}x{height}, body={body_height}, titlebar={titlebar}")
    x0, x1 = math.ceil(width * .1), math.floor(width * .9)
    y0 = max(0, math.ceil(titlebar + 3 * scale))
    y1 = min(height, math.floor(titlebar + 10 * scale))
    if x0 >= x1 or y0 >= y1:
        raise ValueError("empty content top-strip sample")
    colors, matched, count = collections.Counter(), 0, 0
    for row in rows[y0:y1]:
        for x in range(x0, x1):
            offset = x * channels * sample
            if channels <= 2:
                rgb = (row[offset],) * 3
            else:
                rgb = tuple(row[offset + n * sample] for n in range(3))
            colors[rgb] += 1
            count += 1
            if expected and all(abs(a - b) <= tolerance for a, b in zip(rgb, expected)):
                matched += 1
    result = {"png_width": width, "png_height": height, "capture_pixels_per_point": scale,
              "body_height_pixels": body_height, "titlebar_height_pixels": titlebar,
              "sample_rect_pixels": [x0, y0, x1, y1], "sample_count": count,
              "png_profile_chunks": profile, "color_space_comparison": "stored PNG RGB values",
              "dominant_rgb": [{"rgb": "%02x%02x%02x" % rgb, "count": n, "ratio": n / count}
                               for rgb, n in colors.most_common(8)]}
    if expected:
        result.update(expected_rgb="%02x%02x%02x" % expected, channel_tolerance=tolerance,
                      matched_pixels=matched, matched_ratio=matched / count, required_ratio=.70)
    return result


def lock_available(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def live_status(process, state):
    if process.poll() is not None:
        raise RuntimeError(f"own host PID {process.pid} exited early ({process.returncode})")
    status = read_json(state / "status.json")
    if status.get("pid") != process.pid:
        return {}
    if status.get("error"):
        raise RuntimeError(f"own host failed: {status['error']}")
    return status


def close_child(process, control, record):
    if process.poll() is None:
        with control.open("a") as stream:
            stream.write('{"action":"quit"}\n')
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            record["forced_termination"] = True
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    record["exit_code"] = process.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True, help="existing, UI-activated app state; never synthesized")
    parser.add_argument("--runtime", type=Path, required=True, help="expected default version, not a host override")
    parser.add_argument("--out", type=Path, required=True, help="new directory inside the current task's evidence directory")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--expect-top-rgb", help="six hex digits; require at least 70 percent of the content top strip")
    parser.add_argument("--rgb-tolerance", type=int, default=12, help="allowed absolute difference per stored RGB channel")
    parser.add_argument("--no-activate", action="store_true")
    args = parser.parse_args()
    task = task_directory(label="macos-window-launch")
    # Reject prohibited output before resolving app/state paths or starting any
    # child. Resolve symlinks as part of validation, including evidence's parent.
    try:
        output = temporary_path(args.out)
        evidence_root = temporary_path(task / "evidence")
        if output == evidence_root or not output.is_relative_to(evidence_root):
            raise ValueError(f"window evidence must be in a new directory under {evidence_root}")
        state = temporary_path(args.state)
    except ValueError as error:
        parser.error(str(error))
    child_environment = os.environ.copy()
    if not 1 <= args.runs <= 30 or not 0 <= args.rgb_tolerance <= 32:
        parser.error("runs must be 1..30 and rgb-tolerance 0..32")
    expected = None
    if args.expect_top_rgb:
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", args.expect_top_rgb):
            parser.error("expect-top-rgb must contain six hex digits")
        expected = tuple(bytes.fromhex(args.expect_top_rgb))
    bundle = args.app.expanduser().resolve(strict=True)
    runtime = args.runtime.expanduser().resolve(strict=True)
    if not state.is_dir() or not (state / "data").is_dir():
        parser.error("state must be an existing app state with its real data directory")
    output.mkdir(parents=True, exist_ok=False)
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    host = bundle / "Contents/MacOS" / info["CFBundleExecutable"]
    config = read_json(bundle / "Contents/Resources/husk-app.json")
    for key in ("width", "height", "density"):
        value = config.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"invalid installed app {key}")
    store = Path(config["runtime_store"]).resolve(strict=True)
    result = {"schema": 1, "kind": "macos-no-frame-capture-window-regression", "app": str(bundle),
              "task_directory": str(task),
              "temporary_environment": {key: os.environ.get(key) for key in ["TMPDIR", "TMP", "TEMP"]},
              "state": str(state), "expected_runtime": str(runtime), "host_sha256": sha256(host),
              "runtime_override_passed": False, "frame_dir_passed": False, "gpu_frame_readback_requested": False,
              "input_commands_sent": False, "settle_seconds_after_ready": 8, "runs": []}
    try:
        for number in range(1, args.runs + 1):
            case = output / f"run-{number}"
            case.mkdir()
            control = case / "control.jsonl"
            control.touch(exist_ok=False)
            record = {"run": number, "selection_before": read_json(store / "default.json")}
            result["runs"].append(record)
            if sha256(host) != result["host_sha256"]:
                raise RuntimeError("host binary changed during regression")
            command = [str(host), "--state-root", str(state), "--control", str(control), "--quit-after", "120"]
            if args.no_activate:
                command.append("--no-activate")
            record["command"] = command
            with (case / "launcher.log").open("wb") as log:
                process = subprocess.Popen(command, stdout=log, stderr=log, env=child_environment)
                record["pid"] = process.pid
                try:
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        status = live_status(process, state)
                        if status.get("state") == 2 and status.get("frames", 0) > 0:
                            break
                        time.sleep(.2)
                    else:
                        raise TimeoutError("own host did not reach running/rendering within 90 seconds")
                    record["ready_status"] = status
                    if Path(status["runtime"]).resolve() != runtime:
                        raise AssertionError(f"default Runtime mismatch: {status['runtime']}")
                    record["generation"] = status["generation"]
                    if any(old.get("generation") == record["generation"] for old in result["runs"][:-1]):
                        raise AssertionError("execution generation was reused")
                    until = time.monotonic() + 8
                    while time.monotonic() < until:
                        live_status(process, state)
                        time.sleep(.1)
                    status = live_status(process, state)
                    if status.get("state") != 2 or status.get("pid") != process.pid:
                        raise AssertionError(f"own host stopped before capture: {status}")
                    window_id = status.get("window_number")
                    if not isinstance(window_id, int) or window_id <= 0:
                        raise AssertionError("own host did not publish a window ID")
                    image = case / "window.png"
                    capture = subprocess.run(["screencapture", "-x", "-o", "-l", str(window_id), str(image)],
                                             capture_output=True, text=True, timeout=15, env=child_environment)
                    if capture.returncode or not image.is_file():
                        raise RuntimeError(f"own window capture failed: {capture.stderr.strip()}")
                    record["capture_status"] = status
                    record["window_path"] = str(image)
                    record["window_sha256"] = sha256(image)
                    record["top_strip"] = top_strip(image, config, expected, args.rgb_tolerance)
                    if expected and record["top_strip"]["matched_ratio"] < .70:
                        raise AssertionError(f"Account header not visible without input: {record['top_strip']}")
                    record["passed"] = True
                finally:
                    close_child(process, control, record)
                    record["final_status"] = read_json(state / "status.json")
                    record["instance_lock_released"] = lock_available(state / "instance.lock")
            if record.get("forced_termination") or record["exit_code"] != 0 or not record["instance_lock_released"]:
                raise AssertionError(f"own host did not exit normally/release its instance lock: {record}")
        lease = runtime.parent / ".leases" / (runtime.name + ".lock")
        result["runtime_lease_exclusive_available_after_children_exit"] = lock_available(lease)
        if not result["runtime_lease_exclusive_available_after_children_exit"]:
            result["runtime_lease_note"] = "Other live apps may retain this shared version; all owned children have exited."
        result["selection_after"] = read_json(store / "default.json")
        result["host_sha256_after"] = sha256(host)
        if result["host_sha256_after"] != result["host_sha256"]:
            raise AssertionError("host binary changed during regression")
        result["passed"] = True
    except BaseException as error:
        result["error"] = str(error)
        raise
    finally:
        (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(output / "results.json", flush=True)


if __name__ == "__main__":
    main()
