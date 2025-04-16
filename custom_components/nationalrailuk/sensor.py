"""Platform for sensor integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import time

import async_timeout

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .client import NationalRailClient
from .const import (
    CONF_DESTINATIONS,
    CONF_STATION,
    CONF_TOKEN,
    DOMAIN,
    HIGH_FREQUENCY_REFRESH,
    POLLING_INTERVALE,
    REFRESH,
)
from .stations import STATION_MAP
from .config_flow import get_global_api_token

_LOGGER = logging.getLogger(__name__)



async def async_setup_entry(hass, entry, async_add_entities):
    """Config entry example."""
    # Get the API token - either from global entry or this entry
    token = None
    
    _LOGGER.debug("Setting up National Rail UK sensor for station %s", entry.data.get(CONF_STATION))
    
    # Check if token exists in this entry
    if CONF_TOKEN in entry.data:
        token = entry.data.get(CONF_TOKEN)
        _LOGGER.info("Using token from current entry for station %s", entry.data.get(CONF_STATION))
    else:
        # Look for a token-only entry
        token_found = False
        for config_entry in hass.config_entries.async_entries(DOMAIN):
            if (CONF_TOKEN in config_entry.data and 
                CONF_STATION not in config_entry.data):
                token = config_entry.data.get(CONF_TOKEN)
                _LOGGER.info("Found token in token-only entry for station %s", entry.data.get(CONF_STATION))
                token_found = True
                break
        
        # If no token found in entries, check temporary storage
        if not token_found:
            temp_key = f"{DOMAIN}_token_to_save"
            if temp_key in hass.data and hass.data.get(temp_key):
                token = hass.data.get(temp_key)
                _LOGGER.info("Using token from temporary storage for station %s", entry.data.get(CONF_STATION))
                
                # Create a new token-only entry if we have a token
                if token:
                    _LOGGER.info("Creating token-only entry with token from temporary storage")
                    try:
                        result = await hass.config_entries.flow.async_init(
                            DOMAIN,
                            context={"source": "system"},
                            data={CONF_TOKEN: token}
                        )
                        if result and result.get("type") == "create_entry":
                            _LOGGER.info("Successfully created token-only entry")
                            # Clear temporary storage
                            hass.data[temp_key] = None
                        else:
                            _LOGGER.warning("Failed to create token-only entry: %s", result)
                    except Exception as err:
                        _LOGGER.error("Error creating token-only entry: %s", err)
    
    # If still no token, try the helper function as a backup
    if not token:
        token = await get_global_api_token(hass)
        if token:
            _LOGGER.info("Found token using helper function for station %s", entry.data.get(CONF_STATION))
    
    if not token:
        _LOGGER.error("No API token available for station %s", entry.data.get(CONF_STATION))
        return False

    station = entry.data.get(CONF_STATION)
    
    # Handle destinations - may be a string or a list
    destinations = entry.data.get(CONF_DESTINATIONS, "")
    if isinstance(destinations, list):
        # Convert list to string for backward compatibility
        destinations = ",".join(destinations)

    _LOGGER.info("Setting up sensor for %s to %s", station, destinations)

    coordinator = NationalRailScheduleCoordinator(hass, token, station, destinations)
    
    try:
        await coordinator.my_api.setup_client()  # Make sure this is here
        await coordinator.async_config_entry_first_refresh()
        async_add_entities([NationalRailSchedule(coordinator)])
        return True
    except Exception as err:
        _LOGGER.exception("Error setting up sensor: %s", err)
        return False


class NationalRailScheduleCoordinator(DataUpdateCoordinator):
    description: str = None
    friendly_name: str = None
    sensor_name: str = None

    def __init__(self, hass, token, station, destinations):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name=DOMAIN,
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(minutes=REFRESH),
        )
        # Parse destinations if needed
        if isinstance(destinations, str):
            destinations = destinations.split(",") if destinations else []
            # Clean up any whitespace
            destinations = [d.strip() for d in destinations if d.strip()]
            
        self.station = station
        self.destinations = destinations
        self.my_api = NationalRailClient(token, station, destinations)

        self.last_data_refresh = None
        
        # Get friendly names from station map
        self.station_name = STATION_MAP.get(station, station)
        self.destination_names = [STATION_MAP.get(d, d) for d in destinations]

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        # check whether we should refresh the data or not
        if (
            self.last_data_refresh is None
            or (time.time() - self.last_data_refresh) > POLLING_INTERVALE * 60
            or (
                self.data is not None
                and self.data.get("next_train_scheduled") is not None
                and datetime.now(self.data["next_train_scheduled"].tzinfo)
                >= (
                    self.data["next_train_scheduled"]
                    - timedelta(minutes=HIGH_FREQUENCY_REFRESH)
                )
                and not self.data.get("next_train_expected") == "Cancelled"
            )
        ):
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            async with async_timeout.timeout(30):
                try:
                    data = await self.my_api.async_get_data()
                    self.last_data_refresh = time.time()
                except Exception as err:
                    _LOGGER.error("Error getting data: %s", err)
                    # Try to refresh token if possible
                    token = await get_global_api_token(self.hass)
                    if token and token != self.my_api.api_token:
                        _LOGGER.info("Refreshing API token and retrying request")
                        self.my_api.api_token = token
                        try:
                            data = await self.my_api.async_get_data()
                            self.last_data_refresh = time.time()
                        except Exception as err2:
                            _LOGGER.error("Second attempt failed: %s", err2)
                            raise
                    else:
                        raise

            if self.sensor_name is None:
                self.sensor_name = f"train_schedule_{self.station}{'_' + '_'.join(self.destinations) if len(self.destinations) >0 else ''}"

            if self.description is None:
                self.description = (
                    f"Departing trains schedule at {data['station']} station"
                )

            if self.friendly_name is None:
                self.friendly_name = f"Train schedule at {data['station']} station"
                if len(self.destinations) == 1:
                    self.friendly_name += f" for {self.destination_names[0]}"
                elif len(self.destinations) > 1:
                    self.friendly_name += f" for {'&'.join(self.destination_names)}"

            data["name"] = self.sensor_name
            data["description"] = self.description
            data["friendly_name"] = self.friendly_name

            # TODO: should have separate `next_train`s for each destination monitored
            data["next_train_scheduled"] = None
            data["next_train_expected"] = None
            data["destinations"] = None
            data["terminus"] = None
            data["platform"] = None
            data["perturbations"] = False

            for each in data["trains"]:
                if data["next_train_scheduled"] is None and not (
                    (
                        isinstance(each["expected"], str)
                        and each["expected"] == "Cancelled"
                    )
                    or (
                        len(each["destinations"]) > 0
                        and isinstance(
                            each["destinations"][0]["time_at_destination"], str
                        )
                        and each["destinations"][0]["time_at_destination"]
                        == "Cancelled"
                    )
                ):
                    data["next_train_scheduled"] = each["scheduled"]
                    data["next_train_expected"] = each["expected"]
                    data["destinations"] = each["destinations"]
                    data["terminus"] = each["terminus"]
                    data["platform"] = each["platform"]

                data["perturbations"] = data["perturbations"] or each["perturbation"]

        else:
            data = self.data

        return data


class NationalRailSchedule(CoordinatorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """

    attribution = "This uses National Rail Darwin Data Feeds"

    def __init__(self, coordinator):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self.entity_id = f"sensor.{coordinator.data['name'].lower()}"

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self.coordinator.data

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID."""
        return self.coordinator.data["name"]

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.coordinator.data["next_train_expected"]