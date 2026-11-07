"""
Per-user runtime. Each bot user links TWO of their own accounts:
  rec = Source account     (joins the source voice chat and listens)
  bc  = Target account  (joins the target voice chat(s) and plays the processed audio)
A Telegram account can only be in one voice chat at a time, which is why two are needed.
"""
import asyncio
import logging
from typing import Dict, Optional

from pyrogram import Client
from pyrogram.errors import (AuthKeyUnregistered, SessionExpired, SessionRevoked,
                             UserDeactivated, UserDeactivatedBan)
from pytgcalls import PyTgCalls

from . import db
from .audio import Relay
from .config import API_HASH, API_ID
from .crypto import decrypt

log = logging.getLogger("runtime")

ROLE_NAME = {"rec": "Source account", "bc": "Target account"}
NOTIFY = None   # set by main: async fn(uid, text) -> DM the user
DELIVER = None  # set by main: async fn(uid, info) -> str  (sends a recording file)


class NoAccount(Exception):
    def __init__(self, role: str):
        super().__init__(role)
        self.role = role


class AccountInvalid(Exception):
    pass


class Side:
    def __init__(self, role: str, client: Client, call: PyTgCalls):
        self.role, self.client, self.call = role, client, call


class Runtime:
    def __init__(self, uid: int):
        self.uid = uid
        self.sides: Dict[str, Side] = {}
        self._start_lock = asyncio.Lock()

        async def persist():
            await db.save_settings(uid, self.relay.export())

        self.relay = Relay(uid, self, on_change=persist)

        async def _notify(text: str):
            if NOTIFY:
                await NOTIFY(uid, text)

        self.relay.notify = _notify

        async def _deliver(info):
            return await DELIVER(uid, info) if DELIVER else ""

        self.relay.deliver = _deliver

    async def ensure(self, role: str) -> Side:
        async with self._start_lock:
            side = self.sides.get(role)
            if side:
                return side
            doc = await db.get(self.uid)
            a = db.acc(doc, role)
            if not a:
                raise NoAccount(role)
            try:
                session = decrypt(a["session"])
            except ValueError as exc:
                raise AccountInvalid(str(exc))
            client = Client(f"u{self.uid}_{role}", api_id=API_ID, api_hash=API_HASH,
                            session_string=session, in_memory=True)
            try:
                await client.start()
            except (AuthKeyUnregistered, SessionRevoked, SessionExpired, UserDeactivated, UserDeactivatedBan):
                await db.clear_account(self.uid, role)
                raise AccountInvalid(f"Your {ROLE_NAME[role]} session is no longer valid. Please add it again.")
            call = PyTgCalls(client)
            def _make(role_):
                async def _handler(_, update):
                    await self.relay.on_update(update, role_)
                return _handler
            call.on_update()(_make(role))
            try:
                await call.start()
            except Exception:
                await client.stop()
                raise
            side = Side(role, client, call)
            self.sides[role] = side
            log.info("started %s account for %s", role, self.uid)
            return side

    async def stop(self, logout_roles=()) -> None:
        try:
            await self.relay.shutdown()
        except Exception:
            log.exception("relay shutdown")
        for role, side in list(self.sides.items()):
            try:
                if role in logout_roles:
                    await side.client.log_out()  # revokes this session on Telegram
                else:
                    await side.client.stop()
            except Exception:
                log.exception("client stop/logout (%s)", role)
        self.sides.clear()


_runtimes: Dict[int, Runtime] = {}
_locks: Dict[int, asyncio.Lock] = {}


def active_runtimes() -> Dict[int, Runtime]:
    return _runtimes


async def get_runtime(uid: int) -> Runtime:
    lock = _locks.setdefault(uid, asyncio.Lock())
    async with lock:
        rt = _runtimes.get(uid)
        if rt:
            return rt
        rt = Runtime(uid)
        doc = await db.get(uid)
        rt.relay.load((doc or {}).get("settings"))
        _runtimes[uid] = rt
        return rt


async def drop_runtime(uid: int, logout_roles=()) -> None:
    """Stop everything for this user. logout_roles: roles whose Telegram session must be revoked."""
    rt = _runtimes.pop(uid, None)
    if rt:
        await rt.stop(logout_roles)
        return
    # runtime not loaded: still honour a requested revoke
    if logout_roles:
        doc = await db.get(uid)
        for role in logout_roles:
            a = db.acc(doc, role)
            if not a:
                continue
            try:
                c = Client(f"rv{uid}_{role}", api_id=API_ID, api_hash=API_HASH,
                           session_string=decrypt(a["session"]), in_memory=True)
                await c.start()
                await c.log_out()
            except Exception:
                log.warning("could not revoke %s session for %s", role, uid)


async def shutdown_all() -> None:
    for uid in list(_runtimes):
        await drop_runtime(uid)
