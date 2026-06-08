"""
Media Organizer
- Single watch folder (e.g. Videos)
- Files with season/episode info  → Series output folder
- Files with no episode info      → Movie output folder, renamed via TMDB
- Movie naming:
    Online + TMDB match           → Title (Year).ext
    Online + no match             → Cleaned title (year if present).ext
    Offline + year in filename    → Cleaned title (Year).ext
    Offline + no year             → Cleaned title.ext
- Dual-tab logs, stats bar, duplicate detection, system tray, auto-start
"""

import os, re, shutil, time, json, pathlib, threading, sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ── watchdog (bundled in exe, imported directly) ──────────────────────────────
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_OK = True
except ImportError:
    WATCHDOG_OK = False

# ── requests (bundled in exe) ─────────────────────────────────────────────────
try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

# ── pystray + Pillow (optional tray icon) ────────────────────────────────────
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_OK = True
except ImportError:
    TRAY_OK = False

# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════
VIDEO_EXT   = {'.mp4','.mkv','.avi','.mov','.wmv','.m4v','.ts','.flv'}
QUALITY_TAG = re.compile(
    r'\b(2160p|1080p|1080i|720p|480p|4k|uhd|hd|bluray|blu.ray|bdrip|brrip|'
    r'webrip|web.dl|webdl|hdtv|dvdrip|dvdscr|hdrip|xvid|x264|x265|hevc|avc|'
    r'aac|ac3|dts|dd5|h264|h265|repack|proper|extended|theatrical|unrated|'
    r'directors.cut|remux|10bit|hdr|dv|atmos|truehd|multi|dubbed|subbed)\b',
    re.I)
YEAR_PAT    = re.compile(r'\b(19[5-9]\d|20[0-3]\d)\b')
CONFIG_PATH = pathlib.Path.home() / ".media_organizer_config.json"
TMDB_KEY    = ""   # paste your free key from themoviedb.org here

BG     = "#1a1a2e"
PANEL  = "#16213e"
PANEL2 = "#0f3460"
ACCENT = "#e94560"
GREEN  = "#52b788"
BLUE   = "#58a6ff"
MUTED  = "#8892a4"
TEXT   = "#eaeaea"
MONO   = ("Consolas", 9)
UI     = ("Segoe UI", 9)
UI_B   = ("Segoe UI Semibold", 9)

# ═══════════════════════════════════════════════════════════════════════════════
#  STATS
# ═══════════════════════════════════════════════════════════════════════════════
class Stats:
    episodes = 0
    movies   = 0
    dupes    = 0

STATS = Stats()

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
def load_cfg():
    try:    return json.loads(CONFIG_PATH.read_text())
    except: return {}

def save_cfg(data):
    try:    CONFIG_PATH.write_text(json.dumps(data))
    except: pass

# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
WORD_NUM = {
    'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,
    'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12,'thirteen':13,
    'fourteen':14,'fifteen':15,'sixteen':16,'seventeen':17,'eighteen':18,
    'nineteen':19,'twenty':20,
}

def w2n(t):
    if not t: return None
    t = t.strip().lower()
    return int(t) if t.isdigit() else WORD_NUM.get(t)

def _clean_show(s):
    s = re.sub(r'[._\-]', ' ', s).strip().title()
    return re.sub(r'\s+', ' ', s)

def _safe_move(src, dest_dir, new_name):
    os.makedirs(dest_dir, exist_ok=True)
    dest      = os.path.join(dest_dir, new_name)
    base, ext = os.path.splitext(new_name)
    c = 1; is_dupe = False
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{base}_{c}{ext}")
        c += 1; is_dupe = True
    if is_dupe: STATS.dupes += 1
    shutil.move(src, dest)
    return dest, is_dupe

def _win_safe(name):
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()

# ═══════════════════════════════════════════════════════════════════════════════
#  SERIES DETECTION & SORTING
# ═══════════════════════════════════════════════════════════════════════════════
EP_PATTERNS = [
    re.compile(r'^(?P<show>.+?)[.\s_-]+[Ss](?P<season>\d{1,2})[Ee](?P<episode>\d{1,2})', re.I),
    re.compile(r'^(?P<show>.+?)[.\s_-]+(?P<season>\d{1,2})[xX](?P<episode>\d{1,2})'),
    re.compile(r'^(?P<show>.+?)[.\s_-]+[Ss]eason[\s._-]+(?P<season>\d{1,2})[\s._,;-]+[Ee]p(?:isode)?[\s._-]*(?P<episode>\d{1,2})', re.I),
    re.compile(r'^(?P<show>.+?)[.\s_-]+[Ss]eason[\s._-]+(?P<season>[a-z]+?)[\s._,;-]+[Ee]p(?:isode)?[\s._-]*(?P<episode>[a-z]+?)(?:[.\s_-]|$)', re.I),
    re.compile(r'^(?P<show>.+?)[.\s_-]+[Ss]e(?:a)?(?P<season>\d{1,2})[\s._-]+[Ee]p(?P<episode>\d{1,2})', re.I),
    re.compile(r'^(?P<show>.+?)[.\s_-]+(?P<season>\d)(?P<episode>\d{2})(?:[.\s_-]|$)'),
    re.compile(r'^(?P<show>.+?)[.\s_-]+[Ee]p(?:isode)?[\s._-]*(?P<episode>\d{1,2})(?:[.\s_-]|$)', re.I),
]

FOLDER_PATTERNS = [
    re.compile(r'^(?P<show>.+?)[.\s_-]+[Ss](?:eason)?[\s._-]*(?P<season>\d{1,2})\s*$', re.I),
    re.compile(r'^(?P<show>.+?)[.\s_-]+[Ss]eason[\s._-]+(?P<season>[\w]+)\s*$', re.I),
    re.compile(r'^(?P<show>.+?)[.\s_-]+(?P<season>\d{1,2})[xX]\s*$', re.I),
    re.compile(r'^(?P<show>.+)$'),
]

def parse_episode(filename):
    name, _ = os.path.splitext(filename)
    for pat in EP_PATTERNS:
        for cand in (name, re.sub(r'[_.]', ' ', name)):
            m = pat.match(cand)
            if not m: continue
            show    = _clean_show(m.group('show'))
            raw_s   = m.groupdict().get('season')
            raw_e   = m.group('episode')
            season  = w2n(raw_s) if raw_s else None
            episode = w2n(raw_e)
            if episode and episode > 0:
                return show, season, episode
    return None

def is_episode(filename):
    return parse_episode(filename) is not None

def parse_folder_name(folder_name):
    norm = re.sub(r'[_.]', ' ', folder_name).strip()
    for pat in FOLDER_PATTERNS:
        for cand in (folder_name, norm):
            m = pat.match(cand.strip())
            if not m: continue
            show = _clean_show(m.group('show'))
            raw  = m.groupdict().get('season')
            if show:
                return show, w2n(raw) if raw else None
    return folder_name.title(), None

def sort_episode(filepath, series_dir, log):
    fname = os.path.basename(filepath)
    ext   = os.path.splitext(fname)[1].lower()
    if ext not in VIDEO_EXT: return False
    result = parse_episode(fname)
    if not result: return False
    show, season, ep = result
    if season is None:
        dest_dir = os.path.join(series_dir, show, "Unsorted")
        new_name = fname
    else:
        dest_dir = os.path.join(series_dir, show, f"Season {season}")
        new_name = f"{season}{ep:02d}{ext}"
    _, dupe = _safe_move(filepath, dest_dir, new_name)
    label   = f"Season {season}" if season else "Unsorted"
    log(f"📺 {fname}  →  {show}/{label}/{new_name}" + (" [DUPLICATE]" if dupe else ""))
    STATS.episodes += 1
    return True

def sort_episode_folder(folderpath, series_dir, log):
    folder           = os.path.basename(folderpath.rstrip("/\\"))
    f_show, f_season = parse_folder_name(folder)
    log(f"📁 Series folder: {folder}  (show='{f_show}', season={f_season or '?'})")
    count = 0
    for fname in os.listdir(folderpath):
        fpath = os.path.join(folderpath, fname)
        if not os.path.isfile(fpath): continue
        ext = os.path.splitext(fname)[1].lower()
        if ext not in VIDEO_EXT: continue
        result = parse_episode(fname) or parse_episode(f"{f_show} {fname}")
        if result:
            show, season, ep = result
            if season is None and f_season: season = f_season
            if not show or show.lower() in ('episode','ep'): show = f_show
        else:
            log(f"⚠  Skipped: {fname}"); continue
        if season is None:
            dest_dir, new_name = os.path.join(series_dir, show, "Unsorted"), fname
        else:
            dest_dir = os.path.join(series_dir, show, f"Season {season}")
            new_name = f"{season}{ep:02d}{ext}"
        _, dupe = _safe_move(fpath, dest_dir, new_name)
        label   = f"Season {season}" if season else "Unsorted"
        log(f"📺 {fname}  →  {show}/{label}/{new_name}" + (" [DUPLICATE]" if dupe else ""))
        STATS.episodes += 1
        count += 1
    try:
        if not os.listdir(folderpath): os.rmdir(folderpath)
    except: pass
    log(f"── Folder done ({count} file(s)) ──\n")

# ═══════════════════════════════════════════════════════════════════════════════
#  MOVIE DETECTION & SORTING
# ═══════════════════════════════════════════════════════════════════════════════
def clean_movie_name(filename):
    name, _ = os.path.splitext(filename)
    name     = re.sub(r'[._]', ' ', name)
    year_m   = YEAR_PAT.search(name)
    year     = int(year_m.group()) if year_m else None
    cut      = year_m.start() if year_m else len(name)
    q_m      = QUALITY_TAG.search(name)
    if q_m and q_m.start() < cut: cut = q_m.start()
    title = re.sub(r'\s+', ' ', name[:cut]).strip(" -.,_").title()
    return title, year

def tmdb_lookup(title, year=None):
    """TMDB lookup — only called in a background thread, never on startup."""
    if not REQUESTS_OK or not TMDB_KEY:
        return None
    try:
        params = {"api_key": TMDB_KEY, "query": title, "language": "en-US"}
        if year: params["year"] = year
        r = requests.get("https://api.themoviedb.org/3/search/movie",
                         params=params, timeout=5)
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            top  = results[0]
            ttl  = top.get("title", title)
            date = top.get("release_date", "")
            yr   = int(date[:4]) if date else year
            return ttl, yr
    except Exception:
        pass
    return None

def build_movie_filename(title, year, ext):
    name = f"{title} ({year}){ext}" if year else f"{title}{ext}"
    return _win_safe(name)

def sort_movie(filepath, movie_dir, log):
    fname = os.path.basename(filepath)
    ext   = os.path.splitext(fname)[1].lower()
    if ext not in VIDEO_EXT: return

    raw_title, raw_year = clean_movie_name(fname)
    tmdb = tmdb_lookup(raw_title, raw_year)

    if tmdb:
        title, year = tmdb
        log(f"🌐 TMDB: '{raw_title}' → '{title}' ({year})")
    else:
        title, year = raw_title, raw_year
        if not TMDB_KEY:
            log(f"ℹ  No TMDB key set — using cleaned name: '{title}'" +
                (f" ({year})" if year else ""))
        else:
            log(f"📴 No TMDB match — using cleaned name: '{title}'" +
                (f" ({year})" if year else ""))

    new_name       = build_movie_filename(title, year, ext)
    _, dupe        = _safe_move(filepath, movie_dir, new_name)
    log(f"🎬 {fname}  →  {new_name}" + (" [DUPLICATE]" if dupe else ""))
    STATS.movies  += 1

def sort_movie_folder(folderpath, movie_dir, log):
    folder = os.path.basename(folderpath.rstrip("/\\"))
    log(f"📁 Movie folder: {folder}")
    count  = 0
    for fname in os.listdir(folderpath):
        fpath = os.path.join(folderpath, fname)
        if os.path.isfile(fpath) and os.path.splitext(fname)[1].lower() in VIDEO_EXT:
            sort_movie(fpath, movie_dir, log)
            count += 1
    try:
        if not os.listdir(folderpath): os.rmdir(folderpath)
    except: pass
    log(f"── Folder done ({count} file(s)) ──\n")

# ═══════════════════════════════════════════════════════════════════════════════
#  SMART ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
def route_file(filepath, series_dir, movie_dir, s_log, m_log):
    fname = os.path.basename(filepath)
    ext   = os.path.splitext(fname)[1].lower()
    if ext not in VIDEO_EXT: return
    if is_episode(fname):
        sort_episode(filepath, series_dir, s_log)
    else:
        sort_movie(filepath, movie_dir, m_log)

def route_folder(folderpath, series_dir, movie_dir, s_log, m_log):
    folder = os.path.basename(folderpath.rstrip("/\\"))
    try:
        files = [f for f in os.listdir(folderpath)
                 if os.path.isfile(os.path.join(folderpath, f))
                 and os.path.splitext(f)[1].lower() in VIDEO_EXT]
    except Exception:
        return

    _, f_season      = parse_folder_name(folder)
    has_episodes     = any(is_episode(f) for f in files)
    folder_is_series = f_season is not None

    if has_episodes or folder_is_series:
        sort_episode_folder(folderpath, series_dir, s_log)
    else:
        sort_movie_folder(folderpath, movie_dir, m_log)

# ═══════════════════════════════════════════════════════════════════════════════
#  WATCHDOG HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
if WATCHDOG_OK:
    class UnifiedHandler(FileSystemEventHandler):
        def __init__(self, series_dir, movie_dir, s_log, m_log):
            self.series_dir = series_dir
            self.movie_dir  = movie_dir
            self.s_log      = s_log
            self.m_log      = m_log

        def on_created(self, event):
            # Wait for file to finish copying
            time.sleep(2)
            try:
                if event.is_directory:
                    route_folder(event.src_path, self.series_dir,
                                 self.movie_dir, self.s_log, self.m_log)
                else:
                    route_file(event.src_path, self.series_dir,
                               self.movie_dir, self.s_log, self.m_log)
            except Exception as e:
                self.s_log(f"⚠ Error processing file: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════════════
class App:
    def __init__(self, root):
        self.root     = root
        self.observer = None
        self.running  = False
        root.title("Media Organizer")
        root.geometry("780x600")
        root.configure(bg=BG)
        root.resizable(False, False)
        self._build_ui()

    def _build_ui(self):
        tk.Frame(self.root, bg=ACCENT, height=4).pack(fill="x")

        hdr = tk.Frame(self.root, bg=BG, pady=10)
        hdr.pack(fill="x", padx=20)
        tk.Label(hdr, text="🎬  Media Organizer",
                 font=("Segoe UI Semibold", 16), bg=BG, fg=TEXT).pack(side="left")
        tk.Label(hdr, text="series · movies · auto-sort",
                 font=UI, bg=BG, fg=MUTED).pack(side="left", padx=10)

        # Stats bar
        sb = tk.Frame(self.root, bg=PANEL2, pady=6)
        sb.pack(fill="x", padx=20, pady=(0, 8))
        self.lbl_eps  = tk.Label(sb, text="Episodes sorted: 0", font=UI, bg=PANEL2, fg=GREEN)
        self.lbl_mov  = tk.Label(sb, text="Movies sorted: 0",   font=UI, bg=PANEL2, fg=BLUE)
        self.lbl_dupe = tk.Label(sb, text="Duplicates: 0",      font=UI, bg=PANEL2, fg=MUTED)
        for w in (self.lbl_eps, self.lbl_mov, self.lbl_dupe):
            w.pack(side="left", padx=16)

        # Folder pickers
        self.watch_var  = self._folder_row("Watch Folder",  "(your Videos folder)",      self._browse("watch_var"))
        self.series_var = self._folder_row("Series Output", "(organised TV library)",     self._browse("series_var"))
        self.movie_var  = self._folder_row("Movies Output", "(organised movie library)",  self._browse("movie_var"))

        # Buttons
        btn_row = tk.Frame(self.root, bg=BG); btn_row.pack(fill="x", padx=20, pady=4)
        tk.Button(btn_row, text="▶  Sort files already in Watch Folder",
                  command=self._scan, bg=PANEL2, fg=MUTED, relief="flat",
                  font=UI, cursor="hand2", padx=10, pady=4).pack(side="left")

        ctrl = tk.Frame(self.root, bg=BG); ctrl.pack(fill="x", padx=20, pady=2)
        self.toggle_btn = tk.Button(ctrl, text="▶  Start Watching",
                                    command=self._toggle,
                                    bg=ACCENT, fg="white", relief="flat",
                                    font=("Segoe UI Semibold", 10),
                                    cursor="hand2", padx=14, pady=7)
        self.toggle_btn.pack(side="left")
        self.status_lbl = tk.Label(ctrl, text="⏸  Idle", font=UI, bg=BG, fg=MUTED)
        self.status_lbl.pack(side="left", padx=12)

        if not WATCHDOG_OK:
            tk.Label(ctrl, text="⚠ watchdog not available — auto-watch disabled",
                     font=UI, bg=BG, fg=ACCENT).pack(side="left", padx=8)

        # Tabs
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook",     background=BG,    borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                        font=UI_B, padding=[14, 5])
        style.map("TNotebook.Tab",
                  background=[("selected", PANEL2)],
                  foreground=[("selected", TEXT)])

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=20, pady=(4, 10))

        s_tab = tk.Frame(nb, bg=BG)
        m_tab = tk.Frame(nb, bg=BG)
        nb.add(s_tab, text="  📺  Series Log  ")
        nb.add(m_tab, text="  🎬  Movie Log  ")

        self.s_log_box = self._log_box(s_tab)
        self.m_log_box = self._log_box(m_tab)

        self._refresh_stats()

    def _folder_row(self, label, hint, cmd):
        frame = tk.Frame(self.root, bg=BG); frame.pack(fill="x", padx=20, pady=3)
        tk.Label(frame, text=label, font=UI_B, bg=BG, fg=TEXT,
                 width=15, anchor="w").pack(side="left")
        var = tk.StringVar()
        tk.Entry(frame, textvariable=var, bg=PANEL, fg=TEXT, relief="flat",
                 font=UI, insertbackground=TEXT).pack(
                 side="left", fill="x", expand=True, ipady=5, padx=(0,8))
        tk.Button(frame, text="Browse", command=cmd, bg=ACCENT, fg="white",
                  relief="flat", font=UI, cursor="hand2",
                  activebackground="#c73652", padx=10, pady=3).pack(side="left")
        tk.Label(frame, text=hint, font=("Segoe UI", 8),
                 bg=BG, fg=MUTED).pack(side="left", padx=8)
        return var

    def _browse(self, attr):
        def _cb():
            d = filedialog.askdirectory()
            if d: getattr(self, attr).set(d)
        return _cb

    def _log_box(self, parent):
        lf = tk.Frame(parent, bg=PANEL); lf.pack(fill="both", expand=True, pady=4)
        tk.Label(lf, text="Activity Log", font=UI_B, bg=PANEL,
                 fg=MUTED).pack(anchor="w", padx=10, pady=(6,2))
        box = scrolledtext.ScrolledText(lf, bg="#0d1117", fg=BLUE,
                                        font=MONO, relief="flat",
                                        state="disabled", wrap="word")
        box.pack(fill="both", expand=True, padx=10, pady=(0,8))
        return box

    def _log(self, box, msg):
        def _w():
            box.configure(state="normal")
            box.insert("end", msg + "\n")
            box.see("end")
            box.configure(state="disabled")
        self.root.after(0, _w)

    def s_log(self, msg): self._log(self.s_log_box, msg)
    def m_log(self, msg): self._log(self.m_log_box, msg)

    def _refresh_stats(self):
        self.lbl_eps.configure( text=f"Episodes sorted: {STATS.episodes}")
        self.lbl_mov.configure( text=f"Movies sorted: {STATS.movies}")
        self.lbl_dupe.configure(text=f"Duplicates: {STATS.dupes}")
        self.root.after(3000, self._refresh_stats)

    def _toggle(self):
        if self.running: self._stop()
        else:            self._start()

    def _start(self):
        if not WATCHDOG_OK:
            messagebox.showwarning("Not available",
                "watchdog library not found. Use 'Sort files' button instead.")
            return
        watch  = self.watch_var.get().strip()
        series = self.series_var.get().strip()
        movies = self.movie_var.get().strip()
        if not watch or not series or not movies:
            messagebox.showwarning("Missing folders", "Please set all three folders first.")
            return
        if not os.path.isdir(watch):
            messagebox.showerror("Invalid folder", f"Watch folder not found:\n{watch}")
            return
        handler       = UnifiedHandler(series, movies, self.s_log, self.m_log)
        self.observer = Observer()
        self.observer.schedule(handler, watch, recursive=False)
        self.observer.start()
        self.running  = True
        self.toggle_btn.configure(text="⏹  Stop Watching", bg="#2d6a4f")
        self.status_lbl.configure(text="🟢  Watching…", fg=GREEN)
        self.s_log(f"👀 Watching: {watch}")
        self.s_log(f"   Series → {series}")
        self.m_log(f"   Movies → {movies}\n")
        _save(self)

    def _stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self.running = False
        self.toggle_btn.configure(text="▶  Start Watching", bg=ACCENT)
        self.status_lbl.configure(text="⏸  Idle", fg=MUTED)
        self.s_log("⏹ Watcher stopped.\n")

    def _scan(self):
        watch  = self.watch_var.get().strip()
        series = self.series_var.get().strip()
        movies = self.movie_var.get().strip()
        if not watch or not series or not movies:
            messagebox.showwarning("Missing folders", "Please set all three folders first.")
            return
        def _run():
            self.s_log("── Scanning Watch Folder ──")
            try:
                for name in os.listdir(watch):
                    fp = os.path.join(watch, name)
                    if os.path.isdir(fp):
                        route_folder(fp, series, movies, self.s_log, self.m_log)
                    elif os.path.isfile(fp):
                        route_file(fp, series, movies, self.s_log, self.m_log)
            except Exception as e:
                self.s_log(f"⚠ Error during scan: {e}")
            self.s_log("── Scan complete ──\n")
        threading.Thread(target=_run, daemon=True).start()

    def on_close(self):
        self._stop()
        _save(self)
        self.root.destroy()

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _save(app):
    save_cfg({
        "watch":  app.watch_var.get(),
        "series": app.series_var.get(),
        "movies": app.movie_var.get(),
    })

def _restore(app, cfg):
    for k, attr in (("watch","watch_var"),("series","series_var"),("movies","movie_var")):
        if cfg.get(k): getattr(app, attr).set(cfg[k])

def _autostart(app, cfg):
    if cfg.get("watch") and cfg.get("series") and cfg.get("movies"):
        app.s_log("🔄 Resuming previous session…")
        app._start()
    else:
        app.s_log("👋 Set your three folders above, then click Start Watching.")
        app.s_log("   Your settings will be remembered for next time.\n")

# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    cfg  = load_cfg()
    root = tk.Tk()
    app  = App(root)
    _restore(app, cfg)
    # Delay autostart by 1 second so UI fully loads first
    root.after(1000, lambda: _autostart(app, cfg))
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
