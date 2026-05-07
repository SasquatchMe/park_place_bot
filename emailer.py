"""Send a filled form by email via configured SMTP server.

Configuration is read from environment variables (see ``.env.example``)::

    SMTP_HOST          — SMTP host (default ``smtp.mail.ru``).
    SMTP_PORT          — SMTP port (default ``465``, implicit TLS).
    SMTP_USER          — full email address used for auth.
    SMTP_PASSWORD      — Mail.ru "external app password" (NOT the account password).
    SMTP_FROM_NAME     — human-readable name shown to the recipient.
    DEFAULT_RECIPIENT  — fallback recipient email (the office mailbox).

The feature is considered enabled when both ``SMTP_USER`` and ``SMTP_PASSWORD``
are set. Otherwise :data:`EMAIL_ENABLED` is ``False`` and the bot hides the
"send to email" buttons.
"""

from __future__ import annotations

import os
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

SMTP_HOST: str = os.environ.get("SMTP_HOST", "smtp.mail.ru")
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER: str = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD: str = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_NAME: str = os.environ.get("SMTP_FROM_NAME", "")
DEFAULT_RECIPIENT: str = os.environ.get("DEFAULT_RECIPIENT", "")

EMAIL_ENABLED: bool = bool(SMTP_USER and SMTP_PASSWORD)


_MOVE_LABELS: dict[str, str] = {"IN": "внос", "OUT": "вынос"}


def move_label(move_type: str) -> str:
    """Return the Russian word matching ``IN`` / ``OUT`` (``внос`` / ``вынос``)."""
    return _MOVE_LABELS.get(move_type, move_type)


async def send_form_email(
    *,
    recipient: str,
    unit: str,
    move_type: str,
    attachment_bytes: bytes,
    attachment_name: str,
) -> None:
    """Send the filled form as an email attachment.

    :param recipient: destination email address.
    :param unit: helmet / office identifier (D1202, etc.) — used in subject and body.
    :param move_type: ``IN`` (внос) or ``OUT`` (вынос).
    :param attachment_bytes: in-memory docx file content.
    :param attachment_name: filename shown to the recipient in the mail client.
    :raises RuntimeError: when SMTP credentials are not configured.
    """
    if not EMAIL_ENABLED:
        raise RuntimeError("SMTP not configured (set SMTP_USER and SMTP_PASSWORD)")

    move_word = move_label(move_type)

    message = EmailMessage()
    message["From"] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    message["To"] = recipient
    message["Subject"] = f"Заявка на {move_word} имущества — {unit}"

    body = (
        "Добрый день!\n"
        "\n"
        f"Прошу согласовать {move_word} имущества в помещение {unit}.\n"
        "Заполненная форма во вложении.\n"
        "\n"
        "С уважением,\n"
        f"{SMTP_FROM_NAME}\n"
    )
    message.set_content(body)

    message.add_attachment(
        attachment_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=attachment_name,
    )

    await aiosmtplib.send(
        message,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USER,
        password=SMTP_PASSWORD,
        use_tls=True,
    )
