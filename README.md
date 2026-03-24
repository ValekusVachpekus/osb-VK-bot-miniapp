# VK OSB Mini App

Full-featured VK implementation with parity to `osb-bot`:
- user complaint flow
- operator moderation (accept/reject with reason/block)
- admin tools (blocked users, unblock, operator add/remove/list)
- DB persistence + complaint/action logging to VK group chat

## Quick start

1. Copy env template:

```bash
cp .env.example .env
```

2. Set all required values in `.env`:
- `VK_GROUP_ID`
- `VK_GROUP_TOKEN`
- `VK_CONFIRMATION_TOKEN`
- `ADMIN_ID`
- `LOG_PEER_ID`
- `DATABASE_URL`

3. Run with Docker Compose (backend + postgres + longpoll):

```bash
docker compose up
```

4. Open:
- Mini App UI: `http://localhost:8000/`
- VK callback endpoint: `POST /vk/callback`

Longpoll worker starts automatically as `longpoll` service, so bot replies work without public HTTPS callback.

## No public HTTPS (Long Poll mode)

You can run bot processing without Callback API and tunnels:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.longpoll_worker
```

In this mode VK events are pulled via `groups.getLongPollServer`, so public HTTPS callback is not required.

## Local run (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Notes

- All secrets must stay in `.env`.
- `.env.example` keeps only placeholders.
- For production, use PostgreSQL and proper TLS/secret management.
