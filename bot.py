"""
Zyrox Video Downloader Telegram Bot v5 — Button-based UI
- Force Join: @ZyroxV1 + @ZyroxMod
- Live download/upload progress bar with %, thumbnail, size & length

- Pyrogram (MTProto) + yt-dlp, large files via MTProto
- YouTube 403 bypass via Android player client
- Full button menus: user menu + admin panel (no commands needed)
- Broadcast / block / stats / force-join channels, all via buttons
- Menu button (Start/Help) set automatically on startup

Env: API_ID, API_HASH, BOT_TOKEN, OWNER_ID, DOWNLOAD_DIR, PORT
"""
import os
import re
import time
import asyncio
import logging
import sqlite3
import urllib.request
import urllib.parse
import json
import html
import shutil
import subprocess
import base64
import zipfile
import threading
import platform as _platform
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
)
from pyrogram.errors import UserNotParticipant, ChannelPrivate

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ytdl-bot")

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# ===== Force Join (Zyrox channels) =====
# Override with env: FORCE_JOIN_CHANNELS="ZyroxV1,ZyroxMod"  FORCE_JOIN=on/off
FORCE_CHANNELS = [
    c.strip().lstrip("@")
    for c in os.environ.get("FORCE_JOIN_CHANNELS", "ZyroxV1,ZyroxMod").split(",")
    if c.strip()
]
FORCE_JOIN_ENABLED = os.environ.get("FORCE_JOIN", "on").strip().lower() in ("on", "1", "true", "yes")

# ===== Keep-alive (Antideploy/Fly auto-stop fix) =====
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL", "https://suma.antideploy.app/").strip()
try:
    KEEPALIVE_SECONDS = max(60, int(os.environ.get("KEEPALIVE_SECONDS", "180")))
except ValueError:
    KEEPALIVE_SECONDS = 180
CHANNEL_LINKS = [f"https://t.me/{c}" for c in FORCE_CHANNELS]

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise SystemExit("Missing API_ID / API_HASH / BOT_TOKEN env vars")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/ytdl")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
DB_PATH = os.environ.get("DB_PATH", "/tmp/ytdl/bot.db")
COOKIE_DIR = "/tmp/zt_cookies"
os.makedirs(COOKIE_DIR, exist_ok=True)
COOKIE_PATH = os.path.join(COOKIE_DIR, "cookies.txt")
DENO_DIR = os.environ.get("DENO_DIR", "/tmp/deno-bin")

URL_RE = re.compile(r"https?://[^\s]+")
pending = {}
active_tasks = {}
# per-user UI state: user_id -> {"mode": ..., "data": ...}
ui_state = {}

QUALITY_OPTIONS = [
    ("📱 360p",  "bv*[height<=360]+ba/b[height<=360]/b"),
    ("💻 720p",  "bv*[height<=720]+ba/b[height<=720]/b"),
    ("🖥️ 1080p", "bv*[height<=1080]+ba/b[height<=1080]/b"),
    ("🎞️ Best",  "bv*+ba/b"),
    ("🎵 Audio", "bestaudio/b"),
]

# ============ database ============
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT,
                  first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  downloads INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS channels
                 (chat_id INTEGER PRIMARY KEY, title TEXT,
                  invite_link TEXT, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats
                 (date TEXT PRIMARY KEY, downloads INTEGER DEFAULT 0)""")
    c.commit(); c.close()

def get_setting(key, default=None):
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else default

def set_setting(key, value):
    c = db()
    c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
              (key, value))
    c.commit(); c.close()

def register_user(uid, username=None):
    c = db()
    c.execute("INSERT OR IGNORE INTO users (user_id,username) VALUES (?,?)",
              (uid, username))
    if username:
        c.execute("UPDATE users SET username=? WHERE user_id=?",
                  (username, uid))
    c.commit(); c.close()

def is_blocked(uid):
    c = db()
    r = c.execute("SELECT blocked FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return bool(r and r["blocked"])

def bump_downloads(uid):
    c = db()
    c.execute("UPDATE users SET downloads=downloads+1 WHERE user_id=?", (uid,))
    today = time.strftime("%Y-%m-%d")
    c.execute("INSERT INTO stats (date,downloads) VALUES (?,1) "
              "ON CONFLICT(date) DO UPDATE SET downloads=downloads+1", (today,))
    c.commit(); c.close()

def get_channels():
    c = db()
    rows = c.execute("SELECT * FROM channels ORDER BY added_at").fetchall()
    c.close()
    return rows

def add_channel_db(chat_id, title="", invite=""):
    c = db()
    c.execute("INSERT OR REPLACE INTO channels (chat_id,title,invite_link)"
              " VALUES (?,?,?)", (chat_id, title, invite))
    c.commit(); c.close()

def del_channel_db(chat_id):
    c = db()
    c.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
    c.commit(); c.close()

def get_recent_users(limit=10):
    c = db()
    rows = c.execute("SELECT user_id,username,blocked FROM users "
                     "ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return rows

# ============ health check ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        # security headers
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Strict-Transport-Security",
                         "max-age=31536000; includeSubDomains")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a): pass

def run_health_server():
    try:
        HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))),
                   HealthHandler).serve_forever()
    except Exception as e:
        log.warning("health: %s", e)

# ============ keep-alive ============
def run_keepalive():
    if not KEEPALIVE_URL:
        return
    log.info("keepalive: pinging %s every %ss", KEEPALIVE_URL, KEEPALIVE_SECONDS)
    while True:
        time.sleep(KEEPALIVE_SECONDS)
        try:
            req = urllib.request.Request(
                KEEPALIVE_URL, headers={"User-Agent": "zyrox-keepalive/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read(32)
            log.debug("keepalive ok")
        except Exception as e:
            log.warning("keepalive ping failed: %s", e)

# ============ cookies (YouTube datacenter bot-check fix) ============
def get_cookiefile():
    """Cookies.txt path if configured (env or owner-uploaded); never logs contents."""
    try:
        env_path = os.environ.get("YT_COOKIE_FILE", "").strip()
        if env_path and os.path.isfile(env_path) and os.path.getsize(env_path) > 0:
            return env_path
        if os.path.isfile(COOKIE_PATH) and os.path.getsize(COOKIE_PATH) > 0:
            return COOKIE_PATH
        raw = None
        b64 = os.environ.get("YT_COOKIES_B64", "").strip()
        if b64:
            try:
                raw = base64.b64decode(b64).decode("utf-8", "ignore")
            except Exception:
                log.warning("YT_COOKIES_B64 could not be decoded")
        elif os.environ.get("YT_COOKIES"):
            raw = os.environ["YT_COOKIES"]
        if raw:
            with open(COOKIE_PATH, "w", encoding="utf-8") as fh:
                fh.write(raw)
            try: os.chmod(COOKIE_PATH, 0o600)
            except OSError: pass
            log.info("YouTube cookies configured from env (%s bytes)", len(raw))
            return COOKIE_PATH
    except Exception as e:
        log.warning("cookie setup failed: %s", e)
    return None

# ============ JS runtime (deno) for yt-dlp YouTube challenges ============
_DENO_LOCK = threading.Lock()
_DENO_STATE = {"tried": False, "runtime": None, "path": None}

def _verify_runtime(name, path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        ver = ((out.stdout or out.stderr or "").strip().splitlines() or ["?"])[0][:80]
        if out.returncode == 0:
            log.info("JS runtime ready: %s (%s) at %s", name, ver, path)
            return True
    except Exception as e:
        log.warning("JS runtime verify failed for %s: %s", path, e)
    return False

def ensure_js_runtime():
    """Find a JS runtime, or download deno once (buildpack hosts w/o Dockerfile)."""
    if _DENO_STATE["tried"]:
        return _DENO_STATE["runtime"], _DENO_STATE["path"]
    with _DENO_LOCK:
        if _DENO_STATE["tried"]:
            return _DENO_STATE["runtime"], _DENO_STATE["path"]
        _DENO_STATE["tried"] = True
        # deno first — yt-dlp's EJS needs deno, or node >= 22 (node 20 is unsupported)
        found = shutil.which("deno")
        if found and _verify_runtime("deno", found):
            _DENO_STATE.update(runtime="deno", path=found)
            return "deno", found
        cand = os.path.join(DENO_DIR, "deno")
        if os.path.isfile(cand) and _verify_runtime("deno", cand):
            _DENO_STATE.update(runtime="deno", path=cand)
            return "deno", cand
        if os.environ.get("AUTO_INSTALL_DENO", "on").strip().lower() in ("off", "0", "false"):
            log.warning("No JS runtime found and AUTO_INSTALL_DENO is off; YouTube may fail")
            return None, None
        try:
            arch = _platform.machine().lower()
            arch = {"x86_64": "x86_64", "amd64": "x86_64",
                    "aarch64": "aarch64", "arm64": "aarch64"}.get(arch, "x86_64")
            ver = os.environ.get("DENO_VERSION", "v2.5.4").strip()
            dl_url = f"https://dl.deno.land/release/{ver}/deno-{arch}-unknown-linux-gnu.zip"
            log.info("Downloading deno %s (%s) for yt-dlp JS challenges...", ver, arch)
            os.makedirs(DENO_DIR, exist_ok=True)
            zpath = os.path.join(DENO_DIR, "deno.zip")
            req = urllib.request.Request(dl_url, headers={"User-Agent": "zyrox-bot/1.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(zpath, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=1024 * 256)
            with zipfile.ZipFile(zpath) as zf:
                for member in zf.namelist():
                    if os.path.basename(member) == "deno":
                        with zf.open(member) as src, open(cand, "wb") as out:
                            shutil.copyfileobj(src, out, length=1024 * 256)
                        break
            os.remove(zpath)
            os.chmod(cand, 0o755)
            if _verify_runtime("deno", cand):
                os.environ["PATH"] = DENO_DIR + os.pathsep + os.environ.get("PATH", "")
                _DENO_STATE.update(runtime="deno", path=cand)
                return "deno", cand
        except Exception as e:
            log.warning("deno download failed: %s", e)
        for name in ("bun", "node"):
            found2 = shutil.which(name)
            if not found2 or not _verify_runtime(name, found2):
                continue
            if name == "node":
                try:
                    _v = subprocess.run([found2, "--version"], capture_output=True,
                                        text=True, timeout=10).stdout.strip().lstrip("v")
                    if int(_v.split(".")[0]) < 22:
                        log.warning("node %s is too old for yt-dlp EJS (need >=22)", _v)
                        continue
                except Exception:
                    continue
            _DENO_STATE.update(runtime=name, path=found2)
            return name, found2
        log.warning("No JS runtime available; YouTube formats may be missing")
        return None, None

_FFMPEG = {"path": None, "tried": False}

def get_ffmpeg():
    if _FFMPEG["tried"]:
        return _FFMPEG["path"]
    _FFMPEG["tried"] = True
    found = shutil.which("ffmpeg")
    if not found:
        try:
            import imageio_ffmpeg
            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            log.warning("ffmpeg not found (imageio-ffmpeg: %s)", e)
            found = None
    if found:
        log.info("ffmpeg: %s", found)
    _FFMPEG["path"] = found
    return found

# ============ yt-dlp ============
YT_STRATEGIES = [
    ("default", None),
    ("web_embedded", ["web_embedded"]),
    ("android_vr", ["android_vr"]),
    ("web_mweb_android", ["web", "mweb", "android"]),
    ("ios", ["ios"]),
]
_YT_HOST_RE = re.compile(r"(youtube\.com|youtu\.be|youtube-nocookie\.com)", re.I)

def _strategy_order(preferred=None, url=""):
    if url and not _YT_HOST_RE.search(url):
        return [YT_STRATEGIES[0]]
    order = list(YT_STRATEGIES)
    if get_cookiefile():
        order.sort(key=lambda x: 0 if x[0] in ("web_mweb_android", "default") else 1)
    if preferred:
        order.sort(key=lambda x: 0 if x[0] == preferred else 1)
    return order

def _ydl_base_opts(strategy_clients):
    opts = {
        "quiet": True, "no_warnings": False, "noplaylist": True,
        "socket_timeout": 30, "retries": 3, "extractor_retries": 3,
        "force_ipv4": True,
        "remote_components": ["ejs:github"],
    }
    runtime, runtime_path = ensure_js_runtime()
    if runtime and runtime_path:
        opts["js_runtimes"] = {runtime: {"path": runtime_path}}
    cookiefile = get_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if strategy_clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(strategy_clients)}}
    return opts

def _has_playable(info):
    if not isinstance(info, dict):
        return False
    if info.get("url"):
        return True
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        if (f.get("ext") or "") in ("mhtml", "html"):
            continue
        if str(f.get("format_id", "")).startswith("sb"):
            continue
        if (f.get("vcodec") not in (None, "none")) or (f.get("acodec") not in (None, "none")):
            return True
    return False

def extract_info(url: str) -> dict:
    import yt_dlp
    errors = []
    is_yt = bool(_YT_HOST_RE.search(url or ""))
    for name, clients in _strategy_order(url=url):
        opts = _ydl_base_opts(clients)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info is None:
                errors.append(f"{name}: no info")
                continue
            info["_strategy"] = name
            if (not is_yt) or _has_playable(info):
                log.info("yt-dlp extract ok via strategy=%s", name)
                return info
            errors.append(f"{name}: no downloadable formats (Sign in to confirm you're not a bot / SABR?)")
            log.warning("yt-dlp strategy %s returned no playable formats", name)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:220]}")
            log.warning("yt-dlp extract failed strategy=%s: %s", name, str(e)[:300])
    raise RuntimeError(" || ".join(errors)[:900] or "Could not fetch video info")

def _format_fallbacks(format_id, is_audio):
    selectors = [format_id]
    if is_audio:
        selectors += ["bestaudio/best/b", "ba/b/best"]
    else:
        m = re.search(r"height<=(\d+)", format_id or "")
        if m:
            h = m.group(1)
            selectors += [f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b/best",
                          "b/best", "18/b"]
        else:
            selectors += ["bv*+ba/b/best", "b/best", "18/b"]
    seen, out = set(), []
    for sel in selectors:
        if sel not in seen:
            seen.add(sel); out.append(sel)
    return out

def _find_downloaded_file(before, opts, info):
    after = set(os.listdir(DOWNLOAD_DIR))
    new_files = [os.path.join(DOWNLOAD_DIR, f) for f in (after - before)]
    bad_ext = {".mhtml", ".html", ".part", ".ytdl", ".tmp", ".temp"}
    new_files = [f for f in new_files
                 if os.path.isfile(f) and os.path.getsize(f) > 1024
                 and os.path.splitext(f)[1].lower() not in bad_ext]
    if new_files:
        return max(new_files, key=os.path.getsize)
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(opts) as ydl2:
            guess = ydl2.prepare_filename(info)
    except Exception:
        return None
    for cand in [guess, os.path.splitext(guess)[0] + ".mp4",
                 os.path.splitext(guess)[0] + ".mp3",
                 os.path.splitext(guess)[0] + ".mkv",
                 os.path.splitext(guess)[0] + ".webm"]:
        if os.path.isfile(cand) and os.path.getsize(cand) > 1024:
            return cand
    return None

def _cleanup_new_files(before):
    try:
        after = set(os.listdir(DOWNLOAD_DIR))
        for name in (after - before):
            fp = os.path.join(DOWNLOAD_DIR, name)
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass

def download_media(url, format_id, progress_cb=None, preferred_strategy=None):
    import yt_dlp

    def hook(d):
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                try: progress_cb(done/total, d.get("speed") or 0, d.get("eta"), done, total)
                except Exception: pass

    is_audio = format_id.startswith("bestaudio")
    selectors = _format_fallbacks(format_id, is_audio)
    strategies = _strategy_order(preferred=preferred_strategy, url=url)
    errors = []

    for si, (sname, clients) in enumerate(strategies):
        sels = selectors if si == 0 else [selectors[0], selectors[-1]]
        for sel in dict.fromkeys(sels):
            before = set(os.listdir(DOWNLOAD_DIR))
            opts = _ydl_base_opts(clients)
            opts["socket_timeout"] = 60
            opts.update({
                "format": sel,
                "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
                "progress_hooks": [hook],
            })
            ff = get_ffmpeg()
            if ff:
                opts["ffmpeg_location"] = ff
            if is_audio:
                if ff:
                    opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                               "preferredcodec": "mp3",
                                               "preferredquality": "192"}]
            else:
                opts["merge_output_format"] = "mp4"
            try:
                log.info("yt-dlp download attempt strategy=%s format=%s", sname, sel)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                title = (info or {}).get("title", "video")
                path = _find_downloaded_file(before, opts, info)
                if not path:
                    raise FileNotFoundError(
                        "Download finished but no media file found (site may have blocked it)")
                log.info("yt-dlp download ok strategy=%s format=%s file=%s",
                         sname, sel, os.path.basename(path))
                return path, title, info
            except Exception as e:
                em = str(e)
                errors.append(f"{sname}/{sel}: {em[:180]}")
                log.warning("yt-dlp attempt failed strategy=%s format=%s: %s",
                            sname, sel, em[:300])
                _cleanup_new_files(before)
                low = em.lower()
                if "sign in to confirm" in low or "login_required" in low:
                    break
                continue
    raise RuntimeError(" || ".join(errors)[:950] or "Download failed")


def ytdl_error_hint(em):
    low = (em or "").lower()
    if "sign in to confirm" in low or "login_required" in low:
        return ("\n\n💡 YouTube server IP par login maang raha hai. "
                "Owner ko bot me cookies.txt set karni padegi "
                "(owner: /cookies bhejkar file bhejo), phir download chalega.")
    if "requested format is not available" in low:
        return "\n\n💡 Is quality ka format nahi mila — 360p ya Audio try karo."
    if "403" in low:
        return "\n\n💡 YouTube ne 403 block kiya — thodi der baad dobara try karo."
    return ""

def fmt_views(n):
    if not n: return "N/A"
    n = int(n)
    for u, d in (("B",1e9),("M",1e6),("K",1e3)):
        if n >= d: return f"{n/d:.1f}{u}"
    return str(n)

def fmt_dur(s):
    if not s: return "N/A"
    s=int(s); h,r=divmod(s,3600); m,sec=divmod(r,60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

def bar(frac, w=18):
    try:
        frac = max(0.0, min(1.0, float(frac or 0)))
    except Exception:
        frac = 0
    f = int(round(frac * w))
    return "█" * f + "░" * (w - f)

def fmt_speed(bps):
    if not bps: return "—"
    for u in ("B/s","KB/s","MB/s"):
        if bps<1024: return f"{bps:.1f} {u}"
        bps/=1024
    return f"{bps:.1f} GB/s"

def fmt_size(num):
    """Bytes -> 12.4 MB style."""
    if not num:
        return "N/A"
    try:
        num = float(num)
    except Exception:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TB"

def fmt_eta(sec):
    if sec is None:
        return "—"
    try:
        sec = int(sec)
    except Exception:
        return "—"
    if sec < 0:
        return "—"
    m, s_ = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"

def _format_size(fmt, duration):
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    tbr = fmt.get("tbr") or ((fmt.get("vbr") or 0) + (fmt.get("abr") or 0))
    if tbr and duration:
        return int(float(tbr) * 1000 / 8 * float(duration))
    return 0

def estimate_sizes(info):
    """Approx size per QUALITY_OPTIONS entry -> list of byte counts (0=unknown)."""
    try:
        duration = info.get("duration") or 0
        formats = info.get("formats") or []
        audio_sizes = [
            _format_size(f, duration) for f in formats
            if (f.get("vcodec") in (None, "none")) and f.get("acodec") not in (None, "none")
        ]
        best_audio = max(audio_sizes) if audio_sizes else 0

        def for_height(limit):
            cands = []
            for f in formats:
                if f.get("vcodec") in (None, "none"):
                    continue
                h = f.get("height") or 0
                if limit and h and h > limit:
                    continue
                size = _format_size(f, duration)
                if f.get("acodec") in (None, "none"):
                    size += best_audio
                cands.append((h, f.get("tbr") or 0, size))
            if not cands:
                return 0
            cands.sort(key=lambda x: (x[0], x[1]))
            return cands[-1][2]

        return [for_height(360), for_height(720), for_height(1080), for_height(None), best_audio]
    except Exception:
        return [0, 0, 0, 0, 0]

def pick_thumbnail(info):
    thumbs = info.get("thumbnails") or []
    jpg = [t.get("url") for t in thumbs if (t.get("url") or "").lower().split("?")[0].endswith((".jpg", ".jpeg"))]
    if jpg:
        return jpg[-1]
    return info.get("thumbnail")

def download_thumbnail(thumb_url, video_id="thumb"):
    """Download thumbnail locally so it can be attached to the uploaded video/audio."""
    if not thumb_url:
        return None
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(video_id))[:40] or "thumb"
    path = os.path.join(DOWNLOAD_DIR, f"thumb_{safe}_{int(time.time())}.jpg")
    try:
        req = urllib.request.Request(
            thumb_url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read(8 * 1024 * 1024)
        if len(data) < 1024:
            return None
        with open(path, "wb") as fh:
            fh.write(data)
        return path
    except Exception as e:
        log.warning("thumbnail download failed: %s", e)
        return None

# ============ menu button (Telegram side menu) ============
def setup_menu_button():
    """Set the bot's side menu button with Start/Help via Bot HTTP API."""
    try:
        data = urllib.parse.urlencode({
            "menu_button": json.dumps({"type": "commands"}),
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
            data=data)
        urllib.request.urlopen(req, timeout=15).read()

        cmds = json.dumps([
            {"command": "start", "description": "🏠 Main menu"},
            {"command": "help", "description": "❓ How to use"},
        ])
        data = urllib.parse.urlencode({"commands": cmds}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
            data=data)
        urllib.request.urlopen(req, timeout=15).read()
        log.info("menu button + commands set")
    except Exception as e:
        log.warning("menu button setup failed: %s", e)

# ============ bot ============
app = Client("ytdl_bot", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, workers=16)

def is_owner_uid(uid):
    return OWNER_ID and uid == OWNER_ID

# ----- keyboards -----
def main_menu_kb(uid):
    rows = [
        [InlineKeyboardButton("📢 @ZyroxV1", url="https://t.me/ZyroxV1"),
         InlineKeyboardButton("📢 @ZyroxMod", url="https://t.me/ZyroxMod")],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ]
    if is_owner_uid(uid):
        rows.append([InlineKeyboardButton("🛠️ Admin Panel",
                                          callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics", callback_data="admin:stats")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin:broadcast")],
        [InlineKeyboardButton("👥 Users", callback_data="admin:users")],
        [InlineKeyboardButton("📢 Channels", callback_data="admin:channels")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")],
    ])

def channels_kb():
    rows = []
    for ch in get_channels():
        title = ch["title"] or str(ch["chat_id"])
        rows.append([InlineKeyboardButton(
            f"🗑️ {title}", callback_data=f"ch:del:{ch['chat_id']}")])
    fjoin = get_setting("fjoin", "off")
    rows.append([InlineKeyboardButton(
        f"🔒 Force-Join: {'✅ ON' if fjoin=='on' else '❌ OFF'}",
        callback_data="ch:toggle")])
    rows.append([InlineKeyboardButton("➕ Add Channel",
                                      callback_data="ch:add")])
    rows.append([InlineKeyboardButton("🔙 Admin Panel",
                                      callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)

WELCOME = (
    "🚀 <b>Welcome to Zyrox YouTube Downloader Bot</b>\n\n"
    "Send me a YouTube / video link and I will download it for you.\n\n"
    "✅ 360p / 720p / 1080p / Best / Audio MP3\n"
    "✅ Live download + upload progress with %\n"
    "✅ Thumbnail, file size & video length shown\n"
    "✅ Large files via MTProto — no 50MB limit\n\n"
    "📢 <b>Our Channels:</b> @ZyroxV1 • @ZyroxMod\n\n"
    "Just paste a link to get started! 🔥"
)
HELP_TEXT = (
    "📖 <b>How to use</b>\n\n"
    "1️⃣ Join @ZyroxV1 and @ZyroxMod\n"
    "2️⃣ Send a video URL\n"
    "3️⃣ See thumbnail, size & length, then choose quality\n"
    "4️⃣ Watch live progress bar with % while it downloads & uploads\n\n"
    "💡 Tip: use 🛑 Cancel to stop a download."
)

async def check_force_join(uid):
    """User must join @ZyroxV1 + @ZyroxMod (and any admin-added DB channels)."""
    if is_owner_uid(uid):
        return True, []
    if not FORCE_JOIN_ENABLED and get_setting("fjoin", "off") != "on":
        return True, []
    missing = []
    seen = set()
    # 1) Hardcoded / env public channels: @ZyroxV1, @ZyroxMod
    for username in FORCE_CHANNELS:
        key = username.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            member = await app.get_chat_member(f"@{username}", uid)
            status = str(getattr(member, "status", "")).lower()
            if any(x in status for x in ("left", "banned", "kicked")):
                raise UserNotParticipant("not joined")
        except Exception:
            missing.append({
                "chat_id": f"@{username}",
                "title": f"@{username}",
                "invite_link": f"https://t.me/{username}",
            })
    # 2) Extra channels added from Admin Panel (database)
    for ch in get_channels():
        try:
            await app.get_chat_member(ch["chat_id"], uid)
        except Exception:
            d = dict(ch)
            if str(d.get("chat_id", "")).lower() not in seen and str(d.get("title", "")).lower().lstrip("@") not in seen:
                missing.append(d)
    return (len(missing) == 0), missing

def join_kb(missing):
    rows = []
    for ch in missing:
        label = ch.get("title") or str(ch["chat_id"])
        if ch.get("invite_link"):
            rows.append([InlineKeyboardButton(f"📢 Join {label}",
                                              url=ch["invite_link"])])
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify",
                                       callback_data="recheck")])
    return InlineKeyboardMarkup(rows)

# ----- commands (also reachable via menu button) -----
@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    register_user(m.from_user.id,
                  m.from_user.username if m.from_user else None)
    if is_blocked(m.from_user.id):
        return
    ok, missing = await check_force_join(m.from_user.id)
    if not ok:
        await m.reply_text("🔒 <b>Join our channels to continue:</b>",
                           reply_markup=join_kb(missing))
        return
    await m.reply_text(WELCOME, reply_markup=main_menu_kb(m.from_user.id))

@app.on_message(filters.command("help"))
async def help_cmd(_, m: Message):
    await m.reply_text(HELP_TEXT,
                       reply_markup=main_menu_kb(m.from_user.id if m.from_user else 0))

# ----- menu callbacks -----
@app.on_callback_query(filters.regex(r"^menu:"))
async def menu_cb(_, q: CallbackQuery):
    action = q.data.split(":", 1)[1]
    uid = q.from_user.id
    if action == "main":
        await q.message.edit_text(WELCOME, reply_markup=main_menu_kb(uid))
    elif action == "help":
        await q.message.edit_text(HELP_TEXT, reply_markup=main_menu_kb(uid))
    elif action == "admin":
        if not is_owner_uid(uid):
            await q.answer("Owner only.", show_alert=True); return
        await q.message.edit_text("🛠️ <b>Admin Panel</b>\nChoose an option:",
                                  reply_markup=admin_kb())
    await q.answer()

@app.on_callback_query(filters.regex(r"^admin:"))
async def admin_cb(_, q: CallbackQuery):
    if not is_owner_uid(q.from_user.id):
        await q.answer("Owner only.", show_alert=True); return
    action = q.data.split(":", 1)[1]

    if action == "stats":
        c = db()
        total = c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        blocked = c.execute("SELECT COUNT(*) c FROM users WHERE blocked=1").fetchone()["c"]
        dl = c.execute("SELECT COALESCE(SUM(downloads),0) s FROM users").fetchone()["s"]
        today = time.strftime("%Y-%m-%d")
        r = c.execute("SELECT downloads FROM stats WHERE date=?", (today,)).fetchone()
        ch = c.execute("SELECT COUNT(*) c FROM channels").fetchone()["c"]
        c.close()
        await q.message.edit_text(
            "📊 <b>Statistics</b>\n\n"
            f"👥 Users: <b>{total}</b>\n⛔ Blocked: <b>{blocked}</b>\n"
            f"⬇️ Downloads: <b>{dl}</b> (today: <b>{r['downloads'] if r else 0}</b>)\n"
            f"📢 Channels: <b>{ch}</b>",
            reply_markup=admin_kb())

    elif action == "broadcast":
        ui_state[q.from_user.id] = {"mode": "broadcast"}
        await q.message.edit_text(
            "📣 <b>Broadcast mode</b>\n\nSend the message to broadcast to all users "
            "(or /cancel).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Admin Panel",
                                       callback_data="menu:admin")]]))

    elif action == "users":
        rows = get_recent_users(10)
        if not rows:
            await q.message.edit_text("👥 No users yet.",
                                      reply_markup=admin_kb()); return
        kb = []
        for r in rows:
            name = f"@{r['username']}" if r['username'] else str(r['user_id'])
            icon = "⛔" if r['blocked'] else "✅"
            kb.append([InlineKeyboardButton(
                f"{icon} {name}",
                callback_data=f"user:{r['user_id']}")])
        kb.append([InlineKeyboardButton("🔙 Admin Panel",
                                        callback_data="menu:admin")])
        await q.message.edit_text("👥 <b>Recent users</b> — tap to block/unblock:",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "channels":
        await q.message.edit_text(
            "📢 <b>Force-Join Channels</b>\n\n"
            "Tap 🗑️ to remove. Add the bot as admin in a channel first, "
            "then use ➕ Add Channel.",
            reply_markup=channels_kb())
    await q.answer()

@app.on_callback_query(filters.regex(r"^user:"))
async def user_cb(_, q: CallbackQuery):
    if not is_owner_uid(q.from_user.id):
        await q.answer("Owner only.", show_alert=True); return
    uid = int(q.data.split(":")[1])
    c = db()
    r = c.execute("SELECT blocked,username FROM users WHERE user_id=?",
                  (uid,)).fetchone()
    blocked = not (r and r["blocked"])
    c.execute("UPDATE users SET blocked=? WHERE user_id=?",
              (1 if blocked else 0, uid))
    c.commit(); c.close()
    await q.answer(f"{'Blocked' if blocked else 'Unblocked'} {uid}")
    # refresh list
    rows = get_recent_users(10)
    kb = []
    for r2 in rows:
        name = f"@{r2['username']}" if r2['username'] else str(r2['user_id'])
        icon = "⛔" if r2['blocked'] else "✅"
        kb.append([InlineKeyboardButton(f"{icon} {name}",
                                        callback_data=f"user:{r2['user_id']}")])
    kb.append([InlineKeyboardButton("🔙 Admin Panel",
                                    callback_data="menu:admin")])
    await q.message.edit_text("👥 <b>Recent users</b> — tap to block/unblock:",
                              reply_markup=InlineKeyboardMarkup(kb))

@app.on_callback_query(filters.regex(r"^ch:"))
async def ch_cb(_, q: CallbackQuery):
    if not is_owner_uid(q.from_user.id):
        await q.answer("Owner only.", show_alert=True); return
    parts = q.data.split(":")
    if parts[1] == "toggle":
        cur = get_setting("fjoin", "off")
        set_setting("fjoin", "off" if cur == "on" else "on")
        await q.answer(f"Force-join {('OFF' if cur=='on' else 'ON')}")
    elif parts[1] == "del":
        del_channel_db(int(parts[2]))
        await q.answer("Channel removed.")
    elif parts[1] == "add":
        ui_state[q.from_user.id] = {"mode": "addchannel"}
        await q.message.edit_text(
            "➕ <b>Add Channel</b>\n\nSend the channel's chat ID "
            "(e.g. <code>-1001234567890</code>).\n"
            "The bot must be admin in that channel.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Channels",
                                       callback_data="admin:channels")]]))
        await q.answer()
        return
    await q.message.edit_text(
        "📢 <b>Force-Join Channels</b>", reply_markup=channels_kb())
    await q.answer()

# ----- broadcast: intercept ANY content type, with preview + confirm -----
@app.on_message(filters.private, group=-1)
async def broadcast_intercept(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not is_owner_uid(uid):
        return
    state = ui_state.get(uid)
    if not state or state.get("mode") != "broadcast":
        return
    # ignore commands
    if m.text and m.text.startswith("/"):
        return
    # store for preview
    ui_state[uid] = {"mode": "broadcast_confirm",
                     "chat_id": m.chat.id, "msg_id": m.id}
    c = db()
    total = c.execute("SELECT COUNT(*) c FROM users WHERE blocked=0").fetchone()["c"]
    c.close()
    await m.reply_text(
        f"📣 <b>Broadcast Preview</b>\n\n👥 Will send to <b>{total}</b> users.\n"
        "Confirm to start?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & Send", callback_data="bc:confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel")],
        ]))
    await m.stop_propagation()

@app.on_callback_query(filters.regex(r"^bc:"))
async def broadcast_confirm_cb(_, q: CallbackQuery):
    if not is_owner_uid(q.from_user.id):
        await q.answer("Owner only.", show_alert=True); return
    action = q.data.split(":")[1]
    uid = q.from_user.id

    if action == "cancel":
        ui_state.pop(uid, None)
        await q.message.edit_text("❌ Broadcast cancelled.",
                                  reply_markup=admin_kb())
        await q.answer()
        return

    # confirm
    state = ui_state.get(uid)
    if not state or state.get("mode") != "broadcast_confirm":
        await q.answer("No broadcast pending.", show_alert=True); return
    chat_id, msg_id = state["chat_id"], state["msg_id"]
    ui_state.pop(uid, None)

    await q.message.edit_text("📣 <b>Broadcasting…</b>")
    await q.answer()
    c = db()
    users = c.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
    c.close()
    total, sent = len(users), 0
    for (u,) in users:
        try:
            await app.copy_message(u, chat_id, msg_id)
            sent += 1
        except Exception:
            pass
        if sent % 25 == 0:
            try:
                await q.message.edit_text(f"📣 Broadcasting… {sent}/{total}")
            except Exception:
                pass
    await q.message.edit_text(
        f"✅ <b>Broadcast done:</b> {sent}/{total} delivered.",
        reply_markup=admin_kb())

# ----- text input for admin modes (addchannel) -----
@app.on_message(filters.text & ~filters.regex(URL_RE))
async def admin_text(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    state = ui_state.get(uid)
    if not state or not is_owner_uid(uid):
        return
    mode = state.get("mode")

    if mode == "addchannel":
        ui_state.pop(uid, None)
        txt = (m.text or "").strip()
        try:
            chat_id = int(txt)
        except ValueError:
            await m.reply_text("❌ Send a numeric chat ID.",
                               reply_markup=channels_kb()); return
        try:
            chat = await app.get_chat(chat_id)
            title = chat.title or str(chat_id)
            invite = chat.invite_link or ""
            if not invite:
                try: invite = await app.export_chat_invite_link(chat_id)
                except Exception: pass
        except Exception as e:
            await m.reply_text(f"❌ Cannot access: {e}\nMake the bot admin there first.",
                               reply_markup=channels_kb()); return
        add_channel_db(chat_id, title, invite)
        await m.reply_text(f"✅ Added: <b>{title}</b>", reply_markup=channels_kb())

@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if uid in ui_state:
        ui_state.pop(uid, None)
        await m.reply_text("Cancelled.", reply_markup=main_menu_kb(uid))
        return
    cancelled = False
    for k in list(active_tasks):
        if k.startswith(f"{m.chat.id}:"):
            t = active_tasks.pop(k, None)
            if t and not t.done(): t.cancel(); cancelled = True
            pending.pop(k, None)
    await m.reply_text("🛑 Cancelled." if cancelled else "ℹ️ Nothing to cancel.")

@app.on_callback_query(filters.regex(r"^recheck$"))
async def recheck_cb(_, q: CallbackQuery):
    ok, _ = await check_force_join(q.from_user.id)
    if ok:
        await q.message.edit_text(WELCOME,
                                  reply_markup=main_menu_kb(q.from_user.id))
    else:
        await q.answer("Please join all channels first.", show_alert=True)

# ----- download flow -----
@app.on_message(filters.regex(URL_RE))
async def url_handler(_, m: Message):
    # skip if this text was meant for admin input
    uid = m.from_user.id if m.from_user else 0
    if uid in ui_state:
        return
    register_user(uid, m.from_user.username if m.from_user else None)
    if is_blocked(uid):
        return
    ok, missing = await check_force_join(uid)
    if not ok:
        rows = []
        for ch in missing:
            label = ch.get("title") or str(ch["chat_id"])
            if ch.get("invite_link"):
                rows.append([InlineKeyboardButton(f"📢 Join {label}",
                                                  url=ch["invite_link"])])
        rows.append([InlineKeyboardButton("✅ I've Joined — Verify",
                                           callback_data="recheck")])
        await m.reply_text("🔒 <b>Join our channels to continue:</b>",
                           reply_markup=InlineKeyboardMarkup(rows))
        return

    match = URL_RE.search(m.text or "")
    url = match.group(0).rstrip(").>,]")
    status = await m.reply_text("🔍 <b>Fetching video info…</b>")

    def _info():
        try: return extract_info(url)
        except Exception as e: return {"_error": str(e)}
    info = await asyncio.to_thread(_info)
    if "_error" in info:
        err = info["_error"]
        hint = ytdl_error_hint(err)
        await status.edit_text(f"❌ <b>Could not fetch video.</b>\n<code>{html.escape(err[:350])}</code>{hint}")
        return

    title = info.get("title", "Unknown")
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    key = f"{m.chat.id}:{m.id}"
    sizes = estimate_sizes(info)
    pending[key] = {"url": info.get("webpage_url", url), "title": title,
                    "thumb": pick_thumbnail(info), "thumb_url": pick_thumbnail(info),
                    "id": info.get("id"), "duration": info.get("duration"),
                    "uploader": uploader, "width": info.get("width"),
                    "height": info.get("height"),
                    "view_count": info.get("view_count"),
                    "like_count": info.get("like_count"), "sizes": sizes, "strategy": info.get("_strategy")}
    buttons = []
    for i, (lbl, _) in enumerate(QUALITY_OPTIONS):
        sz = sizes[i] if i < len(sizes) else 0
        text = f"{lbl} • {fmt_size(sz)}" if sz else lbl
        buttons.append([InlineKeyboardButton(text, callback_data=f"q|{key}|{i}")])
    _res = f"{info.get('width')}x{info.get('height')}" if info.get("width") and info.get("height") else "N/A"
    _likes = f" • ❤️ {fmt_views(info.get('like_count'))}" if info.get("like_count") else ""
    _size_lines = "\n".join(
        f"{lbl}: <b>{fmt_size(sizes[i])}</b>" for i, (lbl, _) in enumerate(QUALITY_OPTIONS) if i < len(sizes) and sizes[i])
    caption = (
        f"🎬 <b>{html.escape(str(title))[:220]}</b>\n\n"
        f"👤 <b>Channel:</b> {html.escape(str(uploader))}\n"
        f"⏱️ <b>Length:</b> {fmt_dur(info.get('duration'))} ({int(info.get('duration') or 0)} sec)\n"
        f"🖥️ <b>Resolution:</b> {_res}\n"
        f"👁️ <b>Views:</b> {fmt_views(info.get('view_count'))}{_likes}\n"
        + (f"📦 <b>Estimated Size:</b>\n{_size_lines}\n" if _size_lines else "")
        + "\n<b>Select quality:</b>"
    )[:1000]
    try:
        if pending[key]["thumb"]:
            await status.delete()
            await m.reply_photo(pending[key]["thumb"], caption=caption,
                                reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await status.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        try: await status.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception: pass

@app.on_callback_query(filters.regex(r"^q\|"))
async def quality_cb(_, q: CallbackQuery):
    try:
        _, key, idx = q.data.split("|"); idx = int(idx)
    except ValueError:
        await q.answer("Invalid option.", show_alert=True); return
    data = pending.get(key)
    if not data:
        await q.answer("Session expired — send the link again.", show_alert=True); return
    ok, _ = await check_force_join(q.from_user.id)
    if not ok:
        await q.answer("Join channels first.", show_alert=True); return

    label, fmt = QUALITY_OPTIONS[idx]
    url, title = data["url"], data["title"]
    is_audio = "Audio" in label
    cancel_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 Cancel", callback_data=f"x|{key}")]])
    _start_txt = f"⬇️ <b>Downloading</b>\n📌 {html.escape(str(title))[:200]}\n🎚️ {label}\n\n<i>Starting…</i>"
    try:
        await q.message.edit_text(_start_txt, reply_markup=cancel_btn)
        status_msg = q.message
    except Exception:
        # Original message is a photo (thumbnail) -> edit_text fails, replace it
        try:
            await q.message.delete()
        except Exception:
            pass
        status_msg = await app.send_message(q.message.chat.id, _start_txt, reply_markup=cancel_btn)

    loop = asyncio.get_event_loop()
    last = 0.0
    st = {"msg": status_msg}

    async def safe_edit(text, markup=None):
        try: await st["msg"].edit_text(text, reply_markup=markup)
        except Exception: pass

    _dl_t0 = time.time()

    def progress_cb(frac, speed, eta, done=0, total=0):
        nonlocal last
        now = time.time()
        if now - last < 3 and frac < 1:
            return
        last = now
        pct = frac * 100
        elapsed = now - _dl_t0
        done_txt = f"{fmt_size(done)} / {fmt_size(total)}" if total else fmt_size(done)
        txt = (
            f"⬇️ <b>Downloading…</b>\n"
            f"📌 {html.escape(str(title))[:160]}\n"
            f"🎚️ {label}\n\n"
            f"<code>{bar(frac)}</code> <b>{pct:.1f}%</b>\n"
            f"📦 {done_txt}\n"
            f"⚡ <b>Speed:</b> {fmt_speed(speed)} • ⏳ <b>ETA:</b> {fmt_eta(eta)}\n"
            f"⏱️ <b>Elapsed:</b> {fmt_eta(elapsed)}"
        )
        asyncio.run_coroutine_threadsafe(safe_edit(txt, cancel_btn), loop)

    async def _run():
        try:
            path, real_title, info = await asyncio.to_thread(
                download_media, url, fmt, progress_cb, data.get("strategy"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("download failed")
            em = str(e)
            hint = ytdl_error_hint(em)
            await safe_edit(f"❌ <b>Download failed.</b>\n<code>{html.escape(em[:350])}</code>{hint}")
            pending.pop(key, None); return
        # validate file before upload
        if not os.path.isfile(path) or os.path.getsize(path) < 1024:
            await safe_edit("❌ <b>Download failed.</b>\nFile is missing or empty. "
                            "The site may have blocked the download.")
            pending.pop(key, None); return
        file_size = os.path.getsize(path)
        size_mb = file_size / (1024 * 1024)
        duration = int(info.get("duration") or data.get("duration") or 0) or None
        res_txt = ""
        if info.get("width") and info.get("height"):
            res_txt = f" • 🖥️ {info.get('width')}x{info.get('height')}"
        await safe_edit(
            f"⬆️ <b>Uploading…</b>\n📌 {html.escape(str(real_title))[:160]}\n"
            f"📦 {fmt_size(file_size)} • ⏱️ {fmt_dur(duration)}\n\n"
            f"<code>{bar(0)}</code> <b>0.0%</b>\n<i>Please wait…</i>", cancel_btn)

        _up = {"last": 0.0, "t0": time.time(), "last_bytes": 0, "speed": 0}

        async def upload_progress(current, total):
            now = time.time()
            if now - _up["last"] < 3 and current < total:
                return
            dt = now - _up["last"] if _up["last"] else 0
            if dt > 0:
                _up["speed"] = (current - _up["last_bytes"]) / dt
            _up["last"] = now
            _up["last_bytes"] = current
            frac = (current / total) if total else 0
            eta = ((total - current) / _up["speed"]) if _up["speed"] else None
            await safe_edit(
                f"⬆️ <b>Uploading…</b>\n📌 {html.escape(str(real_title))[:160]}\n"
                f"🎚️ {label} • ⏱️ {fmt_dur(duration)}\n\n"
                f"<code>{bar(frac)}</code> <b>{frac * 100:.1f}%</b>\n"
                f"📦 {fmt_size(current)} / {fmt_size(total)}\n"
                f"⚡ <b>Speed:</b> {fmt_speed(_up['speed'])} • ⏳ <b>ETA:</b> {fmt_eta(eta)}",
                cancel_btn)

        caption = (
            f"🎬 <b>{html.escape(str(real_title))[:250]}</b>\n\n"
            f"🎚️ <b>Quality:</b> {label}\n"
            f"📦 <b>Size:</b> {fmt_size(file_size)}\n"
            f"⏱️ <b>Length:</b> {fmt_dur(duration)}{res_txt}\n\n"
            f"📢 @ZyroxV1 • @ZyroxMod"
        )[:1000]
        thumb_path = await asyncio.to_thread(
            download_thumbnail, data.get("thumb_url") or data.get("thumb"), data.get("id") or info.get("id") or "thumb")
        _thumb_kw = {"thumb": thumb_path} if thumb_path else {}
        try:
            if is_audio:
                await app.send_audio(
                    q.message.chat.id, path, caption=caption,
                    title=real_title[:60],
                    performer=info.get("uploader") or info.get("channel"),
                    duration=duration, progress=upload_progress, **_thumb_kw)
            else:
                try:
                    await app.send_video(
                        q.message.chat.id, path, caption=caption,
                        duration=duration,
                        width=info.get("width") or None,
                        height=info.get("height") or None,
                        supports_streaming=True,
                        progress=upload_progress, **_thumb_kw)
                except Exception:
                    log.warning("send_video failed, trying send_document")
                    await app.send_document(q.message.chat.id, path,
                                            caption=caption, progress=upload_progress, **_thumb_kw)
            bump_downloads(q.from_user.id)
            await safe_edit("✅ <b>Done!</b> Enjoy 🎉")
        except Exception as e:
            log.exception("upload failed")
            await safe_edit(f"❌ <b>Upload failed.</b>\n<code>{str(e)[:250]}</code>")
        finally:
            pending.pop(key, None); active_tasks.pop(key, None)
            try: os.remove(path)
            except OSError: pass
            try:
                if thumb_path:
                    os.remove(thumb_path)
            except Exception:
                pass

    task = asyncio.create_task(_run())
    active_tasks[key] = task
    try:
        await task
    except asyncio.CancelledError:
        await safe_edit("🛑 <b>Download cancelled.</b>")
        pending.pop(key, None)

@app.on_callback_query(filters.regex(r"^x\|"))
async def cancel_cb(_, q: CallbackQuery):
    _, key = q.data.split("|", 1)
    t = active_tasks.pop(key, None)
    if t and not t.done():
        t.cancel(); await q.answer("Cancelling…")
    else:
        await q.answer("Nothing to cancel.", show_alert=True)
    pending.pop(key, None)

@app.on_callback_query(filters.regex(r"^noop$"))
async def noop_cb(_, q: CallbackQuery):
    await q.answer("Open the channel and join.", show_alert=True)

@app.on_message(filters.command("cookies"))
async def cookies_cmd(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not is_owner_uid(uid):
        return
    cf = get_cookiefile()
    status = f"✅ Cookies set hain ({os.path.getsize(cf)} bytes)" if cf else "❌ Cookies set nahi hain"
    await m.reply_text(
        f"🍪 <b>YouTube Cookies</b>\n\n{status}\n\n"
        "Nayi cookies lagane ke liye is chat me <b>cookies.txt</b> file bhej do "
        "(Netscape format, burner/dusre Google account se export ki hui).\n"
        "Main account ki cookies mat use karna.")

@app.on_message(filters.document & filters.private)
async def cookies_doc_handler(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not is_owner_uid(uid) or not m.document:
        return
    name = (m.document.file_name or "").lower()
    if "cookie" not in name:
        return
    if (m.document.file_size or 0) > 2 * 1024 * 1024:
        await m.reply_text("❌ Cookies file bahut badi hai (max 2 MB).")
        return
    try:
        tmp_path = await m.download()
        with open(tmp_path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(100_000)
        if "youtube" not in head.lower() and ".google." not in head.lower():
            try: os.remove(tmp_path)
            except OSError: pass
            await m.reply_text("❌ Yeh valid YouTube cookies.txt nahi lagti (Netscape format chahiye).")
            return
        shutil.move(tmp_path, COOKIE_PATH)
        try: os.chmod(COOKIE_PATH, 0o600)
        except OSError: pass
        log.info("YouTube cookies updated by owner upload (%s bytes)", os.path.getsize(COOKIE_PATH))
        await m.reply_text("✅ <b>Cookies save ho gayi!</b> Ab YouTube link dobara bhejo.")
    except Exception as e:
        log.exception("cookies upload failed")
        await m.reply_text(f"❌ Cookies save nahi hui: <code>{html.escape(str(e)[:200])}</code>")

@app.on_message(filters.command("ping"))
async def ping_cmd(_, m: Message):
    if not OWNER_ID or (m.from_user and m.from_user.id == OWNER_ID):
        t0 = time.time()
        msg = await m.reply_text("🏓 <i>ping…</i>")
        await msg.edit_text(f"🏓 <b>Pong!</b> {int((time.time()-t0)*1000)} ms ✅")

if __name__ == "__main__":
    init_db()
    setup_menu_button()
    Thread(target=run_health_server, daemon=True).start()
    Thread(target=ensure_js_runtime, daemon=True).start()
    Thread(target=run_keepalive, daemon=True).start()
    try:
        import yt_dlp as _y
        log.info("diagnostics: yt-dlp=%s ffmpeg=%s cookies=%s keepalive=%s",
                 _y.version.__version__, bool(get_ffmpeg()),
                 bool(get_cookiefile()), KEEPALIVE_URL or "off")
    except Exception as _e:
        log.warning("diagnostics failed: %s", _e)
    log.info("starting Zyrox downloader bot | force-join channels: %s (enabled=%s)", FORCE_CHANNELS, FORCE_JOIN_ENABLED)
    app.run()
