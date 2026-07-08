"""
Catch-all SMTP receiver.

Accepts mail for ANY local-part @ your configured DOMAIN (that's the whole
point of a disposable-mail service), parses it, and writes it to SQLite.
Rejects anything addressed to a different domain, so this box never acts
as an open relay for unrelated mail.

Run standalone:
    sudo python3 smtp_server.py
(port 25 needs root, or see README for setcap instead of running as root)
"""
import base64
import logging
from email import message_from_bytes
from email.policy import default as default_policy

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import Envelope, Session

import config
import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smtp")


def _local_part(address: str) -> str:
    return address.split("@", 1)[0].strip().lower()


def _domain_part(address: str) -> str:
    if "@" not in address:
        return ""
    return address.split("@", 1)[1].strip().lower()


def _accepts_domain(domain: str) -> bool:
    domain = domain.rstrip(".")
    return domain == config.DOMAIN or domain.endswith("." + config.DOMAIN)


def _extract_body_and_attachments(msg):
    body_text, body_html = None, None
    attachments = []

    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = [msg]

    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()

        if disposition == "attachment" or (part.get_filename() and disposition != "inline"):
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": part.get_filename() or "attachment",
                    "content_type": content_type,
                    "size": len(payload),
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                }
            )
            continue

        if content_type == "text/plain" and body_text is None:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            body_text = payload.decode(charset, errors="replace")
        elif content_type == "text/html" and body_html is None:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            body_html = payload.decode(charset, errors="replace")

    return body_text, body_html, attachments


class DisposableMailHandler:
    async def handle_RCPT(self, server, session: Session, envelope: Envelope, address, rcpt_options):
        domain = _domain_part(address)
        if not _accepts_domain(domain):
            log.info("Rejected RCPT to unrelated domain: %s", address)
            return "550 relaying to that domain is not allowed"

        local = _local_part(address)
        if not local or local in config.RESERVED_MAILBOXES:
            return "550 mailbox unavailable"

        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session: Session, envelope: Envelope):
        try:
            msg = message_from_bytes(envelope.content, policy=default_policy)
        except Exception:
            log.exception("Failed to parse incoming message")
            return "550 could not parse message"

        subject = str(msg.get("subject", "") or "(no subject)")
        sender = str(msg.get("from", envelope.mail_from) or envelope.mail_from)
        body_text, body_html, attachments = _extract_body_and_attachments(msg)

        for rcpt in envelope.rcpt_tos:
            mailbox = _local_part(rcpt)
            database.insert_message(
                mailbox=mailbox,
                sender=sender,
                recipient=rcpt,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                attachments=attachments,
            )
            log.info("Stored message for %s (subject=%r)", mailbox, subject)

        return "250 Message accepted for delivery"


def main():
    database.init_db()
    handler = DisposableMailHandler()
    controller = Controller(
        handler,
        hostname=config.SMTP_HOST,
        port=config.SMTP_PORT,
        data_size_limit=config.MAX_MESSAGE_BYTES,
    )
    controller.start()
    log.info(
        "SMTP receiver listening on %s:%s for @%s",
        config.SMTP_HOST,
        config.SMTP_PORT,
        config.DOMAIN,
    )
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        controller.stop()


if __name__ == "__main__":
    main()
