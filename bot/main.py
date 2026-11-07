import asyncio
import html
import logging
import os
import sys

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import CallbackQuery, Message
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as M

from . import actions, db, login, ui
from .config import API_HASH, API_ID, BOT_TOKEN, OWNER_ID
from .runtime import AccountInvalid, NoAccount, drop_runtime, get_runtime, shutdown_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# Dedicated event loop created BEFORE any Pyrogram client (works on every Python 3.9+).
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

bot = Client("audio_forwarder_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             in_memory=True, parse_mode=ParseMode.HTML)

HTML = {"disable_web_page_preview": True}


# ------------------------------------------------------------------ guards
async def gate(user, need_account: bool):
    """Returns (error_text_or_None, doc)."""
    doc = await db.ensure_user(user)
    if user.id == OWNER_ID:
        if doc.get("banned") or not doc.get("access", True):
            await db.set_flags(user.id, banned=False, access=True)
        return None, doc
    if doc.get("banned"):
        return "🚫 You are banned from using this bot.", doc
    if not doc.get("access", True):
        return "🔒 You don't have access yet. Ask the owner to grant it.", doc
    if need_account and not doc.get("session"):
        return "⚠️ Link your Telegram account first: /addaccount", doc
    return None, doc


async def runtime_or_msg(uid: int):
    try:
        return await get_runtime(uid), None
    except NoAccount:
        return None, "⚠️ Link your Telegram account first: /addaccount"
    except AccountInvalid as exc:
        return None, f"❌ {html.escape(str(exc))}\n\nUse /addaccount to link again."
    except Exception as exc:
        log.exception("runtime start failed")
        return None, f"❌ Could not start your account: <code>{html.escape(str(exc))[:150]}</code>"


def user_cmd(names, need_account=True, owner_only=False):
    def deco(fn):
        @bot.on_message(filters.command(names) & filters.private, group=0)
        async def wrapper(c: Client, m: Message):
            if not m.from_user:
                return
            if owner_only:
                if m.from_user.id != OWNER_ID:
                    return
            else:
                err, _ = await gate(m.from_user, need_account)
                if err:
                    return await m.reply(err)
            if login.in_progress(m.from_user.id) and names != ["cancel"]:
                await login.cancel(m.from_user.id)
            try:
                await fn(c, m)
            except FloodWait as fw:
                await m.reply(f"⏳ Flood-wait: try again in {fw.value}s")
            except Exception as exc:
                log.exception("command failed")
                await m.reply(f"❌ Error: <code>{html.escape(str(exc))[:200]}</code>")
        return wrapper
    return deco


def arg_of(m: Message) -> str:
    return " ".join(m.command[1:]) if m.command and len(m.command) > 1 else ""


async def with_rt(m: Message, func, *a):
    rt, err = await runtime_or_msg(m.from_user.id)
    if err:
        return await m.reply(err)
    await m.reply(await func(rt, *a))


# ------------------------------------------------------------------ basic commands
@user_cmd(["start"], need_account=False)
async def cmd_start(c, m):
    doc = await db.get(m.from_user.id)
    await m.reply(ui.welcome(html.escape(m.from_user.first_name or "there"), bool(doc.get("session"))),
                  reply_markup=ui.home_kb(m.from_user.id, bool(doc.get("session"))))


@user_cmd(["help"], need_account=False)
async def cmd_help(c, m):
    await m.reply(ui.HELP, reply_markup=ui.HOME)


@user_cmd(["ping"], need_account=False)
async def cmd_ping(c, m):
    import time
    t = time.time()
    r = await m.reply("🏓 Pong!")
    await r.edit_text(f"🏓 Pong! <b>{int((time.time() - t) * 1000)} ms</b>")


@user_cmd(["stats"], need_account=False)
async def cmd_stats(c, m):
    await m.reply(await actions.bot_stats())


@user_cmd(["panel"])
async def cmd_panel(c, m):
    await m.reply("🎛 <b>Control Panel</b>", reply_markup=ui.panel_kb())


# ------------------------------------------------------------------ account commands
@user_cmd(["addaccount"], need_account=False)
async def cmd_add(c, m):
    await m.reply(ui.CONSENT, reply_markup=ui.add_kb())


@user_cmd(["cancel"], need_account=False)
async def cmd_cancel(c, m):
    await login.cancel(m.from_user.id)
    await m.reply("✖ Cancelled.", reply_markup=ui.HOME)


@user_cmd(["myaccount"])
async def cmd_myacct(c, m):
    doc = await db.get(m.from_user.id)
    await m.reply(acct_text(doc), reply_markup=ui.acct_kb())


@user_cmd(["logout"])
async def cmd_logout(c, m):
    await m.reply("Unlink your account and delete the stored session?", reply_markup=ui.confirm_logout_kb())


def acct_text(doc: dict) -> str:
    return (f"👤 <b>Your account</b>\n{ui.SEP}\n"
            f"Name: <b>{html.escape(doc.get('tg_name', '?'))}</b>\n"
            f"Telegram ID: <code>{doc.get('tg_id', '?')}</code>\n"
            f"Linked via: {html.escape(doc.get('phone', '?'))}")


# ------------------------------------------------------------------ audio commands
@user_cmd(["record"])
async def cmd_record(c, m):
    await with_rt(m, actions.record, arg_of(m))


@user_cmd(["setrecordgroup"])
async def cmd_setrec(c, m):
    await with_rt(m, actions.set_source, arg_of(m))


@user_cmd(["join"])
async def cmd_join(c, m):
    await with_rt(m, actions.join, arg_of(m))


@user_cmd(["rejoin"])
async def cmd_rejoin(c, m):
    await with_rt(m, actions.rejoin)


@user_cmd(["leave"])
async def cmd_leave(c, m):
    await with_rt(m, actions.leave, arg_of(m))


@user_cmd(["leaveall"])
async def cmd_leaveall(c, m):
    await with_rt(m, actions.leave_all)


@user_cmd(["leaverecord"])
async def cmd_leaverec(c, m):
    await with_rt(m, actions.leave_record)


@user_cmd(["mute"])
async def cmd_mute(c, m):
    await with_rt(m, actions.mute, True)


@user_cmd(["unmute"])
async def cmd_unmute(c, m):
    await with_rt(m, actions.mute, False)


@user_cmd(["level", "bass", "treble", "gain", "overdrive"])
async def cmd_fx(c, m):
    await with_rt(m, actions.set_effect, m.command[0].lower(), arg_of(m))


@user_cmd(["echo"])
async def cmd_echo(c, m):
    await with_rt(m, actions.set_echo, arg_of(m))


@user_cmd(["effects"])
async def cmd_effects(c, m):
    await with_rt(m, actions.effects_show)


@user_cmd(["reset"])
async def cmd_reset(c, m):
    await with_rt(m, actions.effects_reset)


@user_cmd(["list"])
async def cmd_list(c, m):
    await with_rt(m, actions.fwd_list)


@user_cmd(["status"])
async def cmd_status(c, m):
    await with_rt(m, actions.status)


@user_cmd(["id"], need_account=False)
async def cmd_id(c, m):
    arg = arg_of(m)
    if not arg:
        return await m.reply(f"🆔 Your ID: <code>{m.from_user.id}</code>\n\n"
                             "To get a group's ID: <code>/id @username</code> or <code>/id t.me/link</code> "
                             "(needs a linked account).")
    rt, err = await runtime_or_msg(m.from_user.id)
    if err:
        return await m.reply(err)
    chat = await rt.client.get_chat(arg)
    await m.reply(f"🆔 <b>Chat Information</b>\n{ui.SEP}\n🆔 Chat ID: <code>{chat.id}</code>\n"
                  f"📊 Type: {chat.type.name.title()}\n🏷 Title: {html.escape(chat.title or chat.first_name or '')}")


# ------------------------------------------------------------------ owner commands
async def _revoke_runtime(uid: int) -> None:
    await drop_runtime(uid)


@user_cmd(["ban"], owner_only=True)
async def cmd_ban(c, m):
    uid = _uid_arg(m)
    if not uid:
        return await m.reply("Usage: /ban &lt;user_id&gt;")
    await db.set_flags(uid, banned=True)
    await _revoke_runtime(uid)
    await m.reply(f"🚫 Banned <code>{uid}</code>.")


@user_cmd(["unban"], owner_only=True)
async def cmd_unban(c, m):
    uid = _uid_arg(m)
    if not uid:
        return await m.reply("Usage: /unban &lt;user_id&gt;")
    await db.set_flags(uid, banned=False)
    await m.reply(f"✅ Unbanned <code>{uid}</code>.")


@user_cmd(["remove"], owner_only=True)
async def cmd_remove(c, m):
    uid = _uid_arg(m)
    if not uid:
        return await m.reply("Usage: /remove &lt;user_id&gt;")
    await remove_access(uid)
    await m.reply(f"❌ Access removed for <code>{uid}</code> (session deleted).")


@user_cmd(["grant"], owner_only=True)
async def cmd_grant(c, m):
    uid = _uid_arg(m)
    if not uid:
        return await m.reply("Usage: /grant &lt;user_id&gt;")
    await db.set_flags(uid, access=True)
    await m.reply(f"✅ Access granted to <code>{uid}</code>.")


@user_cmd(["users"], owner_only=True)
async def cmd_users(c, m):
    text, kb = await owner_list(0)
    await m.reply(text, reply_markup=kb)


@user_cmd(["broadcast"], owner_only=True)
async def cmd_broadcast(c, m):
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


@user_cmd(["restart"], owner_only=True)
async def cmd_restart(c, m):
    await m.reply("♻️ Restarting…")
    await shutdown_all()
    os.execv(sys.executable, [sys.executable, "-m", "bot.main"])


def _uid_arg(m: Message):
    a = arg_of(m).split()
    return int(a[0]) if a and a[0].lstrip("-").isdigit() else None


async def remove_access(uid: int) -> None:
    """Disconnect + delete the stored session + revoke access. Also kills the session on Telegram."""
    await drop_runtime(uid, logout=True)
    await db.clear_session(uid)
    await db.set_flags(uid, access=False)
    await login.cancel(uid)


PAGE = 8


async def owner_list(page: int):
    total = await db.count_users()
    docs = await db.list_page(page * PAGE, PAGE)
    rows = []
    for d in docs:
        flag = "🚫" if d.get("banned") else ("✅" if d.get("access", True) else "🔒")
        link = "🔗" if d.get("session") else "▫️"
        label = f"{flag}{link} {d.get('name') or d['_id']} ({d['_id']})"
        rows.append([B(label[:60], callback_data=f"ow:view:{d['_id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(B("⬅️ Prev", callback_data=f"own:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(B("Next ➡️", callback_data=f"own:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("📊 Stats", callback_data="ow:stats"), B("🏠 Home", callback_data="home")])
    text = (f"👑 <b>Owner Panel</b>\n{ui.SEP}\n👥 Users: {total}\n"
            "✅ ok · 🔒 no access · 🚫 banned · 🔗 account linked\nTap a user to manage:")
    return text, M(rows)


# ------------------------------------------------------------------ login text handler
@bot.on_message(filters.private & filters.text, group=1)
async def text_router(c: Client, m: Message):
    if not m.from_user or (m.text or "").startswith("/"):
        return
    uid = m.from_user.id
    if not login.in_progress(uid):
        return
    err, _ = await gate(m.from_user, need_account=False)
    if err:
        await login.cancel(uid)
        return await m.reply(err)
    try:
        await m.delete()  # remove code / password / session from the chat
    except Exception:
        pass
    reply, done = await login.handle_text(uid, m.text)
    if reply:
        await c.send_message(uid, reply, reply_markup=ui.HOME if done else None)


# ------------------------------------------------------------------ callbacks
async def edit(q: CallbackQuery, text: str, kb=None):
    try:
        await q.message.edit_text(text, reply_markup=kb, **HTML)
    except MessageNotModified:
        pass


@bot.on_callback_query()
async def on_cb(c: Client, q: CallbackQuery):
    data, uid = q.data or "", q.from_user.id
    if data == "noop":
        return await q.answer()

    owner_cb = data.startswith(("own:", "ow:"))
    err, doc = await gate(q.from_user, need_account=False)
    if err and not owner_cb:
        return await q.answer(err, show_alert=True)
    linked = bool(doc.get("session"))

    try:
        if data == "home":
            await login.cancel(uid)
            return await edit(q, ui.welcome(html.escape(q.from_user.first_name or "there"), linked),
                              ui.home_kb(uid, linked))
        if data == "help":
            return await edit(q, ui.HELP, ui.HOME)
        if data == "add":
            return await edit(q, ui.CONSENT, ui.add_kb())
        if data in ("add:phone", "add:string"):
            await login.begin(uid, "phone" if data == "add:phone" else "string")
            msg = ("📱 Send your phone number in international format, e.g. <code>+919876543210</code>\n\n/cancel to abort"
                   if data == "add:phone" else
                   "🔑 Send your <b>Pyrogram v2 string session</b>.\nI'll delete your message right away.\n\n/cancel to abort")
            return await edit(q, msg, M([[B("✖ Cancel", callback_data="home")]]))
        if data == "acct":
            if not linked:
                return await edit(q, "⚠️ No account linked.", ui.add_kb())
            return await edit(q, acct_text(doc), ui.acct_kb())
        if data == "logout":
            return await edit(q, "Unlink your account and delete the stored session?", ui.confirm_logout_kb())
        if data == "logout:yes":
            await drop_runtime(uid, logout=True)
            await db.clear_session(uid)
            return await edit(q, "✅ Logged out. Your session was revoked and deleted.", ui.HOME)
        if data == "panel":
            if not linked:
                return await q.answer("Link your account first.", show_alert=True)
            return await edit(q, "🎛 <b>Control Panel</b>", ui.panel_kb())

        if data.startswith("u:"):
            if not linked:
                return await q.answer("Link your account first.", show_alert=True)
            await q.answer("Working…")
            rt, e = await runtime_or_msg(uid)
            if e:
                return await edit(q, e, ui.HOME)
            act = data[2:]
            table = {
                "record": lambda: actions.record(rt),
                "leaveall": lambda: actions.leave_all(rt),
                "mute": lambda: actions.mute(rt, True),
                "unmute": lambda: actions.mute(rt, False),
                "rejoin": lambda: actions.rejoin(rt),
                "reset": lambda: actions.effects_reset(rt),
                "effects": lambda: actions.effects_show(rt),
                "list": lambda: actions.fwd_list(rt),
                "status": lambda: actions.status(rt),
            }
            text = await table[act]()
            return await edit(q, text, ui.panel_kb())

        # ---------------- owner panel
        if owner_cb:
            if uid != OWNER_ID:
                return await q.answer("Owner only.", show_alert=True)
            if data.startswith("own:"):
                text, kb = await owner_list(int(data.split(":")[1]))
                return await edit(q, text, kb)
            if data == "ow:stats":
                return await edit(q, await actions.bot_stats(), M([[B("⬅️ Back", callback_data="own:0")]]))
            _, action, tid, page = data.split(":")
            tid, page = int(tid), int(page)
            if action == "ban":
                await db.set_flags(tid, banned=True)
                await drop_runtime(tid)
            elif action == "unban":
                await db.set_flags(tid, banned=False)
            elif action == "remove":
                await remove_access(tid)
            elif action == "grant":
                await db.set_flags(tid, access=True)
            d = await db.get(tid)
            if not d:
                return await q.answer("User not found.", show_alert=True)
            st = ("🚫 banned" if d.get("banned") else "✅ ok") + (" · 🔒 no access" if not d.get("access", True) else "")
            text = (f"👤 <b>{html.escape(d.get('name') or str(tid))}</b> "
                    f"{('@' + d['username']) if d.get('username') else ''}\n{ui.SEP}\n"
                    f"ID: <code>{tid}</code>\nStatus: {st}\n"
                    f"Account linked: {'yes — ' + html.escape(d.get('tg_name', '')) if d.get('session') else 'no'}")
            return await edit(q, text, ui.owner_user_kb(d, page))
    except FloodWait as fw:
        await q.answer(f"Flood-wait {fw.value}s", show_alert=True)
    except Exception as exc:
        log.exception("callback failed")
        await q.answer(f"Error: {str(exc)[:150]}", show_alert=True)
    await q.answer()


# ------------------------------------------------------------------ run
async def main():
    await db.init()
    await bot.start()
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
