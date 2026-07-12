#!/usr/bin/env python3
"""
Bot passerelle Telegram — Anti-bot adaptatif
- Trust score (âge du compte, username, photo, premium, langue…)
- Nombre de défis adapté : 1 / 2 / 3 / 4 selon le score
- Store 100% mémoire, auto-purge
- Invite usage unique, 1h, révocation post-join
- Ban progressif : 30m → 2h → 8h → 7j → 30j → définitif
- Serveur HTTP "bot on" pour Render
"""

import asyncio
import logging
import os
import random
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler,
    CommandHandler, ContextTypes,
)

# ═══════════════════════ CONFIG ═══════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", ""))
PORT = int(os.getenv("PORT", "10000"))

MAX_FAILS = 3
STEP_TTL = 90
INVITE_TTL = 3600
MIN_HUMAN_MS = 700
PURGE_INTERVAL = 300
SESSION_TTL = 7200

BAN_TIERS = [30 * 60, 2 * 3600, 8 * 3600, 7 * 86400, 30 * 86400, None]

# Bornes d'ID Telegram → estimation de l'âge du compte
ID_EPOCHS = [
    (100_000_000, 2013), (300_000_000, 2016), (600_000_000, 2018),
    (1_000_000_000, 2019), (1_500_000_000, 2020), (2_000_000_000, 2021),
    (5_000_000_000, 2022), (6_000_000_000, 2023), (7_000_000_000, 2024),
    (8_000_000_000, 2025), (9_000_000_000, 2026),
]

EMOJI_POOL = [
    ("🍎", "pomme"), ("🚗", "voiture"), ("🐶", "chien"), ("⚽", "ballon"),
    ("🌙", "lune"), ("🔑", "clé"), ("🎸", "guitare"), ("🍕", "pizza"),
    ("✈️", "avion"), ("🐱", "chat"), ("☂️", "parapluie"), ("🕰️", "horloge"),
    ("🌵", "cactus"), ("🎈", "ballon gonflable"), ("🐟", "poisson"),
    ("🍄", "champignon"), ("🔥", "feu"), ("💡", "ampoule"),
    ("🚲", "vélo"), ("🎩", "chapeau"), ("🥕", "carotte"), ("⚓", "ancre"),
]

logging.basicConfig(format="%(asctime)s | %(levelname)-7s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("gateway")
STARTED_AT = time.time()


# ═══════════════════════ TRUST SCORE ═══════════════════════
async def trust_score(user, bot) -> tuple[int, list[str]]:
    """
    Score 0-100. Plus c'est haut, plus le compte semble légitime.
    Retourne (score, raisons) — les raisons servent au log admin, pas à l'user.
    """
    score = 50
    why = []

    # ── Âge estimé du compte (le plus gros signal)
    year = 2026
    for bound, y in ID_EPOCHS:
        if user.id < bound:
            year = y
            break
    age = 2026 - year
    if age >= 6:
        score += 25; why.append(f"compte ~{year} (+25)")
    elif age >= 4:
        score += 18; why.append(f"compte ~{year} (+18)")
    elif age >= 2:
        score += 10; why.append(f"compte ~{year} (+10)")
    elif age >= 1:
        score += 2; why.append(f"compte ~{year} (+2)")
    else:
        score -= 20; why.append(f"compte neuf ~{year} (-20)")

    # ── Username
    u = user.username
    if not u:
        score -= 15; why.append("aucun username (-15)")
    else:
        digits = sum(c.isdigit() for c in u)
        if digits >= 5:
            score -= 15; why.append("username très numérique (-15)")
        elif digits >= 3:
            score -= 7; why.append("username numérique (-7)")
        else:
            score += 8; why.append("username propre (+8)")
        if any(k in u.lower() for k in ("bot", "spam", "free", "crypto", "airdrop", "xxx")):
            score -= 20; why.append("username suspect (-20)")
        if len(u) < 5:
            score -= 5; why.append("username très court (-5)")

    # ── Prénom
    fn = (user.first_name or "").strip()
    if len(fn) < 2:
        score -= 10; why.append("prénom vide (-10)")
    if sum(c.isdigit() for c in fn) >= 3:
        score -= 10; why.append("prénom numérique (-10)")

    # ── Telegram Premium (payant → très rarement un bot)
    if getattr(user, "is_premium", False):
        score += 20; why.append("premium (+20)")

    # ── Photo de profil
    try:
        photos = await bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count > 0:
            score += 12; why.append("photo de profil (+12)")
        else:
            score -= 10; why.append("pas de photo (-10)")
    except TelegramError:
        pass

    # ── Langue déclarée
    if not user.language_code:
        score -= 8; why.append("pas de langue (-8)")

    return max(0, min(100, score)), why


def challenges_for(score: int) -> int:
    """Nombre de défis selon le score de confiance."""
    if score >= 80:
        return 1
    if score >= 60:
        return 2
    if score >= 35:
        return 3
    return 4


# ═══════════════════════ STORE MÉMOIRE ═══════════════════════
@dataclass
class Session:
    uid: int
    fails: int = 0
    score: int = 50
    total: int = 3
    idx: int = 0
    touched: float = field(default_factory=time.time)
    challenge: "Challenge | None" = None

    def touch(self):
        self.touched = time.time()


@dataclass
class Sanction:
    level: int = 0
    until: float = 0.0  # -1 = définitif


class MemoryStore:
    def __init__(self):
        self.sessions: dict[int, Session] = {}
        self.sanctions: dict[int, Sanction] = {}
        self.invites: dict[str, dict] = {}
        self.stats = {"solved": 0, "failed": 0, "banned": 0, "issued": 0}

    def session(self, uid) -> Session:
        s = self.sessions.get(uid) or Session(uid=uid)
        self.sessions[uid] = s
        s.touch()
        return s

    def drop(self, uid):
        self.sessions.pop(uid, None)

    def blocked(self, uid):
        s = self.sanctions.get(uid)
        if not s:
            return False, 0
        if s.until == -1:
            return True, None
        if s.until > time.time():
            return True, int(s.until - time.time())
        del self.sanctions[uid]
        return False, 0

    def punish(self, uid) -> str:
        s = self.sanctions.setdefault(uid, Sanction())
        lvl = min(s.level, len(BAN_TIERS) - 1)
        dur = BAN_TIERS[lvl]
        s.until = -1 if dur is None else time.time() + dur
        s.level = min(lvl + 1, len(BAN_TIERS) - 1)
        self.drop(uid)
        self.stats["banned"] += 1
        return "définitif" if dur is None else human(dur)

    def add_invite(self, link, uid, exp):
        self.invites[link] = {"uid": uid, "exp": exp}
        self.stats["issued"] += 1

    def consume(self, link):
        return self.invites.pop(link, None)

    def purge(self):
        now = time.time()
        a, b, c = len(self.sessions), len(self.sanctions), len(self.invites)
        self.sessions = {k: v for k, v in self.sessions.items() if now - v.touched < SESSION_TTL}
        self.sanctions = {k: v for k, v in self.sanctions.items() if v.until == -1 or v.until > now}
        self.invites = {k: v for k, v in self.invites.items() if v["exp"] > now}
        return a - len(self.sessions), b - len(self.sanctions), c - len(self.invites)


DB = MemoryStore()


def human(sec) -> str:
    sec = int(sec)
    for d, u in ((86400, "j"), (3600, "h"), (60, "min")):
        if sec >= d:
            return f"{sec // d} {u}"
    return f"{sec}s"


# ═══════════════════════ DÉFIS ═══════════════════════
class Challenge:
    """kind ∈ {grid, count, lock, math}"""

    def __init__(self, kind: str):
        self.kind = kind
        self.nonce = secrets.token_urlsafe(8)
        self.shown_at = time.time()

        if kind == "grid":
            picks = random.sample(EMOJI_POOL, 9)
            self.answer, self.name = random.choice(picks)
            self.grid = picks[:]
            random.shuffle(self.grid)

        elif kind == "count":
            self.emoji = random.choice(EMOJI_POOL)[0]
            self.n = random.randint(2, 5)
            noise = [e[0] for e in random.sample(EMOJI_POOL, 8) if e[0] != self.emoji][:5]
            seq = [self.emoji] * self.n + noise
            random.shuffle(seq)
            self.sequence = "  ".join(seq)
            self.answer = str(self.n)

        elif kind == "math":
            a, b = random.randint(3, 12), random.randint(2, 9)
            op = random.choice(["+", "-", "×"])
            self.q = f"{a} {op} {b}"
            self.answer = str({"+": a + b, "-": a - b, "×": a * b}[op])

        else:  # lock
            self.answer = "🔓"

    def text(self, i: int, total: int) -> str:
        head = f"<b>Défi {i}/{total}</b>\n\n"
        if self.kind == "grid":
            return head + f"Sélectionne : <b>{self.name.upper()}</b>"
        if self.kind == "count":
            return head + (f"Combien de <b>{self.emoji}</b> ?\n\n"
                           f"<blockquote>{self.sequence}</blockquote>")
        if self.kind == "math":
            return head + f"Combien font <code>{self.q}</code> ?"
        return head + "Appuie sur le <b>cadenas ouvert</b>."

    def keyboard(self) -> InlineKeyboardMarkup:
        n = self.nonce
        if self.kind == "grid":
            rows, row = [], []
            for emo, _ in self.grid:
                row.append(InlineKeyboardButton(emo, callback_data=f"x:{n}:{emo}"))
                if len(row) == 3:
                    rows.append(row); row = []
            return InlineKeyboardMarkup(rows)

        if self.kind in ("count", "math"):
            real = int(self.answer)
            opts = {real}
            while len(opts) < 4:
                opts.add(max(0, real + random.choice([-5, -3, -2, -1, 1, 2, 3, 5])))
            opts = list(opts); random.shuffle(opts)
            return InlineKeyboardMarkup([[
                InlineKeyboardButton(f" {o} ", callback_data=f"x:{n}:{o}") for o in opts
            ]])

        slots = ["🔒", "🔓", "🔐", "🗝️"]
        random.shuffle(slots)
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(s, callback_data=f"x:{n}:{s}") for s in slots
        ]])


def pick_kinds(total: int) -> list[str]:
    pool = ["grid", "count", "math", "lock"]
    random.shuffle(pool)
    return pool[:total]


# ═══════════════════════ HANDLERS ═══════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid, chat = user.id, update.effective_chat.id

    blocked, left = DB.blocked(uid)
    if blocked:
        await update.message.reply_text(
            "⛔ <b>Accès définitivement refusé.</b>" if left is None
            else f"⛔ <b>Accès bloqué.</b>\nRéessaie dans <b>{human(left)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    score, why = await trust_score(user, ctx.bot)
    total = challenges_for(score)
    log.info("uid=%s score=%s défis=%s | %s", uid, score, total, " ; ".join(why))

    s = DB.session(uid)
    s.score, s.total, s.idx, s.fails = score, total, 0, 0

    name = user.username and f"@{user.username}" or (user.first_name or "toi")
    await update.message.reply_text(
        f"Bienvenu <b>{name}</b>,\n\n"
        f"pour rejoindre le <b>shop de Saul</b> il faut que tu accomplisses "
        f"une petite vérification.",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(1)

    s.kinds = pick_kinds(total)
    await next_challenge(chat, uid, ctx)


async def next_challenge(chat: int, uid: int, ctx):
    s = DB.session(uid)
    s.idx += 1
    c = Challenge(s.kinds[s.idx - 1])
    s.challenge = c
    await ctx.bot.send_message(
        chat, c.text(s.idx, s.total), parse_mode=ParseMode.HTML, reply_markup=c.keyboard()
    )
    ctx.job_queue.run_once(
        timeout_job, STEP_TTL,
        data={"uid": uid, "chat": chat, "nonce": c.nonce}, name=f"to:{uid}:{c.nonce}",
    )


async def timeout_job(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    s = DB.sessions.get(d["uid"])
    if not s or not s.challenge or s.challenge.nonce != d["nonce"]:
        return
    s.challenge = None
    await fail(d["uid"], d["chat"], ctx, "temps écoulé")


async def fail(uid: int, chat: int, ctx, reason: str):
    DB.stats["failed"] += 1
    s = DB.session(uid)
    s.fails += 1
    s.challenge = None

    if s.fails >= MAX_FAILS:
        label = DB.punish(uid)
        await ctx.bot.send_message(
            chat,
            f"⛔ <b>Trop d'erreurs.</b>\n\n"
            f"Blocage : <b>{label}</b>\n"
            f"<i>Chaque récidive allonge la sanction.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    s.idx = max(0, s.idx - 1)  # on rejoue le même palier
    await ctx.bot.send_message(
        chat,
        f"❌ <i>{reason}</i> — il te reste <b>{MAX_FAILS - s.fails}</b> essai(s).",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(1.2)
    s.kinds[s.idx] = random.choice(["grid", "count", "math", "lock"])
    await next_challenge(chat, uid, ctx)


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid, chat = q.from_user.id, q.message.chat_id

    blocked, _ = DB.blocked(uid)
    if blocked:
        await q.answer("⛔ Accès bloqué.", show_alert=True); return

    try:
        _, nonce, val = q.data.split(":", 2)
    except ValueError:
        await q.answer(); return

    s = DB.sessions.get(uid)
    c = s.challenge if s else None
    if not c or c.nonce != nonce:
        await q.answer("⚠️ Expiré. Relance /start.", show_alert=True); return
    s.touch()

    if (time.time() - c.shown_at) * 1000 < MIN_HUMAN_MS:
        await q.answer("🤖 Détecté.", show_alert=True)
        await fail(uid, chat, ctx, "réaction non-humaine"); return

    if val != c.answer:
        await q.answer("❌", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        await fail(uid, chat, ctx, "mauvaise réponse"); return

    await q.answer("✅")
    for j in ctx.job_queue.get_jobs_by_name(f"to:{uid}:{c.nonce}"):
        j.schedule_removal()
    s.challenge = None

    if s.idx < s.total:
        await q.edit_message_text(
            f"✅ <b>Défi {s.idx}/{s.total} validé.</b>", parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(0.8)
        await next_challenge(chat, uid, ctx)
    else:
        await q.edit_message_text("✅ <b>Vérification réussie.</b>", parse_mode=ParseMode.HTML)
        DB.stats["solved"] += 1
        DB.drop(uid)
        await asyncio.sleep(0.8)
        await issue_invite(chat, uid, ctx)


async def issue_invite(chat: int, uid: int, ctx):
    exp = datetime.now(timezone.utc) + timedelta(seconds=INVITE_TTL)
    try:
        link = await ctx.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID, name=f"u{uid}"[:32], expire_date=exp, member_limit=1
        )
    except TelegramError as e:
        log.error("invite fail uid=%s: %s", uid, e)
        await ctx.bot.send_message(
            chat, "⚠️ Erreur : le bot doit être admin du canal avec le droit d'inviter."
        )
        return

    DB.add_invite(link.invite_link, uid, exp.timestamp())
    await ctx.bot.send_message(
        chat,
        f"Voici ton lien : {link.invite_link}\n\n"
        f"<i>Usage unique • expire dans 1h</i>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    ctx.job_queue.run_once(
        revoke_job, INVITE_TTL + 5,
        data={"link": link.invite_link}, name=f"rev:{secrets.token_hex(4)}",
    )


async def revoke_job(ctx: ContextTypes.DEFAULT_TYPE):
    link = ctx.job.data["link"]
    DB.consume(link)
    try:
        await ctx.bot.revoke_chat_invite_link(CHANNEL_ID, link)
    except TelegramError:
        pass


async def on_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if cmu.chat.id != CHANNEL_ID or not cmu.invite_link:
        return
    if cmu.new_chat_member.status not in ("member", "administrator"):
        return

    link = cmu.invite_link.invite_link
    rec = DB.consume(link)
    if not rec:
        return

    uid = cmu.new_chat_member.user.id
    if rec["uid"] != uid:
        log.warning("HIJACK uid=%s lien de %s", uid, rec["uid"])
        try:
            await ctx.bot.ban_chat_member(CHANNEL_ID, uid)
        except TelegramError:
            pass
    else:
        log.info("JOIN OK uid=%s", uid)

    try:
        await ctx.bot.revoke_chat_invite_link(CHANNEL_ID, link)
    except TelegramError:
        pass


async def purge_job(ctx):
    a, b, c = DB.purge()
    if a or b or c:
        log.info("PURGE -%s sessions -%s bans -%s invites", a, b, c)


async def on_error(update, ctx):
    log.error("Exception", exc_info=ctx.error)


# ═══════════════════════ SERVEUR RENDER ═══════════════════════
async def h_root(_):
    return web.Response(text="bot on", content_type="text/plain")


async def h_health(_):
    return web.json_response({
        "status": "bot on",
        "uptime_s": int(time.time() - STARTED_AT),
        "memory": {
            "sessions": len(DB.sessions),
            "sanctions": len(DB.sanctions),
            "invites": len(DB.invites),
        },
        "stats": DB.stats,
    })


async def start_web():
    app = web.Application()
    app.add_routes([web.get("/", h_root), web.get("/ping", h_root), web.get("/health", h_health)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("HTTP « bot on » sur :%s", PORT)


async def post_init(app: Application):
    await start_web()
    app.job_queue.run_repeating(purge_job, interval=PURGE_INTERVAL, first=PURGE_INTERVAL)


def main():
    app = (Application.builder().token(BOT_TOKEN)
           .concurrent_updates(True).post_init(post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^x:"))
    app.add_handler(ChatMemberHandler(on_join, ChatMemberHandler.CHAT_MEMBER))
    app.add_error_handler(on_error)
    log.info("Bot ON")
    app.run_polling(
        allowed_updates=["message", "callback_query", "chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()