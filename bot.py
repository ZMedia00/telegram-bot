"""
╔══════════════════════════════════════════╗
║         ⚡ Zed Media Downloader          ║
║  يدعم ملفات أكبر من 50MB بالتقسيم       ║
╚══════════════════════════════════════════╝

المنصات: يوتيوب | انستغرام | تيك توك | تويتر | فيسبوك | وأكثر
"""

import os, re, asyncio, logging, tempfile, shutil, math, subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from telegram.constants import ParseMode
import yt_dlp

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ⚙️  الإعدادات — عدّل هذا فقط
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT_TOKEN   = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MAX_MB      = 49          # حد تيليغرام الفعلي (49 أسلم من 50)
EXECUTOR    = ThreadPoolExecutor(max_workers=4)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

MAX_BYTES = MAX_MB * 1024 * 1024

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🔍  أدوات مساعدة
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLATFORM_PATTERNS = {
    "🎬 YouTube":     r"(youtube\.com|youtu\.be)",
    "📸 Instagram":   r"instagram\.com",
    "🎵 TikTok":      r"tiktok\.com",
    "🐦 Twitter/X":   r"(twitter\.com|x\.com)",
    "👥 Facebook":    r"(facebook\.com|fb\.watch)",
    "👻 Snapchat":    r"snapchat\.com",
    "🤖 Reddit":      r"(reddit\.com|redd\.it)",
    "🎥 Vimeo":       r"vimeo\.com",
    "📺 Dailymotion": r"dailymotion\.com",
    "📌 Pinterest":   r"pinterest\.",
}

def detect_platform(url: str) -> str:
    for name, pat in PLATFORM_PATTERNS.items():
        if re.search(pat, url, re.I):
            return name
    return "🌐 Unknown"

def is_url(text: str) -> bool:
    return bool(re.search(r"https?://\S+", text))

def fmt_size(b: int) -> str:
    return f"{b/1024/1024:.1f} MB"

def fmt_time(sec: int) -> str:
    if not sec:
        return "—"
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ✂️  تقسيم الفيديو بـ FFmpeg
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_duration(filepath: str) -> float:
    """استخرج مدة الفيديو بالثواني"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        return float(out)
    except:
        return 0.0

def split_video(filepath: str, out_dir: str) -> list[str]:
    """
    يقسّم الفيديو إلى أجزاء أصغر من MAX_BYTES.
    يعيد قائمة بمسارات الأجزاء مرتبة.
    """
    file_size = os.path.getsize(filepath)
    
    if file_size <= MAX_BYTES:
        return [filepath]   # لا حاجة للتقسيم
    
    duration = get_duration(filepath)
    if duration <= 0:
        # لا يمكن تحديد المدة → أرسل كما هو (سيفشل لو كان كبيراً)
        return [filepath]
    
    # احسب عدد الأجزاء اللازمة
    num_parts = math.ceil(file_size / MAX_BYTES)
    seg_duration = duration / num_parts
    
    parts = []
    for i in range(num_parts):
        start = i * seg_duration
        out_path = os.path.join(out_dir, f"part_{i+1:02d}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", filepath,
            "-t", str(seg_duration),
            "-c", "copy",          # نسخ بدون إعادة ترميز = سريع جداً
            "-avoid_negative_ts", "make_zero",
            out_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0 and os.path.exists(out_path):
            parts.append(out_path)
    
    return parts if parts else [filepath]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  📥  التحميل بـ yt-dlp
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUALITY_FORMATS = {
    "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "2160p": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
    "audio": "bestaudio/best",
}

def _do_download(url: str, quality: str, tmpdir: str) -> dict:
    is_audio = quality == "audio"
    
    ydl_opts = {
        "outtmpl":              os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "format":               QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"]),
        "merge_output_format":  "mp4",
        "quiet":                True,
        "no_warnings":          True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
        "postprocessors": (
            [{"key": "FFmpegExtractAudio",
              "preferredcodec": "mp3",
              "preferredquality": "320"}]
            if is_audio else []
        ),
        # تجاوز قيود GEO وحماية الحقوق في بعض الحالات
        "geo_bypass": True,
        # تجاهل أخطاء شهادات SSL أحياناً
        "nocheckcertificate": False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            title    = info.get("title", "video")[:60]
            duration = info.get("duration") or 0
            thumb    = info.get("thumbnail", "")
            uploader = info.get("uploader") or info.get("channel") or ""
            
            files = sorted(Path(tmpdir).iterdir(), key=lambda p: p.stat().st_size, reverse=True)
            if not files:
                raise RuntimeError("لم يتم العثور على ملف بعد التحميل")
            
            filepath  = str(files[0])
            file_size = os.path.getsize(filepath)
            
            return {
                "ok":       True,
                "filepath": filepath,
                "title":    title,
                "duration": duration,
                "size":     file_size,
                "thumb":    thumb,
                "uploader": uploader,
                "quality":  quality,
                "is_audio": is_audio,
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def download_media(url: str, quality: str) -> dict:
    tmpdir = tempfile.mkdtemp(prefix="tgbot_")
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(EXECUTOR, _do_download, url, quality, tmpdir)
    result["tmpdir"] = tmpdir
    return result

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  📤  إرسال الفيديو (مع التقسيم التلقائي)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def send_file(bot, chat_id: int, filepath: str, result: dict, part_info: str = ""):
    ext       = Path(filepath).suffix.lower()
    file_size = os.path.getsize(filepath)
    title     = result["title"]
    quality   = result["quality"]
    duration  = result["duration"]
    uploader  = result.get("uploader", "")
    
    caption = (
        f"✅ *{title}*\n"
        f"{'👤 ' + uploader + chr(10) if uploader else ''}"
        f"⏱ {fmt_time(duration)}  |  📦 {fmt_size(file_size)}  |  🎯 {quality}"
        f"{chr(10) + part_info if part_info else ''}"
    )
    
    with open(filepath, "rb") as f:
        if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            await bot.send_video(
                chat_id=chat_id, video=f,
                caption=caption, parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                read_timeout=300, write_timeout=300,
                connect_timeout=60,
            )
        elif ext in (".mp3", ".m4a", ".opus", ".ogg", ".flac"):
            await bot.send_audio(
                chat_id=chat_id, audio=f,
                caption=caption, parse_mode=ParseMode.MARKDOWN,
                read_timeout=300, write_timeout=300,
            )
        elif ext in (".jpg", ".jpeg", ".png", ".webp"):
            await bot.send_photo(
                chat_id=chat_id, photo=f,
                caption=caption, parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await bot.send_document(
                chat_id=chat_id, document=f,
                caption=caption, parse_mode=ParseMode.MARKDOWN,
                read_timeout=300, write_timeout=300,
            )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🤖  أوامر البوت
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Zed Media Downloader*\n\n"
        "📥 أرسل أي رابط وسأحمّله لك بأعلى جودة!\n\n"
        "🌐 *المنصات المدعومة:*\n"
        "يوتيوب • انستغرام • تيك توك • تويتر/X\n"
        "فيسبوك • سناب شات • ريديت • Vimeo\n"
        "Dailymotion • Pinterest وأكثر من 1000 موقع!\n\n"
        "✂️ *الفيديوهات الكبيرة تُقسَّم تلقائياً*\n\n"
        "/help — المساعدة",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Zed Media Downloader — كيفية الاستخدام:*\n\n"
        "1️⃣ انسخ رابط الفيديو أو الصورة\n"
        "2️⃣ أرسله للبوت مباشرة\n"
        "3️⃣ اختر الجودة من الأزرار\n"
        "4️⃣ البوت يحمّل ويرسل تلقائياً ✅\n\n"
        "✂️ *الفيديوهات أكبر من 49MB تُقسَّم تلقائياً*\n"
        "مثلاً: فيديو 150MB → 3 أجزاء بجودة كاملة\n\n"
        "🌐 *يدعم أكثر من 1000 موقع*\n"
        "يوتيوب • انستغرام • تيك توك • تويتر وأكثر!",
        parse_mode=ParseMode.MARKDOWN
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🔗  معالجة الرابط
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # استخرج الرابط من النص
    match = re.search(r"https?://\S+", text)
    if not match:
        await update.message.reply_text(
            "❌ لم أجد رابطاً في رسالتك.\n"
            "أرسل رابطاً يبدأ بـ https://"
        )
        return
    
    url      = match.group(0)
    platform = detect_platform(url)
    
    # حفظ الرابط في بيانات المستخدم
    ctx.user_data["url"] = url
    
    keyboard = [
        [
            InlineKeyboardButton("⭐ أفضل جودة",  callback_data="q:best"),
            InlineKeyboardButton("🖥 4K (2160p)",  callback_data="q:2160p"),
        ],
        [
            InlineKeyboardButton("📺 1080p HD",    callback_data="q:1080p"),
            InlineKeyboardButton("📱 720p",        callback_data="q:720p"),
        ],
        [
            InlineKeyboardButton("📷 480p",        callback_data="q:480p"),
            InlineKeyboardButton("🎵 صوت MP3",     callback_data="q:audio"),
        ],
    ]
    
    await update.message.reply_text(
        f"🔗 *{platform}*\n\n"
        "📥 اختر جودة التحميل:\n"
        "_الفيديوهات الكبيرة تُقسَّم تلقائياً_ ✂️",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🎛️  معالجة الأزرار
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith("q:"):
        return
    
    quality  = data[2:]
    url      = ctx.user_data.get("url")
    chat_id  = query.message.chat_id
    
    if not url:
        await query.edit_message_text("❌ انتهت الجلسة، أعد إرسال الرابط.")
        return
    
    platform = detect_platform(url)
    
    # ─── رسالة الانتظار ───
    status_msg = await query.edit_message_text(
        f"⚡ *Zed Media Downloader*\n\n"
        f"⏳ جاري التحميل...\n"
        f"🌐 {platform}\n"
        f"🎯 الجودة: `{quality}`\n\n"
        "_يرجى الانتظار، قد يستغرق هذا دقيقة..._",
        parse_mode=ParseMode.MARKDOWN,
    )
    
    # ─── التحميل ───
    tmpdir = None
    try:
        result = await download_media(url, quality)
        tmpdir = result.get("tmpdir")
        
        if not result["ok"]:
            err = result.get("error", "خطأ غير معروف")
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"❌ *فشل التحميل*\n\n`{err[:300]}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        
        filepath  = result["filepath"]
        file_size = result["size"]
        title     = result["title"]
        
        # ─── هل يحتاج تقسيم؟ ───
        if file_size > MAX_BYTES:
            parts_count = math.ceil(file_size / MAX_BYTES)
            
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✂️ *الفيديو كبير، سيتم تقسيمه تلقائياً*\n\n"
                    f"📦 الحجم الكلي: {fmt_size(file_size)}\n"
                    f"🔢 عدد الأجزاء: {parts_count} أجزاء\n\n"
                    "_جاري التقسيم والإرسال..._"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            
            # تقسيم في thread منفصل
            split_dir = tempfile.mkdtemp(prefix="split_")
            loop  = asyncio.get_event_loop()
            parts = await loop.run_in_executor(
                EXECUTOR, split_video, filepath, split_dir
            )
            
            # إرسال كل جزء
            for i, part_path in enumerate(parts, 1):
                part_size = os.path.getsize(part_path)
                part_info = f"📂 الجزء {i} من {len(parts)}  |  {fmt_size(part_size)}"
                
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=f"📤 *إرسال الجزء {i}/{len(parts)}...*",
                    parse_mode=ParseMode.MARKDOWN,
                )
                
                await send_file(ctx.bot, chat_id, part_path, result, part_info)
            
            shutil.rmtree(split_dir, ignore_errors=True)
            
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"⚡ *Zed Media Downloader*\n✅ اكتمل الإرسال!\n📂 {len(parts)} أجزاء لـ _{title}_",
                parse_mode=ParseMode.MARKDOWN,
            )
        
        else:
            # ─── ملف عادي أصغر من 49MB ───
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"📤 *جاري الرفع...*\n📦 {fmt_size(file_size)}",
                parse_mode=ParseMode.MARKDOWN,
            )
            await send_file(ctx.bot, chat_id, filepath, result)
        
        # حذف رسالة "جاري التحميل"
        try:
            await status_msg.delete()
        except:
            pass
    
    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=f"❌ *حدث خطأ غير متوقع:*\n`{str(e)[:300]}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🚀  تشغيل البوت
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("=" * 50)
        print("❌  افتح الملف وضع التوكن في:")
        print('   BOT_TOKEN = "توكنك_هنا"')
        print("=" * 50)
        return
    
    # تحقق من وجود ffmpeg
    if shutil.which("ffmpeg") is None:
        print("⚠️  تحذير: ffmpeg غير مثبت!")
        print("   التقسيم لن يعمل. ثبّته بـ:")
        print("   Ubuntu/Debian: sudo apt install ffmpeg")
        print("   macOS:         brew install ffmpeg")
        print("   Windows:       https://ffmpeg.org/download.html\n")
    else:
        print("✅  ffmpeg موجود — التقسيم التلقائي مفعّل")
    
    print("🚀 جاري تشغيل Zed Media Downloader...")
    
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(60)
        .build()
    )
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    print("✅ Zed Media Downloader يعمل! اضغط Ctrl+C للإيقاف\n")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
