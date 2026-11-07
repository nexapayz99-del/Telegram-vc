"""Account linking: phone + OTP (+2FA) or Pyrogram string session. Each user links only their own account."""
import html
import re
import time
from typing import Dict

from pyrogram import Client
from pyrogram.errors import (ApiIdInvalid, FloodWait, PasswordHashInvalid, PhoneCodeEmpty,
                             PhoneCodeExpired, PhoneCodeInvalid, PhoneNumberBanned,
                             PhoneNumberInvalid, SessionPasswordNeeded)

from . import db
from .config import API_HASH, API_ID, LOGIN_TIMEOUT
from .crypto import encrypt
from .runtime import drop_runtime

# uid -> {"step": str, "client": Client|None, "phone": str, "hash": str, "ts": float}
STATES: Dict[int, dict] = {}


async def cancel(uid: int) -> None:
    st = STATES.pop(uid, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass


async def begin(uid: int, method: str) -> None:
    await cancel(uid)
    STATES[uid] = {"step": "phone" if method == "phone" else "string", "client": None,
                   "phone": "", "hash": "", "ts": time.time()}


def in_progress(uid: int) -> bool:
    st = STATES.get(uid)
    if st and time.time() - st["ts"] > LOGIN_TIMEOUT:
        STATES.pop(uid, None)
        return False
    return st is not None


async def _finish(uid: int, client: Client, phone: str) -> str:
    me = await client.get_me()
    if await db.tg_id_taken(me.id, uid):
        return "taken"
    session = await client.export_session_string()
    await db.set_session(uid, encrypt(session), phone, me.id, me.first_name or "")
    await drop_runtime(uid)  # force a fresh runtime with the new session
    return f"ok:{me.first_name or me.id}"


async def handle_text(uid: int, text: str) -> tuple:
    """Returns (reply_html, done). `done` True when the flow ended (success or fatal)."""
    st = STATES.get(uid)
    if not st:
        return "", True
    if time.time() - st["ts"] > LOGIN_TIMEOUT:
        await cancel(uid)
        return "⌛ Login timed out. Tap <b>Add Account</b> to start again.", True
    st["ts"] = time.time()
    step = st["step"]

    # ---------------- string session
    if step == "string":
        sess = text.strip()
        c = Client(f"chk_{uid}", api_id=API_ID, api_hash=API_HASH, session_string=sess, in_memory=True)
        try:
            await c.start()
            result = await _finish(uid, c, "string-session")
        except Exception as exc:
            await cancel(uid)
            return ("❌ That string session was rejected.\n"
                    f"<code>{html.escape(str(exc))[:150]}</code>\n\n"
                    "Use a <b>Pyrogram</b> v2 string session (not Telethon)."), True
        finally:
            try:
                await c.stop()
            except Exception:
                pass
        await cancel(uid)
        if result == "taken":
            return "⚠️ This Telegram account is already linked to another bot user.", True
        return f"✅ Account <b>{html.escape(result[3:])}</b> linked successfully!", True

    # ---------------- phone
    try:
        if step == "phone":
            phone = text.strip().replace(" ", "")
            if not re.fullmatch(r"\+?\d{7,15}", phone):
                return "❌ Send the number in international format, e.g. <code>+919876543210</code>", False
            if not phone.startswith("+"):
                phone = "+" + phone
            client = Client(f"login_{uid}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
            await client.connect()
            sent = await client.send_code(phone)
            st.update(client=client, phone=phone, hash=sent.phone_code_hash, step="code")
            return ("📨 Code sent to your Telegram app / SMS.\n\n"
                    "Send it <b>with spaces between the digits</b>, e.g. <code>1 2 3 4 5</code>\n"
                    "(Telegram blocks codes sent as one plain number.)"), False

        if step == "code":
            code = re.sub(r"\D", "", text)
            if not code:
                return "❌ Send the digits of the code, e.g. <code>1 2 3 4 5</code>", False
            try:
                await st["client"].sign_in(st["phone"], st["hash"], code)
            except SessionPasswordNeeded:
                st["step"] = "password"
                return "🔐 Two-step verification is on. Send your <b>2FA password</b>.", False
            return await _complete_phone(uid, st)

        if step == "password":
            await st["client"].check_password(text.strip())
            return await _complete_phone(uid, st)

    except PhoneNumberInvalid:
        await cancel(uid)
        return "❌ Invalid phone number. Tap <b>Add Account</b> to retry.", True
    except PhoneNumberBanned:
        await cancel(uid)
        return "❌ That number is banned by Telegram.", True
    except (PhoneCodeInvalid, PhoneCodeEmpty):
        return "❌ Wrong code. Send it again (with spaces), or /cancel.", False
    except PhoneCodeExpired:
        await cancel(uid)
        return "⌛ Code expired. Tap <b>Add Account</b> to request a new one.", True
    except PasswordHashInvalid:
        return "❌ Wrong 2FA password. Try again, or /cancel.", False
    except FloodWait as fw:
        await cancel(uid)
        return f"⏳ Telegram asks you to wait {fw.value}s before trying again.", True
    except ApiIdInvalid:
        await cancel(uid)
        return "❌ Bot API_ID / API_HASH are invalid. Tell the bot owner.", True
    except Exception as exc:
        await cancel(uid)
        return f"❌ Login failed: <code>{html.escape(str(exc))[:150]}</code>", True
    return "", True


async def _complete_phone(uid: int, st: dict) -> tuple:
    result = await _finish(uid, st["client"], st["phone"])
    await cancel(uid)
    if result == "taken":
        return "⚠️ This Telegram account is already linked to another bot user.", True
    return f"✅ Account <b>{html.escape(result[3:])}</b> linked successfully!", True
