"""Notification helpers for the Zendure integration.

Sends alerts through the generic ``notify.send_message`` service so any
notify entity works (Telegram, mobile app, e-mail...). The generic service
cannot carry a per-call ``parse_mode``, so when a target turns out to be a
Telegram Bot entity we MarkdownV2-escape the text ourselves. That keeps the
user's Telegram bot on its MarkdownV2 default (needed by their other
automations) while our plain messages still render literally instead of
crashing on reserved characters like ``-`` ``.`` ``!``.
"""

from __future__ import annotations

import logging
import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Integration domain that backs Telegram notify entities.
TELEGRAM_PLATFORM = "telegram_bot"

# Telegram MarkdownV2 reserved characters that must be backslash-escaped:
#   _ * [ ] ( ) ~ ` > # + - = | { } . !   (and the backslash itself)
_MDV2_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_markdown_v2(text: str | None) -> str:
    """Backslash-escape every MarkdownV2 reserved character."""
    if not text:
        return text or ""
    return _MDV2_RE.sub(r"\\\1", text)


def is_telegram_target(hass: HomeAssistant, entity_id: str) -> bool:
    """Return True if the notify entity is provided by the Telegram Bot integration."""
    try:
        entry = er.async_get(hass).async_get(entity_id)
    except Exception:  # pylint: disable=broad-except
        return False
    return bool(entry and entry.platform == TELEGRAM_PLATFORM)


async def async_notify_targets(
    hass: HomeAssistant,
    targets: list[str],
    title: str,
    message: str,
    *,
    blocking: bool = True,
) -> tuple[list[str], list[str]]:
    """Send a notification to each target, escaping MarkdownV2 for Telegram ones.

    Returns ``(ok, failed)``; ``failed`` entries are ``"entity_id: error"``.
    """
    ok: list[str] = []
    failed: list[str] = []
    for target in targets:
        if is_telegram_target(hass, target):
            out_title, out_message = escape_markdown_v2(title), escape_markdown_v2(message)
        else:
            out_title, out_message = title, message
        try:
            await hass.services.async_call(
                "notify",
                "send_message",
                {"entity_id": target, "title": out_title, "message": out_message},
                blocking=blocking,
            )
            ok.append(target)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Notification to %s failed: %s", target, err)
            failed.append(f"{target}: {err}")
    return ok, failed
