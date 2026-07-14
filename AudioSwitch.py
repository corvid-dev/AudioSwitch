#!/usr/bin/env python3
"""
Audio A/B Switcher with a Tkinter interface.
Routes either of two live inputs to one output device.
Implements a global push to talk (PTT) system.
"""

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
    from pycaw.pycaw import IMMDeviceEnumerator, IAudioEndpointVolume
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

VERSION = "1.1"

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
BLOCK_SIZE = 1024
DTYPE = "float32"
DEFAULT_BUFFER_BLOCKS = 4

CAPTURING_BG = "#f9a825"

DEFAULT_PTT_DELAY_MS = 250
MAX_PTT_DELAY_MS = 2000
PTT_FOLLOW_OUTPUT_LABEL = "Same as Output target"

OVERLAY_POSITIONS = ("Top-left", "Top-right", "Bottom-left", "Bottom-right")
DEFAULT_OVERLAY_POSITION = "Top-right"
DEFAULT_OVERLAY_OFFSET = 24
DEFAULT_OVERLAY_LIVE_COLORS = {"A": "#00e5ff", "B": "#ff9100"}
DEFAULT_OVERLAY_MUTED_COLORS = {"A": "#8a1f1f", "B": "#8a1f1f"}

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


_friendly_name_cache = {}  # eid -> name, process-lifetime; only touched from the engine thread


def _endpoint_friendly_name(ep, eid=None):
    """Look up an endpoint's friendly name via its property store.
    Cached by endpoint ID once resolved, since the property-store round
    trip is the expensive part of every device refresh - not the small
    in-process loop over an endpoint's properties. Trade-off: if a device
    is renamed in Windows without its endpoint ID changing, the old name
    sticks around until the app restarts; that's rare enough to accept."""
    if eid is not None and eid in _friendly_name_cache:
        return _friendly_name_cache[eid]
    try:
        store = ep.OpenPropertyStore(0)
        for i in range(store.GetCount()):
            pk = store.GetAt(i)
            if pk.fmtid == _PKEY_NAME and pk.pid == 14:
                name = str(store.GetValue(pk).GetValue())
                if eid is not None:
                    _friendly_name_cache[eid] = name
                return name
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
                name = _endpoint_friendly_name(ep, eid)
                if name:
                    result[eid] = name
            except Exception:
                continue
    except Exception:
        pass
    return result


def merge_ptt_endpoints(capture_map, render_map):
    """Combine the capture and render endpoint maps enumerate_endpoints()
    already fetches into one PTT device list - this is the union that a
    dedicated eAll COM traversal would return, but without paying for a
    third endpoint enumeration on every device refresh. Entries with a
    shared friendly name get numbered, matching GlobalPTT's own list.
    NOTE: relies on capture_map/render_map always coming from the same
    enumerate_endpoints() call (see engine cmd 'enumerate_endpoints') - if
    that ever changes to fetch them independently, this stops being free."""
    combined = {}
    combined.update(capture_map or {})
    combined.update(render_map or {})  # endpoint IDs never collide across flows
    result = {}
    name_counts = {}
    for eid, name in combined.items():
        if not name:
            continue
        name_counts[name] = name_counts.get(name, 0) + 1
        result[eid] = f"[{name_counts[name]}] {name}" if name_counts[name] > 1 else name
    return result


def build_device_labels(devices, kind, endpoint_map=None):
    """Build labeled input or output device entries.
    Includes host API details and endpoint IDs when available."""
    endpoint_map = endpoint_map or {}
    hostapis = sd.query_hostapis()

    # Reverse lookup built once, instead of rescanning endpoint_map for
    # every WASAPI device below. First occurrence wins on a name collision,
    # matching the previous next(...)-based scan's behavior.
    name_to_eid = {}
    for eid, ename in endpoint_map.items():
        if ename not in name_to_eid:
            name_to_eid[ename] = eid

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
            endpoint_id = name_to_eid.get(name)
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
    Posts input events back to the GUI thread.

    on_press_label(label) fires once per new press (existing hotkey/capture behavior).
    on_key_event(label, pressed), if given, fires on every press AND release -
    used for push-to-talk hold detection."""

    def __init__(self, on_press_label, post_to_ui, on_key_event=None):
        self.on_press_label = on_press_label
        self.post_to_ui = post_to_ui
        self.on_key_event = on_key_event
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
            self.post_to_ui(self.on_press_label, label)
        else:
            if label not in self._pressed:
                return
            self._pressed.discard(label)
        if self.on_key_event:
            # Called directly on this input thread (not via post_to_ui/Tk's
            # after(0)) - on_key_event must be thread-safe. This keeps PTT
            # hold detection off the Tk mainloop entirely, since bouncing
            # every key/mouse event through Tk (even ones PTT doesn't care
            # about) adds queuing latency that's very noticeable during
            # fast keyboard/mouse activity like gaming.
            self.on_key_event(label, pressed)

    def stop(self):
        if self._kb_listener:
            self._kb_listener.stop()
            self._kb_listener = None
        if self._ms_listener:
            self._ms_listener.stop()
            self._ms_listener = None


# ============================================================
# 4b. Push-to-talk hold logic (no tkinter dependency)
# ============================================================

class PushToTalk:
    """Hold-to-unmute gate for the output target device.
    Mutes the output endpoint except while one of its bound keys/buttons is
    held, same arm/disarm/release-delay behavior as GlobalPTT's per-channel
    gate, but applied to AudioSwitch's single output target."""

    def __init__(self, engine, post_to_ui):
        self._engine = engine
        self._post_to_ui = post_to_ui
        self.keybinds = []
        self.delay_ms = DEFAULT_PTT_DELAY_MS
        self._active = set()
        self._talking = False
        self._timer = None
        self._lock = threading.Lock()
        self.on_status_change = None

    def set_delay(self, ms):
        with self._lock:
            self.delay_ms = max(0, min(MAX_PTT_DELAY_MS, ms))

    def handle_key(self, label, pressed):
        if label not in self.keybinds:
            return
        with self._lock:
            if pressed:
                self._active.add(label)
                should_arm = not self._talking
            else:
                self._active.discard(label)
                should_arm = False
        if should_arm:
            self._arm()
        elif not pressed:
            self._maybe_disarm()

    def key_removed(self, label):
        """Force-release a key that was just unbound, in case it's still held."""
        with self._lock:
            self._active.discard(label)
        self._maybe_disarm()

    def _arm(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._talking = True
        self._engine.send("ptt_mute", False)
        self._notify("LIVE")

    def _maybe_disarm(self):
        with self._lock:
            if self._active or not self._talking:
                return
            if self._timer:
                self._timer.cancel()
                self._timer = None
            delay = self.delay_ms
        if delay <= 0:
            self._silence(None)
        else:
            t = threading.Timer(delay / 1000.0, lambda: self._silence(t))
            t.daemon = True
            with self._lock:
                self._timer = t
            t.start()

    def _silence(self, timer):
        with self._lock:
            if timer is not None and timer is not self._timer:
                return
            self._timer = None
            if self._active or not self._talking:
                return
            self._talking = False
        self._engine.send("ptt_mute", True)
        self._notify("MUTED")

    def _notify(self, text):
        if self.on_status_change:
            self._post_to_ui(self.on_status_change, text)

    def cancel(self):
        """Stop any pending timer and clear held-key state, e.g. on app close."""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._talking = False
            self._active.clear()


# ============================================================
# 5. Screen overlay (Windows only; degrades to a plain window elsewhere)
# ============================================================

class Overlay:
    """Show a colored square for the active source. Each of A and B has its
    own muted color and live color; while push-to-talk is enabled the square
    picks between the active slot's muted/live color based on gate state.
    Uses Windows click-through and capture-exclusion features when available."""

    _SQUARE_SIZE = 12

    def __init__(self, root):
        self._root = root
        self._win = None
        self._square = None
        self._position = DEFAULT_OVERLAY_POSITION
        self._offset_x = DEFAULT_OVERLAY_OFFSET
        self._offset_y = DEFAULT_OVERLAY_OFFSET
        self._colors = {
            "A": {"muted": DEFAULT_OVERLAY_MUTED_COLORS["A"], "live": DEFAULT_OVERLAY_LIVE_COLORS["A"]},
            "B": {"muted": DEFAULT_OVERLAY_MUTED_COLORS["B"], "live": DEFAULT_OVERLAY_LIVE_COLORS["B"]},
        }
        self._slot = "A"
        self._ptt_mode = False
        self._ptt_muted = True

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
        self._refresh_color()

    def set_color(self, slot, mode, color):
        """mode is 'muted' or 'live'."""
        self._colors.setdefault(slot, {})[mode] = color
        self._refresh_color()

    def set_ptt_mode(self, enabled):
        """While enabled, the muted color is used instead of the live color
        whenever the gate is currently muted."""
        self._ptt_mode = enabled
        self._refresh_color()

    def set_ptt_muted(self, muted):
        self._ptt_muted = muted
        self._refresh_color()

    def _current_color(self):
        effective_muted = self._ptt_mode and self._ptt_muted
        mode = "muted" if effective_muted else "live"
        default = DEFAULT_OVERLAY_MUTED_COLORS if mode == "muted" else DEFAULT_OVERLAY_LIVE_COLORS
        slot_colors = self._colors.get(self._slot, {})
        return slot_colors.get(mode, default.get(self._slot, "#ffffff"))

    def _refresh_color(self):
        if self._square is not None:
            self._square.config(bg=self._current_color())

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
            win, bg=self._current_color(),
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
        # (slot, generation) - the ONLY thing the real-time output callback
        # reads to decide what to play. Written wholesale (one atomic
        # reference swap) by set_active_slot()/toggle(), which both run on
        # the engine thread - never inside the callback. This intentionally
        # avoids a lock in the audio callback: acquiring a lock there could
        # block the real-time thread on contention with the engine thread
        # and cause audible dropouts. _drained_generation belongs solely to
        # the callback thread - nothing else touches it.
        self._switch_state = (self.active_slot, 0)
        self._drained_generation = 0

        self.buffers = {"A": None, "B": None}
        self.input_streams = {"A": None, "B": None}
        self.output_stream = None
        self.output_channels = None

        self.slot_names = {"A": None, "B": None}
        self.output_name = None

        self._mix_scratch = None
        self._mono_scratch = None
        self._pad_scratch = None
        self._converters = {"A": None, "B": None}  # per-slot channel-conversion function, precomputed on open/swap

    @staticmethod
    def _resolve_device(device_index, requested, is_input):
        """Return the usable channel count and device name."""
        info = sd.query_devices(device_index)
        device_max = info["max_input_channels"] if is_input else info["max_output_channels"]
        if device_max <= 0:
            raise ValueError(f"'{info['name']}' has no {'input' if is_input else 'output'} channels")
        return max(1, min(requested, device_max)), info["name"]

    def _build_channel_converter(self, src, target):
        """Return a channel-conversion function specialized for one (src,
        target) pair, computed once when a slot's stream opens or swaps -
        not re-decided (equal/upmix/downmix branch) on every audio block."""
        if src == target:
            return lambda block: block
        if src < target:
            reps, rem = divmod(target, src)

            def upmix(block):
                frames = block.shape[0]
                out = self._mix_scratch[:frames]
                for i in range(reps):
                    out[:, i * src:(i + 1) * src] = block
                if rem:
                    out[:, reps * src:reps * src + rem] = block[:, :rem]
                return out

            return upmix

        def downmix(block):
            frames = block.shape[0]
            mono = self._mono_scratch[:frames]
            block.mean(axis=1, out=mono)
            out = self._mix_scratch[:frames]
            out[:] = mono[:, None]
            return out

        return downmix

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
        return stream, name, ring, actual_channels

    def _open_slot_stream(self, slot):
        """Open and store one source stream during startup."""
        stream, name, ring, actual_channels = self._create_slot_stream(self.slot_devices[slot])
        self.input_streams[slot] = stream
        self.slot_names[slot] = name
        self.buffers[slot] = ring
        self._converters[slot] = self._build_channel_converter(actual_channels, self.output_channels)

    def _output_callback(self, outdata, frames, _time_info, _status):
        active, generation = self._switch_state  # single atomic reference read, no lock
        ring = self.buffers.get(active)
        if generation != self._drained_generation:
            self._drained_generation = generation
            if ring is not None:
                ring.drain_to_latest()
        block = ring.read_latest() if ring is not None else None
        if block is None:
            outdata[:] = 0
            return
        converter = self._converters.get(active)
        block = converter(block) if converter is not None else block
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
        self.active_slot = slot
        self._switch_state = (slot, self._switch_state[1] + 1)
        if self.on_switch:
            self.on_switch(slot)

    def toggle(self):
        slot = "B" if self._switch_state[0] == "A" else "A"
        self.active_slot = slot
        self._switch_state = (slot, self._switch_state[1] + 1)
        if self.on_switch:
            self.on_switch(slot)

    def swap_device(self, slot, new_device_index):
        """Replace one source device while preserving the old stream on failure."""
        stream, name, ring, actual_channels = self._create_slot_stream(new_device_index)

        old_stream = self.input_streams.get(slot)
        self.input_streams[slot] = stream
        self.slot_names[slot] = name
        self.buffers[slot] = ring
        self.slot_devices[slot] = new_device_index
        self._converters[slot] = self._build_channel_converter(actual_channels, self.output_channels)

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


class EndpointGate:
    """Mute/unmute a Windows audio endpoint via its EndpointVolume interface.
    Same technique GlobalPTT uses to gate a mic; here it's pointed at the
    output target endpoint instead. Must be used from the COM thread."""

    def __init__(self):
        self._vol = None
        self._orig_mute = False
        self._orig_vol = 1.0
        self._use_vol = False

    def activate(self, enum, ep_id):
        self.deactivate()
        if enum is None or not ep_id:
            return False
        try:
            vol = enum.GetDevice(ep_id).Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None
            ).QueryInterface(IAudioEndpointVolume)
            self._orig_mute = bool(vol.GetMute())
            self._orig_vol = vol.GetMasterVolumeLevelScalar()
            vol.SetMute(1, None)
            self._use_vol = not bool(vol.GetMute())
            vol.SetMute(0, None)
            self._vol = vol
            return True
        except Exception:
            self.deactivate()
            return False

    def set_mute(self, muted):
        if not self._vol:
            return
        try:
            if self._use_vol:
                self._vol.SetMasterVolumeLevelScalar(0.0 if muted else self._orig_vol, None)
            else:
                self._vol.SetMute(int(muted), None)
        except Exception:
            pass

    def deactivate(self):
        if not self._vol:
            return
        try:
            if self._use_vol:
                self._vol.SetMasterVolumeLevelScalar(self._orig_vol, None)
            else:
                self._vol.SetMute(int(self._orig_mute), None)
        except Exception:
            pass
        self._vol = None


class AudioEngine:
    """Run audio and COM work on a background thread.
    Uses a queue for commands and GUI callbacks."""

    def __init__(self, on_switch, post_to_ui):
        self.post_to_ui = post_to_ui
        self._on_switch = on_switch
        self._q = queue.SimpleQueue()
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
        ptt_gate = EndpointGate()
        try:
            com_enum = create_com_enumerator()
            while True:
                cmd, arg, cb = self._q.get()

                if cmd == "quit":
                    if self._switcher:
                        self._switcher.stop()
                        self._switcher = None
                    ptt_gate.deactivate()
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
                    elif cmd == "ptt_attach":
                        ok = ptt_gate.activate(com_enum, arg)
                        if ok:
                            ptt_gate.set_mute(True)  # PTT starts muted until a key is held
                        self._reply(cb, ok)
                    elif cmd == "ptt_detach":
                        ptt_gate.deactivate()
                        self._reply(cb, True)
                    elif cmd == "ptt_mute":
                        ptt_gate.set_mute(bool(arg))
                        self._reply(cb, True)
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
        self._auto_start_pending = bool(self.prefs.get("was_running", False))

        self._current_device_index = {"A": None, "B": None}
        self._current_output_index = None
        self._input_devices = []
        self._output_devices = []

        post_to_ui = lambda fn, *args: self.after(0, fn, *args)

        self.engine = AudioEngine(self._handle_slot_switch, post_to_ui)
        self.engine.start()

        self.ptt = PushToTalk(self.engine, post_to_ui)
        self.ptt.keybinds = list(self.prefs.get("ptt_keybinds", []))
        self.ptt.set_delay(self.prefs.get("ptt_delay_ms", DEFAULT_PTT_DELAY_MS))
        self.ptt.on_status_change = self._on_ptt_status
        self.ptt_capturing = False
        self._ptt_attached_ep_id = None
        self._ptt_enabled_flag = bool(self.prefs.get("ptt_enabled", False))  # thread-safe mirror, see _on_ptt_key_event
        # PTT target: an endpoint ID (stable, covers render+capture - see
        # merge_ptt_endpoints), or None to follow Output target. Endpoint
        # IDs are already unique/stable so no fuzzy name matching is needed
        # the way source_a/source_b/output require.
        self._current_ptt_ep_id = self.prefs.get("ptt_target_ep_id")
        self._ptt_endpoints = {}  # eid -> display label, covers render+capture
        self.ptt_device_map = {}

        self.hotkey_enabled_var = tk.BooleanVar(value=self.prefs.get("hotkey_enabled", True))
        self.hook = InputHook(self._on_input_label, post_to_ui, on_key_event=self._on_ptt_key_event)
        if HAVE_PYNPUT and (self.hotkey_enabled_var.get() or self.prefs.get("ptt_enabled", False)):
            self.hook.start()

        self.overlay = Overlay(self)
        overlay_position = self.prefs.get("overlay_position", DEFAULT_OVERLAY_POSITION)
        self.overlay.set_position(overlay_position)
        overlay_offset_x = self.prefs.get("overlay_offset_x", DEFAULT_OVERLAY_OFFSET)
        overlay_offset_y = self.prefs.get("overlay_offset_y", DEFAULT_OVERLAY_OFFSET)
        self.overlay.set_offset(overlay_offset_x, overlay_offset_y)
        overlay_color_a_muted = self.prefs.get("overlay_color_a_muted", DEFAULT_OVERLAY_MUTED_COLORS["A"])
        overlay_color_a_live = self.prefs.get(
            "overlay_color_a_live", self.prefs.get("overlay_color_a", DEFAULT_OVERLAY_LIVE_COLORS["A"])
        )
        overlay_color_b_muted = self.prefs.get("overlay_color_b_muted", DEFAULT_OVERLAY_MUTED_COLORS["B"])
        overlay_color_b_live = self.prefs.get(
            "overlay_color_b_live", self.prefs.get("overlay_color_b", DEFAULT_OVERLAY_LIVE_COLORS["B"])
        )
        self.overlay.set_color("A", "muted", overlay_color_a_muted)
        self.overlay.set_color("A", "live", overlay_color_a_live)
        self.overlay.set_color("B", "muted", overlay_color_b_muted)
        self.overlay.set_color("B", "live", overlay_color_b_live)
        self.overlay.set_ptt_mode(self.prefs.get("ptt_enabled", False))
        self.overlay.set_slot(self._active_slot)
        self.overlay_enabled_var = tk.BooleanVar(value=self.prefs.get("overlay_enabled", False))
        self.overlay_position_var = tk.StringVar(value=overlay_position)
        self.overlay_offset_x_var = tk.IntVar(value=overlay_offset_x)
        self.overlay_offset_y_var = tk.IntVar(value=overlay_offset_y)
        self.overlay_offset_x_var.trace_add("write", lambda *_: self._on_overlay_offset_changed())
        self.overlay_offset_y_var.trace_add("write", lambda *_: self._on_overlay_offset_changed())
        self.overlay_color_a_muted_var = tk.StringVar(value=overlay_color_a_muted)
        self.overlay_color_a_live_var = tk.StringVar(value=overlay_color_a_live)
        self.overlay_color_b_muted_var = tk.StringVar(value=overlay_color_b_muted)
        self.overlay_color_b_live_var = tk.StringVar(value=overlay_color_b_live)

        self._build_ui()
        self._sync_input_hook()
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
            hk_frame, text="Enable switch hotkey", variable=self.hotkey_enabled_var,
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
        ttk.Label(left_row2, text="Muted A:", width=8, anchor="w").pack(side="left")
        self.overlay_color_a_muted_button = tk.Button(
            left_row2, text="Color...", command=lambda: self._on_overlay_color_clicked("A", "muted"),
            bg=self.overlay_color_a_muted_var.get(),
        )
        self.overlay_color_a_muted_button.pack(side="left", padx=(2, 12))
        ttk.Label(left_row2, text="Muted B:", width=8, anchor="w").pack(side="left")
        self.overlay_color_b_muted_button = tk.Button(
            left_row2, text="Color...", command=lambda: self._on_overlay_color_clicked("B", "muted"),
            bg=self.overlay_color_b_muted_var.get(),
        )
        self.overlay_color_b_muted_button.pack(side="left", padx=(2, 0))

        left_row3 = ttk.Frame(left_col)
        left_row3.pack(fill="x", pady=(4, 0))
        ttk.Label(left_row3, text="Live A:", width=8, anchor="w").pack(side="left")
        self.overlay_color_a_live_button = tk.Button(
            left_row3, text="Color...", command=lambda: self._on_overlay_color_clicked("A", "live"),
            bg=self.overlay_color_a_live_var.get(),
        )
        self.overlay_color_a_live_button.pack(side="left", padx=(2, 12))
        ttk.Label(left_row3, text="Live B:", width=8, anchor="w").pack(side="left")
        self.overlay_color_b_live_button = tk.Button(
            left_row3, text="Color...", command=lambda: self._on_overlay_color_clicked("B", "live"),
            bg=self.overlay_color_b_live_var.get(),
        )
        self.overlay_color_b_live_button.pack(side="left", padx=(2, 0))

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

        ptt_frame = ttk.LabelFrame(self, text="Push-to-Talk")
        ptt_frame.pack(fill="x", padx=10, pady=(0, 10))

        ptt_top = ttk.Frame(ptt_frame)
        ptt_top.pack(fill="x", padx=8, pady=(8, 4))
        self.ptt_enabled_var = tk.BooleanVar(value=self.prefs.get("ptt_enabled", False))
        self.ptt_enabled_check = ttk.Checkbutton(
            ptt_top, text="Enable push-to-talk", variable=self.ptt_enabled_var,
            command=self._on_ptt_enabled_toggle,
        )
        self.ptt_enabled_check.pack(side="left")
        self.ptt_status_var = tk.StringVar(value="INACTIVE")
        ttk.Label(ptt_top, textvariable=self.ptt_status_var, foreground="#555").pack(side="right")

        ptt_device_label = ttk.Label(ptt_frame, text="Target device:")
        ptt_device_label.pack(anchor="w", padx=8, pady=(0, 2))
        ptt_filter_row = ttk.Frame(ptt_frame)
        ptt_filter_row.pack(fill="x", padx=8, pady=(0, 4))
        ptt_search_var = tk.StringVar(value=self.prefs.get("ptt_search", ""))
        ptt_search_var.trace_add("write", lambda *_: self._on_ptt_search_changed())
        ttk.Entry(ptt_filter_row, textvariable=ptt_search_var).pack(side="left", fill="x", expand=True)

        self.ptt_device_combo = ttk.Combobox(ptt_frame, state="readonly", width=48)
        self.ptt_device_combo.pack(fill="x", padx=8, pady=(0, 4))
        self.ptt_device_combo.bind("<<ComboboxSelected>>", lambda e: self._on_ptt_device_selected())

        self.ptt_ctrl = {"search_var": ptt_search_var, "map": {}}

        ptt_keys_row = ttk.Frame(ptt_frame)
        ptt_keys_row.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(ptt_keys_row, text="Keys:").pack(side="left")
        self.ptt_add_button = tk.Button(
            ptt_keys_row, text="+ Add Key", command=self._begin_ptt_capture
        )
        self.ptt_add_button.pack(side="left", padx=(6, 0))
        self.ptt_binds_frame = ttk.Frame(ptt_frame)
        self.ptt_binds_frame.pack(fill="x", padx=8, pady=(0, 4))

        ptt_delay_row = ttk.Frame(ptt_frame)
        ptt_delay_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(ptt_delay_row, text="Release delay:").pack(side="left")
        self.ptt_delay_var = tk.IntVar(value=self.ptt.delay_ms)
        self.ptt_delay_label_var = tk.StringVar(value=f"{self.ptt.delay_ms} ms")
        ttk.Scale(
            ptt_delay_row, from_=0, to=MAX_PTT_DELAY_MS, orient="horizontal",
            variable=self.ptt_delay_var, command=self._on_ptt_delay_changed,
        ).pack(side="left", fill="x", expand=True, padx=(6, 8))
        ttk.Label(ptt_delay_row, textvariable=self.ptt_delay_label_var, width=7).pack(side="left")

        if not HAVE_PYNPUT or not HAVE_COM:
            self.ptt_enabled_check.config(state="disabled")
            self.ptt_add_button.config(state="disabled")
            self.ptt_device_combo.config(state="disabled")

        self._rebuild_ptt_binds()

        if not HAVE_PYNPUT:
            ttk.Label(
                self, text="Note: 'pynput' package not installed - hotkey and push-to-talk disabled.",
                foreground="#a33",
            ).pack(anchor="w", padx=10)

        if not HAVE_COM:
            ttk.Label(
                self,
                text="Note: 'pycaw'/'comtypes' not installed - device matching falls back "
                     "to name/type text instead of the real device ID, and push-to-talk is disabled.",
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
        self._ptt_endpoints = merge_ptt_endpoints(endpoint_maps.get("input"), endpoint_maps.get("output"))

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
        self._apply_ptt_filter()
        self._ptt_sync()

        if self._auto_start_pending:
            self._auto_start_pending = False
            self._start()

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
        self._schedule_save()

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
        self._ptt_sync()
        self._schedule_save()

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
            self._schedule_save()
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
            self._schedule_save()

        self.engine.send("stop", cb=done)

    def _on_close(self):
        self.hook.stop()
        self.ptt.cancel()
        self.engine.send("ptt_detach")
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
        self._sync_input_hook()

    def _on_hotkey_enabled_toggle(self):
        enabled = self.hotkey_enabled_var.get()
        if enabled:
            self.hotkey_button.config(state="normal")
        else:
            if self.capturing:
                self.capturing = False
                self.hotkey_button.config(text="Set Toggle Hotkey...", bg="SystemButtonFace")
            self.hotkey_button.config(state="disabled")
        self._sync_input_hook()
        self._schedule_save()

    def _sync_input_hook(self):
        """Start or stop the shared global input hook based on whether
        anything currently needs it: the switch hotkey, PTT, or an
        in-progress key capture for either. Kept decoupled from the
        individual checkboxes so turning off the switch hotkey doesn't
        also take PTT down with it, and vice versa."""
        needed = HAVE_PYNPUT and (
            self.hotkey_enabled_var.get() or self.ptt_enabled_var.get()
            or self.capturing or self.ptt_capturing
        )
        if needed:
            self.hook.start()
        else:
            self.hook.stop()

    def _begin_ptt_capture(self):
        if not HAVE_PYNPUT or self.ptt_capturing:
            return
        self.ptt_capturing = True
        self.ptt_add_button.config(text="Press a key...", bg=CAPTURING_BG, state="disabled")
        self._sync_input_hook()

    def _rebuild_ptt_binds(self):
        for child in self.ptt_binds_frame.winfo_children():
            child.destroy()
        if not self.ptt.keybinds:
            ttk.Label(self.ptt_binds_frame, text="none", foreground="#888").pack(side="left")
            return
        for label in self.ptt.keybinds:
            chip = ttk.Frame(self.ptt_binds_frame)
            chip.pack(side="left", padx=(0, 6), pady=2)
            ttk.Label(chip, text=disp(label)).pack(side="left", padx=(4, 0))
            tk.Button(
                chip, text="\u2715", command=lambda l=label: self._remove_ptt_bind(l),
                relief="flat", bd=0, padx=2,
            ).pack(side="left")

    def _remove_ptt_bind(self, label):
        if label in self.ptt.keybinds:
            self.ptt.keybinds.remove(label)
            self.ptt.key_removed(label)
            self._rebuild_ptt_binds()
            self._schedule_save()

    def _apply_ptt_filter(self):
        """Filter the PTT device list (every render+capture endpoint) by
        search text. 'Same as Output target' always stays available at the
        top regardless of the filter, and the current selection is
        preserved even if it gets filtered out of view."""
        q = self.ptt_ctrl["search_var"].get().strip().lower()
        filtered_pairs = sorted(
            ((eid, label) for eid, label in self._ptt_endpoints.items() if not q or q in label.lower()),
            key=lambda pair: pair[1].lower(),
        )
        mapping = {PTT_FOLLOW_OUTPUT_LABEL: None}
        values = [PTT_FOLLOW_OUTPUT_LABEL]
        for eid, label in filtered_pairs:
            mapping[label] = eid
            values.append(label)

        cur_label = None
        if self._current_ptt_ep_id is not None:
            cur_label = self._ptt_endpoints.get(self._current_ptt_ep_id)
            if cur_label and cur_label not in mapping:
                mapping[cur_label] = self._current_ptt_ep_id
                values.append(cur_label)
            elif not cur_label and self._ptt_endpoints:
                # Only clear once we've actually seen a real endpoint list -
                # an empty dict just means enumeration hasn't completed yet.
                self._current_ptt_ep_id = None

        self.ptt_device_map = mapping
        self.ptt_device_combo["values"] = values
        self.ptt_device_combo.set(cur_label if cur_label else PTT_FOLLOW_OUTPUT_LABEL)

    def _on_ptt_search_changed(self):
        self._apply_ptt_filter()
        self._schedule_save()

    def _on_ptt_device_selected(self):
        self._current_ptt_ep_id = self.ptt_device_map.get(self.ptt_device_combo.get())
        self._ptt_attached_ep_id = None  # force a re-attach against the new target
        self._ptt_sync()
        self._schedule_save()

    def _on_ptt_delay_changed(self, value):
        try:
            ms = int(float(value))
        except ValueError:
            return
        self.ptt_delay_label_var.set(f"{ms} ms")
        self.ptt.set_delay(ms)
        self._schedule_save()

    def _on_ptt_enabled_toggle(self):
        self._ptt_enabled_flag = self.ptt_enabled_var.get()
        if not self._ptt_enabled_flag:
            self.ptt.cancel()
        self._sync_input_hook()
        self._ptt_sync()
        self._schedule_save()

    def _on_ptt_status(self, text):
        if self.ptt_enabled_var.get():
            self.ptt_status_var.set(text)
            self.overlay.set_ptt_muted(text != "LIVE")

    def _on_ptt_key_event(self, label, pressed):
        # May be called directly from the pynput listener thread (see
        # InputHook._dispatch), so this must not touch Tk widgets/variables -
        # hence the plain-bool flag instead of self.ptt_enabled_var.get().
        if self._ptt_enabled_flag:
            self.ptt.handle_key(label, pressed)

    def _ptt_sync(self):
        """Attach or detach the PTT gate to match the current target device
        (an explicit endpoint override, or the Output target by default)
        and the enabled state. Called whenever any of those change."""
        if not self.ptt_enabled_var.get() or not HAVE_PYNPUT or not HAVE_COM:
            self.overlay.set_ptt_mode(False)
            if self._ptt_attached_ep_id is not None:
                self.engine.send("ptt_detach")
                self._ptt_attached_ep_id = None
            self.ptt_status_var.set("INACTIVE")
            return
        self.overlay.set_ptt_mode(True)
        if self._current_ptt_ep_id is not None:
            ep_id = self._current_ptt_ep_id
        else:
            entry = entry_for_index(self._current_output_index, self._output_devices)
            ep_id = entry.get("endpoint_id") if entry else None
        if not ep_id:
            if self._ptt_attached_ep_id is not None:
                self.engine.send("ptt_detach")
                self._ptt_attached_ep_id = None
            self.ptt_status_var.set("No endpoint ID - pick the WASAPI copy of the target device")
            self.overlay.set_ptt_muted(True)
            return
        if ep_id == self._ptt_attached_ep_id:
            return
        self._ptt_attached_ep_id = ep_id
        self.overlay.set_ptt_muted(True)  # starts muted until attach confirms / a key is held
        self.engine.send("ptt_attach", ep_id, cb=self._on_ptt_attach)

    def _on_ptt_attach(self, ok, _payload=None):
        if not self.ptt_enabled_var.get():
            return
        self.ptt_status_var.set("MUTED" if ok else "Attach failed")
        self.overlay.set_ptt_muted(True)

    def _on_overlay_enabled_toggle(self):
        if self.overlay_enabled_var.get() and self.running:
            self.overlay.show()
        else:
            self.overlay.hide()
        self._schedule_save()

    def _on_overlay_position_changed(self):
        self.overlay.set_position(self.overlay_position_var.get())
        self._schedule_save()

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

    def _on_overlay_color_clicked(self, slot, mode):
        var_map = {
            ("A", "muted"): self.overlay_color_a_muted_var, ("A", "live"): self.overlay_color_a_live_var,
            ("B", "muted"): self.overlay_color_b_muted_var, ("B", "live"): self.overlay_color_b_live_var,
        }
        button_map = {
            ("A", "muted"): self.overlay_color_a_muted_button, ("A", "live"): self.overlay_color_a_live_button,
            ("B", "muted"): self.overlay_color_b_muted_button, ("B", "live"): self.overlay_color_b_live_button,
        }
        var = var_map[(slot, mode)]
        button = button_map[(slot, mode)]
        _rgb, hex_color = colorchooser.askcolor(
            color=var.get(), title=f"Source {slot} {mode} overlay color"
        )
        if hex_color is None:
            return
        var.set(hex_color)
        button.config(bg=hex_color)
        self.overlay.set_color(slot, mode, hex_color)
        self._schedule_save()

    def _on_input_label(self, label):
        if not label:
            return
        if self.ptt_capturing:
            self.ptt_capturing = False
            self.ptt_add_button.config(text="+ Add Key", bg="SystemButtonFace", state="normal")
            if label not in self.ptt.keybinds:
                self.ptt.keybinds.append(label)
                self._rebuild_ptt_binds()
                self._schedule_save()
            self._sync_input_hook()
            return
        if self.capturing:
            self.capturing = False
            self.hotkey_button.config(text="Set Toggle Hotkey...", bg="SystemButtonFace", state="normal")
            self.toggle_hotkey = label
            self.hotkey_var.set(f"Toggle hotkey: {disp(label)}")
            self._schedule_save()
            self._sync_input_hook()
            return
        if (
            self.hotkey_enabled_var.get()
            and self.toggle_hotkey
            and label == self.toggle_hotkey
            and self.running
        ):
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
            "overlay_color_a_muted": self.overlay_color_a_muted_var.get(),
            "overlay_color_a_live": self.overlay_color_a_live_var.get(),
            "overlay_color_b_muted": self.overlay_color_b_muted_var.get(),
            "overlay_color_b_live": self.overlay_color_b_live_var.get(),
            "overlay_offset_x": self._int_var_value_min0(self.overlay_offset_x_var, DEFAULT_OVERLAY_OFFSET),
            "overlay_offset_y": self._int_var_value_min0(self.overlay_offset_y_var, DEFAULT_OVERLAY_OFFSET),
            "ptt_enabled": self.ptt_enabled_var.get(),
            "ptt_keybinds": list(self.ptt.keybinds),
            "ptt_delay_ms": self.ptt.delay_ms,
            "ptt_target_ep_id": self._current_ptt_ep_id,
            "ptt_search": self.ptt_ctrl["search_var"].get(),
            "was_running": self.running,
        }
        save_prefs(data)
        self.prefs = data


if __name__ == "__main__":
    app = SwitcherApp()
    app.mainloop()