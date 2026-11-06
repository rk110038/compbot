"""Outbound email: email-entry competitions and notifications (via Gmail SMTP with an app password)."""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import httpx

from .. import inbox
from ..config import get_config

log = logging.getLogger("compbot.mail")


def send_mail(to: str, subject: str, body: str, headers: dict | None = None) -> str:
    cfg = get_config()
    if not cfg.mail_enabled:
        raise RuntimeError("Gmail address/app password not configured")
    msg = EmailMessage()
    msg["From"] = cfg.gmail_address
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg.gmail_address.split("@")[-1])
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(body)
    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(cfg.gmail_address, cfg.gmail_app_password)
        s.send_message(msg)
    return msg["Message-ID"]


def compose_entry(comp, profile: dict) -> tuple[str, str]:
    """Build subject + body for an email entry from the post text and the profile."""
    post = (comp.body_text or "").lower()
    subject = comp.email_subject or (comp.prize or comp.title)[:120]
    name = f"{profile.get('title', '')} {profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    address = ", ".join(p for p in [profile.get("address1"), profile.get("address2"), profile.get("town"),
                                    profile.get("county"), profile.get("postcode")] if p)
    lines = []
    if comp.answers:
        if len(comp.answers) == 1:
            lines.append(f"Answer: {comp.answers[0]}")
        else:
            lines += [f"Answer {i}: {a}" for i, a in enumerate(comp.answers, 1)]
        lines.append("")
    lines += [f"Name: {name}", f"Email: {profile.get('email', '')}", f"Address: {address}"]
    if profile.get("phone") and re.search(r"phone|telephone|number|mobile", post):
        lines.append(f"Telephone: {profile['phone']}")
    if profile.get("dob") and re.search(r"date of birth|\bdob\b|\bage\b", post):
        lines.append(f"Date of birth: {profile['dob']}")
    if profile.get("twitter") and re.search(r"twitter|\bx\b handle|x/twitter", post):
        lines.append(f"X/Twitter: {profile['twitter']}")
    lines += ["", "Many thanks,", profile.get("first_name", "")]
    return subject, "\n".join(lines)


def notify(subject: str, text: str, click: str | None = None) -> None:
    """Tell you about something (a possible win): ntfy push and/or an email to NOTIFY_EMAIL. `click` is the page a
    tapped push notification opens."""
    cfg = get_config()
    if cfg.ntfy_url:
        try:
            # As query parameters, which (unlike headers) carry any character: the title quotes the email's subject.
            params = {"title": subject[:200], "tags": "trophy"}
            if click:
                params["click"] = click
            httpx.post(cfg.ntfy_url, content=text.encode("utf-8"), params=params, timeout=15)
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy failed: %s", exc)
    if cfg.notify_email and cfg.mail_enabled:
        try:
            # Tagged so the inbox check never reads it back in as a new email (NOTIFY_EMAIL is often this same Gmail).
            # Only alerts to you carry it: email entries to promoters never say they came from compbot.
            send_mail(cfg.notify_email, f"[compbot] {subject}", text, headers={inbox.OWN_HEADER: "alert"})
        except Exception as exc:  # noqa: BLE001
            log.warning("notify email failed: %s", exc)


def sender_label() -> str:
    cfg = get_config()
    return formataddr(("compbot", cfg.gmail_address or ""))
