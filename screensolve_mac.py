"""
ScreenSolve (macOS port) - A personal AI screen assistant
Press a hotkey, take a screenshot, send it to Gemini, see the answer
in a small transparent overlay window.

This is a macOS port of the original Windows-only `screensolve` script.
Windows-specific pieces (ctypes.windll mutex/messagebox, the `keyboard`
library) are replaced with cross-platform / macOS-friendly equivalents:
    - single-instance lock  -> a local TCP socket bind instead of a
      Win32 named mutex
    - message boxes         -> tkinter messagebox
    - global hotkey capture -> pynput instead of `keyboard`
      (`keyboard` needs root and doesn't reliably work on macOS)

SETUP
    pip install google-genai pillow pynput pystray pyobjc-framework-Cocoa pyobjc-framework-Quartz

    Set your API key as an environment variable (do NOT hard-code it
    in this file):

        export GEMINI_API_KEY="your-key-here"

    Get a free Gemini API key at https://aistudio.google.com/apikey

    macOS also requires you to grant the app running this script
    (Terminal, iTerm, or python itself) two permissions in
    System Settings -> Privacy & Security:
        - Accessibility        (needed for the global hotkey)
        - Screen Recording     (needed to capture real screen content;
                                 without it screenshots come back black)

RUN
    python3 screensolve_mac.py

HOTKEY
    Ctrl+Shift+Space to capture + ask.
    Esc (while the overlay has focus) or the x button closes the overlay.
    Right-click the tray icon (if available) to quit the whole app,
    or just Ctrl+C in the terminal.
"""

import os
import io
import sys
import socket
import queue
import threading
import tkinter as tk
from tkinter import messagebox

from pynput import keyboard as pynput_keyboard
from PIL import ImageGrab, Image, ImageDraw
from google import genai
from google.genai import types

try:
    import pystray
except ImportError:
    pystray = None  # tray icon becomes optional; app still runs

# ---------- Config ----------
HOTKEY = "<ctrl>+<shift>+<space>"
MODEL_NAME = "gemini-3.6-flash"
DEFAULT_PROMPT = (
    "Look at this screenshot and help me. If it's a question or quiz, "
    "give the answer and a short explanation. If it's an error or code, "
    "explain the issue and how to fix it. Be concise.\n\n"
    "Formatting rules (important, this is a plain-text display with no "
    "markdown or LaTeX rendering):\n"
    "- Do NOT use markdown symbols like *, **, #, or backticks.\n"
    "- Do NOT use LaTeX (no $, \\frac, \\times, \\ge, etc.).\n"
    "- Write math using plain text and real unicode symbols instead: "
    "x, /, ×, ÷, ≥, ≤, ², ³, ½, π, √ and plain fractions like 8/7.\n"
    "- Use simple line breaks and dashes (-) for lists, not asterisks.\n"
    "- Keep each answer short and readable as plain text."
)
OVERLAY_ALPHA = 0.88       # 0 = fully transparent, 1 = fully opaque
OVERLAY_WIDTH = 480
OVERLAY_HEIGHT = 260

# ---------- Single-instance lock ----------
# Windows used a named mutex; on macOS we bind a local TCP port instead.
# If a copy is already running, this one just exits quietly instead of
# registering a second hotkey.
_LOCK_PORT = 51823
_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _lock_socket.bind(("127.0.0.1", _LOCK_PORT))
except OSError:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showinfo(
        "ScreenSolve - Already Running",
        "ScreenSolve is already running.\n\n"
        "Check your menu bar for the icon, or quit the other copy "
        "before starting a new one.",
    )
    sys.exit(0)

# ---------- Gemini setup ----------
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "ScreenSolve - Missing API Key",
        "No GEMINI_API_KEY environment variable found.\n\n"
        'Open a terminal and run:\n  export GEMINI_API_KEY="your-key-here"\n\n'
        "Then restart this app.\n"
        "Get a key at https://aistudio.google.com/apikey",
    )
    sys.exit(1)

client = genai.Client(api_key=api_key)

# Queue used to pass results from the background thread to the Tk main thread
result_queue = queue.Queue()


def capture_screen():
    """Grab the whole screen and return it as PNG bytes."""
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def ask_gemini(image_bytes, prompt=DEFAULT_PROMPT):
    """Send the screenshot + prompt to Gemini and return the text answer."""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            prompt,
        ],
    )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("No text returned by the model. It may have blocked the request.")
    return text


def on_hotkey():
    """Runs in a background thread: capture + call the API, then queue the result."""
    result_queue.put(("status", "Capturing screen..."))
    try:
        img_bytes = capture_screen()
        result_queue.put(("status", "Thinking..."))
        answer = ask_gemini(img_bytes)
        result_queue.put(("answer", answer))
    except Exception as e:
        result_queue.put(("answer", f"Error: {e}"))


def trigger():
    threading.Thread(target=on_hotkey, daemon=True).start()


# ---------- Overlay UI ----------
class Overlay:
    def __init__(self, root):
        self.root = root
        self.win = None
        self.text_widget = None

    def show_status(self, text):
        self._ensure_window()
        self.text_widget.config(state="normal")
        self.text_widget.delete("1.0", tk.END)
        self.text_widget.insert(tk.END, text)
        self.text_widget.config(state="disabled")

    def _ensure_window(self):
        if self.win is not None:
            return
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)                # no title bar/border
        self.win.attributes("-topmost", True)           # always on top
        self.win.attributes("-alpha", OVERLAY_ALPHA)    # transparency
        self.win.configure(bg="#1e1e1e")

        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        x = screen_w - OVERLAY_WIDTH - 24
        y = screen_h - OVERLAY_HEIGHT - 60
        self.win.geometry(f"{OVERLAY_WIDTH}x{OVERLAY_HEIGHT}+{x}+{y}")

        header = tk.Frame(self.win, bg="#1e1e1e")
        header.pack(fill="x", padx=10, pady=(8, 0))
        title_label = tk.Label(
            header, text="ScreenSolve", fg="#8ab4f8", bg="#1e1e1e",
            font=("Segoe UI", 10, "bold"),
        )
        title_label.pack(side="left")

        # Since overrideredirect(True) removes the OS title bar, dragging
        # has to be implemented manually: track where the drag started,
        # then move the window by the same delta as the mouse.
        def start_drag(event):
            self.win._drag_x = event.x
            self.win._drag_y = event.y

        def do_drag(event):
            x = self.win.winfo_x() + (event.x - self.win._drag_x)
            y = self.win.winfo_y() + (event.y - self.win._drag_y)
            self.win.geometry(f"+{x}+{y}")

        header.bind("<ButtonPress-1>", start_drag)
        header.bind("<B1-Motion>", do_drag)
        title_label.bind("<ButtonPress-1>", start_drag)
        title_label.bind("<B1-Motion>", do_drag)
        tk.Button(
            header, text="✕", command=self.hide, bg="#1e1e1e", fg="#aaaaaa",
            bd=0, activebackground="#333333", font=("Segoe UI", 10),
        ).pack(side="right")

        self.text_widget = tk.Text(
            self.win, wrap="word", bg="#1e1e1e", fg="#f0f0f0",
            font=("Segoe UI", 11), bd=0, padx=10, pady=8, state="disabled",
        )
        self.text_widget.pack(fill="both", expand=True, padx=6, pady=6)
        self.win.bind("<Escape>", lambda e: self.hide())

    def hide(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None


def poll_queue(root, overlay):
    try:
        while True:
            kind, payload = result_queue.get_nowait()
            if kind in ("status", "answer"):
                overlay.show_status(payload)
    except queue.Empty:
        pass
    root.after(100, poll_queue, root, overlay)


# ---------- Tray icon (best-effort; app still runs without it) ----------
def make_tray_icon_image():
    """Small blue circle with 'S' -- generated so no icon file is needed."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=(30, 30, 30, 255), outline=(138, 180, 248, 255), width=4)
    draw.text((size / 2 - 7, size / 2 - 12), "S", fill=(138, 180, 248, 255))
    return img


def run_tray_icon(root):
    if pystray is None:
        return
    if sys.platform == "darwin":
        # pystray's menu-bar icon uses AppKit, which requires the main
        # thread on macOS. Tkinter's mainloop also needs the main thread,
        # so the two can't share one process here -- skip the tray icon
        # and quit with Ctrl+C in the terminal instead.
        print("ScreenSolve running. Press Ctrl+C in this terminal to quit.")
        return

    def on_capture(icon, item):
        trigger()

    def on_quit(icon, item):
        icon.stop()
        root.after(0, root.destroy)

    icon = pystray.Icon(
        "ScreenSolve",
        make_tray_icon_image(),
        "ScreenSolve",
        menu=pystray.Menu(
            pystray.MenuItem("Capture + Ask", on_capture, default=True),
            pystray.MenuItem("Exit", on_quit),
        ),
    )
    try:
        threading.Thread(target=icon.run, daemon=True).start()
    except Exception:
        pass  # tray icon is optional; ignore failures (e.g. no GUI backend)


def main():
    root = tk.Tk()
    root.withdraw()  # hide the root window; only the overlay is shown

    overlay = Overlay(root)

    hotkey_listener = pynput_keyboard.GlobalHotKeys({HOTKEY: trigger})
    hotkey_listener.start()

    run_tray_icon(root)

    root.after(100, poll_queue, root, overlay)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        hotkey_listener.stop()


if __name__ == "__main__":
    main()
