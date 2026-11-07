"""Audio actions shared by slash-commands and panel buttons. Each returns an HTML string."""
import html
import re
import time

from pyrogram.errors import FloodWait

from . import db
from .audio import Effects, parse_number
from .runtime import Runtime, active_runtimes
from .ui import SEP

START_TIME = time.time()


async def resolve_chat(rt: Runtime, arg: str) -> int:
    arg = arg.strip()
    if re.fullmatch(r"-?\d+", arg):
        return int(arg)
    chat = await rt.client.get_chat(arg)  # @username or t.me link
    return chat.id


def explain(exc: Exception) -> str:
    name = type(exc).__name__
    if isinstance(exc, FloodWait):
        return f"Telegram flood-wait: retry in {exc.value}s"
    if "NoActiveGroupCall" in name:
        return "No active voice chat in that group. Start one first."
    if "AlreadyJoined" in name:
        return "Already joined that voice chat."
    if "ChatAdminRequired" in name or "ChatAdmin" in name:
        return "Your account needs permission to join/speak in that voice chat."
    return f"{name}: {html.escape(str(exc))[:160]}"


def _status_dot(on: bool) -> str:
    return "🟢" if on else "🔴"


async def record(rt: Runtime, arg: str = "") -> str:
    r = rt.relay
    if arg:
        r.source = await resolve_chat(rt, arg)
    if r.source is None:
        return "❌ No source set. Use <code>/record &lt;chat_id&gt;</code> or <code>/setrecordgroup &lt;chat_id&gt;</code>."
    if r.recording:
        return "ℹ️ Already recording."
    if r.source in r.targets:
        return "❌ Source is also a forwarding target. Leave it first."
    try:
        await r.start_record(r.source)
    except Exception as exc:
        return f"❌ Could not start recording: {explain(exc)}"
    return (f"✅ <b>Recording started successfully!</b>\n{SEP}\n"
            f"📡 Source: <code>{r.source}</code>\n🔗 Forwarding: {len(r.targets)} chats\n"
            f"📊 Status: 🟢 Live\n{SEP}\n🎵 Audio is now flowing!")


async def set_source(rt: Runtime, arg: str) -> str:
    if not arg:
        return "Usage: <code>/setrecordgroup &lt;chat_id&gt;</code>"
    rt.relay.source = await resolve_chat(rt, arg)
    await rt.relay._changed()
    return f"✅ Source chat set to <code>{rt.relay.source}</code>. Now send /record."


async def join(rt: Runtime, arg: str) -> str:
    if not arg:
        return "Usage: <code>/join &lt;chat_id&gt;</code>"
    chat_id = await resolve_chat(rt, arg)
    try:
        await rt.relay.join(chat_id)
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    except Exception as exc:
        return f"❌ Could not join: {explain(exc)}"
    return (f"✅ <b>Forwarding started successfully!</b>\n{SEP}\n"
            f"🎯 Target: <code>{chat_id}</code>\n🔗 Total connections: {len(rt.relay.targets)}\n"
            f"📊 Status: 🟢 Connected\n{SEP}\n🎵 Audio is now flowing to this chat!"
            + ("" if rt.relay.recording else "\n\n⚠️ Not recording yet — send /record."))


async def rejoin(rt: Runtime) -> str:
    ok, errs = await rt.relay.rejoin()
    if not ok and not errs:
        return "ℹ️ Nothing to reconnect (everything is already connected or nothing is saved)."
    out = "🔁 <b>Reconnect</b>\n" + SEP
    if ok:
        out += "\n✅ " + "\n✅ ".join(ok)
    if errs:
        out += "\n❌ " + "\n❌ ".join(html.escape(e) for e in errs)
    return out


async def leave(rt: Runtime, arg: str) -> str:
    if not arg:
        return "Usage: <code>/leave &lt;chat_id&gt;</code>"
    chat_id = await resolve_chat(rt, arg)
    try:
        await rt.relay.leave(chat_id)
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    return f"✅ Stopped forwarding to <code>{chat_id}</code>."


async def leave_all(rt: Runtime) -> str:
    src, n = await rt.relay.leave_all()
    return (f"✅ <b>All chats disconnected successfully!</b>\n{SEP}\n"
            f"📡 Source: {'✅ Left' if src else '— not recording'}\n"
            f"🔗 Forwarding: {n} left\n📊 Status: 🔴 All disconnected")


async def leave_record(rt: Runtime) -> str:
    try:
        await rt.relay.leave_record()
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    return "✅ Left the source voice chat."


async def mute(rt: Runtime, on: bool) -> str:
    rt.relay.muted = on
    return "🔇 Muted (silence is forwarded)." if on else "🔊 Unmuted."


async def effects_show(rt: Runtime) -> str:
    return f"🎛 <b>Current effects</b>\n{SEP}\n{rt.relay.effects.describe()}"


async def effects_reset(rt: Runtime) -> str:
    await rt.relay.reset_effects()
    return "♻️ All effects reset to default."


_FX = {"level": "level", "bass": "bass", "treble": "treble", "gain": "gain", "overdrive": "overdrive"}


async def set_effect(rt: Runtime, name: str, arg: str) -> str:
    if not arg:
        cur = getattr(rt.relay.effects, _FX[name])
        return f"Usage: <code>/{name} &lt;0-∞&gt;</code> (current: {cur:g})"
    try:
        value = parse_number(arg.split()[0])
    except ValueError:
        return "❌ Send a number ≥ 0, e.g. <code>/%s 5</code>" % name
    await rt.relay.set_effects(**{_FX[name]: value})
    return f"✅ {name.capitalize()} set to <b>{value:g}</b>"


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


async def fwd_list(rt: Runtime) -> str:
    r = rt.relay
    lines = [f"📋 <b>Forward list</b>\n{SEP}",
             f"📡 Source: <code>{r.source}</code> {_status_dot(r.recording)}" if r.source is not None
             else "📡 Source: not set"]
    if r.saved_targets:
        for t in r.saved_targets:
            lines.append(f"🎯 <code>{t}</code> {_status_dot(t in r.targets)}")
    else:
        lines.append("🎯 No targets saved")
    return "\n".join(lines)


async def status(rt: Runtime) -> str:
    r = rt.relay
    return (f"📊 <b>System status</b>\n{SEP}\n"
            f"🎙 Recording: {_status_dot(r.recording)}\n"
            f"🔗 Active targets: {len(r.targets)}\n"
            f"🔇 Muted: {'yes' if r.muted else 'no'}\n"
            f"⚙️ ffmpeg: {_status_dot(bool(r.proc and r.proc.returncode is None))}\n"
            f"🕒 Bot uptime: {int(time.time() - START_TIME) // 60} min")


async def bot_stats() -> str:
    total, linked, banned = await db.count_users(), await db.count_linked(), await db.count_banned()
    rts = active_runtimes()
    live = sum(1 for r in rts.values() if r.relay.active)
    return (f"📊 <b>Bot stats</b>\n{SEP}\n"
            f"👥 Users: {total}\n🔗 Linked accounts: {linked}\n🚫 Banned: {banned}\n"
            f"🟢 Loaded accounts: {len(rts)}\n🎙 Live relays: {live}\n"
            f"🕒 Uptime: {int(time.time() - START_TIME) // 3600}h {int(time.time() - START_TIME) // 60 % 60}m")
