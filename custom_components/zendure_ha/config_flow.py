"""Config flow for Zendure Integration integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.persistent_notification import async_create as async_create_notification
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .api import Api
from .const import (
    CONF_APPTOKEN,
    CONF_AUTO_MQTT_USER,
    CONF_INFLUX_BUCKET,
    CONF_INFLUX_ENABLE,
    CONF_INFLUX_ORG,
    CONF_INFLUX_TOKEN,
    CONF_INFLUX_URL,
    CONF_MQTTLOCAL,
    CONF_MQTTLOG,
    CONF_MQTTPORT,
    CONF_MQTTPSW,
    CONF_MQTTSERVER,
    CONF_MQTTUSER,
    CONF_P1METER,
    CONF_SIM,
    CONF_NOTIFY_TARGETS,
    CONF_TELEGRAM_ENTITY_ID,
    CONF_WIFIPSW,
    CONF_WIFISSID,
    DOMAIN,
)
from .manager import ZendureConfigEntry
from .notifications import async_notify_targets

_LOGGER = logging.getLogger(__name__)


class ZendureConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zendure Integration."""

    VERSION = 1
    MINOR_VERSION = 7
    _input_data: dict[str, Any]
    data_schema = vol.Schema(
        {
            vol.Required(CONF_APPTOKEN): str,
            vol.Required(CONF_P1METER, description={"suggested_value": "sensor.power_actual"}): selector.EntitySelector(),
            vol.Required(CONF_MQTTLOG): bool,
            vol.Required(CONF_MQTTLOCAL): bool,
        }
    )
    mqtt_schema = vol.Schema(
        {
            vol.Required(CONF_MQTTSERVER): str,
            vol.Required(CONF_MQTTPORT, default=1883): int,
            vol.Required(CONF_MQTTUSER): str,
            vol.Optional(CONF_MQTTPSW): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD,
                ),
            ),
            vol.Optional(CONF_AUTO_MQTT_USER, default=False): bool,
            vol.Optional(CONF_WIFISSID): str,
            vol.Optional(CONF_WIFIPSW): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD,
                ),
            ),
        }
    )

    def __init__(self) -> None:
        """Initialize."""
        self._user_input: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step when user initializes a integration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._user_input = user_input

            try:
                if await Api.Connect(self.hass, self._user_input, False) is None:
                    errors["base"] = "invalid input"
                else:
                    localmqtt = user_input[CONF_MQTTLOCAL]
                    if localmqtt:
                        return await self.async_step_local()

                    await self.async_set_unique_id("Zendure", raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title="Zendure", data=self._user_input)

            except Exception as err:  # pylint: disable=broad-except
                errors["base"] = f"invalid input {err}"

        return self.async_show_form(step_id="user", data_schema=self.data_schema, errors=errors)

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_MQTTSERVER, None) is not None:
            try:
                self._user_input = self._user_input | user_input if self._user_input else user_input
                if await Api.Connect(self.hass, self._user_input, False) is None:
                    errors["base"] = "invalid input"
            except Exception as err:  # pylint: disable=broad-except
                errors["base"] = f"invalid input {err}"
            else:
                await self.async_set_unique_id("Zendure", raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Zendure", data=self._user_input)

        return self.async_show_form(step_id="local", data_schema=self.mqtt_schema, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add reconfigure step to allow to reconfigure a config entry."""
        errors: dict[str, str] = {}

        entry = self._get_reconfigure_entry()
        schema = self.data_schema
        if user_input is not None:
            self._user_input = self._user_input | user_input
            use_mqtt = user_input.get(CONF_MQTTLOCAL, False)
            if use_mqtt:
                schema = self.mqtt_schema
            else:
                try:
                    if await Api.Connect(self.hass, self._user_input, False) is None:
                        errors["base"] = "invalid input"
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.error("Unexpected exception: %s", err)
                    errors["base"] = f"invalid input {err}"
                else:
                    await self.async_set_unique_id("Zendure", raise_on_progress=False)
                    self._abort_if_unique_id_mismatch()

                    return self.async_update_reload_and_abort(entry, data=self._user_input)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                data_schema=schema,
                suggested_values=entry.data | (user_input or {}),
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ZendureConfigEntry) -> ZendureOptionsFlowHandler:
        """Get the options flow for this handler."""
        return ZendureOptionsFlowHandler()


class ZendureOptionsFlowHandler(OptionsFlow):
    """Handles the options flow."""

    _pending: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options form (selector for notification targets, etc.)."""
        if user_input is not None:
            # Stash the chosen options, then offer Test / Save buttons.
            self._pending = dict(self.config_entry.data) | user_input
            return await self.async_step_confirm()

        # Default: keep current targets; migrate a legacy single Telegram entity.
        current_targets = self.config_entry.data.get(CONF_NOTIFY_TARGETS)
        if not current_targets:
            legacy = self.config_entry.data.get(CONF_TELEGRAM_ENTITY_ID)
            current_targets = [legacy] if legacy else []

        options_schema = vol.Schema(
            {
                vol.Required(CONF_P1METER, default=self.config_entry.data.get(CONF_P1METER, "sensor.power_actual")): str,
                vol.Required(CONF_MQTTLOG, default=self.config_entry.data.get(CONF_MQTTLOG, False)): bool,
                vol.Optional(CONF_AUTO_MQTT_USER, default=self.config_entry.data.get(CONF_AUTO_MQTT_USER, False)): bool,
                vol.Optional(CONF_SIM, default=self.config_entry.data.get(CONF_SIM, False)): bool,
                vol.Optional(CONF_NOTIFY_TARGETS, default=current_targets): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="notify", multiple=True)
                ),
                vol.Optional(CONF_INFLUX_ENABLE, default=self.config_entry.data.get(CONF_INFLUX_ENABLE, False)): bool,
                vol.Optional(CONF_INFLUX_URL, default=self.config_entry.data.get(CONF_INFLUX_URL, "")): str,
                vol.Optional(CONF_INFLUX_ORG, default=self.config_entry.data.get(CONF_INFLUX_ORG, "")): str,
                vol.Optional(CONF_INFLUX_TOKEN, default=self.config_entry.data.get(CONF_INFLUX_TOKEN, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_INFLUX_BUCKET, default=self.config_entry.data.get(CONF_INFLUX_BUCKET, "HA_ZENDURE")): str,
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)

    async def async_step_confirm(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer buttons after selection: test notify targets, test InfluxDB, or save."""
        return self.async_show_menu(step_id="confirm", menu_options=["test", "test_influx", "save"])

    async def async_step_test(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Send a test to every selected target, report per-target, return to the menu."""
        targets = list(self._pending.get(CONF_NOTIFY_TARGETS, []) or [])
        if not targets:
            async_create_notification(self.hass, "No notification target selected.", "Zendure - Test notification", "zendure_ha")
            return await self.async_step_confirm()

        ok, failed = await async_notify_targets(
            self.hass,
            targets,
            "Zendure",
            "Zendure test notification - this target works.",
        )

        lines = []
        if ok:
            lines.append("OK: " + ", ".join(ok))
        if failed:
            lines.append("FAILED:\n- " + "\n- ".join(failed))
        async_create_notification(self.hass, "\n".join(lines), "Zendure - Test notification", "zendure_ha")
        return await self.async_step_confirm()

    async def async_step_test_influx(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Write a test point to InfluxDB and report the exact result, then return to the menu."""
        url = self._pending.get(CONF_INFLUX_URL)
        token = self._pending.get(CONF_INFLUX_TOKEN)
        if not url or not token:
            async_create_notification(self.hass, "URL ou jeton InfluxDB manquant.", "Zendure - Test InfluxDB", "zendure_ha")
            return await self.async_step_confirm()

        from .influx import ZendureInflux

        bucket = self._pending.get(CONF_INFLUX_BUCKET) or "HA_ZENDURE"
        writer = ZendureInflux(self.hass, url, self._pending.get(CONF_INFLUX_ORG, ""), token, bucket)
        ok, detail = await writer.test()
        async_create_notification(
            self.hass,
            ("OK - " if ok else "ECHEC - ") + detail + f"\n(org: {self._pending.get(CONF_INFLUX_ORG, '')}, bucket: {bucket})",
            "Zendure - Test InfluxDB",
            "zendure_ha",
        )
        return await self.async_step_confirm()

    async def async_step_save(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Persist the pending options."""
        self.hass.config_entries.async_update_entry(self.config_entry, data=self._pending)
        return self.async_create_entry(title="", data=self._pending)


class ZendureConnectionError(HomeAssistantError):
    """Error to indicate there is a connection issue with Zendure Integration."""

    def __init__(self) -> None:
        """Initialize the connection error."""
        super().__init__("Zendure Integration")
