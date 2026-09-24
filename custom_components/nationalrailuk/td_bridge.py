"""Optional bridge from the Network Rail Window App's MQTT event stream."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


_LOGGER = logging.getLogger(__name__)
BRIDGE_KEY = "td_bridge_unsubscribe"
TD_EVENT = f"{DOMAIN}_td_observation"
MQTT_TOPIC = "nr_window/events"


async def async_setup_td_bridge(hass: HomeAssistant) -> None:
    """Subscribe once when MQTT is available; remain optional otherwise."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if BRIDGE_KEY in domain_data:
        return
    try:
        from homeassistant.components import mqtt

        @callback
        def message_received(message) -> None:
            try:
                payload: dict[str, Any] = json.loads(message.payload)
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring malformed Network Rail Window MQTT event")
                return
            if payload.get("area_id") != "Y1":
                return
            hass.bus.async_fire(TD_EVENT, payload)

        unsubscribe = await mqtt.async_subscribe(
            hass, MQTT_TOPIC, message_received, qos=1, encoding="utf-8"
        )
        domain_data[BRIDGE_KEY] = unsubscribe
        _LOGGER.info("Optional TD bridge subscribed to %s", MQTT_TOPIC)
    except Exception as err:
        # MQTT or the App is optional. Integration setup must continue normally.
        domain_data[BRIDGE_KEY] = None
        _LOGGER.info("Optional TD bridge unavailable; continuing without it: %s", err)

