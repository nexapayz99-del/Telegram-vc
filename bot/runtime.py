"""Per-user runtime: each linked Telegram account gets its own client + call engine + relay."""
import asyncio
import logging
from collections import defaultdict
from typing import Dict

from pyrogram import Client
from pyrogram.errors import (AuthKeyUnregistered, SessionExpired, SessionRevoked,
                             UserDeactivated, UserDeactivatedBan)
from pytgcalls import PyTgCalls

from . import db
from .audio import Relay
from .config import API_HASH, API_ID
from .crypto import decrypt

log = logging.getLogger("runtime")


class NoAccount(Exception):
    pass


class AccountInvalid(Exception):
    pass


class Runtime:
    def __init__(self, uid: int, client: Client, call: PyTgCalls, relay: Relay):
        self.uid, self.client, self.call, self.relay = uid, client, call, relay


_runtimes: Dict[int, Runtime] = {}
_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def active_runtimes() -> Dict[int, Runtime]:
    return _runtimes


async def get_runtime(uid: int) -> Runtime:
    async with _locks[uid]:
        rt = _runtimes.get(uid)
        if rt:
            return rt
        doc = await db.get(uid)
        enc = (doc or {}).get("session")
        if not enc:
            raise NoAccount()
        try:
            session = decrypt(enc)
        except ValueError as exc:
            raise AccountInvalid(str(exc))

        client = Client(f"user_{uid}", api_id=API_ID, api_hash=API_HASH,
                        session_string=session, in_memory=True)
        try:
            await client.start()
        except (AuthKeyUnregistered, SessionRevoked, SessionExpired, UserDeactivated, UserDeactivatedBan):
            await db.clear_session(uid)
            raise AccountInvalid("Your Telegram session is no longer valid. Please add your account again.")

        call = PyTgCalls(client)

        async def persist():
            await db.save_settings(uid, relay.export())

        relay = Relay(uid, call, on_change=persist)
        relay.load((doc or {}).get("settings"))

        @call.on_update()
        async def _frames(_, update):
            await relay.on_update(update)

        try:
            await call.start()
        except Exception:
            await client.stop()
            raise
        rt = Runtime(uid, client, call, relay)
        _runtimes[uid] = rt
        log.info("runtime started for %s", uid)
        return rt


async def drop_runtime(uid: int, logout: bool = False) -> None:
    async with _locks[uid]:
        rt = _runtimes.pop(uid, None)
        if not rt:
            return
        try:
            await rt.relay.shutdown()
        except Exception:
            log.exception("relay shutdown")
        try:
            if logout:
                await rt.client.log_out()  # revokes this session on Telegram
            else:
                await rt.client.stop()
        except Exception:
            log.exception("client stop/logout")


async def shutdown_all() -> None:
    for uid in list(_runtimes):
        await drop_runtime(uid)
