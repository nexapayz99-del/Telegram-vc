import asyncio
import html
import logging
import os
import sys
import time

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatType, ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
                            BotCommandScopeChat, CallbackQuery, Message)
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as M

from . import actions, db, login, ui
from .config import API_HASH, API_ID, BOT_TOKEN, OWNER_ID
from .runtime import AccountInvalid, ROLE_NAME, drop_runtime, get_runtime, shutdown_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# Dedicated event loop created BEFORE any Pyrogram client (works on every Python 3.9+).
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

bot = Client("audio_forwarder_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             in_memory=True, parse_mode=ParseMode.HTML)
actions.BOT = bot

PROMPTS = {}  # uid -> {"kind": "src"|"tgt", "ts": float}


# ------------------------------------------------------------------ access control
async def check(user):
    """Returns (status, doc). status: ok | banned | noaccess"""
    doc = await db.ensure_user(user)
    if user.id == OWNER_ID:
        if doc.get("banned") or not doc.get("access"):
            await db.set_flags(user.id, banned=False, access=True)
            doc["banned"], doc["access"] = False, True
        return "ok", doc
    if doc.get("banned"):
        return "banned", doc
    if not doc.get("access"):
        return "noaccess", doc
    return "ok", doc


async def deny_message(m: Message, status: str, doc: dict):
    if m.chat.type != ChatType.PRIVATE:
        return  # groups: silently ignore non-approved users
    if status == "banned":
        return await m.reply("🚫 You are banned from using this bot.")
    await m.reply(ui.NO_ACCESS, reply_markup=ui.request_kb(bool(doc.get("pending"))))


def is_private(m: Message) -> bool:
    return m.chat.type == ChatType.PRIVATE


def arg_of(m: Message) -> str:
    return " ".join(m.command[1:]) if m.command and len(m.command) > 1 else ""


def cmd_handler(names, private_only=False, owner_only=False, open_cmd=False):
    """Decorator for commands. open_cmd=True: allowed even without approval (start/help/ping/id/request)."""
    def deco(fn):
        @bot.on_message(filters.command(names), group=0)
        async def wrapper(c: Client, m: Message):
            if not m.from_user:
                return
            if private_only and not is_private(m):
                return
            status, doc = await check(m.from_user)
            if owner_only:
                if m.from_user.id != OWNER_ID:
                    return
            elif status != "ok" and not open_cmd:
                return await deny_message(m, status, doc)
            elif status == "banned":
                return await deny_message(m, status, doc)
            if m.from_user.id in login.STATES and names[0] != "cancel":
                await login.cancel(m.from_user.id)
            PROMPTS.pop(m.from_user.id, None)
            try:
                await fn(c, m, doc)
            except FloodWait as fw:
                await m.reply(f"⏳ Flood-wait: try again in {fw.value}s")
            except AccountInvalid as exc:
                await m.reply(f"❌ {html.escape(str(exc))}")
            except Exception as exc:
                log.exception("command failed")
                await m.reply(f"❌ {actions.explain(exc)}")
        return wrapper
    return deco


async def run(m: Message, func, *a):
    rt = await get_runtime(m.from_user.id)
    await m.reply(await func(rt, *a))


def chat_arg(m: Message) -> str:
    """Explicit argument, else the current group's id when the command is sent inside a group."""
    a = arg_of(m)
    if a:
        return a
    return "" if is_private(m) else str(m.chat.id)


# ------------------------------------------------------------------ general commands
@cmd_handler(["start", "menu"], open_cmd=True)
async def cmd_start(c, m, doc):
    status, _ = await check(m.from_user)
    if status != "ok":
        return await deny_message(m, status, doc)
    if not is_private(m):
        return await m.reply("👋 Use me in DM for the full menu. In this group send /record (source) "
                             "or /join (target). See /help.")
    await m.reply(ui.welcome(html.escape(m.from_user.first_name or "there"),
                             bool(db.acc(doc, "rec")), bool(db.acc(doc, "bc"))),
                  reply_markup=ui.home_kb(m.from_user.id))


@cmd_handler(["help"], open_cmd=True)
async def cmd_help(c, m, doc):
    await m.reply("📖 <b>Help Menu</b>\nChoose a section:", reply_markup=ui.help_kb(m.from_user.id))


@cmd_handler(["ping"], open_cmd=True)
async def cmd_ping(c, m, doc):
    t = time.time()
    r = await m.reply("🏓 Pong!")
    await r.edit_text(f"🏓 Pong! <b>{int((time.time() - t) * 1000)} ms</b>")


@cmd_handler(["stats"])
async def cmd_stats(c, m, doc):
    await m.reply(await actions.bot_stats())


@cmd_handler(["id"], open_cmd=True)
async def cmd_id(c, m, doc):
    arg = arg_of(m)
    if not arg:
        extra = "" if is_private(m) else f"\n💬 Chat ID: <code>{m.chat.id}</code>"
        return await m.reply(f"🆔 Your ID: <code>{m.from_user.id}</code>{extra}")
    status, _ = await check(m.from_user)
    if status != "ok":
        return await deny_message(m, status, doc)
    rt = await get_runtime(m.from_user.id)
    chat_id = await actions.resolve_chat(rt, arg)
    await m.reply(f"🆔 Chat ID: <code>{chat_id}</code>")


@cmd_handler(["panel"], private_only=True)
async def cmd_panel(c, m, doc):
    await m.reply("🎙 <b>Audio controls</b>", reply_markup=ui.audio_kb((await get_runtime(m.from_user.id)).relay.sendfile))


# ------------------------------------------------------------------ account commands (DM only)
@cmd_handler(["addaccount", "accounts"], private_only=True)
async def cmd_add(c, m, doc):
    await m.reply(ui.accounts_text(*_accs(doc)), reply_markup=ui.accounts_kb(*_accs(doc)))


@cmd_handler(["myaccount"], private_only=True)
async def cmd_myacct(c, m, doc):
    await m.reply(ui.accounts_text(*_accs(doc)), reply_markup=ui.accounts_kb(*_accs(doc)))


@cmd_handler(["cancel"], private_only=True, open_cmd=True)
async def cmd_cancel(c, m, doc):
    await login.cancel(m.from_user.id)
    PROMPTS.pop(m.from_user.id, None)
    await m.reply("✖ Cancelled.", reply_markup=ui.HOME)


@cmd_handler(["logout"], private_only=True)
async def cmd_logout(c, m, doc):
    await m.reply("Remove <b>both</b> accounts and delete the stored sessions?",
                  reply_markup=M([[B("✅ Yes, remove all", callback_data="rm:all:yes"),
                                   B("✖ Cancel", callback_data="m:acc")]]))


def _accs(doc):
    return db.acc(doc, "rec"), db.acc(doc, "bc")


def _esc_accs(rec, bc):
    def e(a):
        return dict(a, tg_name=html.escape(a.get("tg_name", ""))) if a else None
    return e(rec), e(bc)


# ------------------------------------------------------------------ audio commands (DM or group)
@cmd_handler(["record"])
async def cmd_record(c, m, doc):
    await run(m, actions.record, chat_arg(m))


@cmd_handler(["stoprecord"])
async def cmd_stoprecord(c, m, doc):
    await run(m, actions.stop_record)


@cmd_handler(["stop"])
async def cmd_stop(c, m, doc):
    await run(m, actions.stop_all)


@cmd_handler(["golive", "go"])
async def cmd_golive(c, m, doc):
    await run(m, actions.golive)


@cmd_handler(["target", "settarget"])
async def cmd_target(c, m, doc):
    await run(m, actions.set_target, chat_arg(m))


@cmd_handler(["check"])
async def cmd_check(c, m, doc):
    await run(m, actions.check)


@cmd_handler(["howto"], open_cmd=True)
async def cmd_howto(c, m, doc):
    await m.reply(ui.HOWTO, reply_markup=ui.HOME if is_private(m) else None)


@cmd_handler(["leaverecord"])
async def cmd_leaverec(c, m, doc):
    await run(m, actions.stop_record)


@cmd_handler(["source", "setsource", "setrecordgroup"])
async def cmd_setrec(c, m, doc):
    await run(m, actions.set_source, chat_arg(m))


@cmd_handler(["join"])
async def cmd_join(c, m, doc):
    await run(m, actions.join, chat_arg(m))


@cmd_handler(["rejoin"])
async def cmd_rejoin(c, m, doc):
    await run(m, actions.rejoin)


@cmd_handler(["leave"])
async def cmd_leave(c, m, doc):
    await run(m, actions.leave, chat_arg(m))


@cmd_handler(["leaveall"])
async def cmd_leaveall(c, m, doc):
    await run(m, actions.leave_all)


@cmd_handler(["mute"])
async def cmd_mute(c, m, doc):
    await run(m, actions.mute, True)


@cmd_handler(["unmute"])
async def cmd_unmute(c, m, doc):
    await run(m, actions.mute, False)


@cmd_handler(["sendfile"])
async def cmd_sendfile(c, m, doc):
    await run(m, actions.toggle_sendfile)


@cmd_handler(list(actions.FX_NAMES))
async def cmd_fx(c, m, doc):
    await run(m, actions.set_effect, m.command[0].lower().split("@")[0], arg_of(m))


@cmd_handler(["echo"])
async def cmd_echo(c, m, doc):
    await run(m, actions.set_echo, arg_of(m))


@cmd_handler(["effects"])
async def cmd_effects(c, m, doc):
    await run(m, actions.effects_show)


@cmd_handler(["reset"])
async def cmd_reset(c, m, doc):
    await run(m, actions.effects_reset)


@cmd_handler(["list"])
async def cmd_list(c, m, doc):
    await run(m, actions.fwd_list)


@cmd_handler(["status"])
async def cmd_status(c, m, doc):
    await run(m, actions.status)


# ------------------------------------------------------------------ owner commands
async def revoke_user(uid: int, access: bool = False) -> None:
    """Disconnect, revoke + delete both sessions on Telegram/DB, optionally keep/remove access."""
    await drop_runtime(uid, logout_roles=("rec", "bc"))
    await db.clear_account(uid)
    await db.set_flags(uid, access=access, pending=False)
    await login.cancel(uid)
    PROMPTS.pop(uid, None)


def _uid_arg(m: Message):
    a = arg_of(m).split()
    return int(a[0]) if a and a[0].lstrip("-").isdigit() else None


async def _notify(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


@cmd_handler(["approve", "grant"], owner_only=True, private_only=True)
async def cmd_approve(c, m, doc):
    uid = _uid_arg(m)
    if not uid:
        return await m.reply("Usage: /approve &lt;user_id&gt;")
    await db.users.update_one({"_id": uid}, {"$set": {"access": True, "pending": False, "banned": False}},
                              upsert=True)
    await m.reply(f"✅ Approved <code>{uid}</code>.")
    await _notify(uid, "✅ <b>Access granted.</b> Send /start to begin.")


@cmd_handler(["remove", "disapprove"], owner_only=True, private_only=True)
async def cmd_remove(c, m, doc):
    uid = _uid_arg(m)
    if not uid:
        return await m.reply("Usage: /remove &lt;user_id&gt;")
    await revoke_user(uid)
    await m.reply(f"❌ Access revoked for <code>{uid}</code>. Their accounts were disconnected and deleted.")
    await _notify(uid, "❌ Your access to this bot was revoked by the owner.")


@cmd_handler(["ban"], owner_only=True, private_only=True)
async def cmd_ban(c, m, doc):
    uid = _uid_arg(m)
    if not uid or uid == OWNER_ID:
        return await m.reply("Usage: /ban &lt;user_id&gt;")
    await revoke_user(uid)
    await db.users.update_one({"_id": uid}, {"$set": {"banned": True}}, upsert=True)
    await m.reply(f"🚫 Banned <code>{uid}</code> (accounts disconnected and deleted).")


@cmd_handler(["unban"], owner_only=True, private_only=True)
async def cmd_unban(c, m, doc):
    uid = _uid_arg(m)
    if not uid:
        return await m.reply("Usage: /unban &lt;user_id&gt;")
    await db.set_flags(uid, banned=False)
    await m.reply(f"✅ Unbanned <code>{uid}</code>. (Use /approve to give access again.)")


@cmd_handler(["users"], owner_only=True, private_only=True)
async def cmd_users(c, m, doc):
    text, kb = await owner_list(0)
    await m.reply(text, reply_markup=kb)


@cmd_handler(["pending"], owner_only=True, private_only=True)
async def cmd_pending(c, m, doc):
    rows = await db.list_pending()
    if not rows:
        return await m.reply("📭 No pending requests.")
    for d in rows:
        await m.reply(f"🔔 <b>{html.escape(d.get('name') or '')}</b> "
                      f"{'@' + d['username'] if d.get('username') else ''}\nID: <code>{d['_id']}</code>",
                      reply_markup=ui.request_owner_kb(d["_id"]))


@cmd_handler(["broadcast"], owner_only=True, private_only=True)
async def cmd_broadcast(c, m, doc):
    text = arg_of(m)
    if not text:
        return await m.reply("Usage: /broadcast &lt;text&gt;")
    sent = 0
    for uid in await db.all_user_ids():
        try:
            await c.send_message(uid, text)
            sent += 1
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.reply(f"📢 Sent to {sent} users.")


@cmd_handler(["restart"], owner_only=True, private_only=True)
async def cmd_restart(c, m, doc):
    await m.reply("♻️ Restarting…")
    await shutdown_all()
    os.execv(sys.executable, [sys.executable, "-m", "bot.main"])


PAGE = 8


async def owner_list(page: int):
    total = await db.count_users()
    docs = await db.list_page(page * PAGE, PAGE)
    rows = []
    for d in docs:
        flag = "🚫" if d.get("banned") else ("✅" if d.get("access") else "🔒")
        link = "🔗" if d.get("acc") else "▫️"
        label = f"{flag}{link} {d.get('name') or d['_id']} ({d['_id']})"
        rows.append([B(label[:60], callback_data=f"ow:view:{d['_id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(B("⬅️ Prev", callback_data=f"own:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(B("Next ➡️", callback_data=f"own:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("🔔 Pending", callback_data="ow:pending"), B("📊 Stats", callback_data="ow:stats")])
    rows.append([B("🏠 Home", callback_data="home")])
    text = (f"👑 <b>Owner Panel</b>\n{ui.SEP}\n👥 Users: {total}\n"
            "✅ approved · 🔒 no access · 🚫 banned · 🔗 accounts linked\nTap a user to manage:")
    return text, M(rows)


# ------------------------------------------------------------------ text input (login + prompts)
@bot.on_message(filters.private & filters.text, group=1)
async def text_router(c: Client, m: Message):
    if not m.from_user or (m.text or "").startswith("/"):
        return
    uid = m.from_user.id
    status, doc = await check(m.from_user)
    if status != "ok":
        return
    if login.in_progress(uid):
        try:
            await m.delete()  # remove code / password / session from the chat
        except Exception:
            pass
        reply, done = await login.handle_text(uid, m.text)
        if reply:
            await c.send_message(uid, reply, reply_markup=ui.HOME if done else None)
        return
    p = PROMPTS.pop(uid, None)
    if p and time.time() - p["ts"] < 300:
        rt = await get_runtime(uid)
        try:
            if p["kind"] == "src":
                text = await actions.set_source(rt, m.text.strip())
            else:
                text = await actions.set_target(rt, m.text.strip())
        except AccountInvalid as exc:
            text = f"❌ {html.escape(str(exc))}"
        except Exception as exc:
            text = f"❌ {actions.explain(exc)}"
        await m.reply(text, reply_markup=M([[B("⬅️ Chats", callback_data="m:chats"), ui.HOME_BTN]]))


# ------------------------------------------------------------------ callbacks
async def edit(q: CallbackQuery, text: str, kb=None):
    try:
        await q.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except MessageNotModified:
        pass


async def show_home(q, doc):
    await edit(q, ui.welcome(html.escape(q.from_user.first_name or "there"),
                             bool(db.acc(doc, "rec")), bool(db.acc(doc, "bc"))), ui.home_kb(q.from_user.id))


@bot.on_callback_query()
async def on_cb(c: Client, q: CallbackQuery):
    data, uid = q.data or "", q.from_user.id
    if data == "noop":
        return await q.answer()

    status, doc = await check(q.from_user)

    # ---- access request flow (works for non-approved users)
    if data == "req":
        if status == "banned":
            return await q.answer("You are banned.", show_alert=True)
        if status == "ok":
            return await q.answer("You already have access.", show_alert=True)
        await db.set_flags(uid, pending=True)
        await edit(q, ui.NO_ACCESS, ui.request_kb(True))
        try:
            await c.send_message(OWNER_ID, f"🔔 <b>Access request</b>\n{html.escape(q.from_user.first_name or '')} "
                                           f"{'@' + q.from_user.username if q.from_user.username else ''}\n"
                                           f"ID: <code>{uid}</code>", reply_markup=ui.request_owner_kb(uid))
        except Exception:
            pass
        return await q.answer("Request sent to the owner.")
    if data.startswith("rq:"):
        if uid != OWNER_ID:
            return await q.answer("Owner only.", show_alert=True)
        _, act, tid = data.split(":")
        tid = int(tid)
        if act == "ok":
            await db.users.update_one({"_id": tid}, {"$set": {"access": True, "pending": False}}, upsert=True)
            await _notify(tid, "✅ <b>Access granted.</b> Send /start to begin.")
            await edit(q, f"✅ Approved <code>{tid}</code>.")
        else:
            await db.set_flags(tid, pending=False)
            await _notify(tid, "❌ Your access request was declined.")
            await edit(q, f"✖ Rejected <code>{tid}</code>.")
        return await q.answer()

    if status != "ok":
        return await q.answer("🔒 Access required." if status == "noaccess" else "🚫 Banned.", show_alert=True)

    try:
        # ---------------- navigation
        if data == "home":
            await login.cancel(uid)
            PROMPTS.pop(uid, None)
            return await show_home(q, doc)
        if data == "m:help":
            return await edit(q, "📖 <b>Help Menu</b>\nChoose a section:", ui.help_kb(uid))
        if data.startswith("help:"):
            return await edit(q, ui.HELP_SECTIONS[data[5:]], ui.help_section_kb())
        if data == "m:acc":
            await login.cancel(uid)
            return await edit(q, ui.accounts_text(*_esc_accs(*_accs(doc))), ui.accounts_kb(*_accs(doc)))
        if data == "m:audio":
            rt = await get_runtime(uid)
            return await edit(q, f"🎙 <b>Audio controls</b>\n{ui.SEP}\n"
                                 "Start/stop recording, broadcast control and mute.", ui.audio_kb(rt.relay.sendfile))
        if data == "m:fx":
            rt = await get_runtime(uid)
            return await edit(q, ui.EFFECTS_TEXT, ui.effects_kb(rt.relay.effects))
        if data == "m:chats":
            PROMPTS.pop(uid, None)
            return await edit(q, ui.CHATS_TEXT, ui.chats_kb())
        if data == "m:howto":
            return await edit(q, ui.HOWTO, ui.HOME)
        if data == "m:live" or data.startswith("l:"):
            rt = await get_runtime(uid)
            prefix = ""
            if data.startswith("l:") and data != "l:refresh":
                await q.answer("Working…")
                act = data.split(":")[1]
                prefix = await {"go": actions.golive, "stop": actions.stop_all, "check": actions.check}[act](rt)
            panel = await actions.live_panel(rt)
            return await edit(q, (prefix + "\n\n" if prefix else "") + panel, ui.live_kb())
        if data == "swap":
            await drop_runtime(uid)
            await db.swap_accounts(uid)
            doc = await db.get(uid)
            return await edit(q, "🔄 Swapped.\n\n" + ui.accounts_text(*_esc_accs(*_accs(doc))),
                              ui.accounts_kb(*_accs(doc)))

        # ---------------- accounts
        parts = data.split(":")
        if parts[0] == "add" and len(parts) == 2:
            return await edit(q, ui.CONSENT, ui.method_kb(parts[1]))
        if parts[0] == "add" and len(parts) == 3:
            role, method = parts[1], parts[2]
            await login.begin(uid, role, method)
            msg = (f"📱 <b>{ROLE_NAME[role]}</b>: send the phone number in international format, "
                   "e.g. <code>+919876543210</code>\n\n/cancel to abort" if method == "phone" else
                   f"🔑 <b>{ROLE_NAME[role]}</b>: send your <b>Pyrogram v2 string session</b>.\n"
                   "Your message is deleted right away.\n\n/cancel to abort")
            return await edit(q, msg, M([[B("✖ Cancel", callback_data="m:acc")]]))
        if parts[0] == "rm" and len(parts) == 2:
            return await edit(q, f"Remove the <b>{ROLE_NAME.get(parts[1], 'both')}</b> account and delete its session?",
                              ui.confirm_rm_kb(parts[1]))
        if parts[0] == "rm" and len(parts) == 3 and parts[2] == "yes":
            role = parts[1]
            roles = ("rec", "bc") if role == "all" else (role,)
            await drop_runtime(uid, logout_roles=roles)
            for r_ in roles:
                await db.clear_account(uid, r_)
            doc = await db.get(uid)
            return await edit(q, "✅ Removed — session revoked and deleted.\n\n"
                              + ui.accounts_text(*_esc_accs(*_accs(doc))), ui.accounts_kb(*_accs(doc)))

        # ---------------- audio buttons
        if parts[0] == "a":
            await q.answer("Working…")
            rt = await get_runtime(uid)
            act = parts[1]
            table = {
                "record": lambda: actions.record(rt),
                "stop": lambda: actions.stop_record(rt),
                "rejoin": lambda: actions.rejoin(rt),
                "leaveall": lambda: actions.leave_all(rt),
                "mute": lambda: actions.mute(rt, True),
                "unmute": lambda: actions.mute(rt, False),
                "sendfile": lambda: actions.toggle_sendfile(rt),
                "list": lambda: actions.fwd_list(rt),
                "status": lambda: actions.status(rt),
            }
            text = await table[act]()
            return await edit(q, text, ui.audio_kb(rt.relay.sendfile))

        # ---------------- chats
        if data == "c:src":
            PROMPTS[uid] = {"kind": "src", "ts": time.time()}
            return await edit(q, "📡 Send the <b>source</b> chat: ID (<code>-100…</code>), @username or t.me link.",
                              ui.prompt_kb())
        if data == "c:tgt":
            PROMPTS[uid] = {"kind": "tgt", "ts": time.time()}
            return await edit(q, "🎯 Send the <b>target</b> chat: ID (<code>-100…</code>), @username or t.me link.\n"
                                 "It is saved; press 🚀 Go Live to start.", ui.prompt_kb())
        if data == "c:list":
            rt = await get_runtime(uid)
            return await edit(q, await actions.fwd_list(rt), ui.chats_kb())
        if data == "c:rm":
            rt = await get_runtime(uid)
            if not rt.relay.saved_targets:
                return await q.answer("No targets saved.", show_alert=True)
            return await edit(q, "🗑 Tap a target to stop broadcasting to it:", ui.targets_kb(rt.relay.saved_targets))
        if data.startswith("c:rm:"):
            rt = await get_runtime(uid)
            idx = int(parts[2])
            if idx < len(rt.relay.saved_targets):
                await rt.relay.leave(rt.relay.saved_targets[idx])
            return await edit(q, ui.CHATS_TEXT, ui.chats_kb())

        # ---------------- effects
        if parts[0] == "fx":
            rt = await get_runtime(uid)
            if parts[1] == "reset":
                await rt.relay.reset_effects()
            elif parts[1] == "echo":
                if parts[2] == "off":
                    await rt.relay.set_effects(echo_delay=0, echo_decay=0.0)
                else:
                    await rt.relay.set_effects(echo_delay=int(parts[2]), echo_decay=0.5)
            else:
                await actions.step_effect(rt, parts[1], float(parts[2]))
            return await edit(q, ui.EFFECTS_TEXT, ui.effects_kb(rt.relay.effects))

        # ---------------- owner panel
        if data.startswith(("own:", "ow:")):
            if uid != OWNER_ID:
                return await q.answer("Owner only.", show_alert=True)
            if data.startswith("own:"):
                text, kb = await owner_list(int(parts[1]))
                return await edit(q, text, kb)
            if data == "ow:stats":
                return await edit(q, await actions.bot_stats(), M([[B("⬅️ Back", callback_data="own:0")]]))
            if data == "ow:pending":
                rows = await db.list_pending()
                if not rows:
                    return await q.answer("No pending requests.", show_alert=True)
                for d in rows:
                    await c.send_message(uid, f"🔔 <b>{html.escape(d.get('name') or '')}</b>\nID: <code>{d['_id']}</code>",
                                         reply_markup=ui.request_owner_kb(d["_id"]))
                return await q.answer("Sent below.")
            _, action, tid, page = parts
            tid, page = int(tid), int(page)
            if tid == OWNER_ID and action != "view":
                return await q.answer("You can't do that to the owner.", show_alert=True)
            if action == "ban":
                await revoke_user(tid)
                await db.set_flags(tid, banned=True)
            elif action == "unban":
                await db.set_flags(tid, banned=False)
            elif action == "remove":
                await revoke_user(tid)
                await _notify(tid, "❌ Your access to this bot was revoked by the owner.")
            elif action == "grant":
                await db.set_flags(tid, access=True, pending=False)
                await _notify(tid, "✅ <b>Access granted.</b> Send /start to begin.")
            d = await db.get(tid)
            if not d:
                return await q.answer("User not found.", show_alert=True)
            st = "🚫 banned" if d.get("banned") else ("✅ approved" if d.get("access") else "🔒 no access")
            rec, bc = _esc_accs(*_accs(d))
            text = (f"👤 <b>{html.escape(d.get('name') or str(tid))}</b> "
                    f"{('@' + d['username']) if d.get('username') else ''}\n{ui.SEP}\n"
                    f"ID: <code>{tid}</code>\nStatus: {st}\n"
                    f"🎙 Recorder: {rec['tg_name'] if rec else '—'}\n📡 Broadcaster: {bc['tg_name'] if bc else '—'}")
            return await edit(q, text, ui.owner_user_kb(d, page))
    except FloodWait as fw:
        return await q.answer(f"Flood-wait {fw.value}s", show_alert=True)
    except AccountInvalid as exc:
        return await q.answer(str(exc)[:190], show_alert=True)
    except Exception as exc:
        log.exception("callback failed")
        return await q.answer(actions.explain(exc)[:190].replace("<b>", "").replace("</b>", ""), show_alert=True)
    await q.answer()


# ------------------------------------------------------------------ run
USER_CMDS = [
    ("start", "Open the main menu"), ("menu", "Open the main menu"), ("help", "Help & commands"),
    ("howto", "How to use the bot"), ("golive", "Start: source VC -> target VC"),
    ("stop", "Stop everything & send recording"), ("source", "Set the SOURCE chat"),
    ("target", "Add a TARGET chat"), ("check", "Diagnose setup"),
    ("accounts", "Add / remove Recorder & Broadcaster"), ("record", "Recorder only: start recording"),
    ("stoprecord", "Recorder only: stop recording"), ("join", "Broadcast to a target VC now"),
    ("leave", "Stop broadcasting to a chat"), ("leaveall", "Stop and forget all targets"), ("rejoin", "Reconnect"),
    ("mute", "Mute broadcast"), ("unmute", "Unmute broadcast"), ("effects", "Show effects"),
    ("level", "Set volume"), ("bass", "Set bass"), ("treble", "Set treble"), ("gain", "Set gain"),
    ("overdrive", "Set distortion"), ("echo", "Set echo"), ("reset", "Reset effects"),
    ("list", "Forward list"), ("status", "Status"), ("sendfile", "Toggle recording file delivery"),
    ("id", "Get IDs"), ("ping", "Ping"), ("panel", "Audio control panel"),
]
GROUP_CMDS = [
    ("source", "Use THIS group as the SOURCE voice chat"), ("target", "Use THIS group as a TARGET voice chat"),
    ("golive", "Start live forwarding"), ("stop", "Stop & send recording"), ("check", "Diagnose setup"),
    ("record", "Set THIS group as source and start recording"), ("join", "Broadcast into THIS group's VC now"),
    ("leave", "Stop broadcasting here"),
    ("mute", "Mute"), ("unmute", "Unmute"), ("effects", "Show effects"),
    ("level", "Volume"), ("bass", "Bass"), ("treble", "Treble"), ("gain", "Gain"),
    ("overdrive", "Distortion"), ("echo", "Echo"), ("reset", "Reset effects"), ("id", "Chat ID"),
]
OWNER_CMDS = USER_CMDS + [
    ("users", "User manager"), ("pending", "Access requests"), ("approve", "Approve a user"),
    ("remove", "Revoke a user"), ("ban", "Ban a user"), ("unban", "Unban a user"),
    ("broadcast", "Message all users"), ("restart", "Restart the bot"),
]


async def set_commands():
    try:
        await bot.set_bot_commands([BotCommand(*c) for c in USER_CMDS], scope=BotCommandScopeAllPrivateChats())
        await bot.set_bot_commands([BotCommand(*c) for c in GROUP_CMDS], scope=BotCommandScopeAllGroupChats())
        await bot.set_bot_commands([BotCommand(*c) for c in OWNER_CMDS], scope=BotCommandScopeChat(OWNER_ID))
    except Exception:
        log.exception("could not set bot commands")


async def main():
    await db.init()
    await bot.start()
    await set_commands()
    await db.users.update_one({"_id": OWNER_ID}, {"$set": {"access": True, "banned": False}}, upsert=True)
    me = await bot.get_me()
    log.info("Bot @%s started", me.username)
    try:
        await bot.send_message(OWNER_ID, "✅ Bot started.")
    except Exception:
        pass
    await idle()
    await shutdown_all()
    await bot.stop()


if __name__ == "__main__":
    loop.run_until_complete(main())
