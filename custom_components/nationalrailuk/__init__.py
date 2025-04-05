"""The National Rail UK integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .client import NationalRailClient, NationalRailClientInvalidToken
from .const import CONF_STATION, CONF_TOKEN, DOMAIN, PLATFORMS
from .config_flow import get_global_api_token

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        # Check if this entry has a token
        if CONF_TOKEN in config_entry.data:
            token = config_entry.data[CONF_TOKEN]
            
            # Create a global token entry if needed
            try:
                from .config_flow import ensure_global_token_entry
                await ensure_global_token_entry(hass, token)
                _LOGGER.info("Ensured global API token exists during migration")
            except Exception as ex:
                _LOGGER.exception("Failed to create global token during migration: %s", ex)
            
            # If this is a station entry, update to remove token
            if CONF_STATION in config_entry.data:
                new_data = {k: v for k, v in config_entry.data.items() if k != CONF_TOKEN}
                hass.config_entries.async_update_entry(
                    config_entry, data=new_data, version=2
                )
            else:
                # Just a token entry, update version
                hass.config_entries.async_update_entry(
                    config_entry, version=2
                )
        else:
            # No token in this entry, just update version
            hass.config_entries.async_update_entry(
                config_entry, version=2
            )
        
        _LOGGER.info(
            "Migration to version %s successful for entry %s", 
            config_entry.version, 
            config_entry.entry_id
        )
        return True

    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up National Rail UK from a config entry."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    
    # Store the entry in hass.data
    hass.data[DOMAIN][entry.entry_id] = entry
    
    # If this is an API token entry (no station info), validate the token
    if CONF_STATION not in entry.data and CONF_TOKEN in entry.data:
        try:
            client = NationalRailClient(entry.data[CONF_TOKEN], "PAD", [])
            await client.async_get_data()
            _LOGGER.info("Validated global API token for National Rail UK")
        except NationalRailClientInvalidToken:
            _LOGGER.error("Invalid global API token for National Rail UK")
            return False
        except Exception as err:
            _LOGGER.warning("Error validating API token: %s", err)
            # If there's another error, it might be due to API limits, we'll continue
        
        _LOGGER.info("Added global API token for National Rail UK")
        return True
    
    # Set up the sensor platform for station entries
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Only unload platforms for station entries
    if CONF_STATION in entry.data:
        if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
            # Remove the entry from hass.data
            if entry.entry_id in hass.data[DOMAIN]:
                hass.data[DOMAIN].pop(entry.entry_id)
            return unload_ok
        return False
    else:
        # For token entries, just remove from hass.data
        if entry.entry_id in hass.data[DOMAIN]:
            hass.data[DOMAIN].pop(entry.entry_id)
        return True