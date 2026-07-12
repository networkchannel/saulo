#!/usr/bin/env python3
"""
Bot passerelle Telegram — Anti-bot adaptatif
- Trust score (âge du compte, username, photo, premium, langue…)
- UN SEUL défi, dont la DIFFICULTÉ dépend du score
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
INVITE_TTL = 3600
MIN_HUMAN_MS = 700
PURGE_INTERVAL = 300
SESSION_TTL = 7200

BAN_TIERS = [30 * 60, 2 * 3600, 8 * 3600, 7 * 86400, 30 * 86400, None]

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
    ("🐝", "abeille"), ("🌻", "tournesol"), ("🍇", "raisin"), ("🖐️", "main"),
]

logging.basicConfig(format="%(asctime)s | %(levelname)-7s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("gateway")
STARTED_AT = time.time()


async def trust_score(user, bot) -> tuple[int, list[str]]:
    score, why = 65, []          # base remontée 50 → 65

    year = 2026
    for bound, y in ID_EPOCHS:
        if user.id < bound:
            year = y
            break
    age = 2026 - year
    if age >= 6:
        score += 25; why.append(f"compte ~{year} (+25)")
    elif age >= 4:
        score += 20; why.append(f"compte ~{year} (+20)")
    elif age >= 2:
        score += 15; why.append(f"compte ~{year} (+15)")
    elif age >= 1:
        score += 8;  why.append(f"compte ~{year} (+8)")
    else:
        score -= 12; why.append(f"compte neuf ~{year} (-12)")

    u = user.username
    if not u:
        score -= 6; why.append("aucun username (-6)")
    else:
        digits = sum(c.isdigit() for c in u)
        if digits >= 6:
            score -= 8; why.append("username très numérique (-8)")
        elif digits >= 4:
            score -= 3; why.append("username numérique (-3)")
        else:
            score += 10; why.append("username propre (+10)")
        if any(k in u.lower() for k in ("spam", "crypto", "airdrop", "freegift", "xxx")):
            score -= 25; why.append("username suspect (-25)")

    fn = (user.first_name or "").strip()
    if len(fn) < 2:
        score -= 5; why.append("prénom vide (-5)")
    if sum(c.isdigit() for c in fn) >= 4:
        score -= 6; why.append("prénom numérique (-6)")

    if getattr(user, "is_premium", False):
        score += 25; why.append("premium (+25)")

    try:
        photos = await bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count > 0:
            score += 15; why.append("photo (+15)")
        else:
            score -= 4; why.append("pas de photo (-4)")
    except TelegramError:
        pass

    if not user.language_code:
        score -= 4; why.append("pas de langue (-4)")

    return max(0, min(100, score)), why


def difficulty_for(score: int) -> int:
    """1 = facile … 4 = brutal"""
    if score >= 65:
        return 1
    if score >= 45:
        return 2
    if score >= 25:
        return 3
    return 4


# ═══════════════════════ STORE MÉMOIRE ═══════════════════════
@dataclass
class Session:
    uid: int
    fails: int = 0
    score: int = 50
    diff: int = 3
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


# ═══════════════════════ LE DÉFI UNIQUE ═══════════════════════
class Challenge:
    """
    Un seul défi. Sa difficulté (1-4) pilote :
      - le type d'énigme
      - le nombre de leurres
      - le temps imparti
    """

    TIMEOUT = {1: 120, 2: 100, 3: 80, 4: 60}

    def __init__(self, diff: int):
        self.diff = diff
        self.nonce = secrets.token_urlsafe(8)
        self.shown_at = time.time()
        self.timeout = self.TIMEOUT[diff]

        if diff == 1:
            self._easy()
        elif diff == 2:
            self._medium()
        elif diff == 3:
            self._hard()
        else:
            self._brutal()

    # ── Niveau 1 : trouver un émoji dans une grille 2×3
    def _easy(self):
        picks = random.sample(EMOJI_POOL, 6)
        self.answer, name = random.choice(picks)
        random.shuffle(picks)
        self.grid = picks
        self.cols = 3
        self.prompt = f"Appuie sur : <b>{name.upper()}</b>"

    # ── Niveau 2 : compter les occurrences d'un émoji
    def _medium(self):
        self.emoji = random.choice(EMOJI_POOL)[0]
        n = random.randint(2, 5)
        noise = [e[0] for e in random.sample(EMOJI_POOL, 10) if e[0] != self.emoji][:6]
        seq = [self.emoji] * n + noise
        random.shuffle(seq)
        self.answer = str(n)
        self.opts = self._numeric_opts(n, 4)
        self.prompt = (
            f"Combien de fois vois-tu <b>{self.emoji}</b> ?\n\n"
            f"<blockquote>{'  '.join(seq)}</blockquote>"
        )

    # ── Niveau 3 : grille 4×4 + double condition (émoji ET position)
    def _hard(self):
        picks = random.sample(EMOJI_POOL, 16)
        target, name = random.choice(picks)
        random.shuffle(picks)
        self.grid = picks
        self.cols = 4
        self.answer = target
        row = picks.index(next(p for p in picks if p[0] == target)) // 4 + 1
        col = picks.index(next(p for p in picks if p[0] == target)) % 4 + 1
        self.prompt = (
            f"Dans la grille, trouve la <b>{name.upper()}</b> "
            f"et appuie dessus.\n\n"
            f"<i>Indice : ligne {row}, colonne {col}</i>"
        )

    # ── Niveau 4 : calcul enchaîné avec traduction émoji→valeur
    def _brutal(self):
        e1, e2 = random.sample([e[0] for e in EMOJI_POOL], 2)
        v1, v2 = random.randint(2, 9), random.randint(2, 9)
        op = random.choice(["+", "×", "-"])
        res = {"+": v1 + v2, "×": v1 * v2, "-": v1 - v2}[op]
        self.answer = str(res)
        self.opts = self._numeric_opts(res, 6)
        self.prompt = (
            "<b>Décode puis calcule.</b>\n\n"
            f"<code>{e1} = {v1}</code>\n"
            f"<code>{e2} = {v2}</code>\n\n"
            f"Combien font <code>{e1} {op} {e2}</code> ?"
        )

    @staticmethod
    def _numeric_opts(real: int, count: int) -> list[int]:
        opts = {real}
        while len(opts) < count:
            opts.add(real + random.choice([-8, -5, -3, -2, -1, 1, 2, 3, 5, 8]))
        opts = list(opts)
        random.shuffle(opts)
        return opts

    def text(self) -> str:
        stars = "🔴" * self.diff + "⚪" * (4 - self.diff)
        return (
            f"<b>Vérification</b>  {stars}\n"
            f"<i>Difficulté {self.diff}/4 · {self.timeout}s</i>\n\n"
            f"{self.prompt}"
        )

    def keyboard(self) -> InlineKeyboardMarkup:
        n = self.nonce
        if self.diff in (1, 3):
            rows, row = [], []
            for emo, _ in self.grid:
                row.append(InlineKeyboardButton(emo, callback_data=f"x:{n}:{emo}"))
                if len(row) == self.cols:
                    rows.append(row); row = []
            if row:
                rows.append(row)
            return InlineKeyboardMarkup(rows)

        rows, row = [], []
        for o in self.opts:
            row.append(InlineKeyboardButton(f" {o} ", callback_data=f"x:{n}:{o}"))
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(rows)


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
    diff = difficulty_for(score)
    log.info("uid=%s score=%s diff=%s | %s", uid, score, diff, " ; ".join(why))

    s = DB.session(uid)
    s.score, s.diff, s.fails = score, diff, 0

    name = f"@{user.username}" if user.username else (user.first_name or "toi")
    await update.message.reply_text(
        f"Bienvenu <b>{name}</b>,\n\n"
        f"pour rejoindre le <b>shop de Saul</b> il faut que tu accomplisses "
        f"une petite vérification.",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(1)
    await send_challenge(chat, uid, ctx)


async def send_challenge(chat: int, uid: int, ctx):
    s = DB.session(uid)
    c = Challenge(s.diff)
    s.challenge = c
    await ctx.bot.send_message(
        chat, c.text(), parse_mode=ParseMode.HTML, reply_markup=c.keyboard()
    )
    ctx.job_queue.run_once(
        timeout_job, c.timeout,
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

    await ctx.bot.send_message(
        chat,
        f"❌ <i>{reason}</i> — il te reste <b>{MAX_FAILS - s.fails}</b> essai(s).",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(1.2)
    await send_challenge(chat, uid, ctx)


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


# ═══════════════════════ MAIN (compatible 3.14) ═══════════════════════
async def run():
    app = (Application.builder()
           .token(BOT_TOKEN)
           .concurrent_updates(True)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^x:"))
    app.add_handler(ChatMemberHandler(on_join, ChatMemberHandler.CHAT_MEMBER))
    app.add_error_handler(on_error)

    await start_web()

    async with app:
        app.job_queue.run_repeating(purge_job, interval=PURGE_INTERVAL, first=PURGE_INTERVAL)
        await app.start()
        await app.updater.start_polling(
            allowed_updates=["message", "callback_query", "chat_member"],
            drop_pending_updates=True,
        )
        log.info("Bot ON")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
