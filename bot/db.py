import time
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

from .config import DB_NAME, MONGO_URI, REQUIRE_APPROVAL

_client = AsyncIOMotorClient(MONGO_URI)
users = _client[DB_NAME]["users"]


async def init() -> None:
    await _client.admin.command("ping")
    await users.create_index("tg_id", sparse=True)


async def ensure_user(user) -> dict:
    """Create the user on first contact and refresh name/username."""
    return await users.find_one_and_update(
        {"_id": user.id},
        {
            "$set": {"name": user.first_name or "", "username": user.username or ""},
            "$setOnInsert": {
                "banned": False,
                "access": not REQUIRE_APPROVAL,
                "joined": int(time.time()),
                "settings": {},
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def get(uid: int) -> Optional[dict]:
    return await users.find_one({"_id": uid})


async def set_session(uid: int, encrypted: str, phone: str, tg_id: int, tg_name: str) -> None:
    await users.update_one(
        {"_id": uid},
        {"$set": {"session": encrypted, "phone": phone, "tg_id": tg_id, "tg_name": tg_name,
                  "linked_at": int(time.time())}},
    )


async def clear_session(uid: int) -> None:
    await users.update_one(
        {"_id": uid},
        {"$unset": {"session": "", "phone": "", "tg_id": "", "tg_name": "", "linked_at": ""},
         "$set": {"settings": {}}},
    )


async def tg_id_taken(tg_id: int, uid: int) -> bool:
    return await users.find_one({"tg_id": tg_id, "_id": {"$ne": uid}}) is not None


async def set_flags(uid: int, **flags) -> None:
    await users.update_one({"_id": uid}, {"$set": flags})


async def save_settings(uid: int, settings: dict) -> None:
    await users.update_one({"_id": uid}, {"$set": {"settings": settings}})


async def list_page(skip: int, limit: int) -> list:
    cur = users.find({}, {"session": 0}).sort("joined", -1).skip(skip).limit(limit)
    return await cur.to_list(length=limit)


async def count_users() -> int:
    return await users.count_documents({})


async def count_linked() -> int:
    return await users.count_documents({"session": {"$exists": True}})


async def count_banned() -> int:
    return await users.count_documents({"banned": True})


async def all_user_ids() -> list:
    return [d["_id"] async for d in users.find({}, {"_id": 1})]
