"""
╔══════════════════════════════════════════════════════╗
║       GitHub ZIP Uploader — Telegram Bot             ║
║  يرفع ملفات ZIP مباشرة من تيليجرام إلى GitHub       ║
╚══════════════════════════════════════════════════════╝

المتطلبات:
    pip install pyrogram tgcrypto PyGithub aiofiles python-dotenv

الإعداد:
    1. أنشئ بوت من @BotFather واحصل على BOT_TOKEN
    2. احصل على API_ID و API_HASH من https://my.telegram.org
    3. أنشئ GitHub Token من https://github.com/settings/tokens
       (صلاحيات: repo ✓)
    4. اضبط المتغيرات في قسم CONFIG أدناه أو في ملف .env
    5. شغّل: python github_zip_bot.py
"""

import os
import asyncio
import zipfile
import shutil
import time
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

# ─── تحقق من المكتبات ───────────────────────────────────────────────────────
try:
    from pyrogram import Client, filters
    from pyrogram.types import (
        Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
    )
    from pyrogram.enums import ParseMode
except ImportError:
    raise SystemExit("❌ pyrogram غير مثبت\n   pip install pyrogram tgcrypto")

try:
    from github import Github, GithubException
except ImportError:
    raise SystemExit("❌ PyGithub غير مثبت\n   pip install PyGithub")

try:
    import aiofiles
except ImportError:
    raise SystemExit("❌ aiofiles غير مثبت\n   pip install aiofiles")

# dotenv اختياري
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── LOGGING ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("GitHubBot")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG — اضبط هنا أو في متغيرات البيئة / ملف .env
# ══════════════════════════════════════════════════════════════════════════════
class Config:
    # ─── Telegram ───────────────────────────────────────────────────────────
    BOT_TOKEN:  str = os.getenv("BOT_TOKEN",  "YOUR_BOT_TOKEN_HERE")
    API_ID:     int = int(os.getenv("API_ID",  "0"))       # من my.telegram.org
    API_HASH:   str = os.getenv("API_HASH",   "YOUR_API_HASH_HERE")

    # ─── GitHub (افتراضي — يمكن للمستخدم تغييره) ────────────────────────────
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    # ─── حدود ───────────────────────────────────────────────────────────────
    MAX_FILE_MB: int  = int(os.getenv("MAX_FILE_MB", "50"))   # حجم ZIP الأقصى
    RATE_LIMIT_PAUSE: int = 3   # ثواني بين كل ملف (يحمي من GitHub rate limit)

    # ─── مجلدات مؤقتة ───────────────────────────────────────────────────────
    TEMP_DIR: str = os.getenv("TEMP_DIR", "/tmp/github_bot")

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        if cls.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
            errors.append("BOT_TOKEN غير مضبوط")
        if cls.API_ID == 0:
            errors.append("API_ID غير مضبوط")
        if cls.API_HASH == "YOUR_API_HASH_HERE":
            errors.append("API_HASH غير مضبوط")
        return errors


# ══════════════════════════════════════════════════════════════════════════════
#  حالة المستخدم — نحفظ بيانات كل محادثة في الذاكرة
# ══════════════════════════════════════════════════════════════════════════════
class UserState:
    """بيانات جلسة المستخدم"""
    def __init__(self):
        self.github_token:  Optional[str] = Config.GITHUB_TOKEN or None
        self.repo_name:     Optional[str] = None
        self.is_private:    bool = False
        self.step:          str  = "idle"   # idle | awaiting_token | awaiting_repo | awaiting_zip
        self.upload_stats:  dict = {}

# dict عالمي: user_id → UserState
user_sessions: dict[int, UserState] = {}

def get_state(user_id: int) -> UserState:
    if user_id not in user_sessions:
        user_sessions[user_id] = UserState()
    return user_sessions[user_id]


# ══════════════════════════════════════════════════════════════════════════════
#  مساعدات GitHub
# ══════════════════════════════════════════════════════════════════════════════
async def upload_tree_to_github(
    repo,
    extract_path: str,
    progress_cb,
) -> dict:
    """يرفع جميع الملفات من مجلد محلي إلى GitHub repo"""
    stats = {"uploaded": 0, "failed": 0, "skipped": 0, "errors": []}

    all_files = []
    for root, _, files in os.walk(extract_path):
        for fname in files:
            all_files.append(os.path.join(root, fname))

    total = len(all_files)

    for i, local_path in enumerate(all_files, 1):
        github_path = os.path.relpath(local_path, extract_path)
        # نظّف المسار للـ GitHub (فواصل Unix)
        github_path = github_path.replace("\\", "/")

        try:
            async with aiofiles.open(local_path, "rb") as f:
                content = await f.read()

            # GitHub API — في thread منفصل لعدم حجب event loop
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda p=github_path, c=content: repo.create_file(
                    p, f"add {p}", c
                )
            )
            stats["uploaded"] += 1
            await progress_cb(i, total, github_path, success=True)

        except GithubException as e:
            msg = str(e)
            if "rate limit" in msg.lower():
                await progress_cb(i, total, github_path, success=False,
                                  note="⏳ rate limit، انتظار 60 ثانية...")
                await asyncio.sleep(60)
                # إعادة المحاولة
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda p=github_path, c=content: repo.create_file(
                            p, f"add {p}", c
                        )
                    )
                    stats["uploaded"] += 1
                except Exception as e2:
                    stats["failed"] += 1
                    stats["errors"].append(f"{github_path}: {e2}")
            else:
                stats["failed"] += 1
                stats["errors"].append(f"{github_path}: {e}")
                await progress_cb(i, total, github_path, success=False,
                                  note=f"خطأ: {e.status}")

        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append(f"{github_path}: {e}")
            await progress_cb(i, total, github_path, success=False,
                               note=str(e))

        # توقف بسيط بين الملفات
        if i < total:
            await asyncio.sleep(Config.RATE_LIMIT_PAUSE)

    return stats


# ══════════════════════════════════════════════════════════════════════════════
#  البوت
# ══════════════════════════════════════════════════════════════════════════════
app = Client(
    "github_zip_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
)


# ─── /start ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, msg: Message):
    state = get_state(msg.from_user.id)
    state.step = "awaiting_token"

    # امسح التوكن القديم في كل مرة يكتب /start
    state.github_token = None
    state.repo_name = None

    await msg.reply(
        f"👋 **مرحباً {msg.from_user.first_name}!**\n\n"
        "أنا بوت رفع ملفات ZIP إلى GitHub مباشرةً من تيليجرام.\n\n"
        "🔑 **أرسل GitHub Token الخاص بك للبدء:**\n\n"
        "• احصل عليه من: `https://github.com/settings/tokens`\n"
        "• فعّل صلاحية **repo** ✓\n\n"
        "_(سيُحذف التوكن من المحادثة فور الحفظ)_",
        parse_mode=ParseMode.MARKDOWN
    )


# ─── /help ───────────────────────────────────────────────────────────────────
@app.on_message(filters.command("help") & filters.private)
async def cmd_help(client: Client, msg: Message):
    text = """
📖 **دليل الاستخدام**

**الخطوات:**
1️⃣ احصل على GitHub Token من:
   `https://github.com/settings/tokens`
   (فعّل صلاحية **repo** ✓)

2️⃣ اكتب `/token` وأرسل التوكن

3️⃣ اكتب `/upload` وأدخل:
   • اسم المستودع الجديد
   • نوعه (عام / خاص)

4️⃣ أرسل ملف ZIP مباشرةً في المحادثة

5️⃣ انتظر اكتمال الرفع 🎉

**ملاحظات:**
• الحجم الأقصى للملف: {max_mb} MB
• يدعم جميع أنواع الملفات داخل ZIP
• المستودع يُنشأ تلقائياً
• في حالة rate limit يتوقف تلقائياً ثم يكمل
""".format(max_mb=Config.MAX_FILE_MB)
    await msg.reply(text, parse_mode=ParseMode.MARKDOWN)


# ─── /token ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command("token") & filters.private)
async def cmd_token(client: Client, msg: Message):
    state = get_state(msg.from_user.id)
    state.step = "awaiting_token"
    await msg.reply(
        "🔑 أرسل GitHub Personal Access Token الخاص بك:\n\n"
        "_(الرسالة ستُحذف فور الحفظ لحماية خصوصيتك)_",
        parse_mode=ParseMode.MARKDOWN
    )


# ─── /upload ─────────────────────────────────────────────────────────────────
@app.on_message(filters.command("upload") & filters.private)
async def cmd_upload(client: Client, msg: Message):
    state = get_state(msg.from_user.id)

    if not state.github_token:
        await msg.reply(
            "❌ لم يتم ضبط GitHub Token\n"
            "استخدم /token أولاً"
        )
        return

    state.step = "awaiting_repo"
    await msg.reply(
        "📝 **أدخل اسم المستودع الجديد:**\n\n"
        "_مثال: my-awesome-project_\n"
        "_(بدون مسافات، استخدم - أو \\_)_",
        parse_mode=ParseMode.MARKDOWN
    )


# ─── /status ─────────────────────────────────────────────────────────────────
@app.on_message(filters.command("status") & filters.private)
async def cmd_status(client: Client, msg: Message):
    state = get_state(msg.from_user.id)
    steps = {
        "idle":          "💤 لا توجد عملية جارية",
        "awaiting_token": "⏳ في انتظار GitHub Token",
        "awaiting_repo":  "⏳ في انتظار اسم المستودع",
        "awaiting_zip":   "⏳ في انتظار ملف ZIP",
        "uploading":      "📤 رفع جارٍ...",
    }
    token_mask = (
        f"...{state.github_token[-4:]}" if state.github_token else "غير مُضبوط"
    )
    text = f"""
📊 **حالة الجلسة**

• الخطوة الحالية: {steps.get(state.step, state.step)}
• Token: `{token_mask}`
• المستودع: `{state.repo_name or 'لم يُحدَّد'}`
• النوع: {'🔒 خاص' if state.is_private else '🌍 عام'}
"""
    await msg.reply(text, parse_mode=ParseMode.MARKDOWN)


# ─── /cancel ─────────────────────────────────────────────────────────────────
@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(client: Client, msg: Message):
    state = get_state(msg.from_user.id)
    state.step = "idle"
    state.repo_name = None
    await msg.reply("✅ تم إلغاء العملية. استخدم /upload للبدء من جديد.")


# ─── Callback buttons ─────────────────────────────────────────────────────────
@app.on_callback_query()
async def on_callback(client: Client, query: CallbackQuery):
    state = get_state(query.from_user.id)

    if query.data == "start_upload":
        await query.answer()
        if not state.github_token:
            await query.message.reply(
                "❌ لم يتم ضبط GitHub Token\nاستخدم /token أولاً"
            )
            return
        state.step = "awaiting_repo"
        await query.message.reply(
            "📝 **أدخل اسم المستودع الجديد:**",
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == "set_token":
        await query.answer()
        state.step = "awaiting_token"
        await query.message.reply(
            "🔑 أرسل GitHub Token الخاص بك:"
        )

    elif query.data == "repo_public":
        await query.answer("✅ عام")
        state.is_private = False
        await query.message.edit_text(
            f"✅ المستودع: `{state.repo_name}` — **عام 🌍**\n\n"
            "📦 الآن أرسل ملف ZIP:",
            parse_mode=ParseMode.MARKDOWN
        )
        state.step = "awaiting_zip"

    elif query.data == "repo_private":
        await query.answer("✅ خاص")
        state.is_private = True
        await query.message.edit_text(
            f"✅ المستودع: `{state.repo_name}` — **خاص 🔒**\n\n"
            "📦 الآن أرسل ملف ZIP:",
            parse_mode=ParseMode.MARKDOWN
        )
        state.step = "awaiting_zip"


# ─── معالج الرسائل النصية (machine state) ────────────────────────────────────
@app.on_message(filters.text & filters.private & ~filters.command(["start","help","token","upload","status","cancel"]))
async def on_text(client: Client, msg: Message):
    state = get_state(msg.from_user.id)

    # انتظار token
    if state.step == "awaiting_token":
        token = msg.text.strip()
        # حذف رسالة التوكن فوراً
        try:
            await msg.delete()
        except Exception:
            pass

        # تحقق من صحة التوكن
        verifying = await msg.reply("🔄 جاري التحقق من التوكن...")
        try:
            g = Github(token)
            user = await asyncio.get_event_loop().run_in_executor(
                None, g.get_user
            )
            login = await asyncio.get_event_loop().run_in_executor(
                None, lambda: user.login
            )
            state.github_token = token
            state.step = "idle"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 رفع ZIP", callback_data="start_upload")],
                [InlineKeyboardButton("🔑 تغيير Token", callback_data="set_token")],
            ])
            await verifying.edit_text(
                f"✅ **تم التحقق بنجاح!**\n"
                f"👤 حساب GitHub: `{login}`\n\n"
                "اختر ما تريد:",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await verifying.edit_text(
                f"❌ **Token غير صالح**\n`{e}`\n\n"
                "أرسل التوكن مجدداً أو اضغط /start",
                parse_mode=ParseMode.MARKDOWN
            )
            state.step = "awaiting_token"
        return

    # انتظار اسم المستودع
    if state.step == "awaiting_repo":
        repo_name = msg.text.strip().replace(" ", "-")
        if not repo_name:
            await msg.reply("❌ اسم غير صالح. حاول مجدداً:")
            return
        state.repo_name = repo_name
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🌍 عام",  callback_data="repo_public"),
                InlineKeyboardButton("🔒 خاص", callback_data="repo_private"),
            ]
        ])
        await msg.reply(
            f"✅ اسم المستودع: `{repo_name}`\n\n"
            "اختر نوع المستودع:",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # رسائل عادية
    if state.step == "idle":
        await msg.reply(
            "استخدم /upload لبدء الرفع أو /help للمساعدة."
        )


# ─── معالج ملفات ZIP ──────────────────────────────────────────────────────────
@app.on_message(filters.document & filters.private)
async def on_document(client: Client, msg: Message):
    state = get_state(msg.from_user.id)

    if state.step != "awaiting_zip":
        await msg.reply("أرسل /upload أولاً لبدء عملية رفع جديدة.")
        return

    doc = msg.document
    file_name = doc.file_name or "upload.zip"

    # تحقق من الامتداد
    if not file_name.lower().endswith(".zip"):
        await msg.reply("❌ يجب أن يكون الملف بصيغة ZIP")
        return

    # تحقق من الحجم
    max_bytes = Config.MAX_FILE_MB * 1024 * 1024
    if doc.file_size > max_bytes:
        await msg.reply(
            f"❌ حجم الملف كبير جداً ({doc.file_size // (1024*1024)} MB)\n"
            f"الحد الأقصى: {Config.MAX_FILE_MB} MB"
        )
        return

    state.step = "uploading"

    # ─── مرحلة 1: تنزيل ZIP ───────────────────────────────────────────────
    progress_msg = await msg.reply("⬇️ **جاري تنزيل الملف...**",
                                   parse_mode=ParseMode.MARKDOWN)

    os.makedirs(Config.TEMP_DIR, exist_ok=True)
    work_dir = tempfile.mkdtemp(dir=Config.TEMP_DIR)
    zip_path  = os.path.join(work_dir, file_name)
    extract_path = os.path.join(work_dir, "extracted")

    try:
        await client.download_media(msg, file_name=zip_path)
        await progress_msg.edit_text("✅ **تم تنزيل الملف**\n📦 جاري فك الضغط...",
                                     parse_mode=ParseMode.MARKDOWN)

        # ─── مرحلة 2: فك الضغط ────────────────────────────────────────────
        os.makedirs(extract_path, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(extract_path)
        except zipfile.BadZipFile:
            await progress_msg.edit_text("❌ الملف تالف أو ليس ZIP صحيحاً")
            state.step = "idle"
            shutil.rmtree(work_dir, ignore_errors=True)
            return

        # عدّ الملفات
        all_files = [
            os.path.join(r, f)
            for r, _, fs in os.walk(extract_path)
            for f in fs
        ]
        file_count = len(all_files)
        await progress_msg.edit_text(
            f"✅ **تم فك الضغط** ({file_count} ملف)\n"
            "🔐 جاري الاتصال بـ GitHub...",
            parse_mode=ParseMode.MARKDOWN
        )

        # ─── مرحلة 3: إنشاء المستودع ──────────────────────────────────────
        g    = Github(state.github_token)
        user = await asyncio.get_event_loop().run_in_executor(None, g.get_user)
        login = await asyncio.get_event_loop().run_in_executor(
            None, lambda: user.login
        )

        try:
            repo = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: user.create_repo(
                    state.repo_name,
                    private=state.is_private,
                    auto_init=False,
                )
            )
        except GithubException as e:
            await progress_msg.edit_text(
                f"❌ **فشل إنشاء المستودع**\n`{e.data.get('message', e)}`",
                parse_mode=ParseMode.MARKDOWN
            )
            state.step = "idle"
            shutil.rmtree(work_dir, ignore_errors=True)
            return

        repo_url = await asyncio.get_event_loop().run_in_executor(
            None, lambda: repo.html_url
        )

        await progress_msg.edit_text(
            f"✅ **المستودع جاهز**\n"
            f"🔗 `{repo_url}`\n\n"
            f"📤 جاري رفع {file_count} ملف...",
            parse_mode=ParseMode.MARKDOWN
        )

        # ─── مرحلة 4: رفع الملفات مع تقدم مباشر ──────────────────────────
        last_edit_time = [0.0]

        async def progress_cb(done: int, total: int, path: str,
                               success: bool = True, note: str = ""):
            # تحديث الرسالة كل 3 ثوان فقط (تجنب Telegram flood)
            now = time.time()
            if now - last_edit_time[0] < 3 and done < total:
                return
            last_edit_time[0] = now

            pct   = int(done / total * 100)
            bar   = "█" * (pct // 10) + "░" * (10 - pct // 10)
            icon  = "✔" if success else "✘"
            extra = f"\n_{note}_" if note else ""

            await progress_msg.edit_text(
                f"📤 **رفع الملفات...**\n\n"
                f"`{bar}` {pct}%\n"
                f"{icon} `{path[:45]}`\n"
                f"({done}/{total}){extra}",
                parse_mode=ParseMode.MARKDOWN
            )

        stats = await upload_tree_to_github(repo, extract_path, progress_cb)

        # ─── مرحلة 5: التقرير النهائي ─────────────────────────────────────
        errors_text = ""
        if stats["errors"]:
            sample = "\n".join(f"  • `{e}`" for e in stats["errors"][:5])
            if len(stats["errors"]) > 5:
                sample += f"\n  _...و {len(stats['errors'])-5} أخرى_"
            errors_text = f"\n\n⚠️ **الأخطاء:**\n{sample}"

        await progress_msg.edit_text(
            f"🎉 **اكتمل الرفع!**\n\n"
            f"📊 **الإحصائيات:**\n"
            f"  ✅ مرفوع: {stats['uploaded']}\n"
            f"  ❌ فاشل:  {stats['failed']}\n\n"
            f"🔗 **رابط المستودع:**\n{repo_url}"
            f"{errors_text}",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        log.exception("unexpected error")
        await progress_msg.edit_text(
            f"❌ **خطأ غير متوقع:**\n`{e}`",
            parse_mode=ParseMode.MARKDOWN
        )
    finally:
        state.step = "idle"
        state.repo_name = None
        shutil.rmtree(work_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
#  نقطة الدخول
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    errors = Config.validate()
    if errors:
        print("═" * 50)
        print("⚠️  يجب ضبط المتغيرات التالية:")
        for e in errors:
            print(f"   • {e}")
        print("\nعدّل قسم Config في الكود أو استخدم متغيرات البيئة:")
        print("   BOT_TOKEN=xxx API_ID=yyy API_HASH=zzz python github_zip_bot.py")
        print("═" * 50)
        raise SystemExit(1)

    print("═" * 50)
    print("🤖 GitHub ZIP Bot يعمل...")
    print(f"   الحجم الأقصى: {Config.MAX_FILE_MB} MB")
    print(f"   مجلد مؤقت: {Config.TEMP_DIR}")
    print("═" * 50)

    app.run()
