"""Audio actions shared by slash-commands, group commands and menu buttons. Each returns an HTML string."""
import html
import os
import re
import time
from typing import Optional

from pyrogram.errors import FloodWait

from . import db
from .audio import parse_number
from .runtime import ROLE_NAME, Runtime, active_runtimes
from .ui import SEP

START_TIME = time.time()
BOT = None  # set by main: the bot Client, used to deliver recordings


def _dot(on: bool) -> str:
    return "🟢" if on else "🔴"


async def resolve_chat(rt: Runtime, arg: str) -> int:
    arg = arg.strip()
    if re.fullmatch(r"-?\d+", arg):
        return int(arg)
    try:
        side = await rt.ensure("rec")
    except Exception:
        side = await rt.ensure("bc")
    chat = await side.client.get_chat(arg)  # @username or t.me link
    return chat.id


def explain(exc: Exception) -> str:
    name = type(exc).__name__
    if isinstance(exc, FloodWait):
        return f"Telegram flood-wait: retry in {exc.value}s"
    if "NoAccount" in name:
        return f"Link your {ROLE_NAME.get(getattr(exc, 'role', ''), 'required')} account first (Menu → Accounts)."
    if "NoActiveGroupCall" in name:
        return "No active voice chat in that group. Start one first."
    if "AlreadyJoined" in name:
        return "Already joined that voice chat."
    if "ChatAdmin" in name:
        return "That account needs permission to join/speak in the voice chat."
    return f"{name}: {html.escape(str(exc))[:160]}"


def _fmt(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


# ------------------------------------------------------------------ recording file delivery
async def deliver_recording(rt: Runtime, info: Optional[dict]) -> str:
    """Send the saved recording to the target chats (or the user's DM if none). Returns a summary line."""
    if not info:
        return ""
    path, secs, r = info["path"], info["seconds"], rt.relay
    try:
        if not r.sendfile:
            return "\n📁 Recording file not sent (disabled in Audio menu)."
        targets = [t for t in r.saved_targets if t != r.source] or [rt.uid]  # never back into the source
        caption = f"Voice chat recording - {_fmt(secs)} - source {r.source}"
        ok, bad = [], []
        for chat in targets:
            sent = False
            try:
                await BOT.send_audio(chat, path, caption=caption, duration=secs)
                sent = True
            except Exception:
                for role in ("rec", "bc"):  # bot not admin there? try the user's own account
                    side = rt.sides.get(role)
                    if not side:
                        continue
                    try:
                        await side.client.send_audio(chat, path, caption=caption, duration=secs)
                        sent = True
                        break
                    except Exception:
                        continue
            (ok if sent else bad).append(chat)
        if bad and not ok:  # nothing delivered: fall back to the user's DM
            try:
                await BOT.send_audio(rt.uid, path, caption=caption, duration=secs)
                ok.append(rt.uid)
            except Exception:
                pass
        out = f"\n📁 Recording ({_fmt(secs)}) sent to: " + ", ".join(f"<code>{c}</code>" for c in ok) if ok else ""
        if bad:
            out += "\n⚠️ Could not send to: " + ", ".join(f"<code>{c}</code>" for c in bad) + \
                   " (add the bot as admin there)"
        return out
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ------------------------------------------------------------------ audio
async def record(rt: Runtime, arg: str = "") -> str:
    r = rt.relay
    if arg:
        r.source = await resolve_chat(rt, arg)
    if r.source is None:
        return ("❌ No source chat set.\nUse <code>/record</code> inside the source group, "
                "or <code>/record &lt;chat_id&gt;</code>.")
    if r.recording:
        return "ℹ️ Already recording."
    try:
        await r.start_record(r.source)
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    except Exception as exc:
        return f"❌ Could not start recording: {explain(exc)}"
    fwd = len(r.targets)
    return (f"✅ <b>Recording started successfully!</b>\n{SEP}\n"
            f"📡 Source: <code>{r.source}</code>\n🔗 Forwarding: {fwd} chats\n"
            f"📊 Status: 🟢 Live\n{SEP}\n"
            + ("🎵 Audio is now flowing!" if fwd else "⚠️ No target yet — use /join in the target group."))


async def stop_record(rt: Runtime) -> str:
    try:
        info = await rt.relay.leave_record()
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    return "⏹ <b>Recording stopped.</b>" + await deliver_recording(rt, info)


async def set_source(rt: Runtime, arg: str) -> str:
    if not arg:
        return "Usage: <code>/setrecordgroup &lt;chat_id&gt;</code>"
    chat_id = await resolve_chat(rt, arg)
    if chat_id in rt.relay.saved_targets:
        return "❌ That chat is already a TARGET. Source and target must be different groups."
    rt.relay.source = chat_id
    await rt.relay._changed()
    return (f"✅ <b>Source</b> set to <code>{chat_id}</code> (the Recorder listens here).\n"
            "Next: add a target (<code>/target</code>), then /golive.")


async def join(rt: Runtime, arg: str) -> str:
    if not arg:
        return "Usage: <code>/join &lt;chat_id&gt;</code> (or send /join inside the target group)"
    chat_id = await resolve_chat(rt, arg)
    try:
        await rt.relay.join(chat_id)
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    except Exception as exc:
        return f"❌ Could not join: {explain(exc)}"
    return (f"✅ <b>Forwarding started successfully!</b>\n{SEP}\n"
            f"🎯 Target: <code>{chat_id}</code>\n🔗 Total connections: {len(rt.relay.targets)}\n"
            f"📊 Status: 🟢 Connected\n{SEP}\n🎵 Broadcaster joined the voice chat!"
            + ("" if rt.relay.recording else "\n\n⚠️ Not recording yet — send /record."))


async def rejoin(rt: Runtime) -> str:
    ok, errs = await rt.relay.rejoin()
    if not ok and not errs:
        return "ℹ️ Nothing to reconnect."
    out = "🔁 <b>Reconnect</b>\n" + SEP
    if ok:
        out += "\n✅ " + "\n✅ ".join(ok)
    if errs:
        out += "\n❌ " + "\n❌ ".join(f"{html.escape(l)}: {explain(e)}" for l, e in errs)
    return out


async def leave(rt: Runtime, arg: str) -> str:
    if not arg:
        return "Usage: <code>/leave &lt;chat_id&gt;</code> (or send /leave inside the target group)"
    chat_id = await resolve_chat(rt, arg)
    try:
        await rt.relay.leave(chat_id)
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    return f"✅ Stopped forwarding to <code>{chat_id}</code>."


async def leave_all(rt: Runtime) -> str:
    src, n, info = await rt.relay.leave_all()
    return (f"✅ <b>All chats disconnected successfully!</b>\n{SEP}\n"
            f"📡 Source: {'✅ Left' if src else '— not recording'}\n"
            f"🔗 Forwarding: {n} left\n📊 Status: 🔴 All disconnected"
            + await deliver_recording(rt, info))


async def mute(rt: Runtime, on: bool) -> str:
    rt.relay.muted = on
    return "🔇 Muted (silence is forwarded; the recording keeps going)." if on else "🔊 Unmuted."


async def toggle_sendfile(rt: Runtime) -> str:
    await rt.relay.set_sendfile(not rt.relay.sendfile)
    return "📁 Recording file will be sent to targets when recording stops." if rt.relay.sendfile \
        else "📁 Recording file sending is OFF."


# ------------------------------------------------------------------ effects
async def effects_show(rt: Runtime) -> str:
    return f"🎛 <b>Current effects</b>\n{SEP}\n{rt.relay.effects.describe()}"


async def effects_reset(rt: Runtime) -> str:
    await rt.relay.reset_effects()
    return "♻️ All effects reset to default."


FX_NAMES = ("level", "bass", "treble", "gain", "overdrive")


async def set_effect(rt: Runtime, name: str, arg: str) -> str:
    if not arg:
        return f"Usage: <code>/{name} &lt;0-∞&gt;</code> (current: {getattr(rt.relay.effects, name):g})"
    try:
        value = parse_number(arg.split()[0])
    except ValueError:
        return f"❌ Send a number ≥ 0, e.g. <code>/{name} 5</code>"
    await rt.relay.set_effects(**{name: value})
    return f"✅ {name.capitalize()} set to <b>{value:g}</b>"


async def step_effect(rt: Runtime, name: str, delta: float) -> None:
    cur = getattr(rt.relay.effects, name)
    await rt.relay.set_effects(**{name: max(0.0, round(cur + delta, 4))})


async def set_echo(rt: Runtime, arg: str) -> str:
    parts = arg.split()
    if not parts:
        return "Usage: <code>/echo &lt;delay_ms&gt; [decay 0-1]</code> or <code>/echo off</code>"
    if parts[0].lower() == "off":
        await rt.relay.set_effects(echo_delay=0, echo_decay=0.0)
        return "✅ Echo turned off."
    try:
        delay = int(parse_number(parts[0]))
        decay = parse_number(parts[1]) if len(parts) > 1 else 0.5
    except ValueError:
        return "❌ Use numbers, e.g. <code>/echo 500 0.5</code>"
    delay = max(1, min(delay, 90000))
    decay = max(0.01, min(decay, 1.0))
    await rt.relay.set_effects(echo_delay=delay, echo_decay=decay)
    return f"✅ Echo: <b>{delay}ms</b>, decay <b>{decay:g}</b>"


# ------------------------------------------------------------------ info
async def fwd_list(rt: Runtime) -> str:
    r = rt.relay
    lines = [f"📋 <b>Forward list</b>\n{SEP}",
             f"📡 Source: <code>{r.source}</code> {_dot(r.recording)}" if r.source is not None
             else "📡 Source: not set"]
    if r.saved_targets:
        lines += [f"🎯 <code>{t}</code> {_dot(t in r.targets)}" for t in r.saved_targets]
    else:
        lines.append("🎯 No targets saved")
    return "\n".join(lines)


async def status(rt: Runtime) -> str:
    r = rt.relay
    doc = await db.get(rt.uid)
    rec, bc = db.acc(doc, "rec"), db.acc(doc, "bc")
    return (f"📊 <b>System status</b>\n{SEP}\n"
            f"🎙 Recorder account: {_dot(bool(rec))} {html.escape(rec['tg_name']) if rec else 'not linked'}\n"
            f"📡 Broadcaster account: {_dot(bool(bc))} {html.escape(bc['tg_name']) if bc else 'not linked'}\n"
            f"{SEP}\n"
            f"🎙 Recording: {_dot(r.recording)}\n"
            f"🔗 Active targets: {len(r.targets)}\n"
            f"🔇 Muted: {'yes' if r.muted else 'no'}\n"
            f"📁 Send recording file: {'on' if r.sendfile else 'off'}\n"
            f"⚙️ ffmpeg: {_dot(bool(r.proc and r.proc.returncode is None))}")


async def bot_stats() -> str:
    total, linked, banned = await db.count_users(), await db.count_linked(), await db.count_banned()
    rts = active_runtimes()
    live = sum(1 for r in rts.values() if r.relay.active)
    up = int(time.time() - START_TIME)
    return (f"📊 <b>Bot stats</b>\n{SEP}\n"
            f"👥 Users: {total}\n🔗 Users with accounts: {linked}\n🚫 Banned: {banned}\n"
            f"🟢 Loaded: {len(rts)}\n🎙 Live relays: {live}\n🕒 Uptime: {up // 3600}h {up // 60 % 60}m")


# ------------------------------------------------------------------ guided flow
async def set_target(rt: Runtime, arg: str) -> str:
    if not arg:
        return "Usage: <code>/target &lt;chat_id&gt;</code> (or send /target inside the TARGET group)"
    chat_id = await resolve_chat(rt, arg)
    try:
        await rt.relay.add_target(chat_id)
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    return (f"✅ <b>Target</b> saved: <code>{chat_id}</code> (the Broadcaster plays here).\n"
            "Next: /golive")


async def stop_all(rt: Runtime) -> str:
    src, n, info = await rt.relay.leave_all(forget=False)
    if not src and not n:
        return "ℹ️ Nothing is running."
    return (f"⏹ <b>Stopped.</b>\n{SEP}\n"
            f"📡 Source: {'✅ left' if src else '—'}\n🔗 Targets left: {n}\n"
            "Your source/targets are kept — press Go Live to resume."
            + await deliver_recording(rt, info))


async def golive(rt: Runtime) -> str:
    r = rt.relay
    if r.source is None:
        return "❌ No <b>source</b> set. Use <code>/source</code> in the SOURCE group (or 💬 Chats → Set Source)."
    if not r.saved_targets:
        return "❌ No <b>target</b> set. Use <code>/target</code> in the TARGET group (or 💬 Chats → Add Target)."
    if r.recording and all(t in r.targets for t in r.saved_targets):
        return "ℹ️ Already live."
    ok, errs = await r.rejoin()
    out = []
    if r.recording and r.targets:
        out.append(f"🟢 <b>You are LIVE!</b>\n{SEP}\n"
                   f"🎙 Source: <code>{r.source}</code>\n📡 Targets: {len(r.targets)}\n"
                   "Speak in the SOURCE voice chat with any <b>other</b> account — it plays in the "
                   "TARGET voice chat with your effects.")
    else:
        out.append("⚠️ <b>Not live yet.</b>")
    if errs:
        out.append("❌ " + "\n❌ ".join(f"{html.escape(l)}: {explain(e)}" for l, e in errs))
        out.append("Run /check to diagnose.")
    return "\n\n".join(out)


async def live_panel(rt: Runtime) -> str:
    r = rt.relay
    doc = await db.get(rt.uid)
    rec, bc = db.acc(doc, "rec"), db.acc(doc, "bc")
    live = r.recording and bool(r.targets)
    tg = ", ".join(f"<code>{t}</code> {_dot(t in r.targets)}" for t in r.saved_targets) or "❌ none"
    return (f"🚀 <b>Go Live</b>\n{SEP}\n"
            f"1️⃣ Accounts: 🎙 Recorder {'✅' if rec else '❌'} · 📡 Broadcaster {'✅' if bc else '❌'}\n"
            f"2️⃣ Source VC (listen): {('<code>%s</code> %s' % (r.source, _dot(r.recording))) if r.source is not None else '❌ not set'}\n"
            f"3️⃣ Target VC (play): {tg}\n"
            f"4️⃣ Status: {'🟢 LIVE' if live else '🔴 stopped'}\n{SEP}\n"
            "🎙 SOURCE VC ➜ 🎛 effects ➜ 📡 TARGET VC")


async def _vc_active(client, chat_id: int):
    from pyrogram import raw
    try:
        peer = await client.resolve_peer(chat_id)
        if isinstance(peer, raw.types.InputPeerChannel):
            full = await client.invoke(raw.functions.channels.GetFullChannel(
                channel=raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)))
        else:
            full = await client.invoke(raw.functions.messages.GetFullChat(chat_id=peer.chat_id))
        return bool(full.full_chat.call)
    except Exception:
        return None


async def _chat_line(side, label: str, chat_id: int) -> str:
    try:
        chat = await side.client.get_chat(chat_id)
    except Exception:
        return f"   ❌ {label} <code>{chat_id}</code>: account is not a member / can't see it"
    vc = await _vc_active(side.client, chat_id)
    vc_txt = {True: "voice chat ✅", False: "❌ NO active voice chat — start one", None: "voice chat ?"}[vc]
    return f"   ✅ {label} <b>{html.escape(chat.title or str(chat_id))}</b> · {vc_txt}"


async def check(rt: Runtime) -> str:
    r = rt.relay
    out = [f"🩺 <b>Setup check</b>\n{SEP}"]
    plan = (("rec", "🎙", [("Source", r.source)] if r.source is not None else []),
            ("bc", "📡", [("Target", t) for t in r.saved_targets]))
    for role, icon, chats in plan:
        try:
            side = await rt.ensure(role)
        except Exception as exc:
            out.append(f"{icon} {ROLE_NAME[role]}: ❌ {explain(exc)}")
            continue
        me = await side.client.get_me()
        out.append(f"{icon} {ROLE_NAME[role]}: ✅ {html.escape(me.first_name or '')}")
        for label, cid in chats:
            out.append(await _chat_line(side, label, cid))
    if r.source is None:
        out.append("❌ Source not set")
    if not r.saved_targets:
        out.append("❌ No target set")
    out.append(f"{SEP}\nBoth accounts must be members of their group and a voice chat must be running in each.")
    return "\n".join(out)
