from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as M

from .config import OWNER_ID, OWNER_USERNAME

SEP = "━━━━━━━━━━━━━━━━━━━━━━"


def welcome(name: str, linked: bool) -> str:
    acc = "✅ Account linked" if linked else "⚠️ No account linked yet"
    return (
        f"✨ <b>Welcome to Audio Forwarder</b> ✨\n{SEP}\n"
        f"👋 Hello, <b>{name}</b>!\n{SEP}\n"
        "🎵 <b>Real-time Audio Forwarding</b>\n"
        f"📊 Account: {acc}\n"
        "⚡ <b>Features:</b>\n"
        "• Real-time forwarding\n• Advanced effects\n• Volume control\n"
        "• Multi-chat forwarding\n• Bass/Treble boost\n• Unlimited echo\n"
        "• Distortion/Gain effect\n• Your own account, your own session\n"
        f"{SEP}\n📖 Use /help for commands"
    )


HELP = (
    f"📖 <b>Help Menu</b>\n{SEP}\n"
    "👤 <b>Account</b>\n"
    "/addaccount - Link your account (OTP/2FA or string session)\n"
    "/myaccount - Account info\n"
    "/logout - Unlink &amp; delete your session\n"
    "/cancel - Cancel login\n"
    f"{SEP}\n🎵 <b>Audio Controls</b>\n"
    "/record [id] - Start recording the source voice chat\n"
    "/join &lt;id&gt; - Forward to chat\n"
    "/rejoin - Reconnect all\n"
    "/leave &lt;id&gt; - Stop forwarding to chat\n"
    "/leaveall - Stop all\n"
    "/leaverecord - Leave source\n"
    "/mute - Mute\n/unmute - Unmute\n"
    "/setrecordgroup &lt;id&gt; - Set source chat\n"
    f"{SEP}\n🎛 <b>Effects</b> (no limits!)\n"
    "/level &lt;0-∞&gt; - Volume\n/bass &lt;0-∞&gt; - Bass\n/treble &lt;0-∞&gt; - Treble\n"
    "/gain &lt;0-∞&gt; - Gain\n/overdrive &lt;0-∞&gt; - Distortion\n"
    "/echo &lt;delay_ms&gt; [decay] | off - Echo\n"
    "/effects - Show effects\n/reset - Reset all\n"
    f"{SEP}\n📊 <b>Utility</b>\n"
    "/ping - Check bot\n/stats - Bot stats\n/status - System status\n"
    "/list - Forward list\n/id [chat] - Get chat ID\n/panel - Control panel\n"
    f"{SEP}\n👑 <b>Owner</b>\n"
    "/users - Manage users (buttons)\n"
    "/ban &lt;uid&gt; /unban &lt;uid&gt; /remove &lt;uid&gt; /grant &lt;uid&gt;\n"
    "/broadcast &lt;text&gt; /restart"
)


def home_kb(uid: int, linked: bool) -> M:
    owner_btn = (B("👤 Owner", url=f"https://t.me/{OWNER_USERNAME}") if OWNER_USERNAME
                 else B("👤 Owner", callback_data="noop"))
    rows = [
        [owner_btn, B("📖 Help", callback_data="help")],
        [B("👤 My Account" if linked else "➕ Add Account", callback_data="acct" if linked else "add")],
    ]
    if linked:
        rows.append([B("🎛 Control Panel", callback_data="panel")])
    if uid == OWNER_ID:
        rows.append([B("👑 Owner Panel", callback_data="own:0")])
    return M(rows)


HOME = M([[B("🏠 Home", callback_data="home")]])


def add_kb() -> M:
    return M([
        [B("📱 Phone + OTP / 2FA", callback_data="add:phone")],
        [B("🔑 String Session", callback_data="add:string")],
        [B("🏠 Home", callback_data="home")],
    ])


def acct_kb() -> M:
    return M([
        [B("🚪 Logout / delete session", callback_data="logout")],
        [B("🏠 Home", callback_data="home")],
    ])


def confirm_logout_kb() -> M:
    return M([[B("✅ Yes, logout", callback_data="logout:yes"), B("✖ Cancel", callback_data="acct")]])


def panel_kb() -> M:
    return M([
        [B("🎙 Record", callback_data="u:record"), B("⏹ Leave All", callback_data="u:leaveall")],
        [B("🔇 Mute", callback_data="u:mute"), B("🔊 Unmute", callback_data="u:unmute")],
        [B("🔁 Rejoin", callback_data="u:rejoin"), B("♻️ Reset FX", callback_data="u:reset")],
        [B("🎛 Effects", callback_data="u:effects"), B("📋 List", callback_data="u:list")],
        [B("📊 Status", callback_data="u:status"), B("🏠 Home", callback_data="home")],
    ])


CONSENT = (
    "🔐 <b>Before you link your account</b>\n\n"
    "• The bot will operate <b>your</b> Telegram account only for voice-chat audio forwarding.\n"
    "• Your session is stored <b>encrypted</b>; only you can use it. The owner cannot read it.\n"
    "• Use /logout any time to revoke it and delete it from the database.\n"
    "• Never share codes/sessions with anyone else.\n\n"
    "Choose how to link:"
)


def owner_user_kb(doc: dict, page: int) -> M:
    uid = doc["_id"]
    rows = []
    if doc.get("banned"):
        rows.append([B("✅ Unban", callback_data=f"ow:unban:{uid}:{page}")])
    else:
        rows.append([B("🚫 Ban", callback_data=f"ow:ban:{uid}:{page}")])
    if doc.get("access", True):
        rows.append([B("❌ Remove access", callback_data=f"ow:remove:{uid}:{page}")])
    else:
        rows.append([B("✅ Grant access", callback_data=f"ow:grant:{uid}:{page}")])
    rows.append([B("⬅️ Back", callback_data=f"own:{page}")])
    return M(rows)
