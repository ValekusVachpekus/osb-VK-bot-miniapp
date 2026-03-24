from __future__ import annotations

import aiohttp

from .config import settings


class VKClient:
    BASE_URL = "https://api.vk.com/method"

    def __init__(self) -> None:
        self.token = settings.vk_group_token
        self.version = "5.199"

    async def call(self, method: str, **params):
        if not self.token:
            raise RuntimeError("VK_GROUP_TOKEN is not configured")
        filtered = {k: v for k, v in params.items() if v is not None}
        payload = {"access_token": self.token, "v": self.version, **filtered}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.BASE_URL}/{method}", data=payload, timeout=20) as resp:
                data = await resp.json(content_type=None)
        if "error" in data:
            raise RuntimeError(f"VK API error ({method}): {data['error']}")
        return data.get("response")

    async def send_message(self, peer_id: int, text: str, *, keyboard: str | None = None, attachment: str | None = None, random_id: int = 0):
        return await self.call(
            "messages.send",
            peer_id=peer_id,
            message=text,
            keyboard=keyboard,
            attachment=attachment,
            random_id=random_id,
        )

    async def edit_message(self, peer_id: int, message_id: int, *, keyboard: str | None = None):
        return await self.call("messages.edit", peer_id=peer_id, message_id=message_id, keyboard=keyboard)

    async def get_user_domain(self, user_id: int) -> str | None:
        resp = await self.call("users.get", user_ids=str(user_id), fields="domain")
        if not resp:
            return None
        user = resp[0]
        domain = user.get("domain")
        if isinstance(domain, str) and domain:
            return domain.lower()
        return None
