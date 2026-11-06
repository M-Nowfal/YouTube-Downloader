"""YT Downloader - modern GUI for downloading YouTube audio / video / playlists."""
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import yt_dlp
from PIL import Image, ImageDraw, ImageOps
from yt_dlp.utils import DownloadCancelled

# ------------------------------------------------------------------ constants
APP_NAME = "YT Downloader"
CONFIG_PATH = Path.home() / ".yt_downloader_config.json"
DEFAULT_DIR = r"D:\YouTube-Downloader\downloads"
os.makedirs(DEFAULT_DIR, exist_ok=True)
WIN_W, WIN_H = 880, 820

PURPLE, PURPLE_H = "#8B5CF6", "#7C3AED"
PINK, PINK_H = "#EC4899", "#DB2777"
BLUE, BLUE_H = "#3B82F6", "#2563EB"
TEAL, TEAL_H = "#14B8A6", "#0D9488"
SUCCESS, WARNING = "#22C55E", "#F59E0B"
DANGER, DANGER_H = "#EF4444", "#DC2626"

BG = ("#EEF0FA", "#0E0F1A")
CARD = ("#FFFFFF", "#181A2B")
FIELD = ("#F1F3FB", "#232640")
BORDER = ("#D6D9EC", "#34385A")
MUTED = ("#6B7280", "#9CA3AF")
TEXT = ("#1F2430", "#E8E9F3")
SEC_FG, SEC_H = ("#E4E7F5", "#2A2D4A"), ("#D3D7EC", "#383C61")
SEC_TEXT = ("#1F2430", "#E8E9F3")

AUDIO_QUALITIES = ["128 kbps", "192 kbps", "256 kbps", "320 kbps"]
VIDEO_QUALITIES = ["Best available", "1080p", "720p", "480p", "360p"]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
URL_RE = re.compile(r"^https?://\S+$", re.I)


# ------------------------------------------------------------------ helpers
def find_ffmpeg_dir():
    """ffmpeg+ffprobe: bundled in the exe, next to the script, or on PATH."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(getattr(sys, "_MEIPASS", ""), "bin"))
        candidates.append(os.path.join(os.path.dirname(sys.executable), "bin"))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin"))
    for d in candidates:
        if os.path.isfile(os.path.join(d, "ffmpeg.exe")) and os.path.isfile(os.path.join(d, "ffprobe.exe")):
            return d
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    return os.path.dirname(ff) if ff and fp else None


FFMPEG_DIR = find_ffmpeg_dir()


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
    except Exception:
        pass


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(sec):
    if not sec:
        return ""
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def open_path(path):
    os.startfile(path)


def show_in_folder(path):
    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])


def make_gradient(w, h, stops):
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    seg = len(stops) - 1
    for x in range(w):
        t = x / max(w - 1, 1) * seg
        i = min(int(t), seg - 1)
        f = t - i
        c1, c2 = stops[i], stops[i + 1]
        draw.line([(x, 0), (x, h)], fill=tuple(int(c1[k] + (c2[k] - c1[k]) * f) for k in range(3)))
    return img


def friendly_error(err):
    msg = ANSI_RE.sub("", str(err)).strip()
    low = msg.lower()
    if "ffmpeg" in low or "ffprobe" in low:
        return "FFmpeg problem", "FFmpeg could not be found or failed. Install FFmpeg, or place ffmpeg.exe and ffprobe.exe in a 'bin' folder next to the app."
    if "private video" in low:
        return "Private video", "This video is private and can't be downloaded."
    if "confirm your age" in low or "age-restricted" in low or "age restricted" in low:
        return "Age-restricted", "This video is age-restricted and needs a signed-in account."
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube blocked the request", "YouTube is asking for verification. Try again later or from a different network."
    if "not available in your country" in low or "geo" in low:
        return "Not available in your region", "This video is blocked in your country."
    if "video unavailable" in low or "has been removed" in low or "no longer available" in low:
        return "Video unavailable", "This video was removed or is unavailable."
    if "copyright" in low:
        return "Blocked for copyright", "This video was blocked on copyright grounds."
    if "http error 429" in low or "too many requests" in low:
        return "Too many requests", "YouTube is rate-limiting you. Wait a few minutes and try again."
    if "unsupported url" in low or "not a valid url" in low:
        return "Unsupported link", "Paste a valid YouTube video or playlist link."
    if any(k in low for k in ("urlopen error", "getaddrinfo", "timed out", "connection", "network",
                              "unable to download webpage", "name or service")):
        return "Network problem", "Couldn't reach YouTube. Check your internet connection and try again."
    if "permission" in low or "access is denied" in low:
        return "Can't write file", "Windows denied access to the save folder. Pick another folder."
    if "no space left" in low:
        return "Disk full", "There isn't enough free space on the disk."
    return "Download failed", msg[:350] or "An unknown error occurred."


class SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


class QueueLogger:
    def __init__(self, q):
        self.q, self.failed, self.last_error = q, 0, ""

    def debug(self, msg): pass
    def info(self, msg): pass

    def warning(self, msg):
        self.q.put(("log", "⚠ " + ANSI_RE.sub("", str(msg))[:170]))

    def error(self, msg):
        msg = ANSI_RE.sub("", str(msg))
        self.failed += 1
        self.last_error = msg
        self.q.put(("log", "✖ " + msg[:200]))


# ------------------------------------------------------------------ dialog
class Dialog(ctk.CTkToplevel):
    def __init__(self, parent, title, heading, message, color, actions):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.configure(fg_color=CARD)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(padx=30, pady=26)
        ctk.CTkLabel(body, text=heading, text_color=color,
                     font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(body, text=message, wraplength=420, justify="left",
                     font=ctk.CTkFont(size=14)).pack(anchor="w", pady=(12, 22))

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(anchor="e")
        for label, callback, kind in actions:
            if kind == "primary":
                style = dict(fg_color=PURPLE, hover_color=PURPLE_H, text_color="white")
            elif kind == "danger":
                style = dict(fg_color=DANGER, hover_color=DANGER_H, text_color="white")
            else:
                style = dict(fg_color=SEC_FG, hover_color=SEC_H, text_color=SEC_TEXT)
            ctk.CTkButton(row, text=label, width=120, height=40, corner_radius=10,
                          font=ctk.CTkFont(size=14, weight="bold"),
                          command=lambda cb=callback: self._run(cb), **style).pack(side="left", padx=(8, 0))

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.after(150, self._focus)

    def _focus(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass

    def _run(self, callback):
        self.destroy()
        if callback:
            callback()


# ------------------------------------------------------------------ main app
class App(ctk.CTk):
    def __init__(self):
        self.cfg = load_config()
        ctk.set_appearance_mode(self.cfg.get("theme", "dark"))
        super().__init__()
        self.attributes("-alpha", 0.0)          # stay invisible until centered
        self.title(APP_NAME)
        # Allow both horizontal and vertical resizing, including Windows maximize.
        self.minsize(880, 560)
        self.resizable(True, True)
        self.configure(fg_color=BG)

        self.msg_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.downloading = False
        self.indeterminate = False
        self.mode = "a"
        self.dl = {}

        self.playlist_var = tk.BooleanVar(value=False)
        self.quality_var = tk.StringVar(value="192 kbps")
        self.blank_thumb = ctk.CTkImage(Image.new("RGB", (320, 180), (55, 57, 72)), size=(160, 90))
        self.thumb_img = None

        self.build_ui()
        self.state("zoomed")
        self.attributes("-alpha", 1.0)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll_queue()

        if not FFMPEG_DIR:
            self.log("⚠ FFmpeg not found - MP3 conversion and video merging need it.")
            self.after(600, lambda: self.dialog(
                "FFmpeg missing", "FFmpeg not found",
                "Install FFmpeg (winget install Gyan.FFmpeg), or place ffmpeg.exe and ffprobe.exe "
                "in a 'bin' folder next to this app, then restart.", WARNING, [("OK", None, "secondary")]))

    def center_window(self, w, h):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        h = min(h, sh - 110)
        self.geometry(f"{w}x{h}")
        self.update()
        aw, ah = self.winfo_width(), self.winfo_height()
        x = max((sw - aw) // 2, 0)
        y = max((sh - ah) // 2 - 25, 0)
        self.geometry(f"+{x}+{y}")

    # ---------------------------------------------------------------- UI
    def card(self):
        f = ctk.CTkFrame(self.body, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        f.pack(fill="x", padx=14, pady=8)
        return f

    def button(self, parent, text, command, fg, hover, width=100, height=48, **kw):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height, corner_radius=12,
                             fg_color=fg, hover_color=hover, text_color="white",
                             font=ctk.CTkFont(size=14, weight="bold"), **kw)

    def secondary(self, parent, text, command, width=90, height=48):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height, corner_radius=12,
                             fg_color=SEC_FG, hover_color=SEC_H, text_color=SEC_TEXT,
                             font=ctk.CTkFont(size=14, weight="bold"))

    def entry(self, parent, **kw):
        return ctk.CTkEntry(parent, height=48, corner_radius=12, fg_color=FIELD, border_width=2,
                            border_color=BORDER, font=ctk.CTkFont(size=15), **kw)

    def build_ui(self):
        bold = ctk.CTkFont(size=14, weight="bold")

        # Compact, left-aligned app header with a rounded logo badge.
        self._banner_resize_job = None  # retained for safe shutdown compatibility
        header = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        header.pack(fill="x", padx=28, pady=(18, 6))
        header.grid_columnconfigure(1, weight=1)

        logo = ctk.CTkFrame(
            header, width=48, height=48, corner_radius=15,
            fg_color=PURPLE, border_width=1, border_color=PURPLE_H,
        )
        logo.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
        logo.grid_propagate(False)
        ctk.CTkLabel(
            logo, text="▶", text_color="white",
            font=ctk.CTkFont(size=21, weight="bold"),
        ).place(relx=0.53, rely=0.5, anchor="center")

        ctk.CTkLabel(
            header, text="YT Downloader", text_color=TEXT,
            font=ctk.CTkFont(size=22, weight="bold"), anchor="w",
        ).grid(row=0, column=1, sticky="sw")
        ctk.CTkLabel(
            header, text="Download audio, video and full playlists in one click",
            text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w",
        ).grid(row=1, column=1, sticky="nw", pady=(2, 0))

        self.theme_switch = ctk.CTkSwitch(
            header, text="Dark mode", command=self.toggle_theme,
            progress_color=PURPLE,
        )
        self.theme_switch.grid(row=0, column=2, rowspan=2, sticky="e", padx=(16, 0))
        if ctk.get_appearance_mode() == "Dark":
            self.theme_switch.select()

        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self.body.pack(fill="both", expand=True, padx=12, pady=(8, 12))

        # URL card
        self.url_card = self.card()
        ctk.CTkLabel(self.url_card, text="🔗  Video or playlist link", font=bold).pack(anchor="w", padx=20, pady=(16, 8))
        row = ctk.CTkFrame(self.url_card, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 18))
        self.url_entry = self.entry(row, placeholder_text="Paste a YouTube link here…")
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<Return>", lambda e: self.fetch_info())
        self.paste_btn = self.secondary(row, "Paste", self.paste_url, 80)
        self.paste_btn.pack(side="left", padx=(10, 0))
        self.fetch_btn = self.button(row, "Fetch Info", self.fetch_info, BLUE, BLUE_H, width=120)
        self.fetch_btn.pack(side="left", padx=(10, 0))

        # info card (hidden until fetched)
        self.info_card = ctk.CTkFrame(self.body, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        self.thumb_label = ctk.CTkLabel(self.info_card, text="", image=self.blank_thumb)
        self.thumb_label.pack(side="left", padx=16, pady=16)
        txt = ctk.CTkFrame(self.info_card, fg_color="transparent")
        txt.pack(side="left", fill="both", expand=True, padx=(0, 16), pady=16)
        self.info_title = ctk.CTkLabel(txt, text="", font=ctk.CTkFont(size=16, weight="bold"),
                                       wraplength=500, justify="left", anchor="w")
        self.info_title.pack(anchor="w", fill="x")
        self.info_meta = ctk.CTkLabel(txt, text="", text_color=MUTED, anchor="w")
        self.info_meta.pack(anchor="w", pady=(6, 0))

        # options card
        opt = self.card()
        ctk.CTkLabel(opt, text="🎯  Format", font=bold).pack(anchor="w", padx=20, pady=(16, 8))
        self.mode_seg = ctk.CTkSegmentedButton(
            opt, values=["🎵  Audio (MP3)", "🎬  Video (MP4)"], command=self.on_mode_change, height=46,
            corner_radius=12, font=ctk.CTkFont(size=15, weight="bold"),
            selected_color=PURPLE, selected_hover_color=PURPLE_H, unselected_color=FIELD,
            unselected_hover_color=SEC_H, text_color=("#1F2430", "#FFFFFF"))
        self.mode_seg.set("🎵  Audio (MP3)")
        self.mode_seg.pack(fill="x", padx=20)

        qrow = ctk.CTkFrame(opt, fg_color="transparent")
        qrow.pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(qrow, text="✨  Quality", font=bold).pack(side="left")
        self.quality_menu = ctk.CTkOptionMenu(
            qrow, values=AUDIO_QUALITIES, variable=self.quality_var, width=190, height=40, corner_radius=10,
            fg_color=PINK, button_color=PINK_H, button_hover_color=PINK_H, font=ctk.CTkFont(size=14, weight="bold"))
        self.quality_menu.pack(side="left", padx=(14, 0))
        self.playlist_switch = ctk.CTkSwitch(qrow, text="Download entire playlist", variable=self.playlist_var,
                                             onvalue=True, offvalue=False, progress_color=PURPLE,
                                             font=ctk.CTkFont(size=14))
        self.playlist_switch.pack(side="right")

        ctk.CTkLabel(opt, text="📁  Save to", font=bold).pack(anchor="w", padx=20, pady=(16, 8))
        frow = ctk.CTkFrame(opt, fg_color="transparent")
        frow.pack(fill="x", padx=20, pady=(0, 18))
        self.folder_entry = self.entry(frow)
        self.folder_entry.insert(0, DEFAULT_DIR)
        self.folder_entry.pack(side="left", fill="x", expand=True)
        self.browse_btn = self.button(frow, "Browse", self.browse, TEAL, TEAL_H, width=100)
        self.browse_btn.pack(side="left", padx=(10, 0))
        self.secondary(frow, "Open", self.open_folder, 80).pack(side="left", padx=(10, 0))

        # action buttons
        actions = ctk.CTkFrame(self.body, fg_color="transparent")
        actions.pack(fill="x", padx=14, pady=8)
        actions.columnconfigure(0, weight=1)
        self.download_btn = self.button(actions, "⬇   Download", self.start_download, PURPLE, PURPLE_H, height=58)
        self.download_btn.configure(font=ctk.CTkFont(size=18, weight="bold"), corner_radius=14)
        self.download_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = self.button(actions, "Cancel", self.cancel_download, DANGER, DANGER_H, width=130, height=58)
        self.cancel_btn.configure(state="disabled", corner_radius=14)
        self.cancel_btn.grid(row=0, column=1, padx=(12, 0))

        # progress card
        prog = self.card()
        self.status_label = ctk.CTkLabel(prog, text="Ready", font=ctk.CTkFont(size=17, weight="bold"))
        self.status_label.pack(anchor="w", padx=20, pady=(16, 0))
        self.title_label = ctk.CTkLabel(prog, text="Paste a link to get started", text_color=MUTED, anchor="w")
        self.title_label.pack(anchor="w", padx=20)
        self.item_bar = ctk.CTkProgressBar(prog, height=18, corner_radius=9, progress_color=PINK, fg_color=FIELD)
        self.item_bar.set(0)
        self.item_bar.pack(fill="x", padx=20, pady=(12, 6))
        self.detail_label = ctk.CTkLabel(prog, text="", text_color=MUTED, anchor="w")
        self.detail_label.pack(anchor="w", padx=20, pady=(0, 14))

        self.overall_frame = ctk.CTkFrame(prog, fg_color="transparent")
        self.overall_label = ctk.CTkLabel(self.overall_frame, text="Playlist progress", font=bold)
        self.overall_label.pack(anchor="w")
        self.overall_bar = ctk.CTkProgressBar(self.overall_frame, height=12, corner_radius=6,
                                              progress_color=SUCCESS, fg_color=FIELD)
        self.overall_bar.set(0)
        self.overall_bar.pack(fill="x", pady=(8, 16))

        # log card
        logc = self.card()
        ctk.CTkLabel(logc, text="📜  Activity", font=bold).pack(anchor="w", padx=20, pady=(14, 6))
        self.log_box = ctk.CTkTextbox(logc, height=120, corner_radius=12, fg_color=FIELD,
                                      font=ctk.CTkFont(family="Consolas", size=12), state="disabled")
        self.log_box.pack(fill="x", padx=20, pady=(0, 18))

    # ---------------------------------------------------------------- small UI helpers
    def toggle_theme(self):
        mode = "dark" if self.theme_switch.get() else "light"
        ctk.set_appearance_mode(mode)
        self.cfg["theme"] = mode

    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{datetime.now():%H:%M:%S}] {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def dialog(self, title, heading, message, color, actions):
        Dialog(self, title, heading, message, color, actions)

    def paste_url(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, text)

    def browse(self):
        folder = filedialog.askdirectory(initialdir=self.folder_entry.get() or DEFAULT_DIR)
        if folder:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, os.path.normpath(folder))

    def open_folder(self):
        folder = self.folder_entry.get().strip() or DEFAULT_DIR
        os.makedirs(folder, exist_ok=True)
        open_path(folder)

    def on_mode_change(self, value):
        self.mode = "a" if "Audio" in value else "v"
        values = AUDIO_QUALITIES if self.mode == "a" else VIDEO_QUALITIES
        self.quality_menu.configure(values=values)
        self.quality_var.set("192 kbps" if self.mode == "a" else "Best available")

    def get_url(self):
        return self.url_entry.get().strip()

    def set_busy(self, busy):
        self.downloading = busy
        state = "disabled" if busy else "normal"
        for w in (self.mode_seg, self.quality_menu, self.playlist_switch, self.folder_entry,
                  self.browse_btn, self.fetch_btn, self.paste_btn, self.url_entry):
            w.configure(state=state)
        self.download_btn.configure(state=state, text="Downloading…" if busy else "⬇   Download")
        self.cancel_btn.configure(state="normal" if busy else "disabled", text="Cancel")

    def start_indeterminate(self):
        if not self.indeterminate:
            self.indeterminate = True
            self.item_bar.configure(mode="indeterminate")
            self.item_bar.start()

    def stop_indeterminate(self):
        if self.indeterminate:
            self.indeterminate = False
            self.item_bar.stop()
            self.item_bar.configure(mode="determinate")

    def reset_progress(self):
        self.stop_indeterminate()
        self.item_bar.set(0)
        self.overall_bar.set(0)
        self.overall_frame.pack_forget()
        self.detail_label.configure(text="")

    # ---------------------------------------------------------------- fetch info
    def fetch_info(self):
        if self.downloading:
            return
        url = self.get_url()
        if not URL_RE.match(url):
            self.dialog("Invalid link", "Invalid link", "Please paste a valid link starting with http:// or https://.",
                        WARNING, [("OK", None, "secondary")])
            return
        self.fetch_btn.configure(state="disabled", text="Fetching…")
        self.status_label.configure(text="Fetching info…")
        threading.Thread(target=self.fetch_worker, args=(url, self.playlist_var.get()), daemon=True).start()

    @staticmethod
    def pick_thumb(d):
        if not d:
            return None
        return d.get("thumbnail") or ((d.get("thumbnails") or [{}])[-1].get("url"))

    def fetch_worker(self, url, playlist_mode):
        try:
            opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist",
                    "noplaylist": not playlist_mode, "socket_timeout": 20, "logger": SilentLogger()}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = [e for e in (info.get("entries") or []) if e]
            is_pl = info.get("_type") == "playlist" or bool(entries)
            thumb = self.pick_thumb(info) or (self.pick_thumb(entries[0]) if entries else None)
            self.msg_queue.put(("info", {
                "title": info.get("title") or "Untitled",
                "uploader": info.get("uploader") or info.get("channel") or "",
                "duration": info.get("duration"),
                "is_playlist": is_pl,
                "count": info.get("playlist_count") or len(entries),
            }))
            if thumb:
                try:
                    req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
                    raw = urllib.request.urlopen(req, timeout=10).read()
                    img = ImageOps.fit(Image.open(io.BytesIO(raw)).convert("RGB"), (320, 180))
                    self.msg_queue.put(("thumb", img))
                except Exception:
                    pass
        except Exception as e:
            heading, text = friendly_error(e)
            self.msg_queue.put(("info_error", heading, text))

    # ---------------------------------------------------------------- download
    def start_download(self):
        if self.downloading:
            return
        url = self.get_url()
        if not URL_RE.match(url):
            self.dialog("Invalid link", "Invalid link", "Please paste a valid link starting with http:// or https://.",
                        WARNING, [("OK", None, "secondary")])
            return
        if not FFMPEG_DIR:
            self.dialog("FFmpeg missing", "FFmpeg not found",
                        "FFmpeg is required for MP3 conversion and video merging. Install it and restart the app.",
                        DANGER, [("OK", None, "secondary")])
            return
        out_dir = self.folder_entry.get().strip() or DEFAULT_DIR
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            self.dialog("Folder error", "Can't use that folder", str(e), DANGER, [("OK", None, "secondary")])
            return

        self.cfg["folder"] = out_dir
        self.cancel_event.clear()
        self.reset_progress()
        self.set_busy(True)
        self.status_label.configure(text="Starting…")
        self.title_label.configure(text=url[:80])
        self.log(f"Starting {'playlist' if self.playlist_var.get() else 'download'}: {url}")
        threading.Thread(
            target=self.download_worker,
            args=(url, self.mode, self.quality_var.get(), self.playlist_var.get(), out_dir),
            daemon=True,
        ).start()

    def cancel_download(self):
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled", text="Cancelling…")
        self.status_label.configure(text="Cancelling…")

    def build_options(self, mode, quality, playlist_mode, out_dir, logger):
        if playlist_mode:
            outtmpl = os.path.join(out_dir, "%(playlist_title|Playlist)s",
                                   "%(playlist_index|0)03d - %(title)s.%(ext)s")
        else:
            outtmpl = os.path.join(out_dir, "%(title)s.%(ext)s")

        opts = {
            "outtmpl": outtmpl,
            "noplaylist": not playlist_mode,
            "ignoreerrors": bool(playlist_mode),   # one failed playlist item doesn't stop the rest
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
            "quiet": True,
            "noprogress": True,
            "logger": logger,
            "progress_hooks": [self.progress_hook],
            "postprocessor_hooks": [self.postprocessor_hook],
            "post_hooks": [self.post_hook],
            "ffmpeg_location": FFMPEG_DIR,
        }

        if mode == "a":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality.split()[0]},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ]
        else:
            if quality.startswith("Best"):
                opts["format"] = "bv*+ba[ext=m4a]/bv*+ba/b"
            else:
                h = int(quality.rstrip("p"))
                opts["format"] = f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b"
            opts["merge_output_format"] = "mp4"
        return opts

    def download_worker(self, url, mode, quality, playlist_mode, out_dir):
        logger = QueueLogger(self.msg_queue)
        self.dl = {"mode": mode, "playlist": playlist_mode, "seen": set(), "idx": None,
                   "completed": 0, "last_file": None}
        try:
            with yt_dlp.YoutubeDL(self.build_options(mode, quality, playlist_mode, out_dir, logger)) as ydl:
                ydl.download([url])
            st = self.dl
            if self.cancel_event.is_set():
                self.msg_queue.put(("cancelled",))
            elif st["completed"] == 0:
                if logger.failed:
                    heading, text = friendly_error(logger.last_error)
                else:
                    heading, text = "Nothing downloaded", "No new file was produced. It may already exist in the folder."
                self.msg_queue.put(("error", heading, text))
            else:
                last = st["last_file"]
                self.msg_queue.put(("done", {
                    "completed": st["completed"], "failed": logger.failed, "last_file": last,
                    "folder": os.path.dirname(last) if playlist_mode else out_dir, "playlist": playlist_mode,
                }))
        except DownloadCancelled:
            self.msg_queue.put(("cancelled",))
        except Exception as e:
            if self.cancel_event.is_set():
                self.msg_queue.put(("cancelled",))
            else:
                heading, text = friendly_error(e)
                self.msg_queue.put(("error", heading, text))

    # yt-dlp hooks run on the worker thread -> talk to the UI only through the queue
    def progress_hook(self, d):
        if self.cancel_event.is_set():
            raise DownloadCancelled("Cancelled by user")
        if d.get("status") != "downloading":
            return
        st = self.dl
        info = d.get("info_dict") or {}
        idx = info.get("playlist_index") or 1
        count = (info.get("n_entries") or info.get("playlist_count") or 1) if st["playlist"] else 1
        if idx != st["idx"]:
            st["idx"], st["seen"] = idx, set()

        fname = d.get("filename", "")
        if fname not in st["seen"]:
            st["seen"].add(fname)
            if len(st["seen"]) == 1:
                self.msg_queue.put(("log", f"Downloading: {info.get('title', 'video')}"))

        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        frac = min((d.get("downloaded_bytes") or 0) / total, 1.0) if total else 0.0
        expected = 2 if st["mode"] == "v" else 1
        item_frac = min(((len(st["seen"]) - 1) + frac) / expected, 1.0)
        overall = min(((idx - 1) + item_frac) / count, 1.0)

        parts = [f"{item_frac * 100:.0f}%"]
        if d.get("speed"):
            parts.append(f"{human_bytes(d['speed'])}/s")
        if d.get("eta") is not None:
            parts.append(f"ETA {int(d['eta'])}s")
        self.msg_queue.put(("progress", item_frac, overall, idx, count, info.get("title") or "", "  •  ".join(parts)))

    def postprocessor_hook(self, d):
        if d.get("status") != "started":
            return
        name = str(d.get("postprocessor", "")).lower()
        if "audio" in name:
            self.msg_queue.put(("processing", "Converting to MP3…"))
        elif "merg" in name:
            self.msg_queue.put(("processing", "Merging video and audio…"))

    def post_hook(self, path):
        st = self.dl
        st["completed"] += 1
        st["last_file"] = path
        st["seen"] = set()
        self.msg_queue.put(("log", f"✔ Saved: {os.path.basename(path)}"))

    # ---------------------------------------------------------------- queue -> UI
    def poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                getattr(self, "handle_" + msg[0])(*msg[1:])
        except queue.Empty:
            pass
        self.after(80, self.poll_queue)

    def handle_log(self, text):
        self.log(text)

    def handle_progress(self, item_frac, overall, idx, count, title, detail):
        self.stop_indeterminate()
        self.item_bar.set(item_frac)
        self.detail_label.configure(text=detail)
        self.title_label.configure(text=title[:80])
        if count > 1:
            self.overall_frame.pack(fill="x", padx=20)
            self.overall_bar.set(overall)
            self.overall_label.configure(text=f"Playlist progress  -  item {idx} of {count}  ({overall * 100:.0f}%)")
            self.status_label.configure(text=f"Downloading {idx} of {count}")
        else:
            self.status_label.configure(text="Downloading…")

    def handle_processing(self, text):
        self.status_label.configure(text=text)
        self.detail_label.configure(text="")
        self.start_indeterminate()

    def handle_info(self, data):
        self.info_title.configure(text=data["title"])
        bits = []
        if data["is_playlist"]:
            bits.append(f"Playlist - {data['count']} videos")
        if data["uploader"]:
            bits.append(data["uploader"])
        if data["duration"]:
            bits.append(fmt_duration(data["duration"]))
        self.info_meta.configure(text="   •   ".join(bits))
        self.thumb_label.configure(image=self.blank_thumb)
        self.info_card.pack(fill="x", padx=14, pady=8, after=self.url_card)
        self.playlist_var.set(data["is_playlist"])
        self.fetch_btn.configure(state="normal", text="Fetch Info")
        self.status_label.configure(text="Ready")
        self.title_label.configure(text=data["title"][:80])
        self.log(f"Found: {data['title']}" + (f" ({data['count']} videos)" if data["is_playlist"] else ""))

    def handle_thumb(self, img):
        self.thumb_img = ctk.CTkImage(light_image=img, dark_image=img, size=(160, 90))
        self.thumb_label.configure(image=self.thumb_img)

    def handle_info_error(self, heading, text):
        self.fetch_btn.configure(state="normal", text="Fetch Info")
        self.status_label.configure(text="Ready")
        self.log(f"✖ {heading}: {text}")
        self.dialog(heading, heading, text, DANGER, [("OK", None, "secondary")])

    def handle_done(self, r):
        self.stop_indeterminate()
        self.item_bar.set(1)
        if r["playlist"]:
            self.overall_bar.set(1)
        self.set_busy(False)
        ok, bad = r["completed"], r["failed"]
        self.status_label.configure(text="Completed ✔" if not bad else "Completed with errors")
        self.detail_label.configure(text="")

        if r["playlist"]:
            msg = f"{ok} item(s) downloaded" + (f", {bad} failed." if bad else ".")
            actions = [("Open folder", lambda: open_path(r["folder"]), "primary"), ("Close", None, "secondary")]
        else:
            msg = os.path.basename(r["last_file"])
            actions = [("Open", lambda: open_path(r["last_file"]), "primary"),
                       ("Show in folder", lambda: show_in_folder(r["last_file"]), "secondary"),
                       ("Close", None, "secondary")]
        heading = "Download complete!" if not bad else "Finished with some errors"
        self.dialog("Done", ("✅ " if not bad else "⚠ ") + heading, msg, SUCCESS if not bad else WARNING, actions)

    def handle_error(self, heading, text):
        self.stop_indeterminate()
        self.item_bar.set(0)
        self.set_busy(False)
        self.status_label.configure(text="Failed ✖")
        self.log(f"✖ {heading}: {text}")
        self.dialog("Error", "❌ " + heading, text, DANGER, [("OK", None, "secondary")])

    def handle_cancelled(self):
        self.stop_indeterminate()
        self.item_bar.set(0)
        self.set_busy(False)
        self.status_label.configure(text="Cancelled")
        self.detail_label.configure(text="")
        self.log("Download cancelled.")

    # ---------------------------------------------------------------- exit
    def on_close(self):
        if self.downloading and not messagebox.askyesno(APP_NAME, "A download is in progress. Quit anyway?"):
            return
        self.cancel_event.set()
        self.cfg["folder"] = self.folder_entry.get().strip() or DEFAULT_DIR
        save_config(self.cfg)
        if self._banner_resize_job is not None:
            try:
                self.after_cancel(self._banner_resize_job)
            except tk.TclError:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()