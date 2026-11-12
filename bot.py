"""
ربات پل دلتاچت ↔ تلگرام

پیام‌های گروه دلتاچت را به گروه تلگرام فوروارد می‌کند و پیام ادمین‌ها در
تلگرام را به همان گروه دلتاچت می‌رساند. Reply ها هم دو طرفه منتقل می‌شوند.

راهنمای نصب و اجرا در README.md نوشته شده.
"""

from __future__ import annotations

import asyncio
import functools
import html
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from deltachat_rpc_client import DeltaChat, EventType, Rpc, SpecialContactId
from telegram import Bot, LinkPreviewOptions, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

try:
    import config
except ModuleNotFoundError as e:
    if e.name != "config":
        raise
    raise SystemExit(
        "فایل config.py پیدا نشد.\n"
        "یک کپی از نمونه بساز و مقادیر خودت را داخلش بگذار:\n"
        "    cp config.example.py config.py"
    ) from None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bridge")

# ── محدودیت‌های تلگرام ──────────────────────────────────────────────────────
TG_TEXT_LIMIT = 4000        # سقف واقعی ۴۰۹۶ کاراکتر
TG_CAPTION_LIMIT = 1000     # سقف واقعی ۱۰۲۴ کاراکتر
MAP_LIMIT = 2000            # حداکثر تعداد آیدی پیام که برای Reply نگه می‌داریم
TMP_DIR = Path(__file__).resolve().parent / "media_tmp"

# ── حالت مشترک ──────────────────────────────────────────────────────────────
# dc msg_id → tg message_id و برعکس، برای اینکه Reply ها سر جای خودشان بروند
dc_to_tg: dict[int, int] = {}
tg_to_dc: dict[int, int] = {}

dc_rpc: Rpc | None = None
dc_account = None
dc_group_chat = None
tg_bot: Bot | None = None


def remember(mapping: dict[int, int], key: int, value: int) -> None:
    """ثبت نقشه‌ی آیدی پیام‌ها؛ قدیمی‌ترین‌ها حذف می‌شوند تا حافظه بالا نرود."""
    mapping[key] = value
    while len(mapping) > MAP_LIMIT:
        mapping.pop(next(iter(mapping)))


# ══════════════════════════════════════════════════════════════════════════════
# ابزارهای کوچک
# ══════════════════════════════════════════════════════════════════════════════

async def dc_call(func, *args):
    """متدهای کتابخانه‌ی دلتاچت بلاک‌کننده‌اند، پس در ترد جداگانه اجرا می‌شوند."""
    return await asyncio.to_thread(func, *args)


def check_config() -> None:
    """تنظیمات لازم را چک می‌کند تا به‌جای خطای عجیب، خطای واضح بدهیم."""
    problems = []
    if not config.TG_BOT_TOKEN or config.TG_BOT_TOKEN == "YOUR_BOT_TOKEN":
        problems.append("TG_BOT_TOKEN پر نشده (توکن را از @BotFather بگیر).")
    if not config.TG_CHAT_ID:
        problems.append("TG_CHAT_ID پر نشده.")
    if not config.TG_ADMIN_IDS:
        problems.append("TG_ADMIN_IDS پر نشده.")
    if problems:
        raise SystemExit("تنظیمات ناقص است:\n - " + "\n - ".join(problems))


def build_proxy_url() -> str | None:
    """ساخت آدرس پروکسی تلگرام. اگر PROXY_HOST خالی باشد یعنی پروکسی لازم نیست."""
    if not config.PROXY_HOST:
        return None
    auth = f"{config.PROXY_USER}:{config.PROXY_PASS}@" if config.PROXY_USER else ""
    return f"{config.PROXY_TYPE.lower()}://{auth}{config.PROXY_HOST}:{config.PROXY_PORT}"


def find_rpc_server() -> str:
    """مسیر اجرایی deltachat-rpc-server (هسته‌ی دلتاچت).

    ترتیب جست‌وجو: مسیر دستی در config → کنار مفسر پایتون (محیط مجازی) → PATH.
    """
    manual_path = getattr(config, "DC_RPC_SERVER_PATH", "")
    if manual_path:
        return manual_path

    exe = "deltachat-rpc-server.exe" if os.name == "nt" else "deltachat-rpc-server"
    beside_python = Path(sys.executable).parent / exe
    if beside_python.exists():
        return str(beside_python)

    found = shutil.which("deltachat-rpc-server")
    if found:
        return found

    raise FileNotFoundError(
        "دلتاچت‌کور پیدا نشد. یا «pip install deltachat-rpc-server» را اجرا کن "
        "یا مسیر فایل را در config.DC_RPC_SERVER_PATH بگذار."
    )


def split_text(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    """متن بلند را به تکه‌های مجاز تلگرام می‌شکند (اول پاراگراف، بعد خط، بعد کلمه)."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def reply_params(message_id: int | None) -> ReplyParameters | None:
    """پارامترهای Reply تلگرام (اگر پیام مرجع حذف شده باشد، بدون Reply می‌فرستد)."""
    if not message_id:
        return None
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True)


# ══════════════════════════════════════════════════════════════════════════════
# تشخیص نوع پیام دلتاچت
# ══════════════════════════════════════════════════════════════════════════════

TYPE_LABEL_FA = {
    "image":    "📷 تصویر",
    "gif":      "🎞 گیف",
    "video":    "🎥 ویدیو",
    "audio":    "🔊 صدا",
    "voice":    "🎤 ویس",
    "music":    "🎵 موزیک",
    "sticker":  "🖼 استیکر",
    "file":     "📎 فایل",
    "location": "📍 لوکیشن",
}


def classify_message(snap) -> str:
    """نوع پیام را برمی‌گرداند: text | image | gif | video | audio | voice | music | sticker | file | location"""
    if snap.get("has_location"):
        return "location"

    view_type = snap.get("view_type") or ""
    if view_type == "Image":
        return "image"
    if view_type == "Gif":
        return "gif"
    if view_type == "Video":
        return "video"
    if view_type == "Voice":
        return "voice"
    if view_type == "Sticker":
        return "sticker"
    if view_type == "Audio":
        # موزیک معمولاً تگ دارد و فرمتش با ویس فرق می‌کند
        mime = (snap.get("file_mime") or "").lower()
        if mime in ("audio/mpeg", "audio/mp3", "audio/flac", "audio/mp4", "audio/aac", "audio/x-m4a"):
            return "music"
        return "audio"
    if view_type in ("File", "Vcard", "Webxdc"):
        return "file"
    return "text"


def should_ignore(msg_type: str) -> bool:
    """آیا این نوع پیام باید ایگنور شود؟ (تنظیماتش در config.py است)"""
    return {
        "text":     config.IGNORE_TEXT,
        "image":    config.IGNORE_IMAGE,
        "gif":      config.IGNORE_GIF,
        "video":    config.IGNORE_VIDEO,
        "audio":    config.IGNORE_AUDIO,
        "voice":    config.IGNORE_VOICE,
        "music":    config.IGNORE_MUSIC,
        "sticker":  config.IGNORE_STICKER,
        "file":     config.IGNORE_FILE,
        "location": config.IGNORE_LOCATION,
    }.get(msg_type, False)


def format_head(sender: str, msg_type: str) -> str:
    """سرتیتر پیام تلگرام: [ نام فرستنده ] و نوع پیام."""
    name = html.escape(sender)
    if msg_type == "text":
        return f"<b>[{name}]:</b>"
    label = TYPE_LABEL_FA.get(msg_type, msg_type)
    return f"<b>[{name}]</b> <i>[{label}]</i>"


def build_texts(sender: str, msg_type: str, text: str) -> list[str]:
    """متن دلتاچت را به پیام‌های آماده‌ی تلگرام تبدیل می‌کند.

    اول متن خام تکه می‌شود و بعد escape می‌شود، وگرنه ممکن است یک تکه
    وسط کاراکترهایی مثل &amp; بیفتد و تلگرام خطای پارس بدهد.
    """
    chunks = split_text(text)
    if not chunks:
        return []
    out = [f"{format_head(sender, msg_type)}\n{html.escape(chunks[0])}"]
    out.extend(html.escape(chunk) for chunk in chunks[1:])
    return out


# ══════════════════════════════════════════════════════════════════════════════
# راه‌اندازی دلتاچت
# ══════════════════════════════════════════════════════════════════════════════

async def configure_account(account) -> None:
    """پیکربندی اکانت دلتاچت با اطلاعات config (فقط بار اول لازم است)."""
    if not config.DC_EMAIL or not config.DC_PASSWORD:
        raise RuntimeError("اکانت پیکربندی نشده و DC_EMAIL / DC_PASSWORD خالی است.")

    logger.info("در حال پیکربندی اکانت %s …", config.DC_EMAIL)
    await dc_call(account.set_config, "addr", config.DC_EMAIL)
    await dc_call(account.set_config, "mail_pw", config.DC_PASSWORD)
    await dc_call(account.set_config, "mail_server", config.DC_IMAP_HOST)
    await dc_call(account.set_config, "mail_port", str(config.DC_IMAP_PORT))
    await dc_call(account.set_config, "send_server", config.DC_SMTP_HOST)
    await dc_call(account.set_config, "send_port", str(config.DC_SMTP_PORT))
    await dc_call(account.set_config, "send_security", "3")
    await dc_call(account.set_config, "imap_security", "3")
    await dc_call(account.set_config, "displayname", getattr(config, "DC_DISPLAY_NAME", "user"))
    await dc_call(account.configure)
    logger.info("پیکربندی با موفقیت انجام شد.")


async def find_or_join_group(account):
    """گروه دلتاچت را از بین چت‌ها پیدا می‌کند و اگر نبود، با لینک دعوت جوین می‌شود."""
    for chat in await dc_call(account.get_chatlist):
        snap = await dc_call(chat.get_basic_snapshot)
        if config.DC_GROUP_NAME_HINT and config.DC_GROUP_NAME_HINT in (snap.name or ""):
            logger.info("گروه «%s» پیدا شد.", snap.name)
            return chat

    if not config.DC_INVITE_LINK:
        raise RuntimeError("گروهی با این نام پیدا نشد و DC_INVITE_LINK هم خالی است.")

    logger.info("گروه نبود؛ با لینک دعوت جوین می‌شویم …")
    chat = await dc_call(account.secure_join, config.DC_INVITE_LINK)
    snap = await dc_call(chat.get_basic_snapshot)
    logger.info("جوین شدیم به: %s", snap.name)
    return chat


async def setup_deltachat() -> None:
    """هسته را بالا می‌آورد، اکانت را آماده و گروه را پیدا می‌کند."""
    global dc_rpc, dc_account, dc_group_chat

    dc_rpc = Rpc(accounts_dir=config.DC_ACCOUNTS_DIR, rpc_server_path=find_rpc_server())
    await asyncio.to_thread(dc_rpc.start)
    dc = DeltaChat(dc_rpc)

    accounts = await dc_call(dc.get_all_accounts)
    if accounts:
        dc_account = accounts[0]
        logger.info("اکانت موجود بارگذاری شد.")
    else:
        dc_account = await dc_call(dc.add_account)
        logger.info("اکانت جدید ساخته شد.")

    if not await dc_call(dc_account.is_configured):
        await configure_account(dc_account)

    await dc_call(dc_account.start_io)
    logger.info("IO دلتاچت شروع شد.")

    dc_group_chat = await find_or_join_group(dc_account)


# ══════════════════════════════════════════════════════════════════════════════
# ارسال به تلگرام
# ══════════════════════════════════════════════════════════════════════════════

# نوع پیام دلتاچت → (متد تلگرام، نام آرگومان فایل)
MEDIA_SENDERS = {
    "image":   ("send_photo", "photo"),
    "gif":     ("send_animation", "animation"),
    "video":   ("send_video", "video"),
    "voice":   ("send_voice", "voice"),
    "audio":   ("send_audio", "audio"),
    "music":   ("send_audio", "audio"),
    "sticker": ("send_sticker", "sticker"),
    "file":    ("send_document", "document"),
}


async def send_texts_to_telegram(texts: list[str], reply_to_id: int | None = None) -> list[int]:
    """پیام‌های متنی را به گروه تلگرام می‌فرستد و آیدی‌های تلگرام را برمی‌گرداند."""
    ids: list[int] = []
    for index, text in enumerate(texts):
        sent = await tg_bot.send_message(
            chat_id=config.TG_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_parameters=reply_params(reply_to_id if index == 0 else None),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        ids.append(sent.message_id)
    return ids


async def send_media_to_telegram(
    sender: str,
    msg_type: str,
    path: Path,
    file_name: str,
    text: str,
    reply_to_id: int | None = None,
) -> list[int]:
    """فایل را با متد مناسب نوعش می‌فرستد؛ اگر کپشن جا نشد، متن را جدا می‌فرستد."""
    head = format_head(sender, msg_type)
    caption = ""
    texts: list[str] = []
    if text:
        caption = f"{head}:\n{html.escape(text)}"
        if len(caption) > TG_CAPTION_LIMIT:
            caption = ""
            texts = build_texts(sender, msg_type, text)

    method_name, arg_name = MEDIA_SENDERS.get(msg_type, ("send_document", "document"))
    kwargs = {arg_name: path}
    if arg_name == "document":
        kwargs["filename"] = file_name

    try:
        sent = await getattr(tg_bot, method_name)(
            chat_id=config.TG_CHAT_ID,
            caption=caption or None,
            parse_mode=ParseMode.HTML,
            reply_parameters=reply_params(reply_to_id),
            **kwargs,
        )
    except Exception as e:
        # مثلاً استیکر webp نیست یا ویس با فرمت تلگرام نمی‌خواند
        logger.warning("ارسال «%s» با %s نشد (%s)؛ به صورت فایل معمولی می‌فرستیم.", msg_type, method_name, e)
        sent = await tg_bot.send_document(
            chat_id=config.TG_CHAT_ID,
            document=path,
            filename=file_name,
            caption=caption or None,
            parse_mode=ParseMode.HTML,
            reply_parameters=reply_params(reply_to_id),
        )

    ids = [sent.message_id]
    if texts:
        # متن از سقف کپشن رد شده؛ جدا می‌فرستیم و به همان فایل Reply می‌کنیم
        ids.extend(await send_texts_to_telegram(texts, sent.message_id))
    return ids


# ══════════════════════════════════════════════════════════════════════════════
# دلتاچت → تلگرام
# ══════════════════════════════════════════════════════════════════════════════

async def download_message_file(msg_id: int, snap) -> tuple[object, Path | None]:
    """فایل پیام را در صورت نیاز دانلود می‌کند و snapshot تازه و مسیر محلی را می‌دهد."""
    if snap.get("download_state") != "Done" or not snap.get("file"):
        try:
            await dc_call(dc_rpc.download_full_message, dc_account.id, msg_id)
        except Exception as e:
            logger.warning("دانلود فایل پیام %s نشد: %s", msg_id, e)
            return snap, None

        msg = await dc_call(dc_account.get_message_by_id, msg_id)
        snap = await dc_call(msg.get_snapshot)

    path = snap.get("file")
    if not path or not Path(path).exists():
        return snap, None
    return snap, Path(path)


async def sender_name(snap) -> str:
    """نام فرستنده؛ اگر برای گروه اسم جدا گذاشته باشد، همان اولویت دارد."""
    if snap.get("override_sender_name"):
        return snap.override_sender_name
    contact = await dc_call(snap.sender.get_snapshot)
    return contact.display_name or contact.address


async def forward_to_telegram(msg_id: int) -> None:
    """یک پیام دلتاچت را به گروه تلگرام منتقل می‌کند."""
    msg = await dc_call(dc_account.get_message_by_id, msg_id)
    snap = await dc_call(msg.get_snapshot)

    # پیام‌های سیستمی و پیام‌های خودمان (که همین ربات فرستاده) منتقل نمی‌شوند
    if snap.get("is_info") or snap.get("from_id") in (SpecialContactId.SELF, SpecialContactId.INFO):
        return

    msg_type = classify_message(snap)
    caption = (snap.get("text") or "").strip()
    ignored = should_ignore(msg_type)

    if msg_type == "text" and (ignored or not caption):
        return
    if ignored and (not caption or config.IGNORE_MEDIA_CAPTION):
        return

    reply_to_tg_id = None
    quote = snap.get("quote") or {}
    if quote.get("message_id"):
        reply_to_tg_id = dc_to_tg.get(quote["message_id"])

    sender = await sender_name(snap)

    if msg_type == "text" or ignored:
        texts = build_texts(sender, msg_type, caption)
        message_ids = await send_texts_to_telegram(texts, reply_to_tg_id)
    else:
        snap, path = await download_message_file(msg_id, snap)
        if path is None:
            # فایل دانلود نشد؛ حداقل متن یا نوع پیام را اطلاع بده
            logger.warning("فایل پیام %s در دسترس نیست.", msg_id)
            texts = build_texts(sender, msg_type, caption)
            message_ids = await send_texts_to_telegram(texts, reply_to_tg_id)
        else:
            file_name = snap.get("file_name") or path.name
            message_ids = await send_media_to_telegram(
                sender, msg_type, path, file_name, caption, reply_to_tg_id
            )

    if message_ids:
        remember(dc_to_tg, msg_id, message_ids[0])
        for tg_id in message_ids:
            remember(tg_to_dc, tg_id, msg_id)

    logger.info("دلتاچت → تلگرام: %s از %s", msg_type, sender)


async def deltachat_listener() -> None:
    """حلقه‌ی گوش دادن به پیام‌های گروه دلتاچت."""
    group_snap = await dc_call(dc_group_chat.get_basic_snapshot)
    target_chat_id = group_snap.id
    logger.info("گوش دادن به پیام‌های دلتاچت شروع شد …")

    while True:
        try:
            event = await dc_call(dc_account.wait_for_event, EventType.INCOMING_MSG)
            if event.chat_id != target_chat_id:
                continue
            await forward_to_telegram(event.msg_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("خطا در listener دلتاچت: %s", e, exc_info=True)
            await asyncio.sleep(3)


# ══════════════════════════════════════════════════════════════════════════════
# تلگرام → دلتاچت
# ══════════════════════════════════════════════════════════════════════════════

async def download_telegram_file(message) -> Path | None:
    """فایل پیام تلگرام را در پوشه‌ی موقت پروژه ذخیره می‌کند."""
    attachment = message.effective_attachment
    if isinstance(attachment, (list, tuple)):   # عکس چند سایز دارد؛ بزرگ‌ترین را برمی‌داریم
        attachment = attachment[-1]
    if attachment is None:
        return None

    tg_file = await attachment.get_file()
    name = getattr(attachment, "file_name", None) or Path(tg_file.file_path or "file").name
    TMP_DIR.mkdir(exist_ok=True)
    target = TMP_DIR / f"{int(time.time())}-{name}"
    await tg_file.download_to_drive(target)
    return target


def cleanup_tmp_files(max_age_seconds: int = 24 * 3600) -> None:
    """فایل‌های موقت قدیمی را پاک می‌کند."""
    if not TMP_DIR.exists():
        return
    now = time.time()
    for file in TMP_DIR.iterdir():
        if file.is_file() and now - file.stat().st_mtime > max_age_seconds:
            file.unlink(missing_ok=True)


async def send_to_deltachat(text: str, file_path: Path | None = None, quoted_id: int | None = None) -> None:
    """متن یا فایل ادمین را به گروه دلتاچت می‌فرستد."""
    if file_path is not None:
        await dc_call(functools.partial(
            dc_group_chat.send_message,
            text=text or None,
            file=str(file_path),
            filename=file_path.name,
        ))
    elif quoted_id:
        await dc_call(functools.partial(dc_group_chat.send_message, text=text, quoted_msg=quoted_id))
    else:
        await dc_call(dc_group_chat.send_text, text)


async def telegram_message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام ادمین در گروه تلگرام → گروه دلتاچت."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    if chat.id != config.TG_CHAT_ID or user.id not in config.TG_ADMIN_IDS:
        return

    text = (message.text or message.caption or "").strip()
    file_path = None
    try:
        quoted_id = None
        if message.reply_to_message:
            quoted_id = tg_to_dc.get(message.reply_to_message.message_id)

        if message.effective_attachment is not None:
            file_path = await download_telegram_file(message)

        if not text and file_path is None:
            return

        await send_to_deltachat(text, file_path, quoted_id)
        logger.info("تلگرام → دلتاچت: %s", (text or file_path.name)[:60])
    except Exception as e:
        logger.error("خطا در ارسال به دلتاچت: %s", e, exc_info=True)
        await message.reply_text(f"❌ ارسال به دلتاچت نشد:\n{e}")


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    global tg_bot

    check_config()
    cleanup_tmp_files()

    # ── اتصال به تلگرام (با پروکسی) ─────────────────────────────────────────
    proxy_url = build_proxy_url()
    logger.info("پروکسی تلگرام: %s", proxy_url or "بدون پروکسی")

    request = HTTPXRequest(proxy=proxy_url)
    tg_app = (
        Application.builder()
        .token(config.TG_BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .build()
    )
    tg_bot = tg_app.bot
    tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, telegram_message_handler))

    # ── راه‌اندازی دلتاچت ───────────────────────────────────────────────────
    await setup_deltachat()
    logger.info("ربات پل آماده است ✅")

    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)

        listener = asyncio.create_task(deltachat_listener())
        try:
            await asyncio.Event().wait()      # تا وقتی Ctrl+C نزنیم همین‌جا می‌مانیم
        finally:
            listener.cancel()
            await tg_app.updater.stop()
            await tg_app.stop()
            await asyncio.to_thread(dc_rpc.close)
            logger.info("خداحافظ 👋")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
