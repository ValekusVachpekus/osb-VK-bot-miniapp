from __future__ import annotations

import asyncio
import logging

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal
from .main import process_vk_message_event, startup
from .vk_client import VKClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def get_longpoll_server(vk: VKClient) -> dict:
    return await vk.call("groups.getLongPollServer", group_id=settings.vk_group_id)


async def poll_once(session: aiohttp.ClientSession, server: dict) -> dict:
    params = {
        "act": "a_check",
        "key": server["key"],
        "ts": server["ts"],
        "wait": settings.vk_longpoll_wait,
        "mode": 2,
        "version": 3,
    }
    async with session.get(server["server"], params=params, timeout=settings.vk_longpoll_wait + 10) as resp:
        return await resp.json(content_type=None)


async def process_updates(updates: list[dict]) -> None:
    for upd in updates:
        if upd.get("type") != "message_new":
            continue
        obj = (upd.get("object") or {}).get("message") or {}
        async with SessionLocal() as db:  # type: AsyncSession
            await process_vk_message_event(obj, db)


async def run_longpoll() -> None:
    if not settings.vk_group_id or not settings.vk_group_token:
        raise RuntimeError("VK_GROUP_ID and VK_GROUP_TOKEN are required for longpoll worker")
    await startup()
    vk = VKClient()
    server = await get_longpoll_server(vk)
    logger.info("VK LongPoll started for group_id=%s", settings.vk_group_id)

    async with aiohttp.ClientSession() as http:
        while True:
            try:
                data = await poll_once(http, server)
                if "failed" in data:
                    logger.warning("LongPoll failed=%s, refreshing server", data.get("failed"))
                    server = await get_longpoll_server(vk)
                    continue
                server["ts"] = data.get("ts", server["ts"])
                updates = data.get("updates", [])
                if updates:
                    await process_updates(updates)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("LongPoll loop error: %s", e)
                await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run_longpoll())
