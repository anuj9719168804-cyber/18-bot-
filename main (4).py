import os
import re
import sys
import time
import html
import uuid
import logging
import asyncio
import tempfile
import urllib.parse
from typing import Optional, Dict, Any, List, Set
from dotenv import load_dotenv

import aiohttp
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from terabox import is_terabox_url, get_terabox_info, format_size
from diskwala import is_diskwala_url, get_diskwala_info
from flezen import is_flezen_url, get_flezen_info

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 8080))
MAX_TELEGRAM_UPLOAD_SIZE = 50 * 1024 * 1024   # 50 MB Telegram Bot API limit
SAFE_UPLOAD_SIZE = int(49.5 * 1024 * 1024)    # 49.5 MB threshold to guarantee no 50MB reject
GLOBAL_CONCURRENCY_SEMAPHORE = asyncio.Semaphore(2)  # Max 2 concurrent heavy video downloads/encodes

def get_ffmpeg_binary() -> str:
    """Returns the fastest available FFmpeg executable."""
    import shutil
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


# In-memory user settings dictionary (chat_id -> dict)
user_settings: Dict[int, Dict[str, Any]] = {}

def get_user_mode(chat_id: int) -> str:
    """Returns the user's selected engine mode (default: 'exp')."""
    return user_settings.get(chat_id, {}).get("mode", "exp")

def set_user_mode(chat_id: int, mode: str):
    """Saves the user's selected engine mode."""
    if chat_id not in user_settings:
        user_settings[chat_id] = {}
    user_settings[chat_id]["mode"] = mode.lower()


# Active downloads registry for cancellation
# task_id -> {
#     "task_id": str,
#     "chat_id": int,
#     "url": str,
#     "status_msg": telegram.Message,
#     "cancel_event": asyncio.Event,
#     "asyncio_task": Optional[asyncio.Task],
#     "proc": Optional[asyncio.subprocess.Process],
#     "session": Optional[aiohttp.ClientSession],
#     "temp_files": List[str],
#     "cancelled": bool
# }
active_tasks: Dict[str, Dict[str, Any]] = {}
chat_active_tasks: Dict[int, Set[str]] = {}

# Standard Commands Text Block
COMMANDS_TEXT = (
    "⚙️ <b>Commands:</b>\n"
    "<code>/exp &lt;link&gt;</code>  Fast &amp; Reliable [Recommended]\n"
    "<code>/expHD &lt;link&gt;</code>  For HD Videos [Slow]\n"
    "<code>/get &lt;link&gt;</code>  Direct Link [Small Files]\n"
    "<code>/dw &lt;link&gt;</code>  Download DiskWala video\n"
    "<code>/fz &lt;link&gt;</code>  Download Flezen video\n"
    "<code>/all &lt;link&gt;</code>  Auto-detect link type\n"
    "<code>/cancel</code>  Cancel all your active downloads"
)

# ----------------- Render Web Server (Health Check) -----------------

web_runner: Optional[web.AppRunner] = None

async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint required for Render Web Services."""
    return web.json_response({
        "status": "healthy",
        "service": "Video Downloader Telegram Bot",
        "active": True
    })

async def post_init(application: Application):
    """Starts the health-check web server when the bot initializes."""
    global web_runner
    try:
        web_app = web.Application()
        web_app.router.add_get("/", health_check)
        web_app.router.add_get("/health", health_check)

        web_runner = web.AppRunner(web_app)
        await web_runner.setup()
        site = web.TCPSite(web_runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"Health-check web server started on port {PORT} for Render deployment.")
    except OSError as e:
        logger.warning(f"Could not bind web server to port {PORT}: {e}. Continuing bot polling...")
    except Exception as e:
        logger.warning(f"Failed to start web server: {e}")

async def post_shutdown(application: Application):
    """Gracefully cleans up web server resources on shutdown."""
    global web_runner
    if web_runner:
        logger.info("Cleaning up health-check web server...")
        try:
            await web_runner.cleanup()
        except Exception as e:
            logger.warning(f"Error during web server cleanup: {e}")

# ----------------- UI Helpers & Keyboard Generators -----------------

def make_progress_bar(percent: int, length: int = 10) -> str:
    """Generates a visual progress bar (e.g., [████░░░░░░])."""
    filled = max(0, min(length, int(round((percent / 100.0) * length))))
    return "█" * filled + "░" * (length - filled)

def get_cancel_markup(task_id: str, chat_id: int) -> InlineKeyboardMarkup:
    """Generates the inline keyboard with '❌ Cancel' and optional '🛑 Cancel All'."""
    active_set = chat_active_tasks.get(chat_id, set())
    buttons = [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{task_id}")]
    rows = [buttons]
    if len(active_set) > 1:
        rows.append([InlineKeyboardButton("🛑 Cancel All", callback_data="cancel_all")])
    return InlineKeyboardMarkup(rows)

def get_settings_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    """Builds interactive engine selection keyboard with dynamic checkmarks."""
    def mark(mode_name: str, label: str) -> str:
        return f"✅ {label}" if current_mode.lower() == mode_name.lower() else label

    keyboard = [
        [
            InlineKeyboardButton(mark("exp", "🚀 /exp [Recommended]"), callback_data="set_mode_exp"),
            InlineKeyboardButton(mark("exphd", "🎬 /expHD [HD Videos]"), callback_data="set_mode_exphd"),
        ],
        [
            InlineKeyboardButton(mark("get", "⚡ /get [Small Files]"), callback_data="set_mode_get"),
            InlineKeyboardButton(mark("all", "🔄 /all [Auto-Detect]"), callback_data="set_mode_all"),
        ],
        [
            InlineKeyboardButton(mark("dw", "💾 /dw [DiskWala]"), callback_data="set_mode_dw"),
            InlineKeyboardButton(mark("fz", "🌐 /fz [Flezen]"), callback_data="set_mode_fz"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

class ThrottledUpdater:
    """Safely updates Telegram message text without triggering FloodWait."""
    def __init__(self, message, min_interval: float = 2.0):
        self.message = message
        self.min_interval = min_interval
        self.last_update = 0.0
        self.last_text = ""
        self.lock = asyncio.Lock()

    async def update(self, text: str, reply_markup=None, force: bool = False):
        now = time.time()
        if not force and (now - self.last_update < self.min_interval or text == self.last_text):
            return
        async with self.lock:
            try:
                self.last_update = time.time()
                self.last_text = text
                await self.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
            except Exception:
                pass

# ----------------- Cancellation Logic -----------------

async def cancel_single_task(task_id: str) -> bool:
    """Aborts a running download/processing task and cleans up its files."""
    task_info = active_tasks.get(task_id)
    if not task_info:
        return False

    task_info["cancelled"] = True
    task_info["cancel_event"].set()

    # Kill FFmpeg subprocess if running
    proc = task_info.get("proc")
    if proc:
        try:
            proc.kill()
            logger.info(f"Killed active FFmpeg process for task {task_id}")
        except Exception:
            pass

    # Close HTTP session if active
    session = task_info.get("session")
    if session and not session.closed:
        try:
            await session.close()
        except Exception:
            pass

    # Cancel asyncio task
    async_task = task_info.get("asyncio_task")
    if async_task and not async_task.done():
        async_task.cancel()

    # Clean up files
    for fp in task_info.get("temp_files", []):
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except Exception:
                pass

    # Update Telegram status message
    status_msg = task_info.get("status_msg")
    if status_msg:
        try:
            await status_msg.edit_text(
                "❌ <b>Download Cancelled</b>\n<i>The process was stopped by user.</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    # Remove from registries
    chat_id = task_info.get("chat_id")
    if chat_id and chat_id in chat_active_tasks:
        chat_active_tasks[chat_id].discard(task_id)
        if not chat_active_tasks[chat_id]:
            del chat_active_tasks[chat_id]
    active_tasks.pop(task_id, None)

    return True

async def cancel_all_tasks_for_chat(chat_id: int) -> int:
    """Cancels all active tasks for a specific chat/user."""
    task_ids = list(chat_active_tasks.get(chat_id, set()))
    count = 0
    for tid in task_ids:
        if await cancel_single_task(tid):
            count += 1
    return count

# ----------------- High-Speed Download Engines -----------------

async def download_m3u8_fast(
    session: aiohttp.ClientSession,
    m3u8_url: str,
    headers: Dict[str, str],
    temp_mp4_path: str,
    task_info: Dict[str, Any],
    updater: ThrottledUpdater,
    filename: str,
    cancel_markup: InlineKeyboardMarkup
) -> bool:
    """
    Downloads an M3U8 HLS stream by fetching TS segments concurrently with aiohttp,
    then doing a lightning-fast FFmpeg copy-remux to MP4 in 1 second.
    """
    task_id = task_info["task_id"]
    cancel_event = task_info["cancel_event"]

    # 1. Fetch Playlist
    async with session.get(m3u8_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status != 200:
            logger.warning(f"Failed to fetch M3U8: HTTP {resp.status}")
            return False
        playlist_text = await resp.text()

    if cancel_event.is_set():
        return False

    # Check for encryption - if encrypted, fallback to FFmpeg native reader
    if "#EXT-X-KEY" in playlist_text:
        logger.info(f"[Task {task_id}] M3U8 is encrypted. Using FFmpeg direct stream...")
        return await download_via_ffmpeg_stream(
            m3u8_url, headers, temp_mp4_path, task_info, updater, filename, cancel_markup
        )

    # 2. Extract segment URLs
    lines = playlist_text.splitlines()
    segment_urls: List[str] = []
    for line in lines:
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("#"):
            continue
        full_url = urllib.parse.urljoin(m3u8_url, line_clean)
        segment_urls.append(full_url)

    if not segment_urls:
        logger.warning(f"[Task {task_id}] No TS segments found in M3U8. Falling back to FFmpeg.")
        return await download_via_ffmpeg_stream(
            m3u8_url, headers, temp_mp4_path, task_info, updater, filename, cancel_markup
        )

    total_segments = len(segment_urls)
    logger.info(f"[Task {task_id}] Found {total_segments} HLS segments. Downloading concurrently...")

    temp_ts_path = temp_mp4_path + ".ts"
    task_info["temp_files"].append(temp_ts_path)

    # Progress tracking
    download_state = {
        "downloaded_bytes": 0,
        "completed_segments": 0,
        "total_segments": total_segments,
        "start_time": time.time()
    }

    # Concurrent segment fetcher with bounded concurrency (8 workers)
    sem = asyncio.Semaphore(8)

    async def fetch_segment(seg_url: str, idx: int) -> tuple:
        if cancel_event.is_set():
            return idx, b""
        async with sem:
            for attempt in range(3):
                if cancel_event.is_set():
                    return idx, b""
                try:
                    async with session.get(seg_url, headers=headers, timeout=aiohttp.ClientTimeout(total=40)) as r:
                        if r.status == 200:
                            data = await r.read()
                            download_state["downloaded_bytes"] += len(data)
                            download_state["completed_segments"] += 1

                            # Update progress UI
                            now = time.time()
                            elapsed = max(0.5, now - download_state["start_time"])
                            speed_mbps = (download_state["downloaded_bytes"] / (1024 * 1024)) / elapsed
                            pct = int((download_state["completed_segments"] / total_segments) * 100)
                            bar = make_progress_bar(pct)
                            mb_dl = download_state["downloaded_bytes"] / (1024 * 1024)

                            eta = int((total_segments - download_state["completed_segments"]) / max(0.1, download_state["completed_segments"] / elapsed))

                            ui_text = (
                                f"⬇️ <b>Downloading:</b> <code>{html.escape(filename)}</code>\n\n"
                                f"[{bar}] <b>{pct}%</b>\n"
                                f"📦 <b>{mb_dl:.1f} MB</b> ({download_state['completed_segments']}/{total_segments} parts)\n"
                                f"⚡ <b>Speed:</b> {speed_mbps:.1f} MB/s | <b>ETA:</b> {eta}s"
                            )
                            asyncio.create_task(updater.update(ui_text, reply_markup=cancel_markup))
                            return idx, data
                except Exception:
                    await asyncio.sleep(1.0)
            return idx, b""

    # Execute all segments
    results = await asyncio.gather(*[fetch_segment(url, i) for i, url in enumerate(segment_urls)])
    if cancel_event.is_set():
        return False

    results.sort(key=lambda x: x[0])

    # Write combined TS
    with open(temp_ts_path, "wb") as f_ts:
        for _, chunk_bytes in results:
            if chunk_bytes:
                f_ts.write(chunk_bytes)

    if not os.path.exists(temp_ts_path) or os.path.getsize(temp_ts_path) == 0:
        logger.warning(f"[Task {task_id}] Concatenated TS file is empty.")
        return False

    # 3. Superfast Remux TS -> MP4 using FFmpeg (takes ~0.5 - 1.0s, no re-encoding)
    ffmpeg_exe = get_ffmpeg_binary()
    remux_cmd = [
        ffmpeg_exe, "-y",
        "-i", temp_ts_path,
        "-c", "copy",
        "-movflags", "+faststart",
        temp_mp4_path
    ]
    proc = await asyncio.create_subprocess_exec(
        *remux_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    task_info["proc"] = proc
    await proc.wait()
    task_info["proc"] = None

    if os.path.exists(temp_ts_path):
        try:
            os.remove(temp_ts_path)
        except Exception:
            pass

    return os.path.exists(temp_mp4_path) and os.path.getsize(temp_mp4_path) > 0

async def download_via_ffmpeg_stream(
    stream_url: str,
    headers: Dict[str, str],
    temp_mp4_path: str,
    task_info: Dict[str, Any],
    updater: ThrottledUpdater,
    filename: str,
    cancel_markup: InlineKeyboardMarkup
) -> bool:
    """Fallback FFmpeg direct stream downloader with reconnection & buffer optimization."""
    cancel_event = task_info["cancel_event"]
    ffmpeg_exe = get_ffmpeg_binary()

    headers_str = "".join([f"{k}: {v}\r\n" for k, v in headers.items()])
    cmd = [
        ffmpeg_exe, "-y",
        "-headers", headers_str,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "2",
        "-threads", "4",
        "-i", stream_url,
        "-c", "copy",
        "-movflags", "+faststart",
        temp_mp4_path
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    task_info["proc"] = proc

    # Periodic UI status updater while FFmpeg runs
    start_time = time.time()
    while proc.returncode is None:
        if cancel_event.is_set():
            try:
                proc.kill()
            except Exception:
                pass
            return False
        await asyncio.sleep(2.0)
        if proc.returncode is not None:
            break
        current_size = os.path.getsize(temp_mp4_path) if os.path.exists(temp_mp4_path) else 0
        mb_dl = current_size / (1024 * 1024)
        elapsed = int(time.time() - start_time)
        ui_text = (
            f"⬇️ <b>Downloading Stream:</b> <code>{html.escape(filename)}</code>\n\n"
            f"📦 <b>Downloaded:</b> {mb_dl:.1f} MB\n"
            f"⏱ <b>Elapsed:</b> {elapsed}s"
        )
        await updater.update(ui_text, reply_markup=cancel_markup)

    task_info["proc"] = None
    return os.path.exists(temp_mp4_path) and os.path.getsize(temp_mp4_path) > 0

async def download_chunk(
    session: aiohttp.ClientSession,
    url: str,
    headers: Dict[str, str],
    start: int,
    end: int,
    temp_path: str,
    progress_state: Dict[str, Any],
    cancel_event: asyncio.Event
):
    """Downloads a single byte range chunk for direct URLs."""
    if cancel_event.is_set():
        return
    req_headers = headers.copy()
    req_headers['Range'] = f'bytes={start}-{end}'
    retries = 3
    for attempt in range(retries):
        if cancel_event.is_set():
            return
        try:
            async with session.get(url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                resp.raise_for_status()
                content = await resp.read()
                if cancel_event.is_set():
                    return
                with open(temp_path, "r+b") as f:
                    f.seek(start)
                    f.write(content)
                progress_state["downloaded"] += len(content)
                return
        except Exception as e:
            if attempt == retries - 1:
                logger.warning(f"Failed chunk {start}-{end}: {e}")
                raise
            await asyncio.sleep(1.0)

# ----------------- Processing & Delivery Pipeline -----------------

async def process_single_link(
    update: Update,
    raw_url: str,
    mode: str = "exp"
):
    """
    Complete lifecycle for a single link:
    Metadata Extraction -> Download -> Smart Compression (if >50MB) -> Upload -> Video Delivery.
    """
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    task_id = uuid.uuid4().hex[:8]

    # Pre-register task
    cancel_event = asyncio.Event()
    status_msg = await update.effective_message.reply_text(
        f"⏳ <b>Fetching metadata...</b>\n🔗 <code>{html.escape(raw_url[:45])}...</code>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{task_id}")]]),
        parse_mode="HTML"
    )

    task_info: Dict[str, Any] = {
        "task_id": task_id,
        "chat_id": chat_id,
        "url": raw_url,
        "status_msg": status_msg,
        "cancel_event": cancel_event,
        "asyncio_task": asyncio.current_task(),
        "proc": None,
        "session": None,
        "temp_files": [],
        "cancelled": False
    }

    active_tasks[task_id] = task_info
    if chat_id not in chat_active_tasks:
        chat_active_tasks[chat_id] = set()
    chat_active_tasks[chat_id].add(task_id)

    cancel_markup = get_cancel_markup(task_id, chat_id)
    updater = ThrottledUpdater(status_msg, min_interval=2.0)

    # Temporary file storage
    temp_mp4_path: Optional[str] = None
    t_start = time.time()

    try:
        # Step 1: Platform Identification & Metadata Extraction
        if is_diskwala_url(raw_url):
            platform_label = "DiskWala"
            extractor = lambda: get_diskwala_info(raw_url, mode=mode)
        elif is_flezen_url(raw_url):
            platform_label = "Flezen"
            extractor = lambda: get_flezen_info(raw_url, mode=mode)
        else:
            platform_label = "TeraBox"
            extractor = lambda: get_terabox_info(raw_url, mode=mode)

        if cancel_event.is_set():
            return

        result = await extractor()

        if cancel_event.is_set():
            return

        if not result or not result.get("ok"):
            err_msg = result.get("error", "Failed to retrieve video details.") if result else "Extraction failed."
            error_card = (
                f"❌ <b>Extraction Notice</b>\n\n"
                f"⚠️ <i>{html.escape(err_msg)}</i>\n\n"
                f"{COMMANDS_TEXT}"
            )
            await status_msg.edit_text(error_card, parse_mode="HTML")
            return

        download_url = result.get("download_url")
        filename = result.get("filename", "Video.mp4")
        formatted_size = result.get("formatted_size", "Direct Stream")
        is_m3u8 = result.get("is_m3u8", False)
        cookie_str = result.get("cookie_str", "")

        if not download_url:
            await status_msg.edit_text("❌ <b>Direct Video Link Missing</b>", parse_mode="HTML")
            return

        # Clean filename
        safe_name = re.sub(r'[\\/*?:"<>|\r\n]', "_", filename).strip() or "video.mp4"
        if not safe_name.lower().endswith((".mp4", ".mkv", ".mov", ".webm", ".avi")):
            safe_name = f"{safe_name}.mp4"

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
            temp_mp4_path = temp_file.name
        task_info["temp_files"].append(temp_mp4_path)

        # Step 2: Bounded Concurrency Download
        async with GLOBAL_CONCURRENCY_SEMAPHORE:
            if cancel_event.is_set():
                return

            t_dl_start = time.time()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            }
            if cookie_str:
                headers["Cookie"] = cookie_str
            else:
                tb_cookie = os.getenv("TERABOX_COOKIE", "").strip()
                if tb_cookie:
                    headers["Cookie"] = f"ndus={tb_cookie}" if "ndus=" not in tb_cookie else tb_cookie

            async with aiohttp.ClientSession() as session:
                task_info["session"] = session

                if is_m3u8:
                    download_success = await download_m3u8_fast(
                        session=session,
                        m3u8_url=download_url,
                        headers=headers,
                        temp_mp4_path=temp_mp4_path,
                        task_info=task_info,
                        updater=updater,
                        filename=safe_name,
                        cancel_markup=cancel_markup
                    )
                else:
                    # Direct Link Multi-chunk download
                    async with session.head(download_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status not in (200, 206):
                            async with session.get(download_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as get_resp:
                                total_size = int(get_resp.headers.get("Content-Length", 0))
                        else:
                            total_size = int(resp.headers.get("Content-Length", 0))

                    if total_size <= 0:
                        await status_msg.edit_text("❌ <i>Could not determine file size for download.</i>", parse_mode="HTML")
                        return

                    # Pre-allocate file
                    with open(temp_mp4_path, "wb") as f:
                        f.seek(total_size - 1)
                        f.write(b'\0')

                    download_state = {"downloaded": 0, "total": total_size, "done": False, "start_time": time.time()}

                    async def progress_reporter():
                        while not download_state["done"] and not cancel_event.is_set():
                            await asyncio.sleep(2.0)
                            if download_state["done"] or cancel_event.is_set():
                                break
                            d_bytes = download_state["downloaded"]
                            t_bytes = download_state["total"]
                            pct = min(100, int((d_bytes / t_bytes) * 100))
                            elapsed = max(0.5, time.time() - download_state["start_time"])
                            speed_mbps = (d_bytes / (1024 * 1024)) / elapsed
                            eta = int((t_bytes - d_bytes) / max(1, d_bytes / elapsed))
                            bar = make_progress_bar(pct)
                            mb_dl = d_bytes / (1024 * 1024)
                            mb_tot = t_bytes / (1024 * 1024)

                            ui_text = (
                                f"⬇️ <b>Downloading:</b> <code>{html.escape(safe_name)}</code>\n\n"
                                f"[{bar}] <b>{pct}%</b>\n"
                                f"📦 <b>{mb_dl:.1f} MB</b> / <b>{mb_tot:.1f} MB</b>\n"
                                f"⚡ <b>Speed:</b> {speed_mbps:.1f} MB/s | <b>ETA:</b> {eta}s"
                            )
                            await updater.update(ui_text, reply_markup=cancel_markup)

                    prog_task = asyncio.create_task(progress_reporter())

                    num_connections = 16
                    chunk_size = total_size // num_connections
                    tasks = []
                    for i in range(num_connections):
                        start = i * chunk_size
                        end = (start + chunk_size - 1) if i < num_connections - 1 else total_size - 1
                        tasks.append(download_chunk(session, download_url, headers, start, end, temp_mp4_path, download_state, cancel_event))

                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        download_state["done"] = True
                        prog_task.cancel()

                    download_success = os.path.exists(temp_mp4_path) and os.path.getsize(temp_mp4_path) > 0

                task_info["session"] = None

            if cancel_event.is_set() or not download_success:
                return

            t_dl_done = time.time()
            dl_time = max(1, round(t_dl_done - t_dl_start))

            # Step 3: Smart Compression (ONLY if file exceeds Telegram 50MB limit)
            raw_size = os.path.getsize(temp_mp4_path)
            original_size_str = format_size(raw_size)
            comp_time = 0
            was_compressed = False

            if raw_size > SAFE_UPLOAD_SIZE:
                # Video is > 49.5 MB; compress to guarantee fast delivery under Telegram's 50MB ceiling
                was_compressed = True
                compressed_path = temp_mp4_path + "_compressed.mp4"
                task_info["temp_files"].append(compressed_path)

                compress_text = (
                    f"🗜️ <b>Compressing Video...</b>\n\n"
                    f"📦 <code>{html.escape(safe_name)}</code>\n"
                    f"📐 Size: <b>{original_size_str}</b> &gt; 50MB (Telegram Limit)\n"
                    f"<i>Optimizing resolution &amp; bitrate for fast playback...</i>"
                )
                await updater.update(compress_text, reply_markup=cancel_markup, force=True)

                t_comp_start = time.time()
                ffmpeg_exe = get_ffmpeg_binary()

                proc = await asyncio.create_subprocess_exec(
                    ffmpeg_exe, "-y",
                    "-i", temp_mp4_path,
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "28",
                    "-vf", "scale='min(854,iw)':-2",
                    "-c:a", "aac",
                    "-b:a", "64k",
                    "-movflags", "+faststart",
                    "-fs", "48M",
                    compressed_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                task_info["proc"] = proc
                await proc.wait()
                task_info["proc"] = None

                if cancel_event.is_set():
                    return

                if proc.returncode == 0 and os.path.exists(compressed_path) and os.path.getsize(compressed_path) > 0:
                    try:
                        os.remove(temp_mp4_path)
                    except Exception:
                        pass
                    temp_mp4_path = compressed_path
                    comp_time = max(1, round(time.time() - t_comp_start))
                    formatted_size = format_size(os.path.getsize(temp_mp4_path))
                else:
                    logger.warning(f"[Task {task_id}] Compression failed, attempting original file...")

            # Step 4: Upload to Telegram
            if cancel_event.is_set():
                return

            upload_ui_text = (
                f"⬆️ <b>Uploading to Telegram...</b>\n\n"
                f"📦 <code>{html.escape(safe_name)}</code>\n"
                f"📐 Size: <b>{html.escape(formatted_size)}</b>"
            )
            await updater.update(upload_ui_text, reply_markup=cancel_markup, force=True)

            t_up_start = time.time()
            size_line = (
                f"📐 Size: <b>{html.escape(formatted_size)}</b> <i>(Compressed from {original_size_str})</i>"
                if was_compressed else
                f"📐 Size: <b>{html.escape(formatted_size)}</b>"
            )
            comp_line = f"\n🗜️ Compress: <b>{comp_time}s</b>" if was_compressed else ""

            initial_caption = (
                f"📦 <code>{html.escape(safe_name)}</code>\n"
                f"{size_line}\n\n"
                f"⬇️ Download: <b>{dl_time}s</b>"
                f"{comp_line}\n"
                f"📩 Upload: <b>...</b>\n"
                f"⏱ Total: <b>...</b>"
            )

            with open(temp_mp4_path, "rb") as file_handle:
                video_msg = await update.effective_message.reply_video(
                    video=file_handle,
                    caption=initial_caption,
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    pool_timeout=300,
                    connect_timeout=60,
                    parse_mode="HTML"
                )

            t_up_done = time.time()
            up_time = max(1, round(t_up_done - t_up_start))
            total_time = max(1, round(t_up_done - t_start))

            final_caption = (
                f"📦 <code>{html.escape(safe_name)}</code>\n"
                f"{size_line}\n\n"
                f"⬇️ Download: <b>{dl_time}s</b>"
                f"{comp_line}\n"
                f"📩 Upload: <b>{up_time}s</b>\n"
                f"⏱ Total: <b>{total_time}s</b>"
            )
            try:
                await video_msg.edit_caption(caption=final_caption, parse_mode="HTML")
            except Exception:
                pass

            # Clean up status message
            try:
                await status_msg.delete()
            except Exception:
                pass

    except asyncio.CancelledError:
        logger.info(f"Task {task_id} was cancelled.")
    except Exception as e:
        logger.error(f"Error in task {task_id}: {e}", exc_info=True)
        if not cancel_event.is_set():
            try:
                await status_msg.edit_text(
                    f"⚠️ <i>Could not process the video:</i>\n<code>{html.escape(str(e))}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    finally:
        # Clean up files & registry
        for fp in task_info.get("temp_files", []):
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass

        if chat_id in chat_active_tasks:
            chat_active_tasks[chat_id].discard(task_id)
            if not chat_active_tasks[chat_id]:
                del chat_active_tasks[chat_id]
        active_tasks.pop(task_id, None)

# ----------------- Telegram Handlers -----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command."""
    welcome_text = (
        "👋 <b>Welcome to Video Downloader Bot!</b>\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "🎬 <i>Send me single or multiple links to download fast & stream directly!</i>"
    )
    keyboard = [
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="open_settings"),
            InlineKeyboardButton("ℹ️ Help", callback_data="open_help"),
        ]
    ]
    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /help command."""
    help_text = (
        "ℹ️ <b>How to use:</b>\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "🎥 <b>Features & Performance:</b>\n"
        "• <b>Multiple Links:</b> Send 2 or more links in one message and all are downloaded.\n"
        "• <b>Cancel On Demand:</b> Tap ❌ Cancel at any time to abort immediately.\n"
        "• <b>Turbo Speed:</b> Videos under 50MB stream with 0 compression overhead.\n"
        "• <b>Direct Video Playback:</b> All files are sent as native streaming videos."
    )
    keyboard = [[InlineKeyboardButton("⚙️ Configure Settings", callback_data="open_settings")]]
    if update.message:
        await update.message.reply_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /cancel command to abort all active downloads for the user."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    count = await cancel_all_tasks_for_chat(chat_id)
    if count > 0:
        await update.effective_message.reply_text(
            f"🛑 <b>Cancelled {count} active download(s).</b>",
            parse_mode="HTML"
        )
    else:
        await update.effective_message.reply_text(
            "ℹ️ <i>No active downloads to cancel.</i>",
            parse_mode="HTML"
        )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /settings command."""
    chat_id = update.effective_chat.id
    current_mode = get_user_mode(chat_id)

    settings_text = (
        "⚙️ <b>Download Engine Settings</b>\n\n"
        f"🎯 <b>Current Mode:</b> <code>{html.escape(current_mode)}</code>\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "<i>Select your default engine below:</i>"
    )
    if update.message:
        await update.message.reply_text(settings_text, reply_markup=get_settings_keyboard(current_mode), parse_mode="HTML")

async def callbacks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all inline keyboard callback queries."""
    query = update.callback_query
    if not query:
        return

    chat_id = update.effective_chat.id
    data = query.data or ""

    if data.startswith("cancel:"):
        task_id = data.split(":", 1)[1]
        await cancel_single_task(task_id)
        await query.answer("Download cancelled! ❌", show_alert=False)

    elif data == "cancel_all":
        count = await cancel_all_tasks_for_chat(chat_id)
        await query.answer(f"Cancelled {count} download(s)! 🛑", show_alert=False)

    elif data.startswith("set_mode_"):
        new_mode = data.replace("set_mode_", "")
        set_user_mode(chat_id, new_mode)
        await query.answer(f"Switched default mode to: {new_mode}! ✨", show_alert=False)
        updated_text = (
            "⚙️ <b>Download Engine Settings</b>\n\n"
            f"🎯 <b>Current Mode:</b> <code>{html.escape(new_mode)}</code>\n\n"
            f"{COMMANDS_TEXT}\n\n"
            f"✅ <i>Updated mode to: <b>{html.escape(new_mode)}</b></i>"
        )
        await query.edit_message_text(
            updated_text,
            reply_markup=get_settings_keyboard(new_mode),
            parse_mode="HTML",
        )

    elif data == "open_settings":
        await query.answer()
        current_mode = get_user_mode(chat_id)
        settings_text = (
            "⚙️ <b>Download Engine Settings</b>\n\n"
            f"🎯 <b>Current Mode:</b> <code>{html.escape(current_mode)}</code>\n\n"
            f"{COMMANDS_TEXT}\n\n"
            "<i>Select your default engine below:</i>"
        )
        await query.edit_message_text(
            settings_text,
            reply_markup=get_settings_keyboard(current_mode),
            parse_mode="HTML"
        )

    elif data == "open_help":
        await query.answer()
        help_text = (
            "ℹ️ <b>How to use:</b>\n\n"
            f"{COMMANDS_TEXT}\n\n"
            "🎥 <i>Send any link or multiple links directly to receive videos!</i>"
        )
        keyboard = [[InlineKeyboardButton("⚙️ Configure Settings", callback_data="open_settings")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def link_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects links (including multiple links) and processes each concurrently."""
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    mode = get_user_mode(chat_id)
    text = update.message.text
    urls = re.findall(r"(https?://[^\s]+)", text)

    # Filter supported platform links
    valid_urls = [
        u for u in urls
        if is_terabox_url(u) or is_diskwala_url(u) or is_flezen_url(u)
    ]

    # Deduplicate while preserving order
    seen = set()
    unique_urls = [u for u in valid_urls if not (u in seen or seen.add(u))]

    if not unique_urls:
        if urls:
            await update.message.reply_text(
                "⚠️ <b>Unsupported Link(s)</b>\n\n"
                f"{COMMANDS_TEXT}",
                parse_mode="HTML"
            )
        return

    # If multiple links, show confirmation notice
    if len(unique_urls) > 1:
        await update.message.reply_text(
            f"📥 <b>Detected {len(unique_urls)} links!</b>\n"
            f"🚀 <i>Processing each video concurrently... You can cancel individual downloads or all at once.</i>",
            parse_mode="HTML"
        )

    # Launch task for each link
    for url in unique_urls:
        asyncio.create_task(process_single_link(update, url, mode=mode))

async def command_download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /exp, /expHD, /get, /dw, /fz, /all commands with one or multiple links."""
    if not update.message or not update.message.text:
        return

    raw_cmd = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
    cmd_map = {
        "exp": "exp",
        "exphd": "exphd",
        "get": "get",
        "dw": "dw",
        "fz": "fz",
        "all": "all",
    }
    mode = cmd_map.get(raw_cmd, "exp")

    if context.args and len(context.args) > 0:
        for arg in context.args:
            urls = re.findall(r"(https?://[^\s]+)", arg)
            for u in urls:
                if is_terabox_url(u) or is_diskwala_url(u) or is_flezen_url(u):
                    asyncio.create_task(process_single_link(update, u, mode=mode))
    else:
        sample_domain = "diskwala.com" if mode == "dw" else ("flezen.com" if mode == "fz" else "terabox.app")
        await update.message.reply_text(
            f"ℹ️ <b>Please provide link(s) after the command</b>\n"
            f"Example: <code>/{raw_cmd} https://{sample_domain}/...</code>\n\n"
            f"{COMMANDS_TEXT}",
            parse_mode="HTML"
        )

# ----------------- Main Entrypoint -----------------

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is missing! Set BOT_TOKEN in your environment variables or .env file.")
        sys.exit(1)

    # Build Telegram Application with background web server hooks for Render
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(
        CommandHandler(["exp", "exphd", "get", "dw", "fz", "all"], command_download_handler)
    )

    # Callback handler for settings, cancel & buttons
    application.add_handler(CallbackQueryHandler(callbacks_handler))

    # Message handler for auto-detected links (single or multiple)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_message_handler))

    logger.info("Starting video downloader bot and Render web service...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
