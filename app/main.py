from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal, engine
from .keyboards import complaint_keyboard, delete_employee_keyboard, unblock_keyboard
from .models import AuditLog, Base, BlockedUser, BotSession, Complaint, ComplaintMessage, Employee
from .vk_client import VKClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="VK OSB Mini App", version="1.0.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


async def get_db() -> AsyncSession:
    async with SessionLocal() as s:
        yield s


def parse_payload(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def normalize_identifier(text: str) -> str:
    t = (text or "").strip().lower()
    if t.startswith("https://vk.com/"):
        t = t[len("https://vk.com/"):]
    elif t.startswith("http://vk.com/"):
        t = t[len("http://vk.com/"):]
    if t.startswith("vk.com/"):
        t = t[len("vk.com/"):]
    t = t.lstrip("@").strip("/")
    return t


def parse_vk_user_id(identifier: str) -> int | None:
    ident = normalize_identifier(identifier)
    if ident.isdigit():
        return int(ident)
    if ident.startswith("id") and ident[2:].isdigit():
        return int(ident[2:])
    return None


async def find_employee_for_user(db: AsyncSession, user_id: int, username_or_domain: str | None) -> Employee | None:
    by_id = await db.scalar(select(Employee).where(Employee.user_id == user_id))
    if by_id:
        return by_id
    uname = normalize_identifier(username_or_domain or "")
    if uname:
        return await db.scalar(select(Employee).where(Employee.username == uname))
    return None


async def get_user_role(db: AsyncSession, user_id: int) -> str:
    if user_id == settings.admin_id:
        return "admin"
    emp = await db.scalar(select(Employee).where(Employee.user_id == user_id, Employee.registered == 1))
    return "operator" if emp else "user"


async def is_blocked(db: AsyncSession, user_id: int) -> bool:
    return await db.scalar(select(BlockedUser.user_id).where(BlockedUser.user_id == user_id)) is not None


async def set_state(db: AsyncSession, user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
    sess = await db.get(BotSession, user_id)
    payload = json.dumps(data or {}, ensure_ascii=False)
    if sess:
        sess.state = state
        sess.data_json = payload
    else:
        db.add(BotSession(user_id=user_id, state=state, data_json=payload))
    await db.commit()


async def clear_state(db: AsyncSession, user_id: int) -> None:
    await db.execute(delete(BotSession).where(BotSession.user_id == user_id))
    await db.commit()


async def get_state(db: AsyncSession, user_id: int) -> tuple[str | None, dict[str, Any]]:
    sess = await db.get(BotSession, user_id)
    if not sess:
        return None, {}
    return sess.state, parse_payload(sess.data_json)


def build_complaint_text(c: Complaint) -> str:
    uname = f"@{c.username}" if c.username else "без username"
    return (
        f"📨 Новая жалоба #{c.id}\n\n"
        f"👤 От: {uname} (ID: {c.user_id})\n"
        f"📋 ФИО заявителя: {c.fio}\n"
        f"👮 Сотрудник / жетон: {c.officer_info}\n"
        f"⚠️ Нарушение: {c.violation}"
    )


async def recipients_for_complaint(db: AsyncSession) -> list[int]:
    ids = [settings.admin_id] if settings.admin_id else []
    rows = (await db.scalars(select(Employee.user_id).where(Employee.registered == 1, Employee.user_id.is_not(None)))).all()
    for uid in rows:
        if uid and uid not in ids:
            ids.append(uid)
    return ids


async def send_complaint_to_all(db: AsyncSession, vk: VKClient, c: Complaint):
    recipients = await recipients_for_complaint(db)
    kb = complaint_keyboard(c.id)
    base_text = build_complaint_text(c)
    if c.media_type == "link" and c.media_file_id:
        base_text += f"\n🔗 Доказательство: {c.media_file_id}"
    for rid in recipients:
        try:
            mid = await vk.send_message(rid, base_text, keyboard=kb, attachment=(c.media_file_id if c.media_type in {"photo", "video", "doc"} else None))
            db.add(ComplaintMessage(complaint_id=c.id, peer_id=rid, message_id=int(mid)))
        except Exception as e:
            logger.warning("Could not send complaint with attachment to %s: %s", rid, e)
            try:
                fallback_text = base_text
                if c.media_file_id and c.media_type in {"photo", "video", "doc"}:
                    fallback_text += f"\n📎 Вложение недоступно для пересылки: {c.media_file_id}"
                mid = await vk.send_message(rid, fallback_text, keyboard=kb)
                db.add(ComplaintMessage(complaint_id=c.id, peer_id=rid, message_id=int(mid)))
            except Exception as e2:
                logger.warning("Fallback complaint send failed to %s: %s", rid, e2)
    await db.commit()


async def invalidate_complaint_messages(db: AsyncSession, vk: VKClient, complaint_id: int):
    rows = (await db.scalars(select(ComplaintMessage).where(ComplaintMessage.complaint_id == complaint_id))).all()
    for row in rows:
        try:
            await vk.edit_message(row.peer_id, row.message_id, keyboard=json.dumps({"inline": True, "buttons": []}))
        except Exception as e:
            logger.warning(
                "Could not remove keyboard for complaint %s message %s/%s: %s",
                complaint_id,
                row.peer_id,
                row.message_id,
                e,
            )


async def log_to_vk_group(db: AsyncSession, vk: VKClient, complaint: Complaint, action: str, actor_id: int, reason: str | None = None):
    if not settings.log_peer_id:
        return
    actor_emp = await db.scalar(select(Employee).where(Employee.user_id == actor_id))
    actor_name = f"@{actor_emp.username}" if actor_emp and actor_emp.username else str(actor_id)

    head = "✅" if action == "accepted" else "❌" if action == "rejected" else "🚫"
    text = f"{head} Жалоба #{complaint.id} {action} ({actor_name})\n\n"
    text += build_complaint_text(complaint).split("\n\n", 1)[1]
    if reason:
        text += f"\n📝 Причина отказа: {reason}"
    if complaint.media_type == "link" and complaint.media_file_id:
        text += f"\n🔗 Доказательство: {complaint.media_file_id}"

    try:
        await vk.send_message(settings.log_peer_id, text, attachment=(complaint.media_file_id if complaint.media_type in {"photo", "video", "doc"} else None))
        staff_text = "👮 Карточка сотрудника\n\n"
        if actor_emp:
            staff_text += (
                f"📛 Никнейм: {actor_emp.nickname or '—'}\n"
                f"📋 ФИО: {actor_emp.fio or '—'}\n"
                f"🏷 Должность: {actor_emp.position or '—'}\n"
                f"⭐ Звание: {actor_emp.rank or '—'}\n"
                f"🔗 VK: {actor_name}"
            )
        else:
            staff_text += f"🆔 ID: {actor_id}\n(Администратор)"
        await vk.send_message(settings.log_peer_id, staff_text)
    except Exception as e:
        logger.warning("Could not send group log: %s", e)


async def process_decision(db: AsyncSession, vk: VKClient, *, complaint_id: int, actor_id: int, action: str, reason: str | None = None):
    role = await get_user_role(db, actor_id)
    if role not in {"admin", "operator"}:
        raise HTTPException(status_code=403, detail="Нет доступа")

    complaint = await db.get(Complaint, complaint_id)
    if not complaint:
        raise HTTPException(status_code=404, detail="Жалоба не найдена")
    if complaint.status != "pending":
        raise HTTPException(status_code=409, detail="Жалоба уже обработана")

    if action == "accepted":
        complaint.status = "accepted"
        complaint.accepted_by = actor_id
        await db.commit()
        await vk.send_message(complaint.user_id, f"✅ Ваша жалоба №{complaint.id} принята.")
        await invalidate_complaint_messages(db, vk, complaint.id)
        await log_to_vk_group(db, vk, complaint, "accepted", actor_id)
    elif action == "rejected":
        complaint.status = "rejected"
        complaint.accepted_by = actor_id
        await db.commit()
        await vk.send_message(complaint.user_id, f"❌ Ваша жалоба №{complaint.id} отклонена.\n\nПричина: {reason or '—'}")
        await invalidate_complaint_messages(db, vk, complaint.id)
        await log_to_vk_group(db, vk, complaint, "rejected", actor_id, reason=reason)
    elif action == "blocked":
        complaint.status = "blocked"
        complaint.accepted_by = actor_id
        db.add(BlockedUser(user_id=complaint.user_id, username=complaint.username))
        await db.commit()
        await invalidate_complaint_messages(db, vk, complaint.id)
        await log_to_vk_group(db, vk, complaint, "blocked", actor_id)


@app.on_event("startup")
async def startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("App started")


@app.get("/", response_class=HTMLResponse)
async def root() -> FileResponse:
    return FileResponse("app/static/index.html")


class ComplaintCreate(BaseModel):
    user_id: int
    username: str | None = None
    fio: str
    officer_info: str
    violation: str
    media_file_id: str | None = None
    media_type: str | None = None


class DecisionBody(BaseModel):
    actor_id: int
    reason: str | None = None


class EmployeeAddBody(BaseModel):
    actor_id: int
    username: str


class UnblockBody(BaseModel):
    actor_id: int


@app.get("/api/me")
async def api_me(user_id: int, db: AsyncSession = Depends(get_db)):
    return {"role": await get_user_role(db, user_id), "blocked": await is_blocked(db, user_id)}


@app.post("/api/complaints")
async def api_create_complaint(body: ComplaintCreate, db: AsyncSession = Depends(get_db)):
    if await is_blocked(db, body.user_id):
        raise HTTPException(status_code=403, detail="Пользователь заблокирован")
    c = Complaint(**body.model_dump(), status="pending")
    db.add(c)
    await db.commit()
    await db.refresh(c)
    await send_complaint_to_all(db, VKClient(), c)
    return {"id": c.id, "status": c.status}


@app.get("/api/complaints/active")
async def api_active_complaints(user_id: int, db: AsyncSession = Depends(get_db)):
    role = await get_user_role(db, user_id)
    if role not in {"admin", "operator"}:
        raise HTTPException(status_code=403, detail="Нет доступа")
    rows = (await db.scalars(select(Complaint).where(Complaint.status == "pending").order_by(desc(Complaint.created_at)))).all()
    return [{
        "id": c.id,
        "user_id": c.user_id,
        "username": c.username,
        "fio": c.fio,
        "officer_info": c.officer_info,
        "violation": c.violation,
        "media_file_id": c.media_file_id,
        "media_type": c.media_type,
        "status": c.status,
    } for c in rows]


@app.post("/api/complaints/{complaint_id}/accept")
async def api_accept(complaint_id: int, body: DecisionBody, db: AsyncSession = Depends(get_db)):
    await process_decision(db, VKClient(), complaint_id=complaint_id, actor_id=body.actor_id, action="accepted")
    return {"ok": True}


@app.post("/api/complaints/{complaint_id}/reject")
async def api_reject(complaint_id: int, body: DecisionBody, db: AsyncSession = Depends(get_db)):
    if not body.reason:
        raise HTTPException(status_code=400, detail="Причина обязательна")
    await process_decision(db, VKClient(), complaint_id=complaint_id, actor_id=body.actor_id, action="rejected", reason=body.reason)
    return {"ok": True}


@app.post("/api/complaints/{complaint_id}/block")
async def api_block(complaint_id: int, body: DecisionBody, db: AsyncSession = Depends(get_db)):
    await process_decision(db, VKClient(), complaint_id=complaint_id, actor_id=body.actor_id, action="blocked")
    return {"ok": True}


@app.get("/api/blocked")
async def api_blocked(user_id: int, db: AsyncSession = Depends(get_db)):
    if await get_user_role(db, user_id) != "admin":
        raise HTTPException(status_code=403, detail="Нет доступа")
    rows = (await db.scalars(select(BlockedUser).order_by(desc(BlockedUser.blocked_at)))).all()
    return [{"user_id": b.user_id, "username": b.username, "blocked_at": str(b.blocked_at)} for b in rows]


@app.post("/api/blocked/{target_user_id}/unblock")
async def api_unblock(target_user_id: int, body: UnblockBody, db: AsyncSession = Depends(get_db)):
    if await get_user_role(db, body.actor_id) != "admin":
        raise HTTPException(status_code=403, detail="Нет доступа")
    await db.execute(delete(BlockedUser).where(BlockedUser.user_id == target_user_id))
    await db.commit()
    return {"ok": True}


@app.get("/api/employees")
async def api_employees(user_id: int, db: AsyncSession = Depends(get_db)):
    if await get_user_role(db, user_id) != "admin":
        raise HTTPException(status_code=403, detail="Нет доступа")
    rows = (await db.scalars(select(Employee).order_by(desc(Employee.added_at)))).all()
    return [{
        "username": e.username,
        "user_id": e.user_id,
        "fio": e.fio,
        "position": e.position,
        "rank": e.rank,
        "nickname": e.nickname,
        "registered": e.registered,
    } for e in rows]


@app.post("/api/employees")
async def api_add_employee(body: EmployeeAddBody, db: AsyncSession = Depends(get_db)):
    if await get_user_role(db, body.actor_id) != "admin":
        raise HTTPException(status_code=403, detail="Нет доступа")
    username = body.username.lstrip("@").lower().strip()
    if not username:
        raise HTTPException(status_code=400, detail="Некорректный username")
    exists = await db.scalar(select(Employee.id).where(Employee.username == username))
    if exists:
        raise HTTPException(status_code=409, detail="Сотрудник уже добавлен")
    db.add(Employee(username=username, registered=0))
    await db.commit()
    return {"ok": True}


@app.delete("/api/employees/{username}")
async def api_delete_employee(username: str, actor_id: int, db: AsyncSession = Depends(get_db)):
    if await get_user_role(db, actor_id) != "admin":
        raise HTTPException(status_code=403, detail="Нет доступа")
    await db.execute(delete(Employee).where(Employee.username == username.lower()))
    await db.commit()
    return {"ok": True}


def attachment_from_message(obj: dict[str, Any]) -> tuple[str | None, str | None]:
    attachments = obj.get("attachments") or []
    if not attachments:
        return None, None
    a = attachments[0]
    t = a.get("type")
    if t == "photo":
        p = a.get("photo", {})
        acc = p.get("access_key")
        suffix = f"_{acc}" if acc else ""
        return f"photo{p.get('owner_id')}_{p.get('id')}{suffix}", "photo"
    if t == "video":
        v = a.get("video", {})
        acc = v.get("access_key")
        suffix = f"_{acc}" if acc else ""
        return f"video{v.get('owner_id')}_{v.get('id')}{suffix}", "video"
    if t == "doc":
        d = a.get("doc", {})
        acc = d.get("access_key")
        suffix = f"_{acc}" if acc else ""
        return f"doc{d.get('owner_id')}_{d.get('id')}{suffix}", "doc"
    return None, None


async def handle_start(db: AsyncSession, vk: VKClient, uid: int, username: str | None):
    if uid == settings.admin_id:
        await vk.send_message(uid, "👮 Добро пожаловать, Администратор!\n/add_employee\n/staff\n/blocked\n/complaints")
        return
    if await is_blocked(db, uid):
        await vk.send_message(uid, "❌ Вы заблокированы и не можете использовать этого бота.")
        return
    emp = await find_employee_for_user(db, uid, username)
    if emp:
        if not emp.user_id:
            emp.user_id = uid
            await db.commit()
        if not emp.registered:
            await vk.send_message(uid, "👋 Вы добавлены как сотрудник. Пройдите регистрацию командой /register")
        else:
            await vk.send_message(uid, "👮 Добро пожаловать, сотрудник!\n/complaints — активные жалобы\n/register — перерегистрация")
        return
    await vk.send_message(uid, "👋 Добро пожаловать в веб-приёмную жалоб ОСБ ГАИ!\nИспользуйте /complaint для подачи жалобы.")


async def handle_text_message(db: AsyncSession, vk: VKClient, uid: int, username: str | None, text: str, obj: dict[str, Any]):
    state, data = await get_state(db, uid)
    text = (text or "").strip()

    if text == "/start":
        await handle_start(db, vk, uid, username)
        return

    if text == "/register":
        if uid == settings.admin_id:
            return
        emp = await find_employee_for_user(db, uid, username)
        if not emp:
            await vk.send_message(uid, "❌ Вы не добавлены как сотрудник. Обратитесь к администратору.")
            return
        if not emp.user_id:
            emp.user_id = uid
            await db.commit()
        await set_state(db, uid, "register_fio", {})
        await vk.send_message(uid, "📝 Регистрация сотрудника\nШаг 1/4: Введите ваше ФИО")
        return

    if text == "/add_employee":
        if uid != settings.admin_id:
            return
        await set_state(db, uid, "add_employee_identifier", {})
        await vk.send_message(uid, "Введите VK username/domain или VK ID сотрудника (пример: quuzer, @quuzer, id123, 123):")
        return

    if text == "/staff":
        if uid != settings.admin_id:
            return
        employees = (await db.scalars(select(Employee).order_by(desc(Employee.added_at)))).all()
        if not employees:
            await vk.send_message(uid, "📋 Список сотрудников пуст. Используйте /add_employee")
            return
        await vk.send_message(uid, f"👥 Сотрудники ({len(employees)}):")
        for e in employees:
            status = "✅ Зарегистрирован" if e.registered else "⏳ Ожидает регистрации"
            txt = f"@{e.username}\n📋 ФИО: {e.fio or '—'}\n🏷 Должность: {e.position or '—'}\n⭐ Звание: {e.rank or '—'}\n📛 Никнейм: {e.nickname or '—'}\nСтатус: {status}"
            await vk.send_message(uid, txt, keyboard=delete_employee_keyboard(e.username))
        return

    if text == "/blocked":
        if uid != settings.admin_id:
            return
        users = (await db.scalars(select(BlockedUser).order_by(desc(BlockedUser.blocked_at)))).all()
        if not users:
            await vk.send_message(uid, "📋 Список заблокированных пользователей пуст.")
            return
        await vk.send_message(uid, "🚫 Заблокированные пользователи:")
        for b in users:
            uname = f"@{b.username}" if b.username else f"ID: {b.user_id}"
            await vk.send_message(uid, f"{b.user_id} ({uname})", keyboard=unblock_keyboard(b.user_id))
        return

    if text == "/complaint":
        if await is_blocked(db, uid):
            await vk.send_message(uid, "❌ Вы заблокированы и не можете использовать этого бота.")
            return
        await set_state(db, uid, "complaint_fio", {})
        await vk.send_message(uid, "📝 Подача жалобы\nШаг 1/4: Введите ваше ФИО ((Никнейм))")
        return

    if text == "/complaints":
        if await get_user_role(db, uid) not in {"admin", "operator"}:
            return
        rows = (await db.scalars(select(Complaint).where(Complaint.status == "pending").order_by(desc(Complaint.created_at)))).all()
        if not rows:
            await vk.send_message(uid, "📋 Нет активных жалоб.")
            return
        await vk.send_message(uid, f"📋 Активные жалобы ({len(rows)}):")
        for c in rows:
            txt = build_complaint_text(c)
            if c.media_type == "link" and c.media_file_id:
                txt += f"\n🔗 Доказательство: {c.media_file_id}"
            await vk.send_message(uid, txt, keyboard=complaint_keyboard(c.id), attachment=(c.media_file_id if c.media_type in {"photo", "video", "doc"} else None))
        return

    if state == "register_fio":
        data["fio"] = text
        await set_state(db, uid, "register_position", data)
        await vk.send_message(uid, "Шаг 2/4: Введите вашу должность")
        return
    if state == "register_position":
        data["position"] = text
        await set_state(db, uid, "register_rank", data)
        await vk.send_message(uid, "Шаг 3/4: Введите ваше звание")
        return
    if state == "register_rank":
        data["rank"] = text
        await set_state(db, uid, "register_nickname", data)
        await vk.send_message(uid, "Шаг 4/4: Введите ваш никнейм")
        return
    if state == "register_nickname":
        await clear_state(db, uid)
        emp = await find_employee_for_user(db, uid, username)
        if not emp:
            await vk.send_message(uid, "❌ Не удалось найти вашу запись сотрудника. Обратитесь к администратору.")
            return
        emp.fio = data.get("fio")
        emp.position = data.get("position")
        emp.rank = data.get("rank")
        emp.nickname = text
        emp.registered = 1
        emp.user_id = uid
        await db.commit()
        await vk.send_message(uid, f"✅ Регистрация завершена!\n👤 ФИО: {data.get('fio')}\n🏷 Должность: {data.get('position')}\n⭐ Звание: {data.get('rank')}\n📛 Никнейм: {text}\n\n/complaints — активные жалобы")
        return

    if state == "add_employee_identifier":
        await clear_state(db, uid)
        ident = normalize_identifier(text)
        if not ident:
            await vk.send_message(uid, "❌ Некорректный идентификатор.")
            return
        parsed_user_id = parse_vk_user_id(ident)
        target_user_id = parsed_user_id
        target_username = None if parsed_user_id else ident

        if parsed_user_id:
            existing_by_id = await db.scalar(select(Employee).where(Employee.user_id == parsed_user_id))
            if existing_by_id:
                await vk.send_message(uid, f"⚠️ Сотрудник с VK ID {parsed_user_id} уже добавлен.")
                return
            try:
                domain = await vk.get_user_domain(parsed_user_id)
                if domain:
                    target_username = domain
            except Exception:
                target_username = f"id{parsed_user_id}"
        else:
            existing_by_username = await db.scalar(select(Employee).where(Employee.username == target_username))
            if existing_by_username:
                await vk.send_message(uid, f"⚠️ Сотрудник @{target_username} уже добавлен.")
                return

        if not target_username:
            target_username = f"id{target_user_id}"
        db.add(Employee(username=target_username, user_id=target_user_id, registered=0))
        await db.commit()
        suffix = f"VK ID {target_user_id}" if target_user_id else f"@{target_username}"
        await vk.send_message(uid, f"✅ Сотрудник {suffix} добавлен. Пусть выполнит /register")
        return

    if state == "complaint_fio":
        if await is_blocked(db, uid):
            await clear_state(db, uid)
            await vk.send_message(uid, "❌ Вы заблокированы.")
            return
        data["fio"] = text
        await set_state(db, uid, "complaint_officer_info", data)
        await vk.send_message(uid, "Шаг 2/4: Введите ФИО/жетон сотрудника")
        return
    if state == "complaint_officer_info":
        data["officer_info"] = text
        await set_state(db, uid, "complaint_violation", data)
        await vk.send_message(uid, "Шаг 3/4: Опишите, что нарушил сотрудник")
        return
    if state == "complaint_violation":
        data["violation"] = text
        await set_state(db, uid, "complaint_media", data)
        await vk.send_message(uid, "Шаг 4/4: Прикрепите фото/видео/документ или отправьте ссылку (или /skip)")
        return
    if state == "complaint_media":
        media_id, media_type = attachment_from_message(obj)
        if text == "/skip":
            media_id, media_type = None, None
        elif media_id is None:
            if text.startswith("http://") or text.startswith("https://"):
                media_id, media_type = text, "link"
            else:
                await vk.send_message(uid, "❌ Отправьте фото/видео/документ или ссылку (http/https), либо /skip")
                return

        c = Complaint(
            user_id=uid,
            username=username,
            fio=data.get("fio", ""),
            officer_info=data.get("officer_info", ""),
            violation=data.get("violation", ""),
            media_file_id=media_id,
            media_type=media_type,
            status="pending",
        )
        db.add(c)
        await db.commit()
        await db.refresh(c)
        await clear_state(db, uid)
        await vk.send_message(uid, f"✅ Ваша жалоба №{c.id} успешно отправлена на рассмотрение.")
        await send_complaint_to_all(db, vk, c)
        return

    if state == "reject_reason":
        cid = int(data.get("complaint_id", 0))
        await clear_state(db, uid)
        try:
            await process_decision(db, vk, complaint_id=cid, actor_id=uid, action="rejected", reason=text)
            await vk.send_message(uid, f"❌ Жалоба #{cid} отклонена. Пользователь уведомлён.")
        except HTTPException as e:
            await vk.send_message(uid, f"⚠️ {e.detail}")
        return


async def handle_payload_action(db: AsyncSession, vk: VKClient, uid: int, payload: dict[str, Any]):
    action = payload.get("a")
    if action in {"accept", "reject", "block"}:
        cid = int(payload.get("cid", 0))
        if action == "accept":
            try:
                await process_decision(db, vk, complaint_id=cid, actor_id=uid, action="accepted")
                await vk.send_message(uid, f"✅ Жалоба #{cid} принята. Пользователь уведомлён.")
            except HTTPException as e:
                await vk.send_message(uid, f"⚠️ {e.detail}")
        elif action == "block":
            try:
                await process_decision(db, vk, complaint_id=cid, actor_id=uid, action="blocked")
                await vk.send_message(uid, f"🚫 Пользователь по жалобе #{cid} заблокирован.")
            except HTTPException as e:
                await vk.send_message(uid, f"⚠️ {e.detail}")
        else:
            await set_state(db, uid, "reject_reason", {"complaint_id": cid})
            await vk.send_message(uid, f"✍️ Введите причину отклонения жалобы #{cid}:")
        return

    if action == "demp":
        if uid != settings.admin_id:
            return
        username = str(payload.get("u", "")).lower()
        await db.execute(delete(Employee).where(Employee.username == username))
        await db.commit()
        await vk.send_message(uid, f"🗑 Сотрудник @{username} удалён.")
        return

    if action == "unblock":
        if uid != settings.admin_id:
            return
        target = int(payload.get("uid", 0))
        await db.execute(delete(BlockedUser).where(BlockedUser.user_id == target))
        await db.commit()
        await vk.send_message(uid, f"🔓 Пользователь {target} разблокирован.")


async def process_vk_message_event(obj: dict[str, Any], db: AsyncSession) -> None:
    vk = VKClient()
    uid = int(obj.get("from_id", 0))
    text = (obj.get("text") or "").strip()
    payload = parse_payload(obj.get("payload"))
    username = None
    try:
        username = await vk.get_user_domain(uid)
    except Exception:
        username = (payload.get("username") or None)
    if payload.get("a"):
        await handle_payload_action(db, vk, uid, payload)
    else:
        await handle_text_message(db, vk, uid, username, text, obj)


@app.post("/vk/callback")
async def vk_callback(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    event_type = body.get("type")

    if event_type == "confirmation":
        return PlainTextResponse(content=settings.vk_confirmation_token)

    if body.get("group_id") != settings.vk_group_id:
        return PlainTextResponse(content="ok")

    if event_type == "message_new":
        obj = body.get("object", {}).get("message", {})
        await process_vk_message_event(obj, db)
    return PlainTextResponse(content="ok")
