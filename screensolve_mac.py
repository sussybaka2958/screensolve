"""ScreenSolve for macOS: private Supabase-backed screen help."""
import base64
import io
import json
import queue
import socket
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import messagebox
from PIL import ImageGrab
from pynput import keyboard as pynput_keyboard

from desktop_oauth import google_sign_in

try:
    import keyring
except ImportError:
    keyring = None

APP_NAME, HOTKEY = "ScreenSolve", "<ctrl>+<shift>+<space>"
BG, SURFACE, RAISED, BORDER = "#0B1020", "#141B2D", "#1B2438", "#2B3650"
TEXT, MUTED, PRIMARY, PRIMARY_HOVER = "#F8FAFC", "#AAB4CC", "#7C3AED", "#6D28D9"
SUCCESS, ERROR, FOCUS = "#34D399", "#FCA5A5", "#C4B5FD"
DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
SETTINGS_FILE, CONFIG_FILE = DATA_DIR / "settings.json", Path(__file__).with_name("supabase_config.json")
events, _memory_secrets = queue.Queue(), {}


def load_json(path, default):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return default


def settings(): return load_json(SETTINGS_FILE, {})


def save_settings(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data.pop("session", None)  # sessions belong in Keychain, never plaintext JSON
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def secret(name, value=None):
    """Use Keychain; a memory fallback requires sign-in again after restart."""
    data, legacy = settings(), None
    if name in data:
        legacy = data.pop(name); save_settings(data)
    if keyring:
        try:
            if value is not None:
                keyring.set_password(APP_NAME, name, value); return value
            stored = keyring.get_password(APP_NAME, name)
            if stored: return stored
            if legacy:
                keyring.set_password(APP_NAME, name, legacy); return legacy
        except Exception: pass
    if value is not None: _memory_secrets[name] = value
    return _memory_secrets.get(name)


def delete_secret(name):
    _memory_secrets.pop(name, None)
    if keyring:
        try: keyring.delete_password(APP_NAME, name)
        except Exception: pass


def supabase_config():
    cfg = load_json(CONFIG_FILE, {})
    url, key = cfg.get("url"), cfg.get("publishable_key") or cfg.get("anon_key")
    if not url or not key or "YOUR_" in key:
        raise RuntimeError("ScreenSolve has not been configured by its owner yet. Please try again later.")
    return url.rstrip("/"), key


def api_request(path, payload, token=None, timeout=20):
    url, key = supabase_config()
    headers = {"apikey": key, "Content-Type": "application/json"}
    if token: headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response: return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        try:
            parsed = json.loads(detail); detail = parsed.get("msg") or parsed.get("message") or parsed.get("error") or detail
        except json.JSONDecodeError: pass
        raise RuntimeError(detail) from error


def save_session(response, email=""):
    if not response.get("access_token"): raise RuntimeError("The sign-in service did not return a session. Please try again.")
    secret("session", json.dumps({"access_token": response["access_token"], "refresh_token": response.get("refresh_token")}))
    data = settings(); data["email"] = email or response.get("user", {}).get("email", data.get("email", "")); save_settings(data)


def sign_in(email, password): save_session(api_request("/auth/v1/token?grant_type=password", {"email": email, "password": password}), email)


def sign_up(email, password):
    response = api_request("/auth/v1/signup", {"email": email, "password": password})
    if response.get("access_token"): save_session(response, email); return True
    return False


def sign_in_with_google():
    url, key = supabase_config(); save_session(google_sign_in(url, key))


def session_data():
    try: return json.loads(secret("session") or "{}")
    except json.JSONDecodeError: return {}


def refresh_session():
    token = session_data().get("refresh_token")
    if not token: raise RuntimeError("Your session has expired. Sign in again to continue.")
    response = api_request("/auth/v1/token?grant_type=refresh_token", {"refresh_token": token}); save_session(response); return response["access_token"]


def current_profile():
    token = session_data().get("access_token")
    if not token: raise RuntimeError("Sign in to view your account.")
    url, key = supabase_config()
    req = urllib.request.Request(url + "/rest/v1/profiles?select=role,plan_status,plan_id,monthly_requests,requests_used,free_requests_limit,free_requests_used", headers={"apikey": key, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response: rows = json.loads(response.read().decode())
    except urllib.error.HTTPError as error: raise RuntimeError("We could not load your account. Check your connection and try again.") from error
    if not rows: raise RuntimeError("Your account is still being prepared. Try again in a moment.")
    return rows[0]


def billing_session(name, payload=None):
    token = session_data().get("access_token")
    if not token: raise RuntimeError("Sign in again to manage billing.")
    response = api_request(f"/functions/v1/{name}", payload or {}, token, timeout=30)
    if not response.get("url"): raise RuntimeError("Billing is temporarily unavailable. Please try again later.")
    webbrowser.open(response["url"])


def ask_service(image):
    token = session_data().get("access_token")
    if not token: raise RuntimeError("Sign in through Settings to use ScreenSolve.")
    payload = {"image": base64.b64encode(image).decode("ascii")}
    try: result = api_request("/functions/v1/ask-screen", payload, token, timeout=60)
    except RuntimeError as error:
        if "Invalid session" not in str(error) and "JWT" not in str(error): raise
        result = api_request("/functions/v1/ask-screen", payload, refresh_session(), timeout=60)
    if result.get("text"): return result["text"]
    raise RuntimeError(result.get("error", "The answer service is temporarily unavailable."))


def safe_customer_error(error):
    value = str(error)
    allowed = {"Your 10 free requests are used. Choose a plan to continue.", "Your monthly request allowance is used.", "Screenshot is missing or too large.", "Sign in through Settings to use ScreenSolve.", "Your session has expired. Sign in again to continue."}
    return value if value in allowed else "ScreenSolve could not answer that right now. Check your connection and try again."


def capture_and_ask():
    events.put(("status", "Capturing your screen…"))
    try:
        screen = ImageGrab.grab(); stream = io.BytesIO(); screen.save(stream, format="PNG")
        events.put(("status", "Thinking about it…")); events.put(("answer", ask_service(stream.getvalue())))
    except Exception as error: events.put(("error", safe_customer_error(error)))
    finally: events.put(("idle", None))


class ActionButton(tk.Label):
    """A themed, keyboard-operable control; Aqua ignores Tk Button colors."""
    def __init__(self, parent, text, command, outline=False, padx=18, pady=12, **kwargs):
        self.command, self.outline = command, outline
        self.normal_bg = RAISED if outline else PRIMARY
        self.hover_bg = BORDER if outline else PRIMARY_HOVER
        super().__init__(parent, text=text, fg=TEXT, bg=self.normal_bg, activebackground=self.hover_bg,
                         font=("Helvetica", 11, "bold"), padx=padx, pady=pady, cursor="hand2",
                         takefocus=True, highlightthickness=2, highlightbackground=self.normal_bg,
                         highlightcolor=FOCUS, **kwargs)
        self.bind("<Button-1>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<FocusIn>", lambda _event: self.config(highlightbackground=FOCUS))
        self.bind("<FocusOut>", lambda _event: self.config(highlightbackground=self.normal_bg))

    def _activate(self, _event=None):
        if str(self.cget("state")) != "disabled":
            self.command()
            return "break"

    def _set_hover(self, active):
        if str(self.cget("state")) != "disabled":
            self.config(bg=self.hover_bg if active else self.normal_bg)

    def config(self, cnf=None, **kwargs):
        if "state" in kwargs:
            disabled = kwargs["state"] == "disabled"
            kwargs.setdefault("fg", MUTED if disabled else TEXT)
            kwargs.setdefault("cursor", "arrow" if disabled else "hand2")
            kwargs.setdefault("bg", RAISED if disabled else self.normal_bg)
        return super().config(cnf, **kwargs)


class Overlay:
    def __init__(self, app): self.app, self.win = app, None
    def button(self, parent, text, command, secondary=False):
        return ActionButton(parent, text, command, outline=secondary, padx=16, pady=10)
    def show(self, text, kind="answer"):
        if not self.win: self.build()
        state, title, color = {"status": ("Working", "Working on your screen", FOCUS), "answer": ("Answer", "ScreenSolve", SUCCESS), "error": ("Needs attention", "ScreenSolve", ERROR)}[kind]
        self.title.config(text=title); self.state.config(text=state, fg=color)
        self.body.config(state="normal"); self.body.delete("1.0", "end"); self.body.insert("end", text); self.body.config(state="disabled")
        self.copy_button.config(state="normal" if kind == "answer" else "disabled"); self.retry_button.config(text="Capture again" if kind != "error" else "Try again")
        self.win.deiconify(); self.win.lift(); self.win.focus_force()
    def build(self):
        self.win = tk.Toplevel(self.app.root, bg=BG); self.win.title("ScreenSolve"); self.win.geometry("680x460"); self.win.minsize(460, 320); self.win.attributes("-topmost", True); self.win.protocol("WM_DELETE_WINDOW", self.hide)
        top = tk.Frame(self.win, bg=SURFACE, padx=24, pady=18); top.pack(fill="x")
        self.title = tk.Label(top, text="ScreenSolve", bg=SURFACE, fg=TEXT, font=("Helvetica", 17, "bold")); self.title.pack(side="left")
        self.state = tk.Label(top, text="Ready", bg=SURFACE, fg=SUCCESS, font=("Helvetica", 10, "bold")); self.state.pack(side="right")
        content = tk.Frame(self.win, bg=BG, padx=24, pady=22); content.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(content); scroll.pack(side="right", fill="y")
        self.body = tk.Text(content, wrap="word", yscrollcommand=scroll.set, bg=RAISED, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0, padx=18, pady=16, font=("Helvetica", 13), spacing3=9, state="disabled"); self.body.pack(fill="both", expand=True); scroll.config(command=self.body.yview)
        actions = tk.Frame(self.win, bg=BG, padx=24, pady=0); actions.pack(fill="x", pady=(0, 22))
        self.copy_button = self.button(actions, "Copy answer", self.copy, True); self.copy_button.pack(side="left")
        self.button(actions, "Settings", self.app.show_home, True).pack(side="right")
        self.retry_button = self.button(actions, "Capture again", self.app.trigger); self.retry_button.pack(side="right", padx=(0, 10))
        self.win.bind("<Escape>", lambda _event: self.hide()); self.win.bind("<Command-c>", lambda _event: self.copy())
    def copy(self):
        self.win.clipboard_clear(); self.win.clipboard_append(self.body.get("1.0", "end-1c")); self.state.config(text="Copied", fg=SUCCESS)
    def hide(self): self.win.withdraw()


class SetupWindow:
    def __init__(self, app):
        self.app, self.busy = app, False
        self.win = tk.Toplevel(app.root, bg=BG); self.win.title(APP_NAME)
        width, height = 760, 640
        x = max(20, (self.win.winfo_screenwidth() - width) // 2)
        y = max(40, (self.win.winfo_screenheight() - height) // 2)
        self.win.geometry(f"{width}x{height}+{x}+{y}"); self.win.minsize(580, 520); self.win.protocol("WM_DELETE_WINDOW", self.hide); self.win.bind("<Escape>", lambda _event: self.hide())
        self.landing() if not session_data().get("access_token") else self.home()
    def hide(self): self.win.withdraw()
    def clear(self):
        for widget in self.win.winfo_children(): widget.destroy()
    def label(self, parent, text, size=12, color=TEXT, bold=False, **kwargs): return tk.Label(parent, text=text, fg=color, bg=parent.cget("bg"), font=("Helvetica", size, "bold" if bold else "normal"), justify="left", anchor="w", **kwargs)
    def button(self, parent, text, command, outline=False, **kwargs): return ActionButton(parent, text, command, outline=outline, **kwargs)
    def card(self, parent, padx=24, pady=22): return tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1, padx=padx, pady=pady)
    def header(self, title, subtitle, back=None):
        top = tk.Frame(self.win, bg=BG, padx=44, pady=0); top.pack(fill="x", pady=(34, 18))
        if back: self.button(top, "‹ Back", back, outline=True, padx=10, pady=6).pack(anchor="w", pady=(0, 14))
        self.label(top, title, 26, TEXT, True).pack(anchor="w"); self.label(top, subtitle, 12, MUTED, wraplength=640).pack(anchor="w", pady=(8, 0))
    def _run_async(self, buttons, action, success, status):
        if self.busy: return
        self.busy = True; original = [(button, button.cget("text")) for button in buttons if button.winfo_exists()]
        for button, _text in original: button.config(state="disabled")
        status.config(text="Working…", fg=FOCUS)
        def done(error, result):
            self.busy = False
            for button, text in original:
                if button.winfo_exists(): button.config(state="normal", text=text)
            if error: status.config(text=str(error), fg=ERROR)
            else: success(result)
        def worker():
            try: result = action()
            except Exception as error: self.win.after(0, lambda: done(error, None))
            else: self.win.after(0, lambda: done(None, result))
        threading.Thread(target=worker, daemon=True).start()
    def landing(self):
        self.clear(); frame = tk.Frame(self.win, bg=BG, padx=56, pady=48); frame.pack(fill="both", expand=True)
        self.label(frame, "SCREEN SOLVE", 11, FOCUS, True).pack(anchor="w", pady=(0, 22)); self.label(frame, "Solve anything on\nyour screen.", 31, TEXT, True).pack(anchor="w"); self.label(frame, "A private screen companion that answers after one shortcut.", 13, MUTED, wraplength=510).pack(anchor="w", pady=(14, 28))
        shortcut = self.card(frame, 18, 14); shortcut.pack(fill="x", pady=(0, 20)); self.label(shortcut, "YOUR SHORTCUT", 9, MUTED, True).pack(side="left"); self.label(shortcut, "Control  +  Shift  +  Space", 12, TEXT, True).pack(side="right")
        status = self.label(frame, "", 10, ERROR, wraplength=600); status.pack(side="bottom", anchor="w")
        google = self.button(frame, "Continue with Google", lambda: self._run_async([google], sign_in_with_google, lambda _value: self.permissions_page(), status), outline=True); google.pack(fill="x", pady=(0, 10))
        self.label(frame, "or use your email", 10, MUTED).pack(anchor="center", pady=4); self.button(frame, "Create an account", lambda: self.auth(False)).pack(fill="x", pady=(8, 8)); self.button(frame, "Sign in", lambda: self.auth(True), outline=True).pack(fill="x"); self.label(frame, "10 free solves · No card required · Your API key stays private", 10, FOCUS).pack(anchor="w", pady=(20, 0))
    def auth(self, login):
        self.clear(); self.header("Welcome back" if login else "Create your account", "Use Google, or sign in with an email and password. Your account keeps your free solves and subscription together.", self.landing)
        card = self.card(self.win, 32, 28); card.pack(fill="both", expand=True, padx=44, pady=(0, 40))
        status = self.label(card, "", 10, ERROR, wraplength=570); status.pack(side="bottom", anchor="w", pady=(8, 0))
        self.label(card, "EMAIL", 10, MUTED, True).pack(anchor="w"); email = tk.Entry(card, font=("Helvetica", 13), bg=RAISED, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0); email.pack(fill="x", ipady=12, pady=(6, 18))
        self.label(card, "PASSWORD", 10, MUTED, True).pack(anchor="w"); password = tk.Entry(card, show="•", font=("Helvetica", 13), bg=RAISED, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0); password.pack(fill="x", ipady=12, pady=(6, 6))
        visible = tk.BooleanVar(value=False); tk.Checkbutton(card, text="Show password", variable=visible, command=lambda: password.config(show="" if visible.get() else "•"), fg=MUTED, bg=SURFACE, activebackground=SURFACE, activeforeground=TEXT, selectcolor=RAISED, font=("Helvetica", 10)).pack(anchor="w", pady=(0, 14))
        def submit():
            address, phrase = email.get().strip(), password.get()
            if not address or len(phrase) < 8: status.config(text="Enter a valid email and a password with at least 8 characters.", fg=ERROR); return
            def success(immediate):
                if not login and not immediate: messagebox.showinfo(APP_NAME, "Check your inbox to confirm your account, then sign in."); self.auth(True)
                else: self.permissions_page()
            self._run_async([submit_button, google], lambda: sign_in(address, phrase) if login else sign_up(address, phrase), success, status)
        google = self.button(card, "Continue with Google", lambda: self._run_async([google], sign_in_with_google, lambda _value: self.permissions_page(), status), outline=True); google.pack(fill="x", pady=(0, 18))
        submit_button = self.button(card, "Sign in" if login else "Create account", submit); submit_button.pack(fill="x")
        email.focus_set(); self.win.bind("<Return>", lambda _event: submit())
    def permissions_page(self):
        self.clear(); self.header("One quick macOS setup", "ScreenSolve needs these permissions for the shortcut and screen capture. You can grant them now or return later from Settings.")
        content = tk.Frame(self.win, bg=BG, padx=44); content.pack(fill="both", expand=True)
        for title, detail, url in (("Screen Recording", "Lets ScreenSolve capture the screen only when you use the shortcut.", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"), ("Accessibility", "Lets Control + Shift + Space work outside ScreenSolve.", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")):
            card = self.card(content, 20, 18); card.pack(fill="x", pady=6); self.label(card, title, 14, TEXT, True).pack(anchor="w"); self.label(card, detail, 11, MUTED, wraplength=510).pack(anchor="w", pady=(5, 12)); self.button(card, "Open System Settings", lambda link=url: webbrowser.open(link), outline=True, padx=12, pady=8).pack(anchor="w")
        self.button(content, "Continue to ScreenSolve", self.home).pack(fill="x", pady=(20, 0))
    def home(self):
        self.clear(); self.header("ScreenSolve is ready", "Press Control + Shift + Space anywhere to capture your screen and get help.")
        content = tk.Frame(self.win, bg=BG, padx=44); content.pack(fill="both", expand=True); usage = self.card(content); usage.pack(fill="x", pady=(0, 14))
        self.label(usage, "YOUR ACCOUNT", 9, MUTED, True).pack(anchor="w"); title = self.label(usage, "Checking your request balance…", 16, TEXT, True); title.pack(anchor="w", pady=(7, 5)); detail = self.label(usage, "", 11, MUTED, wraplength=560); detail.pack(anchor="w")
        bar = tk.Frame(usage, bg=BORDER, height=8); bar.pack(fill="x", pady=(16, 4)); fill = tk.Frame(bar, bg=PRIMARY, height=8); fill.place(relwidth=.01, relheight=1)
        self.label(content, settings().get("email", "Signed in"), 11, MUTED).pack(anchor="w", pady=(6, 12)); actions = tk.Frame(content, bg=BG); actions.pack(fill="x"); self.button(actions, "Capture now", self.app.trigger).pack(side="left"); self.button(actions, "Plans", self.plans, outline=True).pack(side="left", padx=10); self.button(actions, "Sign out", self.sign_out, outline=True).pack(side="right")
        status = self.label(content, "", 10, ERROR, wraplength=620); status.pack(anchor="w", pady=(16, 0))
        def render(profile):
            active = profile.get("plan_status") == "active"; used = int(profile.get("requests_used" if active else "free_requests_used", 0) or 0); total = int(profile.get("monthly_requests" if active else "free_requests_limit", 10) or 10); left = max(total - used, 0)
            if active: title.config(text=f"{left:,} requests remaining this month"); detail.config(text=f"{(profile.get('plan_id') or 'Plan').replace('_', ' ').title()} · {used:,} of {total:,} requests used")
            else: title.config(text=f"{left} of {total} free solves remaining"); detail.config(text="No card required. Choose a plan only when you want more solves.")
            fill.place(relwidth=max(.01, min(1, left / max(total, 1))))
        def load():
            try: profile = current_profile()
            except Exception as error: self.win.after(0, lambda: status.config(text=f"{error}  Please retry from the menu."))
            else: self.win.after(0, lambda: render(profile))
        threading.Thread(target=load, daemon=True).start()
    def plans(self):
        self.clear(); self.header("Choose more solves", "Your 10 free solves stay free. Select a plan only when you want a larger monthly allowance.", self.home)
        content = tk.Frame(self.win, bg=BG, padx=44); content.pack(fill="both", expand=True)
        for plan, name, price, allowance in (("starter", "Starter", "$4.99 / month", "100 solves each month"), ("plus", "Plus", "$9.99 / month", "500 solves each month · Most popular"), ("power", "Power", "$19.99 / month", "2,000 solves each month")):
            card = self.card(content, 20, 16); card.pack(fill="x", pady=5); self.label(card, name, 15, TEXT, True).pack(anchor="w"); self.label(card, f"{price}  ·  {allowance}", 11, MUTED).pack(anchor="w", pady=(4, 10)); self.button(card, f"Choose {name}", lambda item=plan: self.billing("create-checkout-session", {"plan": item}), outline=(plan != "plus"), padx=14, pady=8).pack(anchor="e")
        self.button(content, "Manage subscription", lambda: self.billing("create-portal-session"), outline=True).pack(fill="x", pady=(14, 0))
    def billing(self, name, payload=None):
        status = self.label(self.win, "", 10, ERROR, wraplength=600); status.place(relx=.5, rely=.96, anchor="center")
        buttons = [item for parent in self.win.winfo_children() for item in parent.winfo_children() if isinstance(item, ActionButton)]
        self._run_async(buttons, lambda: billing_session(name, payload), lambda _value: status.config(text="Secure checkout opened in your browser.", fg=SUCCESS), status)
    def sign_out(self):
        delete_secret("session"); data = settings(); data.pop("email", None); save_settings(data); self.landing()


class App:
    def __init__(self):
        self.root = tk.Tk(); self.root.withdraw(); self.overlay = Overlay(self); self.home_window = None; self.busy = False; self.listener = None; self.make_menu()
    def make_menu(self):
        menu = tk.Menu(self.root); app_menu = tk.Menu(menu, tearoff=False); app_menu.add_command(label="Open ScreenSolve", command=self.show_home); app_menu.add_command(label="Capture screen", command=self.trigger, accelerator="⌃⇧Space"); app_menu.add_separator(); app_menu.add_command(label="Quit ScreenSolve", command=self.root.destroy); menu.add_cascade(label="ScreenSolve", menu=app_menu); self.root.config(menu=menu)
        try: self.root.createcommand("tk::mac::ReopenApplication", self.show_home)
        except tk.TclError: pass
    def show_home(self):
        if self.home_window and self.home_window.win.winfo_exists(): self.home_window.win.deiconify(); self.home_window.win.lift(); return
        self.home_window = SetupWindow(self)
    def trigger(self):
        if self.busy: return
        self.busy = True; threading.Thread(target=capture_and_ask, daemon=True).start()
    def poll(self):
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "idle": self.busy = False
                else: self.overlay.show(value, kind)
        except queue.Empty: pass
        self.root.after(100, self.poll)
    def run(self):
        self.listener = pynput_keyboard.GlobalHotKeys({HOTKEY: self.trigger}); self.listener.start(); self.root.after(100, self.poll); self.show_home()
        try: self.root.mainloop()
        finally:
            if self.listener: self.listener.stop()


if __name__ == "__main__":
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock_socket.bind(("127.0.0.1", 51823))
    except OSError:
        raise SystemExit("ScreenSolve is already running.")
    App().run()
