"""Email delivery backends. Choose with EMAIL_BACKEND = file | smtp | disabled."""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "text/plain"


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    email_type: str = "general"
    session_id: str | None = None
    reply_to: str | None = None


def to_mime(email: OutgoingEmail, sender: str) -> MimeMessage:
    msg = MimeMessage()
    msg["From"] = sender
    msg["To"] = email.to
    msg["Subject"] = email.subject
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = make_msgid(domain="dgroup-assistant.local")
    if email.reply_to:
        msg["Reply-To"] = email.reply_to
    msg.set_content(email.text)
    if email.html:
        msg.add_alternative(email.html, subtype="html")
    for att in email.attachments:
        maintype, subtype = att.mime_type.split("/", 1)
        msg.add_attachment(att.content, maintype=maintype, subtype=subtype, filename=att.filename)
    return msg


class EmailSender(ABC):
    name = "base"

    @abstractmethod
    def send(self, email: OutgoingEmail) -> None:
        """Deliver or raise."""


class FileEmailSender(EmailSender):
    """Development backend: writes each email as an .eml file to the outbox folder."""

    name = "file"

    def __init__(self, outbox: Path, sender: str) -> None:
        self.outbox = outbox
        self.sender = sender
        self._lock = threading.Lock()

    def send(self, email: OutgoingEmail) -> None:
        self.outbox.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        safe_type = re.sub(r"[^a-z_]", "", email.email_type)
        with self._lock:
            n = len(list(self.outbox.glob(f"{stamp}-*"))) + 1
            path = self.outbox / f"{stamp}-{n:02d}-{safe_type}-{(email.session_id or 'none')[:8]}.eml"
            path.write_bytes(bytes(to_mime(email, self.sender)))


class SMTPEmailSender(EmailSender):
    name = "smtp"

    def __init__(self, host: str, port: int, sender: str, username: str | None = None,
                 password: str | None = None, use_tls: bool = True, timeout: float = 15.0) -> None:
        self.host, self.port, self.sender = host, port, sender
        self.username, self.password = username, password
        self.use_tls, self.timeout = use_tls, timeout

    def send(self, email: OutgoingEmail) -> None:
        msg = to_mime(email, self.sender)
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                  context=ssl.create_default_context()) as smtp:
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(msg)
            return
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(msg)


class DisabledEmailSender(EmailSender):
    name = "disabled"

    def send(self, email: OutgoingEmail) -> None:
        raise RuntimeError("email is disabled")


class MemoryEmailSender(EmailSender):
    """Keeps sent emails in memory (tests / demos)."""

    name = "memory"

    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []

    def send(self, email: OutgoingEmail) -> None:
        self.sent.append(email)


def create_sender(settings) -> EmailSender:
    if settings.email_backend == "smtp":
        return SMTPEmailSender(settings.smtp_host, settings.smtp_port, settings.email_from,
                               settings.smtp_username, settings.smtp_password, settings.smtp_use_tls,
                               settings.smtp_timeout_seconds)
    if settings.email_backend == "file":
        return FileEmailSender(settings.outbox_path, settings.email_from)
    return DisabledEmailSender()
