"""The National Rail UK integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .client import NationalRailClient, NationalRailClientInvalidToken
from .const import CONF_STATION, CONF_TOKEN, DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        # Check if this entry has a token
        if CONF_TOKEN in config_entry.data:
            token = config_entry.data[CONF_TOKEN]
            
            try:
                # If this is a station entry, update to remove token
                if CONF_STATION in config_entry.data:
                    # Add token to hass.data for later use
                    hass.data[f"{DOMAIN}_token_to_save"] = token
                    _LOGGER.debug("Stored token in temporary storage for later use: %s...", token[:5])
                    
                    # Remove token from entry
                    new_data = {k: v for k, v in config_entry.data.items() if k != CONF_TOKEN}
                    hass.config_entries.async_update_entry(
                        config_entry, data=new_data, version=2
                    )
                    _LOGGER.info("Removed token from station entry during migration")
                else:
                    # Just a token entry, update version
                    hass.config_entries.async_update_entry(
                        config_entry, version=2
                    )
            except Exception as ex:
                _LOGGER.exception("Failed during migration: %s", ex)
                return False
        else:
            # No token in this entry, just update version
            hass.config_entries.async_update_entry(
                config_entry, version=2
            )
        
        _LOGGER.info(
            "Migration to version %s successful for entry %s", 
            2,  # Explicitly note version 2
            config_entry.entry_id
        )
        return True

    return False

async def async_setup_services(hass):
    """Set up services for National Rail integration."""
    
    async def migrate_all_entries(service):
        """Migrate all entries to use global token."""
        # Find all station entries with tokens
        station_entries = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            if CONF_STATION in entry.data and CONF_TOKEN in entry.data:
                station_entries.append(entry)
        
        if not station_entries:
            _LOGGER.info("No station entries with tokens found")
            return
        
        # Extract the token from the first entry
        token = station_entries[0].data[CONF_TOKEN]
        
        # Store token for later config flow
        hass.data[f"{DOMAIN}_token_to_save"] = token
        _LOGGER.debug("Stored token in temporary storage: %s...", token[:5])
        
        # Remove token from all station entries
        for entry in station_entries:
            new_data = {k: v for k, v in entry.data.items() if k != CONF_TOKEN}
            hass.config_entries.async_update_entry(
                entry, data=new_data
            )
            _LOGGER.info("Removed token from entry %s", entry.entry_id)
    
    hass.services.async_register(
        DOMAIN, "migrate_tokens", migrate_all_entries
    )    
    
    return True

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the National Rail component."""
    hass.data.setdefault(DOMAIN, {})
    
    # Register services
    await async_setup_services(hass)
    
    # Create a simple token storage key if it doesn't exist
    hass.data.setdefault(f"{DOMAIN}_token_to_save", None)
    
    # Automatically run migration for existing entries
    async def migrate_existing_entries():
        """Find and migrate existing entries with tokens."""
        token_found = False
        station_entries = []
        
        for entry in hass.config_entries.async_entries(DOMAIN):
            if CONF_STATION in entry.data and CONF_TOKEN in entry.data:
                station_entries.append(entry)
                
                # Save the first token we find
                if not token_found:
                    token = entry.data[CONF_TOKEN]
                    hass.data[f"{DOMAIN}_token_to_save"] = token
                    token_found = True
                    _LOGGER.info("Found token to save for later: %s...", token[:5])
                
                # Remove token from entry
                new_data = {k: v for k, v in entry.data.items() if k != CONF_TOKEN}
                hass.config_entries.async_update_entry(
                    entry, data=new_data
                )
                _LOGGER.info("Auto-migrated token from entry %s", entry.entry_id)
        
        # If we found a token, create a token-only entry
        if token_found and token:
            try:
                # Check if a token-only entry already exists
                token_entry_exists = False
                for entry in hass.config_entries.async_entries(DOMAIN):
                    if CONF_TOKEN in entry.data and CONF_STATION not in entry.data:
                        token_entry_exists = True
                        _LOGGER.info("Token-only entry already exists, updating it")
                        # Update the token if it's different
                        if entry.data[CONF_TOKEN] != token:
                            new_data = dict(entry.data)
                            new_data[CONF_TOKEN] = token
                            hass.config_entries.async_update_entry(
                                entry, data=new_data
                            )
                        break
                
                if not token_entry_exists:
                    _LOGGER.info("Creating token-only entry")
                    try:
                        result = await hass.config_entries.flow.async_init(
                            DOMAIN,
                            context={"source": "system"},  # Now properly handled in config_flow.py
                            data={CONF_TOKEN: token}
                        )
                        
                        if result:
                            _LOGGER.info("Flow initiated with type: %s", result["type"])
                            # The system step in config_flow will handle the entry creation
                            if result["type"] == "create_entry":
                                _LOGGER.info("Successfully created token-only entry")
                                # Clear temporary storage since we've created an entry
                                hass.data[f"{DOMAIN}_token_to_save"] = None
                    except Exception as err:
                        _LOGGER.error("Error during flow init: %s", err)
            except Exception as err:
                _LOGGER.error("Error creating token-only entry: %s", err)
    
    # Run migration as a background task
    hass.async_create_task(migrate_existing_entries())
    
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up National Rail UK from a config entry."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
    
    # Store the entry in hass.data
    hass.data[DOMAIN][entry.entry_id] = entry
    
    # If this is an API token entry (no station info), validate the token
    if CONF_TOKEN in entry.data and CONF_STATION not in entry.data:
        try:
            client = NationalRailClient(entry.data[CONF_TOKEN], "PAD", [])
            await client.setup_client()  # Make sure client is set up
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