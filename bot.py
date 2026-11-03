"""
Şan Dersi Takvim Botu v2
------------------------
• Görsel haftalık takvim (Telegram Mini App) – dokun / sürükle ile boya
• Yazıyla anlama: "salı 18-21 müsaitim", "hafta içi akşam", "cmt 10'dan sonra"
• /ortak – herkesin ortak boş saatleri, renkli haftalık tablo
• /ders – ders saatini seç; bot sabitler, bir gün önce ve 1 saat önce hatırlatır
• /gelemem – bu haftaki derse gelemeyeceğini bildir
"""
import logging
import os
import re
import sqlite3
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    ReplyKeyboardMarkup, Update, WebAppInfo,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN eksik. .env dosyasına BOT_TOKEN=... yaz.")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()
HOUR_START = int(os.environ.get("HOUR_START", "9"))
HOUR_END = int(os.environ.get("HOUR_END", "22"))
GROUP_SIZE = int(os.environ.get("GROUP_SIZE", "3"))
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Istanbul"))

DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
DAYS_LONG = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
HOURS = list(range(HOUR_START, HOUR_END))
NH = len(HOURS)
ALL_SLOTS = {(d, h) for d in range(7) for h in HOURS}

DB_PATH = Path(__file__).with_name("takvim.db")
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("san-bot")
BOT_USERNAME = ""


# =============================== veritabanı ===============================
def db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE IF NOT EXISTS slots (user_id INTEGER, day INTEGER, hour INTEGER,
        PRIMARY KEY (user_id, day, hour));
    CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT);
    CREATE TABLE IF NOT EXISTS lesson (chat_id INTEGER PRIMARY KEY, day INTEGER, hour INTEGER,
        set_by TEXT, msg_id INTEGER);
    CREATE TABLE IF NOT EXISTS absent (chat_id INTEGER, user_id INTEGER, week TEXT,
        PRIMARY KEY (chat_id, user_id, week));
    """)
    return con


def register(user, chat=None):
    name = user.first_name or user.username or str(user.id)
    with db() as con:
        con.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (user.id, name))
        if chat is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            con.execute("INSERT OR REPLACE INTO chats VALUES (?,?)", (chat.id, chat.title or ""))


def all_users():
    with db() as con:
        return con.execute("SELECT user_id, name FROM users ORDER BY name").fetchall()


def user_name(uid):
    with db() as con:
        r = con.execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r else str(uid)


def user_slots(uid):
    with db() as con:
        return {(d, h) for d, h in con.execute("SELECT day, hour FROM slots WHERE user_id=?", (uid,))}


def set_slots(uid, slots):
    with db() as con:
        con.execute("DELETE FROM slots WHERE user_id=?", (uid,))
        con.executemany("INSERT INTO slots VALUES (?,?,?)", [(uid, d, h) for d, h in slots])


def add_slots(uid, slots):
    with db() as con:
        con.executemany("INSERT OR IGNORE INTO slots VALUES (?,?,?)", [(uid, d, h) for d, h in slots])


def remove_slots(uid, slots):
    with db() as con:
        con.executemany("DELETE FROM slots WHERE user_id=? AND day=? AND hour=?", [(uid, d, h) for d, h in slots])


def group_chats():
    with db() as con:
        return [r[0] for r in con.execute("SELECT chat_id FROM chats")]


def get_lesson(chat_id):
    with db() as con:
        return con.execute("SELECT day, hour, set_by, msg_id FROM lesson WHERE chat_id=?", (chat_id,)).fetchone()


def set_lesson(chat_id, day, hour, set_by, msg_id=None):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO lesson VALUES (?,?,?,?,?)", (chat_id, day, hour, set_by, msg_id))
        con.execute("DELETE FROM absent WHERE chat_id=?", (chat_id,))


def del_lesson(chat_id):
    with db() as con:
        con.execute("DELETE FROM lesson WHERE chat_id=?", (chat_id,))
        con.execute("DELETE FROM absent WHERE chat_id=?", (chat_id,))


def absentees(chat_id, week):
    with db() as con:
        return [user_name(r[0]) for r in con.execute(
            "SELECT user_id FROM absent WHERE chat_id=? AND week=?", (chat_id, week))]


# =============================== yardımcılar ===============================
def encode(slots):
    v = 0
    for d, h in slots:
        if h in HOURS:
            v |= 1 << (d * NH + (h - HOUR_START))
    return format(v, "x").zfill((7 * NH + 3) // 4)


def decode(hexs):
    try:
        v = int(hexs, 16)
    except ValueError:
        return None
    return {(i // NH, HOUR_START + i % NH) for i in range(7 * NH) if v >> i & 1}


def common_slots():
    users = all_users()
    sets = [user_slots(uid) for uid, _ in users]
    return users, sets, (set.intersection(*sets) if sets else set())


def fmt_ranges(hours):
    hours = sorted(hours)
    out, start, prev = [], None, None
    for h in hours:
        if start is None:
            start = prev = h
        elif h == prev + 1:
            prev = h
        else:
            out.append(f"{start:02d}:00–{prev + 1:02d}:00")
            start = prev = h
    if start is not None:
        out.append(f"{start:02d}:00–{prev + 1:02d}:00")
    return ", ".join(out)


def fmt_schedule(slots):
    lines = []
    for i, d in enumerate(DAYS_LONG):
        hs = [h for dd, h in slots if dd == i]
        if hs:
            lines.append(f"• {d}: {fmt_ranges(hs)}")
    return "\n".join(lines) if lines else "(henüz saat seçilmedi)"


def heatmap(users, sets):
    n = len(users)
    rows = [f"Soldan sağa {HOUR_START:02d}:00 → {HOUR_END:02d}:00"]
    for d in range(7):
        cells = ""
        for h in HOURS:
            c = sum(1 for s in sets if (d, h) in s)
            if n and c == n:
                cells += "🟩"
            elif n >= 3 and c == n - 1:
                cells += "🟨"
            elif c:
                cells += "🟦"
            else:
                cells += "⬜"
        rows.append(f"<code>{DAYS[d]}</code> {cells}")
    return "\n".join(rows)


def webapp_url(uid, tab="me", mode="kb"):
    users = all_users()
    people = ";".join(f"{quote(n)}:{encode(user_slots(u))}" for u, n in users if u != uid)
    return (f"{WEBAPP_URL}#h0={HOUR_START}&h1={HOUR_END}&bot={BOT_USERNAME}&mode={mode}&tab={tab}"
            f"&name={quote(user_name(uid))}&me={encode(user_slots(uid))}&p={people}")


def private_keyboard(uid):
    """Özel sohbette altta duran 'Takvimi aç' butonu (web app)."""
    if not WEBAPP_URL:
        return None
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📅 Takvimi aç", web_app=WebAppInfo(webapp_url(uid, "me"))),
          KeyboardButton("👥 Ortak saatler", web_app=WebAppInfo(webapp_url(uid, "all")))]],
        resize_keyboard=True, is_persistent=True)


def open_in_private_button(param="takvim", text="📅 Görsel takvimi aç"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, url=f"https://t.me/{BOT_USERNAME}?start={param}")]])


def next_lesson_dt(day, hour, now=None):
    now = now or datetime.now(TZ)
    delta = (day - now.weekday()) % 7
    dt = (now + timedelta(days=delta)).replace(hour=hour, minute=0, second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=7)
    return dt


def week_key(dt):
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


# =============================== Türkçe yazı anlama ===============================
def tr_lower(s):
    return s.replace("I", "ı").replace("İ", "i").lower()


WEEKDAYS, WEEKEND, EVERYDAY = [0, 1, 2, 3, 4], [5, 6], list(range(7))
_DAY_PATTERNS = [
    (r"hafta\s*i[çc]i|haftai[çc]i", WEEKDAYS), (r"hafta\s*sonu|haftasonu", WEEKEND),
    (r"her\s*g[üu]n|herg[üu]n|b[üu]t[üu]n\s*hafta|t[üu]m\s*hafta", EVERYDAY),
    (r"pazartesi|pzt\b|pts\b", [0]), (r"sal[ıi]\b|sal\b", [1]), (r"[çc]ar[şs]amba|[çc]ar\b|[çc]r[şs]\b", [2]),
    (r"per[şs]embe|per\b|pr[şs]\b", [3]), (r"cumartesi|cmt\b|cts\b", [5]), (r"cuma\b|cum\b", [4]),
    (r"pazar\b|paz\b", [6]),
]
DAY_RE = re.compile("|".join(f"(?P<d{i}>{p})" for i, (p, _) in enumerate(_DAY_PATTERNS)))
def _N(name):
    return rf"(?P<{name}>\d{{1,2}})(?:[:.]\d{{2}})?"


TIME_RE = re.compile(
    rf"(?P<from>{_N('fh')}\s*['’]?(?:den|dan|ten|tan)\s*(?:sonra|itibaren))"
    rf"|(?P<until>{_N('uh')}\s*['’]?(?:ye|ya|e|a)?\s*kadar)"
    rf"|(?P<range>{_N('ra')}\s*(?:-|–|—|ile|['’]?(?:den|dan|ten|tan))\s*{_N('rb')})"
    r"|(?P<word>[öo][ğg]leden\s*sonra|sabah|[öo][ğg]len?|ak[şs]am|t[üu]m\s*g[üu]n|b[üu]t[üu]n\s*g[üu]n)"
)
WORD_HOURS = {"sabah": (9, 12), "öğle": (12, 14), "öğleden sonra": (14, 18), "akşam": (18, 24), "tüm gün": (0, 24)}
NEG_RE = re.compile(r"de[ğg]il|olmaz|gelemem|hari[çc]|\bsil\b|kald[ıi]r|iptal|bo[şs]\s*de[ğg]il|dolu")


def _word_key(w):
    w = re.sub(r"\s+", " ", w)
    if w.startswith(("öğleden", "ogleden")):
        return "öğleden sonra"
    if w.startswith("sab"):
        return "sabah"
    if w.startswith(("öğ", "og")):
        return "öğle"
    if w.startswith("ak"):
        return "akşam"
    return "tüm gün"


def parse_tr(text):
    """Metni (mode, {(day,hour)}) olarak çözer. mode: 'add' | 'remove' | None (anlaşılmadı)."""
    t = tr_lower(text)
    mode = "remove" if NEG_RE.search(t) else "add"
    tokens = []
    for m in DAY_RE.finditer(t):
        idx = int(m.lastgroup[1:])
        tokens.append((m.start(), "day", _DAY_PATTERNS[idx][1]))
    for m in TIME_RE.finditer(t):
        kind = m.lastgroup
        if kind == "from":
            a, b = int(m.group("fh")), HOUR_END
        elif kind == "until":
            a, b = HOUR_START, int(m.group("uh"))
        elif kind == "range":
            a, b = int(m.group("ra")), int(m.group("rb"))
            if b <= a:
                b += 12 if b + 12 <= 24 else 0   # "1-3" gibi öğleden sonra kısaltmaları
        else:
            a, b = WORD_HOURS[_word_key(m.group("word"))]
        tokens.append((m.start(), "time", (a, b)))
    if not tokens:
        return None, set()
    tokens.sort()
    result, pending, last_was_time, untimed = set(), [], False, []
    for _, kind, val in tokens:
        if kind == "day":
            if last_was_time:
                pending = []
            pending = sorted(set(pending) | set(val))
            last_was_time = False
        else:
            a, b = val
            hours = [h for h in HOURS if a <= h < b]
            if not pending:          # gün yok: bugünden itibaren değil, hata say
                return None, set()
            result |= {(d, h) for d in pending for h in hours}
            last_was_time = True
    if not last_was_time:            # sona kalan günler: tüm gün
        result |= {(d, h) for d in pending for h in HOURS}
    return mode, result


# =============================== komutlar ===============================
HELP = (
    "🎤 <b>Şan dersi takvim botu</b>\n\n"
    "📅 /musait – görsel takvimi aç, saatlerini boya\n"
    "✍️ Ya da yaz: <i>salı 18-21 müsaitim</i>, <i>hafta içi akşam</i>, <i>cmt 10'dan sonra</i>\n"
    "   Gruptaysan: /ekle salı 18-21  •  /sil çarşamba\n"
    "👥 /ortak – herkesin ortak boş saatleri (renkli tablo)\n"
    "🎓 /ders – ders saatini seç, bot hatırlatır\n"
    "🙋 /gelemem – bu haftaki derse gelemiyorum\n"
    "🗓 /takvim – kendi takvimin  •  /herkes – herkesinki\n"
    "🧹 /temizle – takvimini sıfırla\n\n"
    f"Saatler {HOUR_START:02d}:00–{HOUR_END:02d}:00, her hafta tekrar eder."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u, chat = update.effective_user, update.effective_chat
    register(u, chat)
    arg = ctx.args[0] if ctx.args else ""
    if arg.startswith("s_"):                       # mini app'ten derin link ile kayıt
        slots = decode(arg[2:])
        if slots is None:
            await update.message.reply_text("Kod okunamadı 😕")
            return
        set_slots(u.id, slots)
        await update.message.reply_html(f"✅ Kaydedildi.\n\n{fmt_schedule(slots)}", reply_markup=private_keyboard(u.id))
        await notify_groups(ctx, u)
        return
    if arg in ("takvim", "ortak") and chat.type == ChatType.PRIVATE and WEBAPP_URL:
        tab = "all" if arg == "ortak" else "me"
        await update.message.reply_html(
            "Aşağıdaki butona bas, takvim açılsın 👇" if tab == "me" else "👥 Ortak saatler için butona bas 👇",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📅 Takvimi aç", web_app=WebAppInfo(webapp_url(u.id, tab)))]],
                resize_keyboard=True, one_time_keyboard=True))
        return
    await update.message.reply_html(HELP, reply_markup=private_keyboard(u.id) if chat.type == ChatType.PRIVATE else None)


async def cmd_musait(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u, chat = update.effective_user, update.effective_chat
    register(u, chat)
    if not WEBAPP_URL:
        await update.message.reply_text(
            f"📅 {u.first_name}, hangi günü düzenlemek istiyorsun?\n(ya da yaz: salı 18-21 müsaitim)",
            reply_markup=day_keyboard(u.id))
        return
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("Aşağıdaki butondan takvimi aç 👇", reply_markup=private_keyboard(u.id))
    else:
        await update.message.reply_html(
            f"📅 {u.first_name}, takvimini görsel olarak düzenlemek için butona bas.\n"
            "Ya da buraya yaz: <code>/ekle salı 18-21, cmt sabah</code>",
            reply_markup=open_in_private_button())


async def on_webapp_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    register(u, update.effective_chat)
    data = update.message.web_app_data.data
    slots = decode(data[2:]) if data.startswith("s_") else None
    if slots is None:
        await update.message.reply_text("Veri okunamadı 😕")
        return
    set_slots(u.id, slots)
    await update.message.reply_html(f"✅ Kaydedildi.\n\n{fmt_schedule(slots)}", reply_markup=private_keyboard(u.id))
    await notify_groups(ctx, u)


async def notify_groups(ctx, user):
    """Biri takvimini güncelleyince gruba kısa bilgi ver."""
    users, sets, common = common_slots()
    line = f"🗓 {user.first_name} takvimini güncelledi."
    if common:
        line += f"\n✅ Ortak saatler:\n{fmt_schedule(common)}"
    elif len(users) >= GROUP_SIZE:
        line += "\n❌ Herkesin ortak boş saati hâlâ yok."
    for cid in group_chats():
        try:
            await ctx.bot.send_message(cid, line)
        except Exception as e:  # bot gruptan atılmış olabilir
            log.warning("grup bildirimi gönderilemedi %s: %s", cid, e)


async def apply_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text, force_mode=None, quiet=False):
    u, chat = update.effective_user, update.effective_chat
    mode, slots = parse_tr(text)
    if force_mode:
        mode = force_mode
    if not slots:
        if not quiet:
            await update.message.reply_html(
                "Anlayamadım 😕 Örnekler:\n<code>salı 18-21</code>\n<code>hafta içi akşam</code>\n"
                "<code>cmt 10'dan sonra, paz 14'e kadar</code>\n<code>çarşamba müsait değilim</code>")
        return
    register(u, chat)
    if mode == "remove":
        remove_slots(u.id, slots)
        verb = "silindi ➖"
    else:
        add_slots(u.id, slots)
        verb = "eklendi ➕"
    await update.message.reply_html(
        f"✅ {u.first_name}: {fmt_schedule(slots)}\n<i>{verb}</i>\n\n🗓 Güncel takvimin:\n{fmt_schedule(user_slots(u.id))}",
        reply_markup=private_keyboard(u.id) if chat.type == ChatType.PRIVATE else None)
    if chat.type == ChatType.PRIVATE:
        await notify_groups(ctx, u)


async def cmd_ekle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await apply_text(update, ctx, " ".join(ctx.args), force_mode="add")


async def cmd_sil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await apply_text(update, ctx, " ".join(ctx.args), force_mode="remove")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    text = update.message.text or ""
    is_private = chat.type == ChatType.PRIVATE
    mentioned = f"@{BOT_USERNAME}".lower() in tr_lower(text) or (
        update.message.reply_to_message and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == ctx.bot.id)
    if not is_private and not mentioned:
        mode, slots = parse_tr(text)
        if not slots:
            return                                  # grup sohbetine karışma
    await apply_text(update, ctx, text.replace(f"@{BOT_USERNAME}", ""), quiet=not is_private)


async def cmd_takvim(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    register(u, update.effective_chat)
    await update.message.reply_html(f"🗓 <b>{u.first_name}</b> – müsait saatler:\n{fmt_schedule(user_slots(u.id))}")


async def cmd_herkes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    users = all_users()
    if not users:
        await update.message.reply_text("Henüz kimse kayıt olmadı. Herkes /start yazsın.")
        return
    await update.message.reply_html("\n\n".join(
        f"👤 <b>{name}</b>\n{fmt_schedule(user_slots(uid))}" for uid, name in users))


async def cmd_ortak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    register(update.effective_user, update.effective_chat)
    users, sets, common = common_slots()
    if not users:
        await update.message.reply_text("Henüz kimse kayıt olmadı. Herkes /start yazsın.")
        return
    n = len(users)
    head = f"👥 <b>{n} kişi:</b> " + ", ".join(nm for _, nm in users)
    if n < GROUP_SIZE:
        head += f"\n⚠️ {GROUP_SIZE - n} kişi daha /start yazmalı."
    missing = [nm for (uid, nm), s in zip(users, sets) if not s]
    if missing:
        head += "\n⚠️ Henüz saat seçmeyen: " + ", ".join(missing)
    body = "✅ <b>Herkesin ortak boş saatleri:</b>\n" + fmt_schedule(common) if common \
        else "❌ Herkesin ortak boş olduğu saat yok."
    lesson = get_lesson(update.effective_chat.id)
    tail = f"\n\n🎓 Ders: <b>{DAYS_LONG[lesson[0]]} {lesson[1]:02d}:00</b>" if lesson else "\n\nDers saatini seçmek için /ders"
    kb = open_in_private_button("ortak", "🗓 Tabloyu görsel aç") if WEBAPP_URL and update.effective_chat.type != ChatType.PRIVATE else None
    await update.message.reply_html(f"{head}\n\n{heatmap(users, sets)}\n🟩 herkes  🟨 biri hariç  🟦 az kişi\n\n{body}{tail}", reply_markup=kb)


async def cmd_temizle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_slots(update.effective_user.id, set())
    await update.message.reply_text("🧹 Takvimin sıfırlandı.",
                                    reply_markup=private_keyboard(update.effective_user.id)
                                    if update.effective_chat.type == ChatType.PRIVATE else None)


# ------------------------------- ders & hatırlatma -------------------------------
async def cmd_ders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, u = update.effective_chat, update.effective_user
    register(u, chat)
    args = " ".join(ctx.args)
    if args.strip() in ("iptal", "sil", "kaldır"):
        del_lesson(chat.id)
        await update.message.reply_text("🎓 Ders saati kaldırıldı.")
        return
    if args:
        mode, slots = parse_tr(args)
        if len({d for d, _ in slots}) == 1 and slots:
            d = next(iter(slots))[0]
            h = min(h for _, h in slots)
            await confirm_lesson(update.message, ctx, chat.id, d, h, u.first_name)
            return
        await update.message.reply_text("Örnek: /ders salı 19")
        return
    users, sets, common = common_slots()
    if not common:
        await update.message.reply_text(
            "Herkesin ortak boş saati yok; önce takvimler dolsun (/ortak).\n"
            "Yine de elle seçmek istersen: /ders salı 19")
        return
    rows, row = [], []
    for d, h in sorted(common)[:40]:
        row.append(InlineKeyboardButton(f"{DAYS[d]} {h:02d}:00", callback_data=f"L:{d}:{h}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await update.message.reply_text("🎓 Ders hangi saatte olsun? (herkesin boş olduğu saatler)",
                                    reply_markup=InlineKeyboardMarkup(rows))


async def confirm_lesson(message, ctx, chat_id, d, h, by):
    set_lesson(chat_id, d, h, by)
    nxt = next_lesson_dt(d, h)
    text = (f"🎓 <b>Ders saati belirlendi: her {DAYS_LONG[d]} {h:02d}:00</b>\n"
            f"İlk ders: {nxt.strftime('%d.%m.%Y')} ({by} seçti)\n"
            "⏰ Bir gün önce akşam ve dersten 1 saat önce hatırlatırım.\n"
            "Gelemeyeceksen /gelemem yaz.")
    sent = await message.reply_html(text)
    try:
        await ctx.bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
    except Exception:
        pass


async def on_lesson_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, d, h = q.data.split(":")
    await q.answer()
    await q.edit_message_text(f"Seçildi: {DAYS_LONG[int(d)]} {int(h):02d}:00")
    await confirm_lesson(q.message, ctx, q.message.chat_id, int(d), int(h), q.from_user.first_name)


async def cmd_gelemem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, u = update.effective_chat, update.effective_user
    lesson = get_lesson(chat.id)
    if not lesson:
        await update.message.reply_text("Henüz ders saati yok. /ders ile seçin.")
        return
    nxt = next_lesson_dt(lesson[0], lesson[1])
    with db() as con:
        con.execute("INSERT OR IGNORE INTO absent VALUES (?,?,?)", (chat.id, u.id, week_key(nxt)))
    await update.message.reply_text(
        f"🙋 {u.first_name} {nxt.strftime('%d.%m')} {lesson[1]:02d}:00 dersine gelemeyecek. "
        "Fikrin değişirse /geliyorum")


async def cmd_geliyorum(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, u = update.effective_chat, update.effective_user
    lesson = get_lesson(chat.id)
    if not lesson:
        return
    nxt = next_lesson_dt(lesson[0], lesson[1])
    with db() as con:
        con.execute("DELETE FROM absent WHERE chat_id=? AND user_id=? AND week=?", (chat.id, u.id, week_key(nxt)))
    await update.message.reply_text(f"👍 {u.first_name} derse geliyor.")


async def job_evening(ctx: ContextTypes.DEFAULT_TYPE):
    """Her akşam 20:00: yarın ders varsa hatırlat."""
    now = datetime.now(TZ)
    for cid in group_chats():
        lesson = get_lesson(cid)
        if not lesson:
            continue
        nxt = next_lesson_dt(lesson[0], lesson[1], now)
        if nxt.date() == (now + timedelta(days=1)).date():
            ab = absentees(cid, week_key(nxt))
            msg = f"⏰ Yarın ders var: <b>{DAYS_LONG[lesson[0]]} {lesson[1]:02d}:00</b> 🎤"
            msg += ("\n🙋 Gelemeyecek: " + ", ".join(ab)) if ab else "\nGelemeyecek varsa /gelemem"
            try:
                await ctx.bot.send_message(cid, msg, parse_mode=ParseMode.HTML)
            except Exception as e:
                log.warning("hatırlatma gönderilemedi: %s", e)


async def job_hourly(ctx: ContextTypes.DEFAULT_TYPE):
    """5 dakikada bir: derse 55-60 dk kaldıysa hatırlat."""
    now = datetime.now(TZ)
    for cid in group_chats():
        lesson = get_lesson(cid)
        if not lesson:
            continue
        mins = (next_lesson_dt(lesson[0], lesson[1], now) - now).total_seconds() / 60
        if 55 < mins <= 60:
            try:
                await ctx.bot.send_message(cid, f"🎤 1 saat sonra ders! ({lesson[1]:02d}:00)")
            except Exception as e:
                log.warning("hatırlatma gönderilemedi: %s", e)


# ------------------------------- buton tabanlı yedek arayüz -------------------------------
def day_keyboard(uid):
    slots = user_slots(uid)
    rows, row = [], []
    for i, d in enumerate(DAYS):
        n = sum(1 for dd, _ in slots if dd == i)
        row.append(InlineKeyboardButton(f"{d} ({n})" if n else d, callback_data=f"d:{uid}:{i}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✅ Bitti", callback_data=f"x:{uid}")])
    return InlineKeyboardMarkup(rows)


def hour_keyboard(uid, day):
    slots = user_slots(uid)
    rows, row = [], []
    for h in HOURS:
        mark = "🟢" if (day, h) in slots else "⚪"
        row.append(InlineKeyboardButton(f"{mark} {h:02d}:00", callback_data=f"t:{uid}:{day}:{h}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🧹 Günü temizle", callback_data=f"c:{uid}:{day}"),
                 InlineKeyboardButton("◀ Günler", callback_data=f"b:{uid}")])
    return InlineKeyboardMarkup(rows)


async def cmd_butonlar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    register(u, update.effective_chat)
    await update.message.reply_text(f"📅 {u.first_name}, hangi günü düzenlemek istiyorsun?", reply_markup=day_keyboard(u.id))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    kind, owner = parts[0], int(parts[1])
    me = q.from_user
    if me.id != owner:
        await q.answer("Bu takvim senin değil 🙂 /musait yaz.", show_alert=True)
        return
    if kind == "d":
        day = int(parts[2])
        await q.answer()
        await q.edit_message_text(f"🕘 {me.first_name} – {DAYS_LONG[day]}\nMüsait saatlere bas (🟢 = müsait):",
                                  reply_markup=hour_keyboard(owner, day))
    elif kind == "t":
        day, hour = int(parts[2]), int(parts[3])
        (remove_slots if (day, hour) in user_slots(owner) else add_slots)(owner, {(day, hour)})
        await q.answer()
        await q.edit_message_reply_markup(reply_markup=hour_keyboard(owner, day))
    elif kind == "c":
        day = int(parts[2])
        remove_slots(owner, {(day, h) for h in HOURS})
        await q.answer("Gün temizlendi")
        await q.edit_message_reply_markup(reply_markup=hour_keyboard(owner, day))
    elif kind == "b":
        await q.answer()
        await q.edit_message_text(f"📅 {me.first_name}, hangi günü düzenlemek istiyorsun?", reply_markup=day_keyboard(owner))
    elif kind == "x":
        await q.answer("Kaydedildi ✅")
        await q.edit_message_text(f"🗓 {me.first_name} – müsait saatler:\n{fmt_schedule(user_slots(owner))}\n\nOrtak saatler için /ortak")


# =============================== başlat ===============================
async def post_init(app: Application):
    global BOT_USERNAME
    BOT_USERNAME = (await app.bot.get_me()).username
    await app.bot.set_my_commands([
        ("musait", "Takvimini düzenle"), ("ortak", "Herkesin ortak boş saatleri"),
        ("ders", "Ders saatini seç"), ("gelemem", "Bu hafta derse gelemiyorum"),
        ("ekle", "Yazıyla ekle: /ekle salı 18-21"), ("sil", "Yazıyla sil: /sil çarşamba"),
        ("takvim", "Kendi takvimin"), ("herkes", "Herkesin takvimi"),
        ("temizle", "Takvimini sıfırla"), ("start", "Yardım"),
    ])
    log.info("Bot hazır: @%s  webapp=%s", BOT_USERNAME, WEBAPP_URL or "(yok)")


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    for name, fn in [("start", cmd_start), ("musait", cmd_musait), ("takvim", cmd_takvim), ("herkes", cmd_herkes),
                     ("ortak", cmd_ortak), ("temizle", cmd_temizle), ("ekle", cmd_ekle), ("sil", cmd_sil),
                     ("ders", cmd_ders), ("gelemem", cmd_gelemem), ("geliyorum", cmd_geliyorum),
                     ("butonlar", cmd_butonlar)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_lesson_pick, pattern=r"^L:"))
    app.add_handler(CallbackQueryHandler(on_button))
    app.job_queue.run_daily(job_evening, time=dtime(20, 0, tzinfo=TZ))
    app.job_queue.run_repeating(job_hourly, interval=300, first=10)
    log.info("Bot başlıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
