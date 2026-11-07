from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as M

from .config import OWNER_ID, OWNER_USERNAME

SEP = "━━━━━━━━━━━━━━━━━━━━━━"


def _back(to="home", label="⬅️ Back"):
    return B(label, callback_data=to)


HOME_BTN = B("🏠 Home", callback_data="home")
HOME = M([[HOME_BTN]])


# ------------------------------------------------------------------ home
def welcome(name: str, rec: bool, bc: bool) -> str:
    return (
        f"✨ <b>Welcome to Audio Forwarder</b> ✨\n{SEP}\n"
        f"👋 Hello, <b>{name}</b>!\n{SEP}\n"
        "🎵 <b>Real-time Audio Forwarding</b>\n"
        f"🎙 Recorder account: {'✅' if rec else '❌ not linked'}\n"
        f"📡 Broadcaster account: {'✅' if bc else '❌ not linked'}\n"
        "⚡ <b>Features:</b>\n"
        "• Record a voice chat, play it live in another\n• Bass / Treble / Echo / Distortion / Gain\n"
        "• Multi-chat forwarding\n• Recording file sent to your target chats\n"
        f"{SEP}\n📖 Use the menu below or /help"
    )


def home_kb(uid: int) -> M:
    owner_btn = (B("👤 Owner", url=f"https://t.me/{OWNER_USERNAME}") if OWNER_USERNAME
                 else B("👤 Owner", callback_data="noop"))
    rows = [
        [B("👥 Accounts", callback_data="m:acc"), B("🎙 Audio", callback_data="m:audio")],
        [B("🎛 Effects", callback_data="m:fx"), B("💬 Chats", callback_data="m:chats")],
        [owner_btn, B("📖 Help", callback_data="m:help")],
    ]
    if uid == OWNER_ID:
        rows.append([B("👑 Owner Panel", callback_data="own:0")])
    return M(rows)


def request_kb(pending: bool) -> M:
    if pending:
        return M([[B("⏳ Request sent — waiting for the owner", callback_data="noop")]])
    return M([[B("🔔 Request access", callback_data="req")]])


NO_ACCESS = ("🔒 <b>Access required</b>\n\nOnly users approved by the owner can use this bot. "
             "Tap below to send a request.")


# ------------------------------------------------------------------ accounts
def accounts_text(rec, bc) -> str:
    def line(label, a):
        return f"{label}: ✅ <b>{a['tg_name']}</b> (<code>{a['tg_id']}</code>)" if a else f"{label}: ❌ not linked"
    return (f"👥 <b>Accounts</b>\n{SEP}\n"
            f"{line('🎙 Recorder  ', rec)}\n{line('📡 Broadcaster', bc)}\n{SEP}\n"
            "The <b>Recorder</b> joins the source voice chat and listens.\n"
            "The <b>Broadcaster</b> joins the target voice chat and plays the audio with your effects.\n"
            "They must be two different accounts.")


def accounts_kb(rec, bc) -> M:
    return M([
        [B("♻️ Re-add Recorder" if rec else "➕ Add Recorder", callback_data="add:rec"),
         B("🗑 Remove", callback_data="rm:rec")] if rec else [B("➕ Add Recorder", callback_data="add:rec")],
        [B("♻️ Re-add Broadcaster" if bc else "➕ Add Broadcaster", callback_data="add:bc"),
         B("🗑 Remove", callback_data="rm:bc")] if bc else [B("➕ Add Broadcaster", callback_data="add:bc")],
        [_back(), HOME_BTN],
    ])


CONSENT = (
    "🔐 <b>Before you link an account</b>\n\n"
    "• The bot operates <b>your</b> account only for voice-chat audio forwarding.\n"
    "• The session is stored <b>encrypted</b>. The owner cannot read it.\n"
    "• Remove it any time (🗑 Remove) — it is revoked on Telegram and deleted.\n"
    "• Never share login codes or sessions with anyone else.\n\n"
    "How do you want to link it?"
)


def method_kb(role: str) -> M:
    return M([
        [B("📱 Phone + OTP / 2FA", callback_data=f"add:{role}:phone")],
        [B("🔑 String Session", callback_data=f"add:{role}:string")],
        [_back("m:acc")],
    ])


def confirm_rm_kb(role: str) -> M:
    return M([[B("✅ Yes, remove", callback_data=f"rm:{role}:yes"), B("✖ Cancel", callback_data="m:acc")]])


# ------------------------------------------------------------------ audio
def audio_kb(sendfile: bool) -> M:
    return M([
        [B("🎙 Start Recording", callback_data="a:record"), B("⏹ Stop Recording", callback_data="a:stop")],
        [B("🔁 Rejoin All", callback_data="a:rejoin"), B("⛔ Leave All", callback_data="a:leaveall")],
        [B("🔇 Mute", callback_data="a:mute"), B("🔊 Unmute", callback_data="a:unmute")],
        [B(f"📁 Send file on stop: {'ON' if sendfile else 'OFF'}", callback_data="a:sendfile")],
        [B("📋 Forward List", callback_data="a:list"), B("📊 Status", callback_data="a:status")],
        [_back(), HOME_BTN],
    ])


# ------------------------------------------------------------------ chats
def chats_kb() -> M:
    return M([
        [B("📡 Set Source (record from)", callback_data="c:src")],
        [B("🎯 Add Target (broadcast to)", callback_data="c:tgt")],
        [B("🗑 Remove Target", callback_data="c:rm")],
        [B("📋 Forward List", callback_data="c:list")],
        [_back(), HOME_BTN],
    ])


CHATS_TEXT = (f"💬 <b>Chats</b>\n{SEP}\n"
              "Set where to record from and where to broadcast to.\n"
              "You can send a chat ID (<code>-100…</code>), <code>@username</code> or a t.me link.\n\n"
              "💡 Easier: add the bot to the groups and send <code>/record</code> in the source group "
              "and <code>/join</code> in the target group.")


def targets_kb(targets: list) -> M:
    rows = [[B(f"🗑 {t}", callback_data=f"c:rm:{i}")] for i, t in enumerate(targets)]
    rows.append([_back("m:chats")])
    return M(rows)


def prompt_kb() -> M:
    return M([[B("✖ Cancel", callback_data="m:chats")]])


# ------------------------------------------------------------------ effects
def effects_kb(e) -> M:
    def row(icon, name, label, val, step):
        return [B(f"{icon} {label}: {val:g}", callback_data="noop"),
                B("➖", callback_data=f"fx:{name}:-{step}"), B("➕", callback_data=f"fx:{name}:{step}")]
    echo = f"{e.echo_delay}ms/{e.echo_decay:g}" if e.echo_delay else "off"
    return M([
        row("🔊", "level", "Level", e.level, 0.25),
        row("🥁", "bass", "Bass", e.bass, 2),
        row("🎼", "treble", "Treble", e.treble, 2),
        row("📈", "gain", "Gain", e.gain, 2),
        row("🔥", "overdrive", "Overdrive", e.overdrive, 5),
        [B(f"🔁 Echo: {echo}", callback_data="noop")],
        [B("Off", callback_data="fx:echo:off"), B("300ms", callback_data="fx:echo:300"),
         B("600ms", callback_data="fx:echo:600"), B("1000ms", callback_data="fx:echo:1000")],
        [B("♻️ Reset all", callback_data="fx:reset")],
        [_back(), HOME_BTN],
    ])


EFFECTS_TEXT = (f"🎛 <b>Effects</b> (no limits!)\n{SEP}\n"
                "Tap ➖ / ➕, or type exact values: <code>/bass 40</code>, <code>/overdrive 80</code>, "
                "<code>/echo 500 0.6</code>. Effects apply live to the broadcast.")


# ------------------------------------------------------------------ help
HELP_SECTIONS = {
    "acc": ("👥 <b>Accounts</b> (DM only)\n"
            "/addaccount - Link Recorder / Broadcaster\n/myaccount - Show linked accounts\n"
            "/logout - Remove both accounts\n/cancel - Cancel login"),
    "audio": ("🎵 <b>Audio</b>\n"
              "/record [id] - Start recording the source voice chat\n"
              "/stoprecord - Stop recording (file is sent to targets)\n"
              "/join [id] - Broadcast into a target voice chat\n/leave [id] - Stop broadcasting to a chat\n"
              "/leaveall - Stop everything\n/rejoin - Reconnect saved chats\n"
              "/mute /unmute - Mute broadcast\n/sendfile - Toggle recording file delivery\n"
              "/setrecordgroup &lt;id&gt; - Set source chat\n/list - Forward list"),
    "fx": ("🎛 <b>Effects</b> (no limits!)\n"
           "/level &lt;0-∞&gt; - Volume\n/bass &lt;0-∞&gt; - Bass\n/treble &lt;0-∞&gt; - Treble\n"
           "/gain &lt;0-∞&gt; - Gain\n/overdrive &lt;0-∞&gt; - Distortion\n"
           "/echo &lt;delay_ms&gt; [decay] | off - Echo\n/effects - Show effects\n/reset - Reset all"),
    "group": ("👥 <b>Group mode</b>\n"
              "Add the bot to your groups (your Recorder and Broadcaster accounts must be members too).\n"
              "• In the <b>source</b> group: <code>/record</code>\n"
              "• In the <b>target</b> group: <code>/join</code>\n"
              "• Stop: <code>/stoprecord</code>, <code>/leave</code>, <code>/leaveall</code>\n"
              "Only approved users can control the bot; everyone else is ignored."),
    "util": ("📊 <b>Utility</b>\n/ping /stats /status /id [chat] /panel /menu"),
    "owner": ("👑 <b>Owner</b>\n/users - User manager (buttons)\n/pending - Access requests\n"
              "/approve &lt;uid&gt; - Grant access\n/remove &lt;uid&gt; - Revoke access + delete accounts\n"
              "/ban &lt;uid&gt; /unban &lt;uid&gt;\n/broadcast &lt;text&gt; /restart"),
}


def help_kb(uid: int) -> M:
    rows = [
        [B("👥 Accounts", callback_data="help:acc"), B("🎵 Audio", callback_data="help:audio")],
        [B("🎛 Effects", callback_data="help:fx"), B("👥 Group mode", callback_data="help:group")],
        [B("📊 Utility", callback_data="help:util")],
    ]
    if uid == OWNER_ID:
        rows.append([B("👑 Owner", callback_data="help:owner")])
    rows.append([_back(), HOME_BTN])
    return M(rows)


def help_section_kb() -> M:
    return M([[_back("m:help"), HOME_BTN]])


# ------------------------------------------------------------------ owner
def owner_user_kb(doc: dict, page: int) -> M:
    uid = doc["_id"]
    rows = []
    rows.append([B("✅ Unban", callback_data=f"ow:unban:{uid}:{page}")] if doc.get("banned")
                else [B("🚫 Ban", callback_data=f"ow:ban:{uid}:{page}")])
    rows.append([B("❌ Remove access", callback_data=f"ow:remove:{uid}:{page}")] if doc.get("access")
                else [B("✅ Grant access", callback_data=f"ow:grant:{uid}:{page}")])
    rows.append([B("⬅️ Back", callback_data=f"own:{page}")])
    return M(rows)


def request_owner_kb(uid: int) -> M:
    return M([[B("✅ Approve", callback_data=f"rq:ok:{uid}"), B("✖ Reject", callback_data=f"rq:no:{uid}")]])
