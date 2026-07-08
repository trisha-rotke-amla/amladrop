"""
REST API for the disposable-mail web UI, plus a background sweep that
deletes expired messages. Also serves the static frontend so you only
need to run one web-facing process.
"""
import asyncio
import base64
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import database

MAILBOX_RE = re.compile(r"^[a-zA-Z0-9._%+-]{1,64}$")
MAX_SUBJECT_CHARS = 300
MAX_BODY_CHARS = 20_000


async def _cleanup_loop():
    while True:
        try:
            removed = database.delete_expired(config.RETENTION_HOURS)
            if removed:
                print(f"[cleanup] removed {removed} expired message(s)")
        except Exception as e:
            print(f"[cleanup] error: {e}")
        await asyncio.sleep(config.CLEANUP_INTERVAL_MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="Disposable Mail API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _normalize_mailbox(mailbox: str) -> str:
    mailbox = mailbox.strip().lower()
    if "@" in mailbox:
        mailbox = mailbox.split("@", 1)[0]
    if not MAILBOX_RE.match(mailbox):
        raise HTTPException(400, "invalid mailbox name")
    return mailbox


@app.get("/api/domain")
def get_domain():
    return {"domain": config.DOMAIN, "retention_hours": config.RETENTION_HOURS}


@app.get("/api/inbox/{mailbox}")
def get_inbox(mailbox: str):
    mailbox = _normalize_mailbox(mailbox)
    return {"mailbox": mailbox, "messages": database.list_messages(mailbox)}


class SendMessageRequest(BaseModel):
    sender: str
    to: str
    subject: str = ""
    body: str = ""


@app.post("/api/send")
def send_message(req: SendMessageRequest):
    """Deliver mail directly into another mailbox on this same domain.

    This never talks to the outside world — there's no outbound SMTP here,
    since this domain's SPF record (-all) already tells everyone this
    service doesn't send real mail. It just writes into the recipient's
    inbox the same way the SMTP receiver would.
    """
    sender_mailbox = _normalize_mailbox(req.sender)

    to = req.to.strip().lower()
    if "@" not in to:
        raise HTTPException(400, "recipient must include a domain")
    to_local, to_domain = to.split("@", 1)
    if to_domain != config.DOMAIN:
        raise HTTPException(400, f"recipient must be @{config.DOMAIN}")
    if not MAILBOX_RE.match(to_local):
        raise HTTPException(400, "invalid recipient mailbox")
    if to_local in config.RESERVED_MAILBOXES:
        raise HTTPException(400, "mailbox unavailable")

    subject = (req.subject.strip() or "(no subject)")[:MAX_SUBJECT_CHARS]
    body = req.body.strip()[:MAX_BODY_CHARS]

    database.insert_message(
        mailbox=to_local,
        sender=f"{sender_mailbox}@{config.DOMAIN}",
        recipient=to,
        subject=subject,
        body_text=body,
        body_html=None,
        attachments=[],
    )
    return {"ok": True}


@app.get("/api/message/{message_id}")
def get_message(message_id: int):
    msg = database.get_message(message_id)
    if not msg:
        raise HTTPException(404, "message not found")
    # Don't ship attachment bytes into the message payload; the frontend
    # fetches those separately via /api/attachment.
    msg["attachments"] = [
        {"filename": a["filename"], "content_type": a["content_type"], "size": a["size"]}
        for a in msg["attachments"]
    ]
    return msg


@app.get("/api/attachment/{message_id}/{index}")
def get_attachment(message_id: int, index: int):
    msg = database.get_message(message_id)
    if not msg or index < 0 or index >= len(msg["attachments"]):
        raise HTTPException(404, "attachment not found")
    att = msg["attachments"][index]
    data = base64.b64decode(att["data_b64"])
    return Response(
        content=data,
        media_type=att["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{att["filename"]}"'},
    )


@app.delete("/api/message/{message_id}")
def delete_message(message_id: int):
    database.delete_message(message_id)
    return {"ok": True}


@app.delete("/api/inbox/{mailbox}")
def clear_inbox(mailbox: str):
    mailbox = _normalize_mailbox(mailbox)
    database.clear_mailbox(mailbox)
    return {"ok": True}


# Serve the frontend last so /api/* routes above take priority.
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
