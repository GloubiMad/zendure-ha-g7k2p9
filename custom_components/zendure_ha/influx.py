"""InfluxDB v2 exporter for the Zendure integration.

Writes a periodic snapshot of every manager/device parameter into a
dedicated InfluxDB v2 bucket (default "HA_ZENDURE"), so there is always a
permanent, queryable trace (Grafana). Pure line-protocol over the shared
aiohttp session — no extra dependency. Fire-and-forget: any InfluxDB outage
is logged and ignored, it never disturbs the power-control loop.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


def _esc_tag(value: str) -> str:
    """Escape a tag key/value per line protocol (backslash, space, comma, equals)."""
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _field(value: Any) -> str:
    """Render a single field value in line protocol."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{float(value)}"  # always float -> stable Influx field type
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def line(measurement: str, tags: dict[str, str], fields: dict[str, Any]) -> str | None:
    """Build one line-protocol record (no timestamp -> InfluxDB uses server time)."""
    rendered = ",".join(f"{k}={_field(v)}" for k, v in fields.items() if v is not None)
    if not rendered:
        return None
    prefix = measurement
    for k, v in tags.items():
        prefix += f",{_esc_tag(k)}={_esc_tag(v)}"
    return f"{prefix} {rendered}"


def mqtt_points(device_name: str, subtopic: str, payload: Any) -> list[str]:
    """Aplati un message MQTT BRUT en points line-protocol (mesure `zendure_mqtt`).

    Stocke TOUT (numériques ET textes) : les scalaires tels quels, les structures imbriquées
    (dicts/listes) sérialisées en JSON-string. Si le payload a une clé `properties` (cas courant
    des Hyper), on aplatit son contenu ; sinon on prend le dict de premier niveau. Tague par
    device + sous-topic. Retourne [] si rien d'exploitable.
    """
    src = payload.get("properties") if isinstance(payload, dict) and isinstance(payload.get("properties"), dict) else payload
    if not isinstance(src, dict):
        return []
    fields: dict[str, Any] = {}
    for k, v in src.items():
        if isinstance(v, bool):
            fields[str(k)] = v
        elif isinstance(v, (int, float)):
            fields[str(k)] = float(v)
        elif isinstance(v, str):
            # Type Influx STABLE : un même champ (ex. messageId) arrive tantôt en nombre, tantôt en
            # string "123" -> on coerce les strings numériques en float pour éviter le conflit de type
            # (422 "field type conflict: messageId is type string, already exists as float").
            try:
                fields[str(k)] = float(v)
            except ValueError:
                fields[str(k)] = v
        else:
            fields[str(k)] = json.dumps(v, default=str)
    p = line("zendure_mqtt", {"device": device_name, "topic": subtopic}, fields)
    return [p] if p else []


class ZendureInflux:
    """Minimal InfluxDB v2 writer (line protocol over the shared aiohttp session)."""

    def __init__(self, hass: HomeAssistant, url: str, org: str, token: str, bucket: str) -> None:
        """Initialize the writer with the connection settings."""
        self.hass = hass
        self.write_url = f"{url.rstrip('/')}/api/v2/write?org={org}&bucket={bucket}&precision=ms"
        self.headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "text/plain; charset=utf-8",
        }
        self._failed = 0

    async def write(self, points: list[str]) -> None:
        """POST a batch of line-protocol points. Never raises (fire-and-forget)."""
        body = "\n".join(p for p in points if p)
        if not body:
            return
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                self.write_url,
                data=body.encode("utf-8"),
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    if self._failed < 3:  # avoid log spam on a persistent misconfig
                        _LOGGER.warning("InfluxDB write failed (%s): %s", resp.status, (await resp.text())[:200])
                    self._failed += 1
                else:
                    self._failed = 0
        except Exception as err:  # pylint: disable=broad-except
            if self._failed < 3:
                _LOGGER.warning("InfluxDB write error: %s", err)
            self._failed += 1

    async def test(self) -> tuple[bool, str]:
        """Write one test point and return (ok, detail) — surfaces the real error for the config flow."""
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                self.write_url,
                data=b"zendure_test,source=config_flow value=1",
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()
                if resp.status < 400:
                    return True, f"OK (HTTP {resp.status}) - point de test ecrit."
                return False, f"HTTP {resp.status}: {text[:300]}"
        except Exception as err:  # pylint: disable=broad-except
            return False, f"{type(err).__name__}: {err}"
