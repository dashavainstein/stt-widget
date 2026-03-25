#!/usr/bin/env python3
"""STT Widget: floating button + hotkey → Groq Whisper → paste.

Left-click OR Ctrl+Shift+Space = toggle recording.
Right-click drag = move button.
"""

import ctypes
import io
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import numpy as np
import pyperclip
import sounddevice as sd
import soundfile as sf
from openai import OpenAI
from PIL import Image, ImageDraw
from pynput import keyboard
import pystray

# ---- Config ----
GROQ_API_URL = "https://api.groq.com/openai/v1"
MODEL = "whisper-large-v3-turbo"
LANGUAGE = "ru"
SAMPLE_RATE = 16000
CHANNELS = 1

HOTKEY_COMBO = {keyboard.Key.ctrl_l, keyboard.Key.shift_l, keyboard.Key.space}

MAX_RECORDING_SECONDS = 180  # auto-stop after 3 minutes

COLOR_READY = "#8b5cf6"
COLOR_RECORDING = "#ff1744"
COLOR_BUSY = "#1a1a2e"

# ---- State ----
recording = False
audio_frames = []
stream = None
client = None
pressed_keys = set()
toggle_lock = threading.Lock()
last_toggle_time = 0
recording_start_time = 0
auto_stop_timer = None
countdown_timer = None
target_hwnd = None  # Window to paste into (captured at recording start)
tray_icon_ref = None  # Reference to tray icon for state updates
tray_icon_creator = None  # Function to create tray icons


def get_api_key():
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key
    for path in [
        Path.home() / ".groq" / "api_key",
        Path(__file__).parent.parent / ".octopus" / "groq-key.txt",
    ]:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    print("ERROR: GROQ_API_KEY not found. Set env var or create ~/.groq/api_key")
    sys.exit(1)


def make_window_noactivate(root):
    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = style | WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def start_recording():
    global recording, audio_frames, stream, target_hwnd
    audio_frames = []
    recording = True
    # Remember which window was active before clicking REC
    target_hwnd = ctypes.windll.user32.GetForegroundWindow()
    print("[REC]  Recording...")

    def callback(indata, frames, time_info, status):
        if recording:
            audio_frames.append(indata.copy())

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        callback=callback,
    )
    stream.start()


def stop_and_transcribe(btn, root):
    global recording, stream
    recording = False

    # Check if user switched to a different window during recording
    current_hwnd = ctypes.windll.user32.GetForegroundWindow()
    # Use current window if user explicitly switched; otherwise use the original
    paste_hwnd = current_hwnd if current_hwnd and current_hwnd != target_hwnd and ctypes.windll.user32.IsWindow(current_hwnd) else target_hwnd

    if stream:
        stream.stop()
        stream.close()
        stream = None

    if not audio_frames:
        root.after(0, lambda: reset_btn(btn))
        return

    audio = np.concatenate(audio_frames, axis=0)
    duration = len(audio) / SAMPLE_RATE
    print(f"[STOP]  {duration:.1f}s. Transcribing...")

    if duration < 0.3:
        root.after(0, lambda: reset_btn(btn))
        return

    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV")
    buf.seek(0)
    buf.name = "recording.wav"

    try:
        result = client.audio.transcriptions.create(
            model=MODEL,
            file=buf,
            language=LANGUAGE,
            response_format="text",
        )
        text = result.strip()
        if text:
            print(f"[OK] {text}")
            paste_text(text, paste_hwnd)
        else:
            print("[WARN]  Empty")
    except Exception as e:
        print(f"[ERR] {e}")

    root.after(0, lambda: reset_btn(btn))


def paste_text(text, hwnd=None):
    pyperclip.copy(text)
    user32 = ctypes.windll.user32
    paste_target = hwnd or target_hwnd
    if paste_target and user32.IsWindow(paste_target):
        user32.SetForegroundWindow(paste_target)
    time.sleep(0.25)
    VK_CONTROL = 0x11
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def reset_btn(btn):
    btn.config(text="REC", bg=COLOR_READY, fg="white", state=tk.NORMAL)
    # Sync parent frame & close btn colors
    parent = btn.master
    parent.config(bg=COLOR_READY)
    for w in parent.winfo_children():
        if isinstance(w, tk.Label):
            w.config(bg=COLOR_READY)


def show_limit_popup(root):
    """Flash a small popup near the button warning that the recording limit was reached."""
    popup = tk.Toplevel(root)
    popup.overrideredirect(True)
    popup.attributes("-topmost", True)
    popup.configure(bg="#1a1a2e")
    lbl = tk.Label(
        popup,
        text=f"  Limit {MAX_RECORDING_SECONDS // 60} min  ",
        font=("Segoe UI", 10, "bold"),
        bg="#1a1a2e",
        fg="#ff1744",
        padx=10,
        pady=6,
    )
    lbl.pack()
    popup.update_idletasks()
    pw = popup.winfo_reqwidth()
    x = root.winfo_x() + root.winfo_width() // 2 - pw // 2
    y = root.winfo_y() - popup.winfo_reqheight() - 6
    popup.geometry(f"+{x}+{y}")
    popup.after(3000, popup.destroy)


def toggle_recording_ui(btn, root):
    global last_toggle_time, recording_start_time, auto_stop_timer, countdown_timer
    now = time.time()
    if now - last_toggle_time < 0.5:
        return
    last_toggle_time = now

    with toggle_lock:
        if not recording:
            start_recording()
            recording_start_time = time.time()
            btn.config(text="STOP", bg=COLOR_RECORDING, fg="white")
            btn.master.config(bg=COLOR_RECORDING)
            # Hide hover elements when recording starts
            if hasattr(btn, '_close_btn'):
                btn._close_btn.place_forget()
            if hasattr(btn, '_tooltip'):
                btn._tooltip.withdraw()
            # Update tray icon to recording state
            if tray_icon_ref and tray_icon_creator:
                tray_icon_ref.icon = tray_icon_creator(True)
            # Schedule auto-stop
            auto_stop_timer = root.after(
                MAX_RECORDING_SECONDS * 1000,
                lambda: auto_stop_recording(btn, root),
            )
            # Start countdown display on button
            update_recording_timer(btn, root)
        else:
            # Cancel timers
            if auto_stop_timer is not None:
                root.after_cancel(auto_stop_timer)
                auto_stop_timer = None
            if countdown_timer is not None:
                root.after_cancel(countdown_timer)
                countdown_timer = None
            btn.config(text="...", bg=COLOR_BUSY, fg="#8b5cf6", state=tk.DISABLED)
            btn.master.config(bg=COLOR_BUSY)
            # Update tray icon back to idle
            if tray_icon_ref and tray_icon_creator:
                tray_icon_ref.icon = tray_icon_creator(False)
            threading.Thread(
                target=stop_and_transcribe, args=(btn, root), daemon=True
            ).start()


def update_recording_timer(btn, root):
    """Update button text with remaining time while recording."""
    global countdown_timer
    if not recording:
        countdown_timer = None
        return
    elapsed = time.time() - recording_start_time
    remaining = max(0, MAX_RECORDING_SECONDS - int(elapsed))
    mins, secs = divmod(remaining, 60)
    btn.config(text=f"{mins}:{secs:02d}")
    countdown_timer = root.after(1000, lambda: update_recording_timer(btn, root))


def auto_stop_recording(btn, root):
    """Called by timer when MAX_RECORDING_SECONDS exceeded."""
    global auto_stop_timer
    auto_stop_timer = None
    if not recording:
        return
    show_limit_popup(root)
    toggle_recording_ui(btn, root)


# ---- Hotkey ----
def on_press(key):
    k = key if isinstance(key, keyboard.Key) else getattr(key, "key", key)
    pressed_keys.add(k)


def on_release(key):
    k = key if isinstance(key, keyboard.Key) else getattr(key, "key", key)
    pressed_keys.discard(k)


def hide_console():
    """Hide the console window on Windows."""
    if sys.platform == "win32":
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE


def main():
    global client

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        hide_console()

    client = OpenAI(api_key=get_api_key(), base_url=GROQ_API_URL)

    root = tk.Tk()
    root.title("STT")
    root.configure(bg="#0a0a0f")
    root.resizable(False, False)

    # Position: bottom-right corner, above taskbar
    btn_w, btn_h = 56, 32
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    pos_x = screen_w - btn_w - 12
    pos_y = screen_h - btn_h - 52  # above taskbar
    root.geometry(f"{btn_w}x{btn_h}+{pos_x}+{pos_y}")

    # Remove window decorations AFTER geometry is set
    root.overrideredirect(True)
    root.attributes("-topmost", True)

    # Force window to appear (needed for pythonw)
    root.update_idletasks()
    root.deiconify()
    root.lift()

    # Container frame for button + close
    frame = tk.Frame(root, bg=COLOR_READY)
    frame.pack(fill=tk.BOTH, expand=True)

    btn = tk.Button(
        frame,
        text="REC",
        font=("Segoe UI", 9, "bold"),
        bg=COLOR_READY,
        fg="white",
        activebackground="#a78bfa",
        relief=tk.FLAT,
        padx=2,
        pady=1,
        command=lambda: toggle_recording_ui(btn, root),
    )
    btn.pack(fill=tk.BOTH, expand=True)

    btn_visible = [True]

    # Minimize button (hidden, appears on hover) — hides to tray
    close_btn = tk.Label(
        frame,
        text="\u2013",
        font=("Segoe UI", 10, "bold"),
        bg=COLOR_READY,
        fg="#ffffff",
        cursor="hand2",
    )
    close_btn.bind("<Button-1>", lambda e: (root.withdraw(), btn_visible.__setitem__(0, False)))
    btn._close_btn = close_btn

    # Tooltip
    tooltip = tk.Toplevel(root)
    tooltip.withdraw()
    tooltip.overrideredirect(True)
    tooltip.attributes("-topmost", True)
    tip_frame = tk.Frame(tooltip, bg="#1a1a2e")
    tip_frame.pack()
    tk.Label(
        tip_frame,
        text="Ctrl+Shift+Space",
        font=("Segoe UI", 9),
        bg="#1a1a2e",
        fg="#a78bfa",
        anchor="e",
        padx=6,
        pady=1,
    ).pack(fill=tk.X)
    tk.Label(
        tip_frame,
        text="RMB+hold \u2014 move",
        font=("Segoe UI", 9),
        bg="#1a1a2e",
        fg="#ffffff",
        anchor="e",
        padx=6,
        pady=1,
    ).pack(fill=tk.X)
    btn._tooltip = tooltip

    def show_hover(_e=None):
        if recording:
            return
        # Show close button in top-right corner
        close_btn.place(relx=1.0, x=2, y=-4, anchor="ne", width=18, height=18)
        # Show tooltip above the button, right-aligned
        tooltip.update_idletasks()
        tip_w = tooltip.winfo_reqwidth()
        x = root.winfo_x() + root.winfo_width() - tip_w
        y = root.winfo_y() - tooltip.winfo_reqheight() - 2
        tooltip.geometry(f"+{x}+{y}")
        tooltip.deiconify()

    def hide_hover(_e=None):
        close_btn.place_forget()
        tooltip.withdraw()

    frame.bind("<Enter>", show_hover)
    btn.bind("<Enter>", show_hover)
    close_btn.bind("<Enter>", show_hover)
    frame.bind("<Leave>", hide_hover)
    # Only hide when mouse actually leaves the whole widget area
    def check_leave(e):
        # Get mouse position relative to root
        mx, my = root.winfo_pointerxy()
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        rw, rh = root.winfo_width(), root.winfo_height()
        if not (rx <= mx <= rx + rw and ry <= my <= ry + rh):
            hide_hover()

    root.bind("<Leave>", check_leave)

    # Sync close_btn bg with main button state
    def sync_close_bg(*_args):
        try:
            close_btn.config(bg=btn.cget("bg"))
            frame.config(bg=btn.cget("bg"))
        except tk.TclError:
            pass

    # Right-click drag to move
    def start_drag(e):
        root._drag_x = e.x
        root._drag_y = e.y

    def get_work_area(x, y):
        """Get the work area (excluding taskbar) of the monitor at (x, y)."""
        import ctypes.wintypes
        MONITOR_DEFAULTTONEAREST = 2

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT(x, y)
        hmon = ctypes.windll.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        return mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom

    def do_drag(e):
        x = root.winfo_x() + e.x - root._drag_x
        y = root.winfo_y() + e.y - root._drag_y
        # Clamp to work area of nearest monitor (excludes taskbar, allows multi-monitor)
        wl, wt, wr, wb = get_work_area(x, y)
        w, h = root.winfo_width(), root.winfo_height()
        x = max(wl, min(x, wr - w))
        y = max(wt, min(y, wb - h))
        root.geometry(f"+{x}+{y}")

    btn.bind("<Button-3>", start_drag)
    btn.bind("<B3-Motion>", do_drag)

    make_window_noactivate(root)

    # ---- System tray ----
    def create_tray_icon(is_recording=False):
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        if is_recording:
            draw.ellipse([4, 4, 28, 28], fill="#ff1744")
            draw.rectangle([12, 12, 20, 20], fill="white")  # stop square
        else:
            draw.ellipse([4, 4, 28, 28], fill="#8b5cf6")
            draw.ellipse([12, 12, 20, 20], fill="white")
        return img

    def toggle_button(_icon=None, _item=None):
        if recording:
            return  # Don't hide during recording
        if btn_visible[0]:
            root.after(0, root.withdraw)
            btn_visible[0] = False
        else:
            root.after(0, lambda: (root.deiconify(), root.lift()))
            btn_visible[0] = True

    def quit_app(icon, _item=None):
        icon.stop()
        root.after(0, root.destroy)

    tray_menu = pystray.Menu(
        pystray.MenuItem("Show/Hide", toggle_button, default=True),
        pystray.MenuItem("Quit", quit_app),
    )
    tray_icon = pystray.Icon("octos_stt", create_tray_icon(), "Octos STT", tray_menu)
    global tray_icon_ref, tray_icon_creator
    tray_icon_ref = tray_icon
    tray_icon_creator = create_tray_icon
    tray_thread = threading.Thread(target=tray_icon.run, daemon=True)
    tray_thread.start()

    # Hotkey listener
    hotkey_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    hotkey_listener.daemon = True
    hotkey_listener.start()

    # Poll hotkey combo
    def check_hotkey():
        if HOTKEY_COMBO.issubset(pressed_keys):
            toggle_recording_ui(btn, root)
        root.after(100, check_hotkey)

    root.after(100, check_hotkey)

    print("=" * 50)
    print("Octos STT")
    print("  Click button OR Ctrl+Shift+Space = toggle")
    print("  Right-drag = move")
    print("  Tray icon = show/hide")
    print("=" * 50)

    root.mainloop()
    tray_icon.stop()


if __name__ == "__main__":
    main()
