import os
import re
import sys
import time
import html
import logging
import asyncio
import tempfile
import aiohttp
from typing import Optional, Dict, Any
from dotenv import load_dotenv

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
MAX_TELEGRAM_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB Telegram Bot API limit

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

# Standard Commands Text Block
COMMANDS_TEXT = (
    "⚙️ <b>Commands:</b>\n"
    "<code>/exp &lt;link&gt;</code>  Reliable &amp; Fast [Recommended]\n"
    "<code>/expHD &lt;link&gt;</code>  For HD Videos [Slow]\n"
    "<code>/get &lt;link&gt;</code>  Unstable [Use for small files]\n"
    "<code>/dw &lt;link&gt;</code>  Download Diskwala video\n"
    "<code>/fz &lt;link&gt;</code>  Download Flezen video\n"
    "<code>/all &lt;link&gt;</code>  Auto-detect link type"
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

# ----------------- UI Component Generators -----------------

async def animate_message(msg, base_text: str, emoji: str = "🔍"):
    """Background task to animate text with dots."""
    dots = 1
    try:
        while True:
            await asyncio.sleep(1.5)
            dots = (dots % 3) + 1
            try:
                await msg.edit_text(f"{emoji} <i>{base_text}{'.' * dots}</i>", parse_mode="HTML")
            except Exception:
                pass
    except asyncio.CancelledError:
        pass

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

# ----------------- Telegram Bot Handlers -----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    welcome_text = (
        "👋 <b>Welcome to the Video Downloader Bot!</b>\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "🎬 <i>Send me any supported link to stream or download videos directly!</i>"
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
    """Handles the /help command."""
    help_text = (
        "ℹ️ <b>How to use:</b>\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "🎥 <b>Video Playback:</b>\n"
        "• Videos under 50MB are downloaded and sent as direct playable video files.\n"
        "• The bot only outputs direct videos, no buttons or files."
    )
    keyboard = [
        [InlineKeyboardButton("⚙️ Configure Settings", callback_data="open_settings")]
    ]
    if update.message:
        await update.message.reply_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /settings command."""
    chat_id = update.effective_chat.id
    current_mode = get_user_mode(chat_id)

    settings_text = (
        "⚙️ <b>Download Engine Settings</b>\n\n"
        f"🎯 <b>Current Mode:</b> <code>{html.escape(current_mode)}</code>\n\n"
        f"{COMMANDS_TEXT}\n\n"
        "<i>Select your default engine below:</i>"
    )

    reply_markup = get_settings_keyboard(current_mode)
    if update.message:
        await update.message.reply_text(settings_text, reply_markup=reply_markup, parse_mode="HTML")

async def callbacks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button callbacks."""
    query = update.callback_query
    if not query:
        return

    chat_id = update.effective_chat.id
    data = query.data or ""

    if data.startswith("set_mode_"):
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
            "🎥 <i>Send any link directly to receive the video!</i>"
        )
        keyboard = [
            [InlineKeyboardButton("⚙️ Configure Settings", callback_data="open_settings")]
        ]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def download_file_and_send(update: Update, result: Dict[str, Any], status_msg) -> bool:
    """Streams and sends ONLY as a native playable Telegram video."""
    if not update.message:
        return False

    download_url = result.get("download_url")
    filename = result.get("filename", "Video.mp4")
    formatted_size = result.get("formatted_size", "Direct Stream")
    is_m3u8 = result.get("is_m3u8", False)
    cookie_str = result.get("cookie_str", "")

    t_start = time.time()
    
    # Ensure clean video filename ending in .mp4
    safe_name = re.sub(r'[\\/*?:"<>|\r\n]', "_", filename).strip() or "video.mp4"
    if not safe_name.lower().endswith((".mp4", ".mkv", ".mov", ".webm", ".avi")):
        safe_name = f"{safe_name}.mp4"

    temp_path: Optional[str] = None
    upload_anim_task = None
    anim_task = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
            temp_path = temp_file.name

        if is_m3u8:
            anim_task = asyncio.create_task(animate_message(status_msg, "Downloading", "⬇️"))
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            
            headers = f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
            if cookie_str:
                headers += f"Cookie: {cookie_str}\r\n"
            headers += "Referer: https://1024terabox.com/\r\n"

            cmd = [exe, "-y", "-headers", headers, "-i", download_url, "-c", "copy", temp_path]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            
            if anim_task:
                anim_task.cancel()
                
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                await status_msg.edit_text("❌ <i>Failed to stream video via M3U8.</i>", parse_mode="HTML")
                return False
                
            # Update size for M3U8 downloaded file
            formatted_size = format_size(os.path.getsize(temp_path))
        else:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            }
            cookie = os.getenv("TERABOX_COOKIE", "").strip()
            if cookie:
                headers["Cookie"] = f"ndus={cookie}" if "ndus=" not in cookie else cookie

            downloaded_bytes = 0
            total_size = 0
            
            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, headers=headers, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                    if resp.status != 200:
                        await status_msg.edit_text(f"❌ <i>Failed to download video from server (HTTP {resp.status}).</i>", parse_mode="HTML")
                        return False

                    total_size = int(resp.headers.get("Content-Length", 0))
                    download_state = {"downloaded": 0, "total": total_size, "done": False}
                    
                    async def update_progress():
                        while not download_state["done"]:
                            await asyncio.sleep(2.0)
                            if download_state["done"]: break
                            if download_state["total"] > 0:
                                d_bytes = download_state["downloaded"]
                                t_bytes = download_state["total"]
                                percent = min(100, int(d_bytes / t_bytes * 100))
                                filled = int(percent / 10)
                                bar = "█" * filled + "░" * (10 - filled)
                                mb_dl = d_bytes / (1024 * 1024)
                                mb_tot = t_bytes / (1024 * 1024)
                                ui_text = f"⬇️ <b>Downloading...</b>\n\n[{bar}] {percent}%\n📦 {mb_dl:.1f}MB / {mb_tot:.1f}MB"
                                try:
                                    await status_msg.edit_text(ui_text, parse_mode="HTML")
                                except Exception:
                                    pass
                                    
                    progress_task = asyncio.create_task(update_progress())

                    try:
                        with open(temp_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(1024 * 1024 * 2):
                                f.write(chunk)
                                download_state["downloaded"] += len(chunk)
                    finally:
                        download_state["done"] = True
                        progress_task.cancel()

        t_dl_done = time.time()
        dl_time = max(1, round(t_dl_done - t_start))

        # Check if compression is needed (Only for direct MP4, M3U8 is usually small)
        if os.path.getsize(temp_path) > MAX_TELEGRAM_UPLOAD_SIZE:
            await status_msg.edit_text("🗜️ <i>Compressing video.</i>", parse_mode="HTML")
            anim_task = asyncio.create_task(animate_message(status_msg, "Compressing video", "🗜️"))
            compressed_path = temp_path + "_compressed.mp4"
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_exe, "-y", "-i", temp_path,
                "-vf", "scale=-2:480",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "28",
                "-c:a", "aac",
                "-b:a", "64k",
                "-fs", "49M",
                compressed_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            
            if anim_task:
                anim_task.cancel()
                
            if proc.returncode == 0 and os.path.exists(compressed_path):
                os.remove(temp_path)
                temp_path = compressed_path
                formatted_size = format_size(os.path.getsize(temp_path))
            else:
                await status_msg.edit_text("❌ <i>Failed to compress video.</i>", parse_mode="HTML")
                return False

        await status_msg.edit_text("⬆️ <i>Uploading.</i>", parse_mode="HTML")
        upload_anim_task = asyncio.create_task(animate_message(status_msg, "Uploading", "⬆️"))
        t_up_start = time.time()

        initial_caption = (
            f"📦 <code>{html.escape(safe_name)}</code>\n"
            f"📐 Size: <b>{html.escape(formatted_size)}</b>\n\n"
            f"⬇️ Download: <b>{dl_time}s</b>\n"
            f"📩 Upload: <b>...</b>\n"
            f"⏱ Total: <b>...</b>"
        )

        with open(temp_path, "rb") as file_handle:
            video_msg = await update.message.reply_video(
                video=file_handle,
                caption=initial_caption,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                pool_timeout=120,
                parse_mode="HTML"
            )

        upload_anim_task.cancel()

        t_up_done = time.time()
        up_time = max(1, round(t_up_done - t_up_start))
        total_time = max(1, round(t_up_done - t_start))

        final_caption = (
            f"📦 <code>{html.escape(safe_name)}</code>\n"
            f"📐 Size: <b>{html.escape(formatted_size)}</b>\n\n"
            f"⬇️ Download: <b>{dl_time}s</b>\n"
            f"📩 Upload: <b>{up_time}s</b>\n"
            f"⏱ Total: <b>{total_time}s</b>"
        )

        try:
            await video_msg.edit_caption(caption=final_caption, parse_mode="HTML")
        except Exception:
            pass

        try:
            await status_msg.delete()
        except Exception:
            pass
        return True

    except Exception as e:
        logger.error(f"Error streaming video: {e}")
        if 'upload_anim_task' in locals() and upload_anim_task:
            upload_anim_task.cancel()
        if 'anim_task' in locals() and anim_task:
            anim_task.cancel()
        try:
            await status_msg.edit_text(f"⚠️ <i>Could not process the video:</i>\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")
        except Exception:
            pass
        return False
    finally:
        if 'upload_anim_task' in locals() and upload_anim_task:
            upload_anim_task.cancel()
        if 'anim_task' in locals() and anim_task:
            anim_task.cancel()
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

async def handle_link_extraction(update: Update, raw_url: str, mode: str = "exp"):
    """Intelligently processes TeraBox, DiskWala, or Flezen URLs to get direct videos."""
    if not update.message:
        return

    # Identify platform
    if is_diskwala_url(raw_url):
        platform_label = "DiskWala"
        extractor = lambda: get_diskwala_info(raw_url, mode=mode)
    elif is_flezen_url(raw_url):
        platform_label = "Flezen"
        extractor = lambda: get_flezen_info(raw_url, mode=mode)
    else:
        platform_label = "TeraBox"
        extractor = lambda: get_terabox_info(raw_url, mode=mode)

    status_msg = await update.message.reply_text(
        f"🔍 <i>Features Detected.</i>",
        parse_mode="HTML"
    )
    
    anim_task = asyncio.create_task(animate_message(status_msg, "Features Detected", "🔍"))

    result = await extractor()
    
    anim_task.cancel()

    if not result.get("ok"):
        error_msg = result.get("error", "Failed to retrieve video details.")
        
        if platform_label in ["DiskWala", "Flezen"]:
            fallback_url = raw_url
            if is_diskwala_url(raw_url):
                fallback_url = f"https://www.diskwala.com/app/{result.get('file_id')}" if result.get('file_id') else raw_url
                
            error_card = (
                f"❌ <b>Extraction Notice</b>\n\n"
                f"⚠️ <i>{html.escape(error_msg)}</i>\n\n"
                f"You can still watch it online below:"
            )
            keyboard = [[InlineKeyboardButton(f"📺 Watch on {platform_label}", url=fallback_url)]]
            await status_msg.edit_text(error_card, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            error_card = (
                "❌ <b>Extraction Notice</b>\n\n"
                f"⚠️ <i>{html.escape(error_msg)}</i>\n\n"
                f"{COMMANDS_TEXT}"
            )
            await status_msg.edit_text(error_card, parse_mode="HTML")
        return

    download_url = result.get("download_url")
    
    if not download_url:
        fallback_url = raw_url
        if is_diskwala_url(raw_url):
            fallback_url = f"https://www.diskwala.com/app/{result.get('file_id')}" if result.get('file_id') else raw_url
            
        error_card = (
            f"❌ <b>Direct Video Blocked</b>\n\n"
            f"⚠️ <i>{platform_label}'s security prevented direct video extraction.</i>\n\n"
            f"You can still watch it online below:"
        )
        keyboard = [[InlineKeyboardButton(f"📺 Watch on {platform_label}", url=fallback_url)]]
        await status_msg.edit_text(error_card, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # Send ONLY direct video
    await download_file_and_send(update, result, status_msg)


async def link_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects links in messages and extracts them using user's configured mode."""
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    mode = get_user_mode(chat_id)
    text = update.message.text
    urls = re.findall(r"(https?://[^\s]+)", text)

    for url in urls:
        if is_terabox_url(url) or is_diskwala_url(url) or is_flezen_url(url):
            await handle_link_extraction(update, url, mode=mode)
            return

    if urls:
        await update.message.reply_text(
            "⚠️ <b>Unsupported Link</b>\n\n"
            f"{COMMANDS_TEXT}",
            parse_mode="HTML"
        )

async def command_download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /exp, /expHD, /get, /dw, /fz, /all commands with link."""
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
        url = context.args[0]
        await handle_link_extraction(update, url, mode=mode)
    else:
        sample_domain = "diskwala.com" if mode == "dw" else ("flezen.com" if mode == "fz" else "terabox.app")
        await update.message.reply_text(
            f"ℹ️ <b>Please provide a link after the command</b>\n"
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
    application.add_handler(
        CommandHandler(["exp", "exphd", "get", "dw", "fz", "all"], command_download_handler)
    )

    # Callback handler for settings & buttons
    application.add_handler(CallbackQueryHandler(callbacks_handler))

    # Message handler for auto-detected links
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_message_handler))

    logger.info("Starting video downloader bot and Render web service...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
