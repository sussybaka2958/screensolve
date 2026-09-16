"""ScreenSolve desktop client. Run with pythonw ScreenSolve.pyw."""
import ctypes
from ctypes import wintypes
import base64
import io
import json
import os
import queue
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

import keyboard
from PIL import Image, ImageDraw, ImageGrab
try:
    import keyring
except ImportError:
    keyring = None
try:
    import pystray
except ImportError:
    pystray = None

APP_NAME = "ScreenSolve"
HOTKEY = "ctrl+shift+space"
ACCENT, BG, PANEL, TEXT, MUTED = "#A78BFA", "#080B14", "#141A2D", "#F7F7FB", "#A8B0C7"
SURFACE, HEADER, SUCCESS = "#101624", "#0C1120", "#8B5CF6"
HEADER_HEIGHT = 58
WM_NCHITTEST, HTTRANSPARENT = 0x0084, -1
GWL_WNDPROC = -4
LONG_PTR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
WNDPROC = ctypes.WINFUNCTYPE(LONG_PTR, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32 = ctypes.windll.user32
user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongPtrW.restype = LONG_PTR
user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, LONG_PTR)
user32.SetWindowLongPtrW.restype = LONG_PTR
user32.CallWindowProcW.argtypes = (LONG_PTR, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.CallWindowProcW.restype = LONG_PTR
DATA_DIR = Path(os.getenv("APPDATA", Path.home())) / APP_NAME
SETTINGS_FILE = DATA_DIR / "settings.json"
# In a frozen app this keeps the public Supabase configuration beside the EXE,
# instead of looking in PyInstaller's temporary extraction directory.
CONFIG_FILE = (Path(sys.executable).with_name("supabase_config.json")
               if getattr(sys, "frozen", False) else Path(__file__).with_name("supabase_config.json"))
events = queue.Queue()


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def settings():
    return load_json(SETTINGS_FILE, {})


def save_settings(values):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(values, indent=2), encoding="utf-8")


def secret(name, value=None):
    """Use Windows Credential Manager when keyring is installed."""
    if keyring:
        try:
            if value is not None:
                keyring.set_password(APP_NAME, name, value)
            return keyring.get_password(APP_NAME, name)
        except Exception:
            # Some stripped-down Windows installs have no credential backend.
            # Fall back to local settings rather than making the app unusable.
            pass
    data = settings()
    if value is not None:
        data[name] = value
        save_settings(data)
    return data.get(name)


def supabase_config():
    return load_json(CONFIG_FILE, {})


def api_request(path, payload):
    cfg = supabase_config()
    url, anon = cfg.get("url"), (cfg.get("publishable_key") or cfg.get("anon_key"))
    if not url or not anon or "YOUR_" in anon:
        raise RuntimeError("Accounts are not configured yet. Follow the README Supabase setup.")
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode(), method="POST",
        headers={"apikey": anon, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("msg") or json.loads(detail).get("message") or detail
        except json.JSONDecodeError:
            pass
        raise RuntimeError(detail) from error


def sign_in(email, password):
    response = api_request("/auth/v1/token?grant_type=password", {"email": email, "password": password})
    secret("session", json.dumps({"access_token": response["access_token"], "refresh_token": response.get("refresh_token")}))
    data = settings(); data["email"] = email; save_settings(data)


def sign_up(email, password):
    response = api_request("/auth/v1/signup", {"email": email, "password": password})
    if response.get("access_token"):
        secret("session", json.dumps({"access_token": response["access_token"], "refresh_token": response.get("refresh_token")}))
    data = settings(); data["email"] = email; save_settings(data)
    return bool(response.get("access_token"))


def session_data():
    try:
        return json.loads(secret("session") or "{}")
    except json.JSONDecodeError:
        return {}


def refresh_session():
    refresh_token = session_data().get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Your session has expired. Please sign in again.")
    response = api_request("/auth/v1/token?grant_type=refresh_token", {"refresh_token": refresh_token})
    secret("session", json.dumps({"access_token": response["access_token"], "refresh_token": response.get("refresh_token")}))
    return response["access_token"]


def current_profile():
    cfg = supabase_config(); public_key = cfg.get("publishable_key") or cfg.get("anon_key")
    token = session_data().get("access_token")
    if not token: return {}
    req = urllib.request.Request(cfg["url"].rstrip("/") + "/rest/v1/profiles?select=role,plan_status,plan_id,monthly_requests,requests_used", headers={"apikey": public_key, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            rows = json.loads(response.read().decode())
            return rows[0] if rows else {}
    except Exception:
        return {}


def billing_session(function_name, payload=None):
    """Open Stripe-hosted checkout or its customer portal. No payment secret reaches Windows."""
    cfg = supabase_config(); public_key = cfg.get("publishable_key") or cfg.get("anon_key")
    token = session_data().get("access_token")
    if not token:
        raise RuntimeError("Please sign in again.")
    req = urllib.request.Request(
        cfg["url"].rstrip("/") + "/functions/v1/" + function_name,
        data=json.dumps(payload or {}).encode(), method="POST",
        headers={"apikey": public_key, "Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode(errors="replace")) from error
    if not result.get("url"):
        raise RuntimeError(result.get("error", "Billing is temporarily unavailable."))
    webbrowser.open(result["url"])


def ask_service(image_bytes):
    """Call our protected Edge Function; the Gemini key never reaches the client."""
    cfg = supabase_config()
    public_key = cfg.get("publishable_key") or cfg.get("anon_key")
    token = session_data().get("access_token")
    if not token:
        raise RuntimeError("Please sign in again.")
    payload = {"image": base64.b64encode(image_bytes).decode("ascii")}
    def request(access_token):
        req = urllib.request.Request(
            cfg["url"].rstrip("/") + "/functions/v1/ask-screen",
            data=json.dumps(payload).encode(), method="POST",
            headers={"apikey": public_key, "Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode())
    try:
        result = request(token)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            # Never expose a raw Edge Function, provider, or deployment error to
            # customers. In particular, a missing server-side Gemini secret must
            # not look like something a customer can or should configure locally.
            try:
                server_error = json.loads(error.read().decode(errors="replace")).get("error", "")
            except (json.JSONDecodeError, UnicodeDecodeError):
                server_error = ""
            allowed_messages = {
                "Your plan is inactive or its monthly allowance is used.",
                "Screenshot is missing or too large.",
                "Sign in required",
                "Invalid session",
            }
            if server_error in allowed_messages:
                raise RuntimeError(server_error) from error
            raise RuntimeError("The answer service is temporarily unavailable. Please try again shortly.") from error
        result = request(refresh_session())
    if not result.get("text"):
        # The server's friendly, allow-listed errors may be shown. Do not relay
        # unexpected strings: they can contain provider configuration details.
        server_error = result.get("error", "")
        if server_error in {"Your plan is inactive or its monthly allowance is used.",
                            "Screenshot is missing or too large.", "Sign in required", "Invalid session"}:
            raise RuntimeError(server_error)
        raise RuntimeError("The answer service returned no usable answer. Please try again shortly.")
    return result["text"]


def capture_and_ask():
    events.put(("status", "Capturing your screen…"))
    try:
        image = ImageGrab.grab()
        stream = io.BytesIO(); image.save(stream, format="PNG")
        events.put(("status", "Thinking…"))
        answer = ask_service(stream.getvalue())
        if not answer:
            raise RuntimeError("The model did not return an answer.")
        events.put(("answer", answer))
    except Exception as exc:
        events.put(("answer", f"Something went wrong\n\n{exc}"))
    finally:
        events.put(("idle", None))


class Overlay:
    def __init__(self, root):
        self.root, self.win, self.busy = root, None, False

    def show(self, text, is_status=False):
        if not self.win: self._build()
        self.title.config(text="SCREEN SOLVE  ·  " + ("WORKING" if is_status else "ANSWER"))
        self.state.config(text="THINKING" if is_status else "READY")
        self.body.config(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("end", text)
        self.body.config(state="disabled")
        self.win.deiconify(); self.win.lift()

    def _build(self):
        w, h = 590, 400
        self.win = tk.Toplevel(self.root, bg=BG)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        # Glass-like transparency while preserving scrolling, text selection, and clicks.
        # Tk applies alpha to the text as well as the background. Keep the
        # answer crisp; the hit-testing below still makes its area click-through.
        self.win.attributes("-alpha", 1.0)
        x, y = self.win.winfo_screenwidth() - w - 30, self.win.winfo_screenheight() - h - 76
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        border = tk.Frame(self.win, bg="#6D5CC6"); border.pack(fill="both", expand=True)
        card = tk.Frame(border, bg=SURFACE); card.pack(fill="both", expand=True, padx=1, pady=1)
        bar = tk.Frame(card, bg=HEADER, height=58); bar.pack(fill="x"); bar.pack_propagate(False)
        tk.Label(bar, text="✦", font=("Segoe UI Symbol", 17), fg=ACCENT, bg=HEADER).pack(side="left", padx=(20, 9))
        self.title = tk.Label(bar, text="SCREEN SOLVE", font=("Segoe UI Variable", 10, "bold"), fg=TEXT, bg=HEADER)
        self.title.pack(side="left")
        self.state = tk.Label(bar, text="READY", font=("Segoe UI Variable", 8, "bold"), fg="#DAD2FF", bg="#292052", padx=10, pady=5)
        self.state.pack(side="right", padx=(0, 8))
        tk.Button(bar, text="×", command=self.hide, font=("Segoe UI", 17), fg=MUTED, bg=HEADER, activebackground="#202944", activeforeground=TEXT, bd=0, width=3, cursor="hand2").pack(side="right", padx=8)
        # Drag bindings intentionally exist only on this top bar, never on the answer panel.
        for widget in (bar, self.title):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag)
        content = tk.Frame(card, bg=SURFACE); content.pack(fill="both", expand=True, padx=18, pady=18)
        scroll = tk.Scrollbar(content, bg="#27304A", troughcolor=SURFACE, activebackground=ACCENT, bd=0, width=7)
        scroll.pack(side="right", fill="y")
        self.body = tk.Text(content, wrap="word", yscrollcommand=scroll.set, bg="#121A2C", fg=TEXT,
                            insertbackground=TEXT, relief="flat", bd=0, padx=18, pady=16,
                            font=("Segoe UI Variable", 11), spacing1=2, spacing3=9, cursor="arrow", state="disabled")
        self.body.pack(fill="both", expand=True); scroll.config(command=self.body.yview)
        self.body.bind("<MouseWheel>", lambda e: self.body.yview_scroll(-int(e.delta / 120), "units"))
        self.win.bind("<Escape>", lambda e: self.hide())
        self._make_answer_click_through()

    def _make_answer_click_through(self):
        """Only the top bar owns mouse input; the answer area targets the app beneath it."""
        hwnd = self.win.winfo_id()
        original_proc = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)

        @WNDPROC
        def hit_test(window, message, wparam, lparam):
            if message == WM_NCHITTEST:
                # lparam contains screen coordinates. Anything below the header
                # is transparent to input, so the underlying app receives it.
                screen_y = ctypes.c_short((int(lparam) >> 16) & 0xFFFF).value
                if screen_y - self.win.winfo_rooty() >= HEADER_HEIGHT:
                    return HTTRANSPARENT
            return user32.CallWindowProcW(original_proc, window, message, wparam, lparam)

        # Keep the callback alive for the entire lifetime of the Tk window.
        callback_ptr = ctypes.cast(hit_test, ctypes.c_void_p).value
        user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, LONG_PTR(callback_ptr))
        self._original_proc = original_proc
        self._hit_test_proc = hit_test

    def _drag_start(self, event): self.drag_x, self.drag_y = event.x_root, event.y_root
    def _drag(self, event):
        self.win.geometry(f"+{self.win.winfo_x() + event.x_root - self.drag_x}+{self.win.winfo_y() + event.y_root - self.drag_y}")
        self.drag_x, self.drag_y = event.x_root, event.y_root
    def hide(self):
        if self.win: self.win.withdraw()


class SetupWindow:
    def __init__(self, app, first_run=True):
        self.app = app
        self.first_run = first_run
        self.win = tk.Toplevel(app.root, bg=BG); self.win.title("ScreenSolve"); self.win.geometry("640x610"); self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", app.root.destroy)
        self._landing()

    def clear(self):
        for widget in self.win.winfo_children(): widget.destroy()
    def label(self, parent, text, size=11, color=TEXT, bold=False):
        return tk.Label(parent, text=text, fg=color, bg=parent.cget("bg"), font=("Segoe UI", size, "bold" if bold else "normal"), justify="left", anchor="w")
    def button(self, parent, text, command, outline=False):
        return tk.Button(parent, text=text, command=command, font=("Segoe UI", 10, "bold"), fg=TEXT, bg=BG if outline else ACCENT, activebackground="#6D45CF", activeforeground=TEXT, relief="flat", bd=0, pady=12, cursor="hand2")
    def _landing(self):
        self.clear(); frame = tk.Frame(self.win, bg=BG); frame.pack(fill="both", expand=True, padx=64, pady=56)
        self.label(frame, "✦  ScreenSolve", 16, "#C4B5FD", True).pack(anchor="w", pady=(0, 48))
        self.label(frame, "Understand anything\non your screen.", 28, TEXT, True).pack(anchor="w")
        self.label(frame, "Your private, on-demand AI screen companion.", 12, MUTED).pack(anchor="w", pady=(12, 38))
        self.button(frame, "Create your account", lambda: self._auth(False)).pack(fill="x", pady=6)
        self.button(frame, "Sign in", lambda: self._auth(True), True).pack(fill="x", pady=6)
        self.label(frame, "Already configured this device? Open Settings after signing in.", 9, MUTED).pack(anchor="w", pady=(22, 0))
    def _auth(self, login):
        self.clear(); card = tk.Frame(self.win, bg=PANEL); card.pack(fill="both", expand=True, padx=64, pady=54)
        inner = tk.Frame(card, bg=PANEL); inner.pack(fill="both", expand=True, padx=42, pady=42)
        self.label(inner, "Welcome back" if login else "Create an account", 21, TEXT, True).pack(anchor="w")
        self.label(inner, "Sign in to keep your ScreenSolve setup together." if login else "Start with an email and a secure password.", 10, MUTED).pack(anchor="w", pady=(8, 28))
        self.label(inner, "EMAIL", 9, MUTED, True).pack(anchor="w"); email = tk.Entry(inner, font=("Segoe UI", 12), bg="#0B1020", fg=TEXT, insertbackground=TEXT, relief="flat")
        email.pack(fill="x", ipady=11, pady=(6, 20)); self.label(inner, "PASSWORD", 9, MUTED, True).pack(anchor="w")
        password = tk.Entry(inner, show="•", font=("Segoe UI", 12), bg="#0B1020", fg=TEXT, insertbackground=TEXT, relief="flat")
        password.pack(fill="x", ipady=11, pady=(6, 24))
        status = self.label(inner, "", 9, "#FCA5A5"); status.pack(anchor="w", pady=(0, 8))
        def submit():
            try:
                if not email.get().strip() or len(password.get()) < 8: raise RuntimeError("Enter an email and a password of at least 8 characters.")
                immediate = sign_in(email.get().strip(), password.get()) if login else sign_up(email.get().strip(), password.get())
                if not login and not immediate:
                    messagebox.showinfo(APP_NAME, "Check your email to confirm your account, then sign in.")
                    return self._auth(True)
                self._settings()
            except Exception as exc: status.config(text=str(exc))
        self.button(inner, "Sign in" if login else "Create account", submit).pack(fill="x")
        tk.Button(inner, text="← Back", command=self._landing, fg=MUTED, bg=PANEL, bd=0, font=("Segoe UI", 10), cursor="hand2").pack(anchor="w", pady=18)
    def _settings(self):
        self.clear(); frame = tk.Frame(self.win, bg=BG); frame.pack(fill="both", expand=True, padx=64, pady=48)
        self.label(frame, "Your ScreenSolve", 25, TEXT, True).pack(anchor="w")
        self.label(frame, "Your plan and payment are managed securely. Your device never receives the Gemini API key.", 11, MUTED).pack(anchor="w", pady=(10, 22))
        profile = current_profile()
        if profile:
            used, total = profile.get("requests_used", 0), profile.get("monthly_requests", 0)
            plan = (profile.get("plan_id") or profile.get("plan_status") or "inactive").replace("_", " ").title()
            self.label(frame, f"{plan}  ·  {used:,} of {total:,} requests used this month", 10, "#C4B5FD", True).pack(anchor="w", pady=(0, 24))
        self.label(frame, "HOW IT WORKS", 9, MUTED, True).pack(anchor="w")
        self.label(frame, "1. Press Ctrl + Shift + Space\n2. Your screen is sent to the protected ScreenSolve service\n3. The answer appears in this window", 11, TEXT).pack(anchor="w", pady=(8, 22))
        if profile.get("role") == "admin":
            self.button(frame, "Developer Console", self._developer_console, True).pack(fill="x", pady=(0, 12))
        else:
            self.button(frame, "Choose or upgrade plan", self._plans).pack(fill="x", pady=(0, 8))
            self.button(frame, "Manage subscription", lambda: self._billing("create-portal-session"), True).pack(fill="x", pady=(0, 12))
        def finish():
            self.win.destroy()
            if self.first_run: self.app.start()
        self.button(frame, "Open ScreenSolve", finish).pack(fill="x", pady=(30, 8))
        self.label(frame, "Press Ctrl + Shift + Space anywhere to ask about your screen.", 10, "#C4B5FD").pack(anchor="w", pady=18)
    def _billing(self, function_name, payload=None):
        try:
            billing_session(function_name, payload)
            messagebox.showinfo(APP_NAME, "Your secure Stripe page opened in your browser. Return here when you are finished.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
    def _plans(self):
        self.clear(); frame = tk.Frame(self.win, bg=BG); frame.pack(fill="both", expand=True, padx=64, pady=42)
        self.label(frame, "Choose your plan", 24, TEXT, True).pack(anchor="w")
        self.label(frame, "Secure checkout opens in your browser.", 11, MUTED).pack(anchor="w", pady=(8, 24))
        for plan, label in (("starter", "Starter · 100 requests / month · $4.99"), ("plus", "Plus · 500 requests / month · $9.99"), ("power", "Power · 2,000 requests / month · $19.99")):
            self.button(frame, label, lambda p=plan: self._billing("create-checkout-session", {"plan": p})).pack(fill="x", pady=5)
        self.button(frame, "← Back", self._settings, True).pack(fill="x", pady=(20, 0))
    def _developer_console(self):
        self.clear(); frame = tk.Frame(self.win, bg=BG); frame.pack(fill="both", expand=True, padx=64, pady=48)
        profile = current_profile()
        self.label(frame, "Developer Console", 25, TEXT, True).pack(anchor="w")
        self.label(frame, "Authenticated administrator session", 11, "#C4B5FD").pack(anchor="w", pady=(8, 34))
        self.label(frame, "Your account", 9, MUTED, True).pack(anchor="w")
        self.label(frame, settings().get("email", ""), 12, TEXT).pack(anchor="w", pady=(6, 22))
        self.label(frame, "Service security", 9, MUTED, True).pack(anchor="w")
        self.label(frame, "Gemini credentials stay server-side.\nCustomers only receive Supabase sessions and their own answers.", 11, TEXT).pack(anchor="w", pady=(6, 28))
        self.label(frame, "Manage plans and customer roles in your Supabase dashboard until a billing dashboard is added.", 10, MUTED).pack(anchor="w")
        self.button(frame, "← Back to settings", self._settings, True).pack(fill="x", pady=(28, 0))


class App:
    def __init__(self):
        self.root = tk.Tk(); self.root.withdraw(); self.overlay = Overlay(self.root); self.busy = False
    def trigger(self):
        if self.busy: return
        self.busy = True; threading.Thread(target=capture_and_ask, daemon=True).start()
    def poll(self):
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "idle": self.busy = False
                else: self.overlay.show(value, kind == "status")
        except queue.Empty: pass
        self.root.after(100, self.poll)
    def settings_window(self):
        window = SetupWindow(self, first_run=False)
        window.win.protocol("WM_DELETE_WINDOW", window.win.destroy)
        window._settings()
    def tray(self):
        if not pystray: return
        icon_image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon_image)
        draw.ellipse((4, 4, 60, 60), fill="#141B32", outline="#8B5CF6", width=4)
        draw.text((24, 18), "✦", fill="#C4B5FD")
        def capture(icon, item): self.trigger()
        def preferences(icon, item): self.root.after(0, self.settings_window)
        def quit_app(icon, item):
            icon.stop(); keyboard.unhook_all_hotkeys(); self.root.after(0, self.root.destroy)
        icon = pystray.Icon(APP_NAME, icon_image, APP_NAME, pystray.Menu(
            pystray.MenuItem("Capture + Ask", capture, default=True),
            pystray.MenuItem("Settings", preferences), pystray.MenuItem("Exit", quit_app)))
        threading.Thread(target=icon.run, daemon=True).start()
    def start(self):
        keyboard.add_hotkey(HOTKEY, self.trigger); self.tray(); self.root.after(100, self.poll)
    def run(self):
        if not secret("session"): SetupWindow(self)
        else: self.start()
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
