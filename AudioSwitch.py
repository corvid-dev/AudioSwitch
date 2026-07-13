#!/usr/bin/env python3
"""Audio A/B Switcher with a Tkinter interface.
Routes either of two live inputs to one output device."""

# ============================================================
# 0. Imports
# ============================================================

import os
import sys
import json
import threading
import queue
import ctypes
import tkinter as tk
import numpy as np
import sounddevice as sd
from tkinter import ttk, messagebox, colorchooser

try:
    from pynput import keyboard as pynput_keyboard, mouse as pynput_mouse
    HAVE_PYNPUT = True
except ImportError:
    pynput_keyboard = None
    pynput_mouse = None
    HAVE_PYNPUT = False

try:
    from pycaw.pycaw import IMMDeviceEnumerator
    from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize, CoCreateInstance, GUID
    HAVE_COM = True
except ImportError:
    HAVE_COM = False

    def CoInitialize():
        pass

    def CoUninitialize():
        pass


try:
    _user32 = ctypes.windll.user32
    HAVE_WIN32 = True
except AttributeError:
    _user32 = None
    HAVE_WIN32 = False


def resource_path(filename):
    """Return a bundled or source-relative resource path."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)


# ============================================================
# 1. Constants
# ============================================================

VERSION = "1.0"

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
BLOCK_SIZE = 1024
DTYPE = "float32"
DEFAULT_BUFFER_BLOCKS = 4

CAPTURING_BG = "#f9a825"

OVERLAY_POSITIONS = ("Top-left", "Top-right", "Bottom-left", "Bottom-right")
DEFAULT_OVERLAY_POSITION = "Top-right"
DEFAULT_OVERLAY_OFFSET = 24
DEFAULT_OVERLAY_COLORS = {"A": "#00e5ff", "B": "#ff9100"}

# Win32 constants for the overlay window (click-through + excluded from
# screen capture). See the Overlay class for what each one does.
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WDA_EXCLUDEFROMCAPTURE = 0x00000011
LWA_COLORKEY = 0x00000001

if HAVE_WIN32:
    _user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
    _user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
    _user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_ubyte, ctypes.c_uint32
    ]


# ============================================================
# 2. Preferences file I/O
# ============================================================

PREFS_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "AudioSwitch", "prefs.json"
)


def load_prefs():
    try:
        with open(PREFS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(data):
    """Save preferences with an atomic file replacement."""
    try:
        directory = os.path.dirname(PREFS_PATH)
        os.makedirs(directory, exist_ok=True)
        temp_path = PREFS_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, PREFS_PATH)
    except OSError:
        pass


# ============================================================
# 3. Device enumeration / labeling / selection-resolution
# ============================================================

# Windows Core Audio endpoint IDs, for stable device-ID matching.
_CLSID_ENUM = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}") if HAVE_COM else None
_PKEY_NAME = GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}") if HAVE_COM else None
EDATAFLOW_RENDER = 0
EDATAFLOW_CAPTURE = 1
DEVICE_STATE_ACTIVE = 1


def explain_stream_error(error_text):
    if not error_text:
        return "Unknown error"
    low = error_text.lower()
    if "wdm-ks" in low or "wdmsyncioctl" in low:
        return (
            f"{error_text}\n\nThis device is using the WDM-KS driver, which needs "
            f"exclusive hardware access -- it fails like this if another app already "
            f"has the device open, or if it doesn't support the requested format. "
            f"Try picking the WASAPI (or DirectSound) copy of this same device instead."
        )
    if "invalid number of channels" in low or "invalid sample rate" in low:
        return (
            f"{error_text}\n\nThis is usually a sample-rate/channel mismatch between "
            f"devices. Check each device's default sample rate."
        )
    return f"{error_text}\n\nTry a different host-API copy of this device (type filter)."


def _endpoint_friendly_name(ep):
    try:
        store = ep.OpenPropertyStore(0)
        for i in range(store.GetCount()):
            pk = store.GetAt(i)
            if pk.fmtid == _PKEY_NAME and pk.pid == 14:
                return str(store.GetValue(pk).GetValue())
    except Exception:
        pass
    return ""


def create_com_enumerator():
    """Create the Windows audio endpoint enumerator."""
    if not HAVE_COM:
        return None
    try:
        return CoCreateInstance(_CLSID_ENUM, IMMDeviceEnumerator, CLSCTX_ALL)
    except Exception:
        return None


def enumerate_com_endpoints(com_enum, dataflow):
    result = {}
    if com_enum is None:
        return result
    try:
        col = com_enum.EnumAudioEndpoints(dataflow, DEVICE_STATE_ACTIVE)
        for i in range(col.GetCount()):
            try:
                ep = col.Item(i)
                eid = ep.GetId()
                name = _endpoint_friendly_name(ep)
                if name:
                    result[eid] = name
            except Exception:
                continue
    except Exception:
        pass
    return result


def build_device_labels(devices, kind, endpoint_map=None):
    """Build labeled input or output device entries.
    Includes host API details and endpoint IDs when available."""
    endpoint_map = endpoint_map or {}
    hostapis = sd.query_hostapis()

    def hostapi_name(idx):
        try:
            return hostapis[idx]["name"]
        except (IndexError, TypeError):
            return "Unknown"

    name_counts = {}
    for d in devices:
        ok = d["max_input_channels"] > 0 if kind == "input" else d["max_output_channels"] > 0
        if ok:
            name_counts[d["name"]] = name_counts.get(d["name"], 0) + 1

    seen = {}
    entries = []
    for i, d in enumerate(devices):
        ok = d["max_input_channels"] > 0 if kind == "input" else d["max_output_channels"] > 0
        if not ok:
            continue
        name = d["name"]
        seen[name] = seen.get(name, 0) + 1
        base = f"[{seen[name]}] {name}" if name_counts[name] > 1 else name
        hostapi = hostapi_name(d["hostapi"])
        endpoint_id = None
        if "wasapi" in hostapi.lower():
            endpoint_id = next((eid for eid, ename in endpoint_map.items() if ename == name), None)
        entries.append((i, f"{base} \u2014 {hostapi}", name, hostapi, endpoint_id))
    return entries


def label_for_index(entries, idx):
    for i, label, _, _, _ in entries:
        if i == idx:
            return label
    return None


def entry_for_index(idx, entries):
    """Return a serializable record for the selected device."""
    if idx is None:
        return None
    for i, _, name, hostapi, endpoint_id in entries:
        if i == idx:
            return {"name": name, "hostapi": hostapi, "endpoint_id": endpoint_id}
    return None


def pick_preferred(entries, name_filter=None):
    pool = entries if name_filter is None else [e for e in entries if e[2] == name_filter]
    if not pool:
        return None
    wasapi = [e for e in pool if "wasapi" in e[3].lower()]
    return (wasapi or pool)[0][0]


def resolve_selection(current_idx, saved, entries, default_pos):
    """Resolve a saved or current device selection.
    Prefers stable endpoint IDs and WASAPI devices."""
    valid = {i for i, _, _, _, _ in entries}
    if current_idx is not None and current_idx in valid:
        return current_idx

    saved_id = saved.get("endpoint_id") if isinstance(saved, dict) else None
    if saved_id:
        match = next((i for i, _, _, _, eid in entries if eid and eid == saved_id), None)
        if match is not None:
            return match

    saved_name = saved.get("name") if isinstance(saved, dict) else saved
    saved_hostapi = saved.get("hostapi") if isinstance(saved, dict) else None
    if saved_name:
        if saved_hostapi:
            exact = next((i for i, _, name, h, _ in entries
                          if name == saved_name and h == saved_hostapi), None)
            if exact is not None:
                return exact
        picked = pick_preferred(entries, name_filter=saved_name)
        if picked is not None:
            return picked

    if not entries:
        return None
    wasapi_only = [e for e in entries if "wasapi" in e[3].lower()]
    pool = wasapi_only or entries
    return pool[min(default_pos, len(pool) - 1)][0]


# ============================================================
# 4. Global hotkey capture (no tkinter dependency)
# ============================================================

MOUSE_LABELS = {}
if pynput_mouse is not None:
    MOUSE_LABELS = {
        pynput_mouse.Button.left: "Mouse-Left",
        pynput_mouse.Button.right: "Mouse-Right",
        pynput_mouse.Button.middle: "Mouse-Middle",
        pynput_mouse.Button.x1: "Mouse-X1",
        pynput_mouse.Button.x2: "Mouse-X2",
    }

DISPLAY_NAMES = {
    "ctrl_l": "L-Ctrl", "ctrl_r": "R-Ctrl", "shift_l": "L-Shift", "shift_r": "R-Shift",
    "alt_l": "L-Alt", "alt_r": "R-Alt", "Mouse-Left": "Mouse L", "Mouse-Right": "Mouse R",
    "Mouse-Middle": "Mouse M", "Mouse-X1": "Mouse 4", "Mouse-X2": "Mouse 5",
}


def key_label(k):
    if isinstance(k, str):
        return k
    try:
        return k.char or str(k).replace("Key.", "")
    except Exception:
        return str(k).replace("Key.", "")


def disp(label):
    if not label:
        return "none"
    return DISPLAY_NAMES.get(label, label.upper() if len(label) == 1 else label.title())


class InputHook:
    """Listen for global keyboard and mouse presses.
    Posts input events back to the GUI thread."""

    def __init__(self, on_press_label, post_to_ui):
        self.on_press_label = on_press_label
        self.post_to_ui = post_to_ui
        self._pressed = set()
        self._kb_listener = None
        self._ms_listener = None

    def start(self):
        if self._kb_listener is not None or self._ms_listener is not None:
            return  # already running
        self._pressed = set()
        if pynput_keyboard is None:
            return
        self._kb_listener = pynput_keyboard.Listener(
            on_press=lambda k: self._dispatch(key_label(k), True),
            on_release=lambda k: self._dispatch(key_label(k), False),
        )
        self._kb_listener.start()
        self._ms_listener = pynput_mouse.Listener(
            on_click=lambda x, y, b, pressed: self._dispatch(MOUSE_LABELS.get(b, str(b)), pressed)
        )
        self._ms_listener.start()

    def _dispatch(self, label, pressed):
        if pressed:
            if label in self._pressed:
                return
            self._pressed.add(label)
        else:
            self._pressed.discard(label)
            return
        self.post_to_ui(self.on_press_label, label)

    def stop(self):
        if self._kb_listener:
            self._kb_listener.stop()
            self._kb_listener = None
        if self._ms_listener:
            self._ms_listener.stop()
            self._ms_listener = None


# ============================================================
# 5. Screen overlay (Windows only; degrades to a plain window elsewhere)
# ============================================================

class Overlay:
    """Show a colored square for the active source.
    Uses Windows click-through and capture-exclusion features when available."""

    _SQUARE_SIZE = 12

    def __init__(self, root):
        self._root = root
        self._win = None
        self._square = None
        self._position = DEFAULT_OVERLAY_POSITION
        self._offset_x = DEFAULT_OVERLAY_OFFSET
        self._offset_y = DEFAULT_OVERLAY_OFFSET
        self._colors = dict(DEFAULT_OVERLAY_COLORS)
        self._slot = "A"

    def set_position(self, position):
        self._position = position if position in OVERLAY_POSITIONS else DEFAULT_OVERLAY_POSITION
        self._reposition()

    def set_offset(self, offset_x, offset_y):
        """Set the horizontal and vertical distance from the selected corner."""
        self._offset_x = max(0, offset_x)
        self._offset_y = max(0, offset_y)
        self._reposition()

    def set_slot(self, slot):
        self._slot = slot
        if self._square is not None:
            self._square.config(bg=self._colors.get(slot, DEFAULT_OVERLAY_COLORS["A"]))

    def set_color(self, slot, color):
        self._colors[slot] = color
        if self._square is not None and self._slot == slot:
            self._square.config(bg=color)

    def show(self):
        if self._win is not None:
            return
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="black")
        try:
            win.attributes("-transparentcolor", "black")  # Windows-only Tk feature
        except tk.TclError:
            pass
        self._square = tk.Frame(
            win, bg=self._colors.get(self._slot, DEFAULT_OVERLAY_COLORS["A"]),
            width=self._SQUARE_SIZE, height=self._SQUARE_SIZE,
        )
        self._square.pack_propagate(False)
        self._square.pack()
        self._win = win
        self._reposition()
        self._apply_click_through()
        self._apply_capture_exclusion()

    def hide(self):
        if self._win is not None:
            self._win.destroy()
            self._win = None
            self._square = None

    def _reposition(self):
        if self._win is None:
            return
        self._win.update_idletasks()
        w = self._win.winfo_reqwidth()
        h = self._win.winfo_reqheight()
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()
        mx, my = self._offset_x, self._offset_y
        x, y = {
            "Top-left": (mx, my),
            "Top-right": (sw - w - mx, my),
            "Bottom-left": (mx, sh - h - my),
            "Bottom-right": (sw - w - mx, sh - h - my),
        }.get(self._position, (sw - w - mx, my))
        self._win.geometry(f"{w}x{h}+{x}+{y}")

    def _apply_click_through(self):
        if not HAVE_WIN32 or self._win is None:
            return
        hwnd = self._win.winfo_id()
        style = int(_user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        _user32.SetLayeredWindowAttributes(hwnd, 0x000000, 0, LWA_COLORKEY)

    def _apply_capture_exclusion(self):
        if not HAVE_WIN32 or self._win is None:
            return
        _user32.SetWindowDisplayAffinity(self._win.winfo_id(), WDA_EXCLUDEFROMCAPTURE)


# ============================================================
# 6. Audio engine (no tkinter dependency)
# ============================================================

class _AudioRingBuffer:
    """Store preallocated audio blocks for one producer and one consumer."""

    def __init__(self, n_slots, blocksize, channels, dtype=DTYPE):
        self._capacity = max(2, n_slots + 1)
        self._slots = np.zeros((self._capacity, blocksize, channels), dtype=dtype)
        self._blocksize = blocksize
        self._write_idx = 0
        self._read_idx = 0

    def write(self, block):
        """Producer-only. Drops the oldest block if full."""
        w = self._write_idx
        nxt = w + 1
        if nxt == self._capacity:
            nxt = 0
        if nxt == self._read_idx:
            r = self._read_idx + 1  # full: drop oldest
            self._read_idx = 0 if r == self._capacity else r
        n = block.shape[0]
        self._slots[w, :n] = block
        if n < self._blocksize:
            self._slots[w, n:] = 0
        self._write_idx = nxt

    def read_latest(self):
        """Consumer-only. Returns the oldest unread block, or None."""
        r = self._read_idx
        if r == self._write_idx:
            return None
        block = self._slots[r]
        r += 1
        self._read_idx = 0 if r == self._capacity else r
        return block

    def drain_to_latest(self):
        """Consumer-only. Discards backlog, keeps only the newest block."""
        if self._read_idx == self._write_idx:
            return
        newest = self._write_idx - 1
        self._read_idx = self._capacity - 1 if newest < 0 else newest


class AudioSwitcher:
    """Route two live input streams to one output stream.
    Switching changes which input supplies the output."""

    def __init__(self, slot_devices, output_index, samplerate, channels,
                 blocksize=BLOCK_SIZE, buffer_blocks=DEFAULT_BUFFER_BLOCKS,
                 on_switch=None, initial_slot="A"):
        self.slot_devices = dict(slot_devices)
        self.output_index = output_index
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.buffer_blocks = max(1, buffer_blocks)
        self.on_switch = on_switch

        self.active_slot = initial_slot if initial_slot in ("A", "B") else "A"
        self._take_latest = False
        self.lock = threading.Lock()

        self.buffers = {"A": None, "B": None}
        self.input_streams = {"A": None, "B": None}
        self.output_stream = None
        self.output_channels = None

        self.slot_names = {"A": None, "B": None}
        self.output_name = None

        self._mix_scratch = None
        self._mono_scratch = None
        self._pad_scratch = None

    @staticmethod
    def _resolve_device(device_index, requested, is_input):
        """Return the usable channel count and device name."""
        info = sd.query_devices(device_index)
        device_max = info["max_input_channels"] if is_input else info["max_output_channels"]
        if device_max <= 0:
            raise ValueError(f"'{info['name']}' has no {'input' if is_input else 'output'} channels")
        return max(1, min(requested, device_max)), info["name"]

    def _adapt_channels(self, block, target):
        src = block.shape[1]
        if src == target:
            return block
        frames = block.shape[0]
        out = self._mix_scratch[:frames]
        if src < target:
            reps, rem = divmod(target, src)
            for i in range(reps):
                out[:, i * src:(i + 1) * src] = block
            if rem:
                out[:, reps * src:reps * src + rem] = block[:, :rem]
        else:
            mono = self._mono_scratch[:frames]
            block.mean(axis=1, out=mono)
            out[:] = mono[:, None]
        return out

    @staticmethod
    def _make_ring_write_callback(ring):
        def callback(indata, _frames, _time_info, _status):
            ring.write(indata)

        return callback

    def _create_slot_stream(self, dev_index):
        """Create and start an input stream with its ring buffer."""
        actual_channels, name = self._resolve_device(dev_index, self.channels, is_input=True)
        ring = _AudioRingBuffer(self.buffer_blocks, self.blocksize, actual_channels, dtype=DTYPE)
        stream = sd.InputStream(
            device=dev_index,
            channels=actual_channels,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            latency="low",
            dtype=DTYPE,
            callback=self._make_ring_write_callback(ring),
        )
        stream.start()
        return stream, name, ring

    def _open_slot_stream(self, slot):
        """Open and store one source stream during startup."""
        stream, name, ring = self._create_slot_stream(self.slot_devices[slot])
        self.input_streams[slot] = stream
        self.slot_names[slot] = name
        self.buffers[slot] = ring

    def _output_callback(self, outdata, frames, _time_info, _status):
        with self.lock:
            active = self.active_slot
            take_latest = self._take_latest
            self._take_latest = False
        ring = self.buffers.get(active)
        if take_latest and ring is not None:
            ring.drain_to_latest()
        block = ring.read_latest() if ring is not None else None
        if block is None:
            outdata[:] = 0
            return
        block = self._adapt_channels(block, self.output_channels)
        if block.shape[0] != frames:
            if self._pad_scratch is None:
                self._pad_scratch = np.zeros((self.blocksize, self.output_channels), dtype=DTYPE)
            fixed = self._pad_scratch[:frames]
            fixed[:] = 0
            n = min(frames, block.shape[0])
            fixed[:n] = block[:n]
            outdata[:] = fixed
        else:
            outdata[:] = block

    def set_active_slot(self, slot):
        """Set the active source and notify the GUI."""
        with self.lock:
            self.active_slot = slot
            self._take_latest = True
        if self.on_switch:
            self.on_switch(slot)

    def toggle(self):
        with self.lock:
            self.active_slot = "B" if self.active_slot == "A" else "A"
            self._take_latest = True
            slot = self.active_slot
        if self.on_switch:
            self.on_switch(slot)

    def swap_device(self, slot, new_device_index):
        """Replace one source device while preserving the old stream on failure."""
        stream, name, ring = self._create_slot_stream(new_device_index)

        old_stream = self.input_streams.get(slot)
        self.input_streams[slot] = stream
        self.slot_names[slot] = name
        self.buffers[slot] = ring
        self.slot_devices[slot] = new_device_index

        if old_stream is not None:
            try:
                old_stream.stop()
                old_stream.close()
            except Exception:
                pass

    def start(self):
        try:
            self.output_channels, self.output_name = self._resolve_device(
                self.output_index, self.channels, is_input=False
            )
            self._mix_scratch = np.zeros((self.blocksize, self.output_channels), dtype=DTYPE)
            self._mono_scratch = np.zeros(self.blocksize, dtype=DTYPE)
            for slot in ("A", "B"):
                self._open_slot_stream(slot)
            self.output_stream = sd.OutputStream(
                device=self.output_index,
                channels=self.output_channels,
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                latency="low",
                dtype=DTYPE,
                callback=self._output_callback,
            )
            self.output_stream.start()
        except Exception:
            self.stop()
            raise

    def stop(self):
        for slot in ("A", "B"):
            stream = self.input_streams.get(slot)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            self.input_streams[slot] = None
        if self.output_stream:
            try:
                self.output_stream.stop()
                self.output_stream.close()
            except Exception:
                pass
            self.output_stream = None


class AudioEngine:
    """Run audio and COM work on a background thread.
    Uses a queue for commands and GUI callbacks."""

    def __init__(self, on_switch, post_to_ui):
        self.post_to_ui = post_to_ui
        self._on_switch = on_switch
        self._q = queue.Queue()
        self._switcher = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def send(self, cmd, arg=None, cb=None):
        self._q.put((cmd, arg, cb))

    def close(self, timeout=2.0):
        """Stop the engine thread and wait briefly for cleanup."""
        self.send("quit")
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _reply(self, cb, ok, payload=None):
        if cb:
            self.post_to_ui(cb, ok, payload)

    def _notify_switch(self, slot):
        self.post_to_ui(self._on_switch, slot)

    def _run(self):
        CoInitialize()
        try:
            com_enum = create_com_enumerator()
            while True:
                cmd, arg, cb = self._q.get()

                if cmd == "quit":
                    if self._switcher:
                        self._switcher.stop()
                        self._switcher = None
                    break

                try:
                    if cmd == "start":
                        slot_devices, output_index, samplerate, channels, buffer_blocks, initial_slot = arg
                        switcher = AudioSwitcher(
                            slot_devices, output_index, samplerate, channels,
                            buffer_blocks=buffer_blocks, on_switch=self._notify_switch,
                            initial_slot=initial_slot,
                        )
                        switcher.start()
                        self._switcher = switcher
                        self._reply(cb, True)
                    elif cmd == "stop":
                        if self._switcher:
                            self._switcher.stop()
                            self._switcher = None
                        self._reply(cb, True)
                    elif cmd == "swap":
                        slot, dev_index = arg
                        if self._switcher:
                            self._switcher.swap_device(slot, dev_index)
                            self._switcher.set_active_slot(slot)
                        self._reply(cb, True)
                    elif cmd == "toggle":
                        if self._switcher:
                            self._switcher.toggle()
                        self._reply(cb, True)
                    elif cmd == "enumerate_endpoints":
                        result = {
                            "input": enumerate_com_endpoints(com_enum, EDATAFLOW_CAPTURE),
                            "output": enumerate_com_endpoints(com_enum, EDATAFLOW_RENDER),
                        }
                        self._reply(cb, True, result)
                except Exception as e:
                    self._reply(cb, False, str(e))
        finally:
            CoUninitialize()

    def snapshot(self):
        s = self._switcher
        if not s:
            return None
        return {
            "active_slot": s.active_slot,
            "slot_names": dict(s.slot_names),
            "output_name": s.output_name,
        }


# ============================================================
# 7. GUI (widget construction + event wiring only)
# ============================================================

class SwitcherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            self.iconbitmap(resource_path("AudioSwitch.ico"))
        except tk.TclError:
            pass
        self.title(f"AudioSwitch - v{VERSION}")
        self.resizable(False, False)

        self.prefs = load_prefs()
        self.running = False
        self.capturing = False
        self._save_after_id = None  # debounce timer for _schedule_save()
        self.toggle_hotkey = self.prefs.get("toggle_hotkey")
        self._active_slot = self.prefs.get("active_slot") if self.prefs.get("active_slot") in ("A", "B") else "A"

        self._current_device_index = {"A": None, "B": None}
        self._current_output_index = None
        self._input_devices = []
        self._output_devices = []

        post_to_ui = lambda fn, *args: self.after(0, fn, *args)

        self.engine = AudioEngine(self._handle_slot_switch, post_to_ui)
        self.engine.start()

        self.hotkey_enabled_var = tk.BooleanVar(value=self.prefs.get("hotkey_enabled", True))
        self.hook = InputHook(self._on_input_label, post_to_ui)
        if HAVE_PYNPUT and self.hotkey_enabled_var.get():
            self.hook.start()

        self.overlay = Overlay(self)
        overlay_position = self.prefs.get("overlay_position", DEFAULT_OVERLAY_POSITION)
        self.overlay.set_position(overlay_position)
        overlay_offset_x = self.prefs.get("overlay_offset_x", DEFAULT_OVERLAY_OFFSET)
        overlay_offset_y = self.prefs.get("overlay_offset_y", DEFAULT_OVERLAY_OFFSET)
        self.overlay.set_offset(overlay_offset_x, overlay_offset_y)
        overlay_color_a = self.prefs.get("overlay_color_a", DEFAULT_OVERLAY_COLORS["A"])
        overlay_color_b = self.prefs.get("overlay_color_b", DEFAULT_OVERLAY_COLORS["B"])
        self.overlay.set_color("A", overlay_color_a)
        self.overlay.set_color("B", overlay_color_b)
        self.overlay.set_slot(self._active_slot)
        self.overlay_enabled_var = tk.BooleanVar(value=self.prefs.get("overlay_enabled", False))
        self.overlay_position_var = tk.StringVar(value=overlay_position)
        self.overlay_offset_x_var = tk.IntVar(value=overlay_offset_x)
        self.overlay_offset_y_var = tk.IntVar(value=overlay_offset_y)
        self.overlay_offset_x_var.trace_add("write", lambda *_: self._on_overlay_offset_changed())
        self.overlay_offset_y_var.trace_add("write", lambda *_: self._on_overlay_offset_changed())
        self.overlay_color_a_var = tk.StringVar(value=overlay_color_a)
        self.overlay_color_b_var = tk.StringVar(value=overlay_color_b)

        self._build_ui()
        self._refresh_devices()
        self._fit()

        if self.toggle_hotkey:
            self.hotkey_var.set(f"Toggle hotkey: {disp(self.toggle_hotkey)}")

    def _fit(self):
        self.update_idletasks()
        self.geometry(f"{self.winfo_reqwidth()}x{self.winfo_reqheight()}")

    @staticmethod
    def _validate_positive_int(proposed):
        return proposed == "" or proposed.isdigit()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        vcmd = (self.register(self._validate_positive_int), "%P")

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="Refresh devices", command=self._refresh_devices).pack(side="left")

        panes = ttk.Frame(self)
        panes.pack(fill="x", padx=10, pady=(4, 10))
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=1)

        self.pane_a = self._build_pane(panes, "A")
        self.pane_a["frame"].grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        self.pane_b = self._build_pane(panes, "B")
        self.pane_b["frame"].grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        hk_frame = ttk.Frame(self)
        hk_frame.pack(fill="x", padx=10, pady=(0, 10))
        self.hotkey_var = tk.StringVar(value="Toggle hotkey: none")
        ttk.Label(hk_frame, textvariable=self.hotkey_var).pack(side="left")
        self.hotkey_button = tk.Button(
            hk_frame, text="Set Toggle Hotkey...", command=self._begin_capture
        )
        self.hotkey_button.pack(side="right")
        self.hotkey_enabled_check = ttk.Checkbutton(
            hk_frame, text="Enable hotkey", variable=self.hotkey_enabled_var,
            command=self._on_hotkey_enabled_toggle,
        )
        self.hotkey_enabled_check.pack(side="right", padx=(0, 8))
        if not HAVE_PYNPUT:
            self.hotkey_button.config(state="disabled")
            self.hotkey_enabled_check.config(state="disabled")
        elif not self.hotkey_enabled_var.get():
            self.hotkey_button.config(state="disabled")

        overlay_frame = ttk.LabelFrame(self, text="Overlay")
        overlay_frame.pack(fill="x", padx=10, pady=(0, 10))

        left_col = ttk.Frame(overlay_frame)
        left_col.pack(side="left", padx=8, pady=8)

        left_row1 = ttk.Frame(left_col)
        left_row1.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(
            left_row1, text="Show overlay", variable=self.overlay_enabled_var,
            command=self._on_overlay_enabled_toggle,
        ).pack(side="left")
        ttk.Label(left_row1, text="Position:").pack(side="left", padx=(12, 4))
        overlay_combo = ttk.Combobox(
            left_row1, state="readonly", width=14,
            values=OVERLAY_POSITIONS, textvariable=self.overlay_position_var,
        )
        overlay_combo.pack(side="left")
        overlay_combo.bind("<<ComboboxSelected>>", lambda e: self._on_overlay_position_changed())

        left_row2 = ttk.Frame(left_col)
        left_row2.pack(fill="x")
        ttk.Label(left_row2, text="A:").pack(side="left")
        self.overlay_color_a_button = tk.Button(
            left_row2, text="Color...", command=lambda: self._on_overlay_color_clicked("A"),
            bg=self.overlay_color_a_var.get(),
        )
        self.overlay_color_a_button.pack(side="left", padx=(2, 12))
        ttk.Label(left_row2, text="B:").pack(side="left")
        self.overlay_color_b_button = tk.Button(
            left_row2, text="Color...", command=lambda: self._on_overlay_color_clicked("B"),
            bg=self.overlay_color_b_var.get(),
        )
        self.overlay_color_b_button.pack(side="left", padx=(2, 0))

        offset_col = ttk.Frame(overlay_frame)
        offset_col.pack(side="left", padx=(20, 8), pady=8)

        offset_row_x = ttk.Frame(offset_col)
        offset_row_x.pack(fill="x", pady=(0, 4))
        ttk.Label(offset_row_x, text="X offset:").pack(side="left")
        ttk.Entry(
            offset_row_x, textvariable=self.overlay_offset_x_var, width=5,
            validate="key", validatecommand=vcmd,
        ).pack(side="left", padx=(4, 0))

        offset_row_y = ttk.Frame(offset_col)
        offset_row_y.pack(fill="x")
        ttk.Label(offset_row_y, text="Y offset:").pack(side="left")
        ttk.Entry(
            offset_row_y, textvariable=self.overlay_offset_y_var, width=5,
            validate="key", validatecommand=vcmd,
        ).pack(side="left", padx=(4, 0))

        output_frame = self._build_output_section(self)
        output_frame.pack(fill="x", padx=10, pady=(0, 10))

        settings = ttk.Frame(self)
        settings.pack(fill="x", **pad)
        ttk.Label(settings, text="Sample rate:").pack(side="left")
        self.samplerate_var = tk.IntVar(value=self.prefs.get("samplerate", DEFAULT_SAMPLE_RATE))
        ttk.Entry(settings, textvariable=self.samplerate_var, width=8,
                  validate="key", validatecommand=vcmd).pack(side="left", padx=(4, 12))
        ttk.Label(settings, text="Channels:").pack(side="left")
        self.channels_var = tk.IntVar(value=self.prefs.get("channels", DEFAULT_CHANNELS))
        ttk.Entry(settings, textvariable=self.channels_var, width=4,
                  validate="key", validatecommand=vcmd).pack(side="left", padx=(4, 12))
        ttk.Label(settings, text="Buffer (blocks):").pack(side="left")
        self.buffer_blocks_var = tk.IntVar(value=self.prefs.get("buffer_blocks", DEFAULT_BUFFER_BLOCKS))
        ttk.Entry(settings, textvariable=self.buffer_blocks_var, width=4,
                  validate="key", validatecommand=vcmd).pack(side="left", padx=4)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)
        self.start_button = ttk.Button(ctrl, text="Start routing", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(ctrl, text="Stop", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.toggle_button = ttk.Button(
            ctrl, text="Switch Source", command=self._toggle_source, state="disabled"
        )
        self.toggle_button.pack(side="left")

        ttk.Separator(self).pack(fill="x", padx=10, pady=8)

        self.status_var = tk.StringVar(value="Not running.")
        ttk.Label(self, textvariable=self.status_var, foreground="#555").pack(
            anchor="w", padx=10, pady=(0, 6)
        )

        if not HAVE_PYNPUT:
            ttk.Label(
                self, text="Note: 'pynput' package not installed - hotkey disabled.",
                foreground="#a33",
            ).pack(anchor="w", padx=10)

        if not HAVE_COM:
            ttk.Label(
                self,
                text="Note: 'pycaw'/'comtypes' not installed - device matching falls back "
                     "to name/type text instead of the real device ID.",
                foreground="#a33",
            ).pack(anchor="w", padx=10)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_pane(self, parent, slot):
        frame = ttk.LabelFrame(parent, text=f"Source {slot}")

        filter_row = ttk.Frame(frame)
        filter_row.pack(fill="x", padx=8, pady=(8, 4))
        search_var = tk.StringVar()
        search_var.trace_add("write", lambda *_, s=slot: self._apply_filter(s))
        ttk.Entry(filter_row, textvariable=search_var).pack(side="left", fill="x", expand=True)

        type_combo = ttk.Combobox(filter_row, state="readonly", width=25)
        type_combo.pack(side="left", padx=(4, 0))
        type_combo.bind("<<ComboboxSelected>>", lambda e, s=slot: self._apply_filter(s))

        combo = ttk.Combobox(frame, state="readonly", width=44)
        combo.pack(fill="x", padx=8, pady=(0, 8))
        combo.bind("<<ComboboxSelected>>", lambda e, s=slot: self._on_device_selected(s))

        return {
            "frame": frame, "combo": combo, "map": {},
            "search_var": search_var, "type_combo": type_combo,
        }

    def _build_output_section(self, parent):
        frame = ttk.LabelFrame(parent, text="Output target (your virtual cable)")

        filter_row = ttk.Frame(frame)
        filter_row.pack(fill="x", padx=8, pady=(8, 4))
        search_var = tk.StringVar()
        search_var.trace_add("write", lambda *_: self._apply_filter("output"))
        ttk.Entry(filter_row, textvariable=search_var).pack(side="left", fill="x", expand=True)

        type_combo = ttk.Combobox(filter_row, state="readonly", width=25)
        type_combo.pack(side="left", padx=(4, 0))
        type_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filter("output"))

        combo = ttk.Combobox(frame, state="readonly", width=80)
        combo.pack(fill="x", padx=8, pady=(0, 8))
        combo.bind("<<ComboboxSelected>>", lambda e: self._on_output_selected())

        self.output_combo = combo
        self.output_ctrl = {
            "frame": frame, "combo": combo, "map": {},
            "search_var": search_var, "type_combo": type_combo,
        }
        return frame

    def _refresh_devices(self):
        self.engine.send("enumerate_endpoints", cb=self._on_endpoints_ready)

    def _on_endpoints_ready(self, ok, result):
        endpoint_maps = result if ok else {}
        devices = sd.query_devices()
        self._input_devices = build_device_labels(devices, "input", endpoint_maps.get("input"))
        self._output_devices = build_device_labels(devices, "output", endpoint_maps.get("output"))

        self._current_device_index["A"] = resolve_selection(
            self._current_device_index.get("A"), self.prefs.get("source_a"), self._input_devices, 0
        )
        self._current_device_index["B"] = resolve_selection(
            self._current_device_index.get("B"), self.prefs.get("source_b"), self._input_devices, 1
        )
        self._current_output_index = resolve_selection(
            self._current_output_index, self.prefs.get("output"), self._output_devices, 0
        )

        input_hostapis = ["All"] + sorted({h for _, _, _, h, _ in self._input_devices})
        output_hostapis = ["All"] + sorted({h for _, _, _, h, _ in self._output_devices})
        self._populate_type_filter(self.pane_a["type_combo"], input_hostapis)
        self._populate_type_filter(self.pane_b["type_combo"], input_hostapis)
        self._populate_type_filter(self.output_ctrl["type_combo"], output_hostapis)

        self._apply_filter("A")
        self._apply_filter("B")
        self._apply_filter("output")

    @staticmethod
    def _populate_type_filter(combo, hostapi_list):
        current = combo.get()
        combo["values"] = hostapi_list
        if current in hostapi_list:
            combo.set(current)
            return
        combo.set(next((h for h in hostapi_list if "wasapi" in h.lower()), "All"))

    def _ctrl_for(self, which):
        """Return the controls, devices, and selection for a source or output."""
        if which == "A":
            return self.pane_a, self._input_devices, self._current_device_index.get("A")
        if which == "B":
            return self.pane_b, self._input_devices, self._current_device_index.get("B")
        return self.output_ctrl, self._output_devices, self._current_output_index

    def _apply_filter(self, which):
        ctrl, devices, current_idx = self._ctrl_for(which)
        q = ctrl["search_var"].get().strip().lower()
        type_filter = ctrl["type_combo"].get()
        filtered = [
            e for e in devices
            if (not q or q in e[1].lower()) and (type_filter in ("All", "") or e[3] == type_filter)
        ]
        self._populate_combo(ctrl, filtered, devices, current_idx)

    @staticmethod
    def _populate_combo(ctrl, filtered, full_list, current_idx):
        mapping = {label: idx for idx, label, _, _, _ in filtered}
        values = [label for idx, label, _, _, _ in filtered]
        cur_label = label_for_index(full_list, current_idx) if current_idx is not None else None
        if cur_label and cur_label not in mapping:
            values.append(cur_label)
            mapping[cur_label] = current_idx
        ctrl["map"] = mapping
        ctrl["combo"]["values"] = values
        ctrl["combo"].set(cur_label if cur_label else (values[0] if values else ""))

    def _on_device_selected(self, slot):
        pane = self.pane_a if slot == "A" else self.pane_b
        dev_index = pane["map"].get(pane["combo"].get())
        if dev_index is None:
            return
        self._current_device_index[slot] = dev_index
        self._save()

        if self.running:
            self.status_var.set(f"Switching Source {slot}...")

            def done(ok, error):
                if not ok:
                    messagebox.showerror("Failed to switch device", explain_stream_error(error))
                    self.status_var.set("Device switch failed - see error.")

            self.engine.send("swap", (slot, dev_index), cb=done)

    def _on_output_selected(self):
        dev_index = self.output_ctrl["map"].get(self.output_combo.get())
        if dev_index is None:
            return
        self._current_output_index = dev_index
        self._save()

    def _start(self):
        index_a = self._current_device_index.get("A")
        index_b = self._current_device_index.get("B")
        if index_a is None or index_b is None:
            messagebox.showwarning("Pick sources", "Select a device for both Source A and Source B.")
            return
        if index_a == index_b:
            messagebox.showwarning("Pick different devices", "Source A and Source B must be different devices.")
            return
        output_index = self._current_output_index
        if output_index is None:
            messagebox.showwarning("Pick target", "Select an output target device.")
            return

        try:
            samplerate = int(self.samplerate_var.get())
            channels = int(self.channels_var.get())
            buffer_blocks = int(self.buffer_blocks_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Invalid settings", "Sample rate, channels, and buffer must be numbers.")
            return

        if samplerate < 1:
            samplerate = 1
            self.samplerate_var.set(1)
        if channels < 1:
            channels = 1
            self.channels_var.set(1)
        if buffer_blocks < 1:
            buffer_blocks = 1
            self.buffer_blocks_var.set(1)

        self.start_button.config(state="disabled")
        self.status_var.set("Starting...")

        def done(ok, error):
            if not ok:
                messagebox.showerror("Failed to start", explain_stream_error(error))
                self.start_button.config(state="normal")
                self.status_var.set("Not running.")
                return
            self.running = True
            self.stop_button.config(state="normal")
            self.toggle_button.config(state="normal")
            self.output_combo.config(state="disabled")
            if self.overlay_enabled_var.get():
                self.overlay.show()
            self._save()
            self._refresh_status()

        self.engine.send(
            "start",
            ({"A": index_a, "B": index_b}, output_index, samplerate, channels, buffer_blocks, self._active_slot),
            cb=done,
        )

    def _stop(self):
        self.stop_button.config(state="disabled")
        self.toggle_button.config(state="disabled")

        def done(ok, error):
            self.running = False
            self.start_button.config(state="normal")
            self.output_combo.config(state="readonly")
            self.status_var.set("Not running.")
            self.overlay.hide()
            for s, pane in (("A", self.pane_a), ("B", self.pane_b)):
                pane["frame"].config(text=f"Source {s}")

        self.engine.send("stop", cb=done)

    def _on_close(self):
        self.hook.stop()
        self.overlay.hide()
        self.engine.close()
        if self._save_after_id is not None:
            self.after_cancel(self._save_after_id)
            self._save_after_id = None
        self._save()
        self.destroy()

    def _handle_slot_switch(self, slot):
        self._active_slot = slot
        self.overlay.set_slot(slot)
        self._refresh_status()

    def _refresh_status(self):
        snap = self.engine.snapshot()
        if not snap:
            return
        slot = snap["active_slot"]
        active_name = snap["slot_names"].get(slot) or "?"
        output_name = snap["output_name"] or "?"
        self.status_var.set(f"Active: [{slot}] {active_name}   ->   Target: {output_name}")
        for s, pane in (("A", self.pane_a), ("B", self.pane_b)):
            suffix = "  \u25cf ACTIVE" if s == slot else ""
            pane["frame"].config(text=f"Source {s}{suffix}")

    def _begin_capture(self):
        if not HAVE_PYNPUT or self.capturing:
            return
        self.capturing = True
        self.hotkey_button.config(text="Press a key...", bg=CAPTURING_BG, state="disabled")

    def _on_hotkey_enabled_toggle(self):
        enabled = self.hotkey_enabled_var.get()
        if enabled:
            self.hook.start()
            self.hotkey_button.config(state="normal")
        else:
            if self.capturing:
                self.capturing = False
                self.hotkey_button.config(text="Set Toggle Hotkey...", bg="SystemButtonFace")
            self.hook.stop()
            self.hotkey_button.config(state="disabled")
        self._save()

    def _on_overlay_enabled_toggle(self):
        if self.overlay_enabled_var.get() and self.running:
            self.overlay.show()
        else:
            self.overlay.hide()
        self._save()

    def _on_overlay_position_changed(self):
        self.overlay.set_position(self.overlay_position_var.get())
        self._save()

    def _on_overlay_offset_changed(self):
        x = self._int_var_value_min0(self.overlay_offset_x_var, DEFAULT_OVERLAY_OFFSET)
        y = self._int_var_value_min0(self.overlay_offset_y_var, DEFAULT_OVERLAY_OFFSET)
        self.overlay.set_offset(x, y)
        self._schedule_save()

    def _schedule_save(self, delay_ms=400):
        """Debounce preference saves triggered by rapid UI changes."""
        if self._save_after_id is not None:
            self.after_cancel(self._save_after_id)
        self._save_after_id = self.after(delay_ms, self._run_scheduled_save)

    def _run_scheduled_save(self):
        self._save_after_id = None
        self._save()

    def _on_overlay_color_clicked(self, slot):
        var = self.overlay_color_a_var if slot == "A" else self.overlay_color_b_var
        button = self.overlay_color_a_button if slot == "A" else self.overlay_color_b_button
        _rgb, hex_color = colorchooser.askcolor(
            color=var.get(), title=f"Source {slot} overlay color"
        )
        if hex_color is None:
            return
        var.set(hex_color)
        button.config(bg=hex_color)
        self.overlay.set_color(slot, hex_color)
        self._save()

    def _on_input_label(self, label):
        if not label:
            return
        if self.capturing:
            self.capturing = False
            self.hotkey_button.config(text="Set Toggle Hotkey...", bg="SystemButtonFace", state="normal")
            self.toggle_hotkey = label
            self.hotkey_var.set(f"Toggle hotkey: {disp(label)}")
            self._save()
            return
        if self.toggle_hotkey and label == self.toggle_hotkey and self.running:
            self.engine.send("toggle")

    def _toggle_source(self):
        if self.running:
            self.engine.send("toggle")

    def _int_var_value(self, var, default):
        try:
            return max(1, int(var.get()))
        except (tk.TclError, ValueError):
            return default

    def _int_var_value_min0(self, var, default):
        try:
            return max(0, int(var.get()))
        except (tk.TclError, ValueError):
            return default

    def _save(self):
        data = {
            "source_a": entry_for_index(self._current_device_index.get("A"), self._input_devices),
            "source_b": entry_for_index(self._current_device_index.get("B"), self._input_devices),
            "output": entry_for_index(self._current_output_index, self._output_devices),
            "toggle_hotkey": self.toggle_hotkey,
            "samplerate": self._int_var_value(self.samplerate_var, DEFAULT_SAMPLE_RATE),
            "channels": self._int_var_value(self.channels_var, DEFAULT_CHANNELS),
            "buffer_blocks": self._int_var_value(self.buffer_blocks_var, DEFAULT_BUFFER_BLOCKS),
            "active_slot": self._active_slot,
            "hotkey_enabled": self.hotkey_enabled_var.get(),
            "overlay_enabled": self.overlay_enabled_var.get(),
            "overlay_position": self.overlay_position_var.get(),
            "overlay_color_a": self.overlay_color_a_var.get(),
            "overlay_color_b": self.overlay_color_b_var.get(),
            "overlay_offset_x": self._int_var_value_min0(self.overlay_offset_x_var, DEFAULT_OVERLAY_OFFSET),
            "overlay_offset_y": self._int_var_value_min0(self.overlay_offset_y_var, DEFAULT_OVERLAY_OFFSET),
        }
        save_prefs(data)
        self.prefs = data


if __name__ == "__main__":
    app = SwitcherApp()
    app.mainloop()