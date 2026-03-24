from __future__ import annotations

import json


def _btn(label: str, payload: dict, color: str = "primary") -> dict:
    return {
        "action": {"type": "text", "label": label, "payload": json.dumps(payload, ensure_ascii=False)},
        "color": color,
    }


def complaint_keyboard(complaint_id: int) -> str:
    data = {
        "inline": True,
        "buttons": [[
            _btn("✅ Принять", {"a": "accept", "cid": complaint_id}, "positive"),
            _btn("❌ Отклонить", {"a": "reject", "cid": complaint_id}, "negative"),
            _btn("🚫 Заблокировать", {"a": "block", "cid": complaint_id}, "secondary"),
        ]],
    }
    return json.dumps(data, ensure_ascii=False)


def delete_employee_keyboard(username: str) -> str:
    return json.dumps({"inline": True, "buttons": [[_btn("🗑 Удалить", {"a": "demp", "u": username}, "secondary")]]}, ensure_ascii=False)


def unblock_keyboard(user_id: int) -> str:
    return json.dumps({"inline": True, "buttons": [[_btn("🔓 Разблокировать", {"a": "unblock", "uid": user_id}, "positive")]]}, ensure_ascii=False)
