"""The National Rail UK integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

# Import necessary constants and classes FROM your component files
from .client import NationalRailClient, NationalRailClientInvalidToken # Add exception if needed
from .const import (
    DOMAIN,
    CONF_STATION,
    CONF_DESTINATIONS,
    CONF_TOKEN,
    CONF_MONITOR_TRAIN,
    PLATFORMS, # ["sensor"]
)
from .config_flow import get_global_api_token # Assuming this helper exists
# Import the Coordinator class DEFINITION from sensor.py (or coordinator.py)
from .sensor import NationalRailScheduleCoordinator
from .stations import STATION_MAP

_LOGGER = logging.getLogger(__name__)

# --- Your Migration Function (IF NEEDED) ---
async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating %s from version %s", config_entry.entry_id, config_entry.version)

    if config_entry.version == 1:
        # Add token to hass.data for later use if it's a station entry
        if CONF_STATION in config_entry.data and CONF_TOKEN in config_entry.data:
             token = config_entry.data[CONF_TOKEN]
             # Use a specific key unlikely to clash
             hass.data[f"{DOMAIN}_migrated_token_{config_entry.entry_id}"] = token
             _LOGGER.debug("Stored token from %s in temporary storage", config_entry.entry_id)
             # Remove token from entry data
             new_data = {k: v for k, v in config_entry.data.items() if k != CONF_TOKEN}
             hass.config_entries.async_update_entry(config_entry, data=new_data, version=2)
             _LOGGER.info("Removed token from station entry %s during migration to v2", config_entry.entry_id)
        else:
             # Just update version for token-only entries or entries without tokens
             hass.config_entries.async_update_entry(config_entry, version=2)
             _LOGGER.info("Updated entry %s to v2 (no token migration needed)", config_entry.entry_id)

        _LOGGER.info("Migration to version 2 successful for entry %s", config_entry.entry_id)
        return True
    # Add migrations for other versions if needed

    _LOGGER.debug("No migration needed for %s version %s", config_entry.entry_id, config_entry.version)
    return True # Return True if no migration needed for this version


# --- Corrected Setup Entry Function ---
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up National Rail UK from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # --- Handle Token-Only Entry (Validation) ---
    # If it's just a token entry, validate it and we're done with *this specific entry*.
    # Another entry will handle the station.
    if CONF_TOKEN in entry.data and CONF_STATION not in entry.data:
        _LOGGER.info("Setting up Token-Only entry: %s", entry.entry_id)
        token = entry.data[CONF_TOKEN]
        try:
            # Basic validation: Can we create a client and maybe make a test call?
            # Using a common station like PAD for validation is okay.
            client = NationalRailClient(token, "PAD", [])
            await client.setup_client() # Ensure session exists if needed by client
            # Optional: Make a lightweight test API call if available, otherwise just client creation is a basic check.
            # await client.async_get_data() # This might be too heavy, depends on API
            _LOGGER.info("Token-Only entry %s validated successfully.", entry.entry_id)
            # No need to store anything specific for this entry, get_global_api_token will find it.
            # No platforms to forward to for this entry type.
            return True
        except NationalRailClientInvalidToken:
            _LOGGER.error("Invalid API token provided in Token-Only entry %s.", entry.entry_id)
            return False # Fail setup for this entry
        except Exception as err:
            _LOGGER.warning("Error validating token for entry %s: %s. Assuming token might be valid.", entry.entry_id, err)
            # Allow setup to continue, maybe API issue is temporary. Sensor setup might fail later.
            return True # Proceed cautiously

    # --- Handle Station Entry (Coordinator Setup) ---
    if CONF_STATION not in entry.data:
        _LOGGER.error("Entry %s is not a Token-Only entry but is missing station data.", entry.entry_id)
        return False # Cannot proceed without station

    station = entry.data[CONF_STATION]
    _LOGGER.info("Setting up Station entry for %s: %s", station, entry.entry_id)

    # --- Token Acquisition Logic (Coordinator Needs It) ---
    token = None
    # Try migrated token first (clean up after use)
    migrated_token_key = f"{DOMAIN}_migrated_token_{entry.entry_id}"
    if migrated_token_key in hass.data:
        token = hass.data.pop(migrated_token_key) # Use and remove
        _LOGGER.info("Using migrated token for station %s", station)

    # If no migrated token, find global token
    if not token:
        token = await get_global_api_token(hass) # Use helper to find token-only entry

    if not token:
        _LOGGER.error("No API token available (checked migration and global) for station %s. Cannot set up entry.", station)
        raise ConfigEntryNotReady(f"No API token found for National Rail UK entry {entry.title} ({station})")
    # --- End Token Acquisition ---

    # --- Destinations Handling ---
    destinations_input = entry.data.get(CONF_DESTINATIONS, "")
    destinations_list = []
    if isinstance(destinations_input, str):
        if destinations_input:
            destinations_list = [d.strip() for d in destinations_input.split(",") if d.strip()]
    elif isinstance(destinations_input, list):
        destinations_list = [d.strip() for d in destinations_input if isinstance(d, str) and d.strip()]
    # --- End Destinations Handling ---

    _LOGGER.info("Setting up coordinator for %s to %s", station, ', '.join(destinations_list) if destinations_list else "all destinations")

    # --- Coordinator Creation and Initial Refresh ---
    coordinator = NationalRailScheduleCoordinator(hass, token, station, destinations_list)

    try:
        await coordinator.my_api.setup_client() # Setup underlying client if needed
        await coordinator.async_config_entry_first_refresh()

        # Check for issues after refresh
        if coordinator.data is None and not coordinator.last_update_success:
             _LOGGER.error("Coordinator failed initial refresh for %s. Setup cannot proceed.", station)
             raise ConfigEntryNotReady(f"Coordinator initial refresh failed for {station}")
        if coordinator.data is None and coordinator.last_update_success:
             _LOGGER.warning("Coordinator initial refresh for %s succeeded but data is None. Proceeding cautiously.", station)

    except UpdateFailed as err:
         _LOGGER.error("Initial data update failed during setup for station %s: %s", station, err)
         raise ConfigEntryNotReady(f"Initial data update failed for {station}") from err
    except Exception as err:
        _LOGGER.exception("Unexpected error setting up coordinator for station %s: %s", station, err)
        raise ConfigEntryNotReady(f"Unexpected error during coordinator setup for {station}") from err
    # --- End Coordinator Creation ---

    # --- Store the COORDINATOR object ---
    hass.data[DOMAIN][entry.entry_id] = coordinator
    _LOGGER.debug("Stored coordinator for entry %s under hass.data[%s][%s]", entry.entry_id, DOMAIN, entry.entry_id)

    # --- Forward Setup to Platforms (e.g., sensor) ---
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Optional: Add update listener if needed for options flow, etc.
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True # Indicate successful setup of the integration entry

# --- Corrected Unload Function ---
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # If it's a station entry, unload platforms and remove coordinator
    if CONF_STATION in entry.data:
        _LOGGER.debug("Unloading platforms for station entry %s", entry.entry_id)
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unload_ok:
            coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
            if coordinator:
                 _LOGGER.debug("Removed coordinator for %s from hass.data", entry.entry_id)
                 # Optional coordinator cleanup (e.g., close client session)
                 if hasattr(coordinator.my_api, 'close_session') and callable(coordinator.my_api.close_session):
                     # await coordinator.my_api.close_session() # Example if needed
                     pass
            else:
                 _LOGGER.debug("Coordinator for %s already removed or not found during unload.", entry.entry_id)
        return unload_ok
    else:
        # For token-only entries, just confirm unload (nothing stored in hass.data[DOMAIN][entry.entry_id])
        _LOGGER.debug("Unloading token-only entry %s", entry.entry_id)
        # Remove any potential migration data if it wasn't cleaned up
        hass.data.pop(f"{DOMAIN}_migrated_token_{entry.entry_id}", None)
        return True

# --- Update Listener ---
async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Update listener called for %s", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
