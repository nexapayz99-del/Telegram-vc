import time
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

from .config import DB_NAME, MONGO_URI, REQUIRE_APPROVAL

_client = AsyncIOMotorClient(MONGO_URI)
users = _client[DB_NAME]["users"]

ROLES = ("rec", "bc")  # rec = Recorder account, bc = Broadcaster account
_NO_SESSIONS = {"acc.rec.session": 0, "acc.bc.session": 0}


async def init() -> None:
    await _client.admin.command("ping")
    await users.create_index("acc.rec.tg_id", sparse=True)
    await users.create_index("acc.bc.tg_id", sparse=True)


def acc(doc: Optional[dict], role: str) -> Optional[dict]:
    return ((doc or {}).get("acc") or {}).get(role)


async def ensure_user(user) -> dict:
    """Create the user on first contact and refresh name/username."""
    return await users.find_one_and_update(
        {"_id": user.id},
        {
            "$set": {"name": user.first_name or "", "username": user.username or ""},
            "$setOnInsert": {
                "banned": False,
                "access": not REQUIRE_APPROVAL,
                "pending": False,
                "joined": int(time.time()),
                "settings": {},
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def get(uid: int) -> Optional[dict]:
    return await users.find_one({"_id": uid})


async def set_account(uid: int, role: str, encrypted: str, phone: str, tg_id: int, tg_name: str) -> None:
    await users.update_one(
        {"_id": uid},
        {"$set": {f"acc.{role}": {"session": encrypted, "phone": phone, "tg_id": tg_id,
                                  "tg_name": tg_name, "linked_at": int(time.time())}}},
    )


async def clear_account(uid: int, role: Optional[str] = None) -> None:
    if role:
        await users.update_one({"_id": uid}, {"$unset": {f"acc.{role}": ""}})
    else:
        await users.update_one({"_id": uid}, {"$unset": {"acc": ""}, "$set": {"settings": {}}})


async def tg_id_taken(tg_id: int, uid: int) -> bool:
    q = {"_id": {"$ne": uid}, "$or": [{"acc.rec.tg_id": tg_id}, {"acc.bc.tg_id": tg_id}]}
    return await users.find_one(q, {"_id": 1}) is not None


async def set_flags(uid: int, **flags) -> None:
    await users.update_one({"_id": uid}, {"$set": flags})


async def save_settings(uid: int, settings: dict) -> None:
    await users.update_one({"_id": uid}, {"$set": {"settings": settings}})


async def list_page(skip: int, limit: int) -> list:
    cur = users.find({}, _NO_SESSIONS).sort("joined", -1).skip(skip).limit(limit)
    return await cur.to_list(length=limit)


async def list_pending() -> list:
    return await users.find({"pending": True, "access": False}, _NO_SESSIONS).to_list(length=50)


async def count_users() -> int:
    return await users.count_documents({})


async def count_linked() -> int:
    return await users.count_documents({"$or": [{"acc.rec": {"$exists": True}}, {"acc.bc": {"$exists": True}}]})


async def count_banned() -> int:
    return await users.count_documents({"banned": True})


async def all_user_ids() -> list:
    return [d["_id"] async for d in users.find({}, {"_id": 1})]


async def swap_accounts(uid: int) -> None:
    doc = await users.find_one({"_id": uid}, {"acc": 1})
    a = (doc or {}).get("acc") or {}
    new = {}
    if a.get("bc"):
        new["rec"] = a["bc"]
    if a.get("rec"):
        new["bc"] = a["rec"]
    await users.update_one({"_id": uid}, {"$set": {"acc": new}} if new else {"$unset": {"acc": ""}})
