from __future__ import annotations
from datetime import datetime, timedelta, time
import logging
import time as time_module # Use alias to avoid conflict with time class
import async_timeout
from homeassistant.core import HomeAssistant, callback, Context, Event
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed
)
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
    async_call_later
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.util import dt as dt_util

# Imports FROM your component
from .client import NationalRailClient
from .const import (
    DOMAIN,
    CONF_DESTINATIONS,
    CONF_STATION,
    HIGH_FREQUENCY_REFRESH,
    POLLING_INTERVALE,
    REFRESH,
    CONF_MONITOR_TRAIN,
    CONF_TARGET_TIME,
    CONF_TARGET_DESTINATION,
    CONF_TIME_WINDOW_MINUTES,
    CONF_MONITORED_TRAIN_NAME,
    DEFAULT_TIME_WINDOW
)
from .stations import STATION_MAP

_LOGGER = logging.getLogger(__name__)
# <<< CHANGE 1: Update Version >>>
COMPONENT_VERSION = "VER_26_EXTRA_FIELDS"
_LOGGER.info("NationalRailUK Sensor Platform Code - %s - Loaded", COMPONENT_VERSION)
# Consolidated error states from coordinator and monitored sensor
ERROR_STATES = [
    "Data Error",
    "Source Error",
    "Not Found",
    "No Trains Found",
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
    "Waiting for Data",
    "Waiting for Source", # Added from MonitoredTrainSensor init state
    "error",
    "Error",
    "Cancelled",
    "Cancelled (Final)",
    "Lost Track",
    "Departed",
    "All Cancelled/Invalid",
    "Initializing",
    "API Error", # From coordinator logic
    "Error: Setup Failed", # From MonitoredTrainSensor setup
    "Error: Listener Failed", # From MonitoredTrainSensor setup
    "Searching (Reset)", # New state after reset
]

# Define reset delay
RESET_DELAY_SECONDS = 3 * 60 * 60 # 3 hours

# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# COORDINATOR CLASS
# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
class NationalRailScheduleCoordinator(DataUpdateCoordinator):
    """Coordinator for fetching train schedules."""
    description: str | None = None
    friendly_name: str | None = None
    sensor_name: str | None = None

    def __init__(self, hass: HomeAssistant, token: str, station: str, destinations: list[str]):
        """Initialize my coordinator."""
        self.station = station
        self.destinations = destinations # Should be a list from config flow
        self.my_api = NationalRailClient(token, station, destinations)
        self.last_data_refresh: float | None = None
        self.station_name = STATION_MAP.get(station, station)
        self.destination_names = [STATION_MAP.get(d, d) for d in destinations]

        # Ensure destinations are sorted for consistent naming
        dest_suffix = ('_' + '_'.join(sorted(d.lower() for d in self.destinations))
                       if self.destinations else '')
        # Make sensor_name safe for entity_id
        self.sensor_name = f"train_schedule_{self.station.lower()}{dest_suffix}"
        self.description = f"Departing trains schedule at {self.station_name} station"
        self.friendly_name = f"Train schedule at {self.station_name} station"
        if len(self.destinations) == 1:
            self.friendly_name += f" for {self.destination_names[0]}"
        elif len(self.destinations) > 1:
            # Sort names for consistent display
            self.friendly_name += f" for {' & '.join(sorted(self.destination_names))}"

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{station}", # Use DOMAIN and station for coordinator name
            update_interval=timedelta(minutes=REFRESH), # Base interval
        )
        _LOGGER.debug("Coordinator initialized for %s. Sensor Name: %s", self.station_name, self.sensor_name)

    def _parse_time_safely(self, time_input: str | datetime | None) -> datetime | None:
        """Safely parse various time inputs into timezone-aware datetimes."""
        # _LOGGER.debug("Parsing time input: '%s' (Type: %s)", time_input, type(time_input).__name__) # DEBUG -> too verbose

        if time_input is None:
            # _LOGGER.debug("Input is None, returning None.")
            return None

        if isinstance(time_input, datetime):
            # _LOGGER.debug("Input is already datetime. Ensuring timezone.")
            # Ensure timezone-aware using local timezone if naive
            if time_input.tzinfo is None or time_input.tzinfo.utcoffset(time_input) is None:
                local_dt = dt_util.as_local(time_input)
                # _LOGGER.debug("Converted naive datetime to local: %s (Timezone: %s)", local_dt, local_dt.tzinfo)
                return local_dt
            # _LOGGER.debug("Input datetime is already timezone-aware: %s (Timezone: %s)", time_input, time_input.tzinfo)
            return time_input

        if isinstance(time_input, str) and time_input:
            # Handle specific non-time strings first
            if time_input.lower() in ["cancelled", "delayed", "on time", "no report", "no trains found", "all cancelled/invalid", "api error", "data error"]:
                 # _LOGGER.debug("Input is a status string ('%s'), returning None for time parsing.", time_input)
                 return None

            try:
                # Try parsing as ISO format first
                dt = dt_util.parse_datetime(time_input)
                if dt:
                    # _LOGGER.debug("Successfully parsed '%s' into datetime: %s (Timezone: %s)", time_input, dt, dt.tzinfo)
                    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                        local_dt = dt_util.as_local(dt)
                        # _LOGGER.debug("Converted naive parsed datetime to local: %s (Timezone: %s)", local_dt, local_dt.tzinfo)
                        return local_dt
                    return dt
                else:
                     # _LOGGER.debug("dt_util.parse_datetime returned None for string input '%s'. Trying HH:MM format.", time_input)
                     # Fall through to HH:MM parsing below
                     pass
            except (ValueError, TypeError) as e:
                _LOGGER.debug("Could not parse time string '%s' using dt_util.parse_datetime. Error: %s. Trying HH:MM format.", time_input, e)
                # Fall through to HH:MM parsing below
            except Exception as e: # Catch any other unexpected errors during datetime parse
                _LOGGER.exception("Unexpected error parsing time string '%s' with dt_util.parse_datetime: %s", time_input, e)
                # Try HH:MM parsing as a fallback

            # Try parsing as HH:MM[:SS]
            try:
                parsed_time = dt_util.parse_time(time_input) # Returns time object
                if parsed_time:
                    now_local = dt_util.now()
                    today = now_local.date()
                    naive_dt = datetime.combine(today, parsed_time)
                    local_dt = dt_util.as_local(naive_dt)

                    # Adjust day based on proximity to current time to handle past-midnight schedules
                    time_diff = local_dt - now_local
                    if time_diff < timedelta(hours=-12): # If it looks like it was > 12 hours ago, assume it's tomorrow
                         local_dt += timedelta(days=1)
                    elif time_diff > timedelta(hours=12): # If it looks like it's > 12 hours in future, assume it was yesterday
                         local_dt -= timedelta(days=1)

                    # _LOGGER.debug("Successfully parsed '%s' as HH:MM[:SS] into local datetime: %s", time_input, local_dt)
                    return local_dt
            except (ValueError, TypeError) as e2:
                _LOGGER.debug("Could not parse time string '%s' as HH:MM[:SS] either. Error: %s", time_input, e2)
                return None
            except Exception as e: # Catch any other unexpected errors during time parse
                _LOGGER.exception("Unexpected error parsing time string '%s' as HH:MM[:SS]: %s", time_input, e)
                return None

        elif isinstance(time_input, str) and not time_input:
             # _LOGGER.debug("Input is an empty string, returning None.")
             return None

        _LOGGER.warning("Unexpected type or format for time input: %s ('%s'). Returning None.", type(time_input).__name__, time_input)
        return None

    async def _async_update_data(self):
        """Fetch data from API endpoint based on refresh logic."""
        _LOGGER.debug("COORD [%s]: Starting _async_update_data. self.data is None: %s. last_update_success: %s",
              self.name, self.data is None, self.last_update_success)
        _LOGGER.debug("Coordinator checking for update for %s", self.station_name)
        now = time_module.time()
        now_dt = dt_util.now()

        should_fetch = False
        reason = ""

        # --- Determine if fetch is needed ---
        if self.last_data_refresh is None:
            should_fetch = True
            reason = "Initial data fetch"
        elif (now - self.last_data_refresh) >= POLLING_INTERVALE * 60:
            should_fetch = True
            reason = f"Normal polling interval ({POLLING_INTERVALE} min) elapsed"
        elif self.data and "next_train_scheduled" in self.data:
            next_sched_str = self.data.get("next_train_scheduled")
            next_expected_str = self.data.get("next_train_expected")
            next_scheduled_dt = self._parse_time_safely(next_sched_str)
            is_cancelled = isinstance(next_expected_str, str) and next_expected_str.lower() == "cancelled"
            if (next_scheduled_dt is not None
                and now_dt >= (next_scheduled_dt - timedelta(minutes=HIGH_FREQUENCY_REFRESH))
                and not is_cancelled):
                should_fetch = True
                reason = f"High-frequency condition met (train departs within {HIGH_FREQUENCY_REFRESH} min)"

        # --- Fetch Data (if needed) ---
        fetched_data = None
        if should_fetch:
            _LOGGER.info("Fetching new data for %s. Reason: %s", self.station_name, reason)
            try:
                async with async_timeout.timeout(30):
                    fetched_data = await self.my_api.async_get_data()
                    _LOGGER.debug("COORD [%s]: Raw fetched_data received (is None: %s). Keys: %s", self.name, fetched_data is None, fetched_data.keys() if fetched_data else "N/A")
                    self.last_data_refresh = now
                    _LOGGER.debug("Successfully fetched new data for %s (%s trains)",
                                  self.station_name, len(fetched_data.get("trains", [])) if fetched_data else 0)
            except Exception as err:
                 _LOGGER.error("Error during API data fetch for %s: %s", self.station_name, err)
                 if self.data is None:
                      raise UpdateFailed(f"Initial data fetch failed for {self.station_name}") from err
                 else:
                      _LOGGER.warning("Fetch failed for %s, returning previous data.", self.station_name)
                      self.data["api_error"] = f"Fetch Failed: {err}"
                      self.data["last_update"] = dt_util.now().isoformat()
                      return self.data # Return existing data on fetch failure (if available)
        else:
            # --- No Fetch Needed ---
            if self.data is None:
                 _LOGGER.warning("No cached data available for %s and no fetch condition met, forcing fetch.", self.station_name)
                 _LOGGER.debug("COORD [%s]: self.data is None, calling _async_update_data_force()", self.name)
                 return await self._async_update_data_force()
            else:
                 _LOGGER.debug("COORD [%s]: No fetch needed. Returning existing self.data. Keys: %s",
                                               self.name, self.data.keys() if self.data else "None")
                 return self.data # Return existing data

        # --- Handle Case Where Fetch Succeeded BUT Returned None ---
        if fetched_data is None:
             _LOGGER.warning("No new data received from fetch for %s, returning previous.", self.station_name)
             if self.data:
                 self.data["api_error"] = "Fetch Failed (No Data Received)"
                 self.data["last_update"] = dt_util.now().isoformat()
                 return self.data
             else:
                 # Return minimal error structure if NO data ever existed
                 return {
                    "name": self.sensor_name, "description": self.description, "friendly_name": self.friendly_name,
                    "station": self.station_name, "station_code": self.station, "trains": [],
                    "last_update": dt_util.now().isoformat(), "api_error": "Fetch Failed - No Data",
                    "next_train_scheduled": "API Error", "next_train_expected": "API Error",
                    "perturbations": False,
                    "nrcc_messages": None,
                 }

        # --- Process Fetched Data ---
        try: # <--- START TRY BLOCK
            processed_data = {
                "name": self.sensor_name,
                "description": self.description,
                "friendly_name": self.friendly_name,
                "station": fetched_data.get("station_name", self.station_name),
                "station_code": self.station,
                "trains": fetched_data.get("trains", []),
                "last_update": dt_util.now().isoformat(),
                "next_train_scheduled": None,
                "next_train_expected": None,
                "next_train_destination": None,
                "next_train_terminus": None,
                "next_train_platform": None,
                "perturbations": False,
                "api_error": fetched_data.get("error"),
                "nrcc_messages": None,
            }

            # Extract NRCC Messages
            nrcc_data = fetched_data.get("nrccMessages")
            messages_list = []
            if isinstance(nrcc_data, dict):
                message_entries = nrcc_data.get("message")
                if isinstance(message_entries, list):
                    for entry in message_entries:
                        if isinstance(entry, dict):
                             msg_text = entry.get("_value_1")
                             if isinstance(msg_text, str) and msg_text.strip():
                                 messages_list.append(msg_text.strip())
            processed_data["nrcc_messages"] = messages_list if messages_list else None

            # Handle API Error or No Trains
            if processed_data["api_error"]:
                _LOGGER.error("API error reported by client for %s: %s", self.station_name, processed_data["api_error"])
                processed_data["next_train_scheduled"] = "API Error"
                processed_data["next_train_expected"] = "API Error"
                processed_data["trains"] = []
            elif not processed_data["trains"]:
                _LOGGER.info("No trains found in data for %s.", self.station_name)
                processed_data["next_train_scheduled"] = "No Trains Found"
                processed_data["next_train_expected"] = "No Trains Found"
            else:
                # Find the first valid (non-cancelled, parsable time) train
                first_valid_train = None
                earliest_departure_dt = None

                for train in processed_data["trains"]:
                    expected_str_check = train.get("expected", "")
                    if isinstance(expected_str_check, str) and expected_str_check.lower() == "cancelled":
                        continue # Skip cancelled trains

                    processed_data["perturbations"] = processed_data["perturbations"] or train.get("perturbation", False)

                    scheduled_str = train.get("scheduled")
                    expected_str = train.get("expected") # Re-fetch for original case ('On time')

                    scheduled_dt = self._parse_time_safely(scheduled_str)
                    expected_dt = self._parse_time_safely(expected_str) # May be None

                    # Determine effective departure time
                    effective_departure_dt = None
                    if expected_dt:
                        effective_departure_dt = expected_dt
                    elif isinstance(expected_str, str) and expected_str == "On time" and scheduled_dt:
                        effective_departure_dt = scheduled_dt
                    elif scheduled_dt:
                         effective_departure_dt = scheduled_dt
                    else:
                         continue # Skip train if no effective time can be determined

                    # Check if this is the earliest valid train
                    if first_valid_train is None or effective_departure_dt < earliest_departure_dt:
                        first_valid_train = train
                        earliest_departure_dt = effective_departure_dt

                # Populate data based on the first valid train found
                if first_valid_train:
                    _LOGGER.debug("Selected first valid train: %s", first_valid_train.get("scheduled"))
                    scheduled_time = first_valid_train.get("scheduled")
                    expected_time = first_valid_train.get("expected") # Could be None

                    processed_data["next_train_scheduled"] = scheduled_time
                    # Fallback logic: If expected is None, use scheduled. Otherwise use expected.
                    processed_data["next_train_expected"] = expected_time if expected_time is not None else scheduled_time

                    processed_data["next_train_terminus"] = first_valid_train.get("terminus")
                    processed_data["next_train_platform"] = first_valid_train.get("platform")

                    dest_list = first_valid_train.get("destinations", [])
                    if dest_list and isinstance(dest_list, list) and len(dest_list) > 0:
                         first_dest_info = dest_list[0]
                         if isinstance(first_dest_info, dict):
                             processed_data["next_train_destination"] = first_dest_info.get("name")
                else:
                    # No valid trains found after checking all
                    _LOGGER.warning("No valid (non-cancelled, parsable time) trains found for %s.", self.station_name)
                    processed_data["next_train_scheduled"] = "All Cancelled/Invalid"
                    processed_data["next_train_expected"] = "All Cancelled/Invalid"

            _LOGGER.debug("Finished processing update for %s. Next Scheduled: %s, Next Expected: %s",
                          self.station_name, processed_data.get("next_train_scheduled"), processed_data.get("next_train_expected"))

            # Assign to internal cache and return successful data
            self.data = processed_data
            return processed_data

        except Exception as processing_err: # <--- START EXCEPT BLOCK
            _LOGGER.exception("CRITICAL: Error during data processing phase for %s after fetch: %s", self.station_name, processing_err)
            # If processing fails, return previous data if possible, otherwise raise UpdateFailed
            if self.data:
                _LOGGER.warning("Processing failed for %s, returning previous data due to error.", self.station_name)
                self.data["processing_error"] = f"Processing Failed: {processing_err}" # Add error marker
                self.data["last_update"] = dt_util.now().isoformat() # Mark attempt time
                return self.data # Return existing data
            else:
                _LOGGER.error("Processing failed for %s and no previous data exists.", self.station_name)
                # Raise UpdateFailed so the coordinator framework handles the failure state correctly
                raise UpdateFailed(f"Data processing failed for {self.station_name} after fetch") from processing_err
        # --- END EXCEPT BLOCK ---


    async def _async_update_data_force(self):
            """Force fetch data from API endpoint regardless of timing."""
            _LOGGER.info("Forcing data fetch for %s", self.station_name)
            fetched_data = None
            # --- Fetch Data ---
            try:
                async with async_timeout.timeout(30):
                    fetched_data = await self.my_api.async_get_data()
                    self.last_data_refresh = time_module.time()
                    _LOGGER.debug("Successfully fetched data during forced update for %s", self.station_name)
            except Exception as err:
                _LOGGER.error("Error during forced API data fetch for %s: %s", self.station_name, err)
                if self.data is None:
                    raise UpdateFailed(f"Forced fetch failed for {self.station_name} and no previous data exists") from err
                else:
                     _LOGGER.warning("Forced fetch failed for %s, returning previous data.", self.station_name)
                     self.data["api_error"] = f"Forced Fetch Failed: {err}"
                     self.data["last_update"] = dt_util.now().isoformat()
                     return self.data # Return previous data on fetch failure

            # --- Handle Case Where Fetch Succeeded BUT Returned None ---
            if fetched_data is None:
                 _LOGGER.error("Forced fetch returned None but did not raise error or return previous data.")
                 # Return minimal error structure if NO data ever existed (even previous self.data)
                 return {
                     "name": self.sensor_name, "description": self.description, "friendly_name": self.friendly_name,
                     "station": self.station_name, "station_code": self.station, "trains": [],
                     "last_update": dt_util.now().isoformat(), "api_error": "Forced Fetch Failed - Unknown",
                     "next_train_scheduled": "API Error", "next_train_expected": "API Error",
                     "perturbations": False,
                     "nrcc_messages": None, # <-- Ensure key exists even on error return
                  }

            # --- Process Fetched Data ---
            try: # <--- START TRY BLOCK FOR PROCESSING
                 _LOGGER.debug("Forced fetch successful, processing data.")
                 processed_data = {
                     "name": self.sensor_name, "description": self.description, "friendly_name": self.friendly_name,
                     "station": fetched_data.get("station_name", self.station_name), "station_code": self.station,
                     "trains": fetched_data.get("trains", []), "last_update": dt_util.now().isoformat(),
                     "next_train_scheduled": None, "next_train_expected": None, "next_train_destination": None,
                     "next_train_terminus": None, "next_train_platform": None, "perturbations": False,
                     "api_error": fetched_data.get("error"),
                     "nrcc_messages": None, # <-- Initialize the key here
                 }

                 # --- START: Added NRCC message extraction logic ---
                 nrcc_data = fetched_data.get("nrccMessages")
                 messages_list = []
                 if isinstance(nrcc_data, dict):
                     message_entries = nrcc_data.get("message")
                     if isinstance(message_entries, list):
                         for entry in message_entries:
                             if isinstance(entry, dict):
                                  msg_text = entry.get("_value_1")
                                  if isinstance(msg_text, str) and msg_text.strip():
                                      messages_list.append(msg_text.strip())
                 processed_data["nrcc_messages"] = messages_list if messages_list else None
                 _LOGGER.debug("FORCE: Extracted %d NRCC messages.", len(messages_list)) # Optional logging
                 # --- END: Added NRCC message extraction logic ---


                 # Handle API Error or No Trains
                 if processed_data["api_error"]:
                     _LOGGER.error("API error reported by client for %s during forced fetch: %s", self.station_name, processed_data["api_error"])
                     processed_data["next_train_scheduled"] = "API Error"
                     processed_data["next_train_expected"] = "API Error"
                     processed_data["trains"] = []
                 elif not processed_data["trains"]:
                     _LOGGER.info("No trains found in data for %s during forced fetch.", self.station_name)
                     processed_data["next_train_scheduled"] = "No Trains Found"
                     processed_data["next_train_expected"] = "No Trains Found"
                 else:
                     # Find the first valid train
                     first_valid_train = None
                     earliest_departure_dt = None
                     for train in processed_data["trains"]:
                         expected_str = train.get("expected", "")
                         if isinstance(expected_str, str) and expected_str.lower() == "cancelled":
                             continue

                         processed_data["perturbations"] = processed_data["perturbations"] or train.get("perturbation", False)
                         scheduled_str = train.get("scheduled")
                         expected_str_orig = train.get("expected")
                         scheduled_dt = self._parse_time_safely(scheduled_str)
                         expected_dt = self._parse_time_safely(expected_str_orig)

                         effective_departure_dt = None
                         if expected_dt: effective_departure_dt = expected_dt
                         elif isinstance(expected_str_orig, str) and expected_str_orig == "On time" and scheduled_dt: effective_departure_dt = scheduled_dt
                         elif scheduled_dt: effective_departure_dt = scheduled_dt
                         else: continue

                         if first_valid_train is None or effective_departure_dt < earliest_departure_dt:
                             first_valid_train = train
                             earliest_departure_dt = effective_departure_dt

                     # Populate data based on the first valid train found
                     if first_valid_train:
                         _LOGGER.debug("FORCE: Selected first valid train: %s", first_valid_train.get("scheduled"))
                         scheduled_time = first_valid_train.get("scheduled")
                         expected_time = first_valid_train.get("expected")

                         processed_data["next_train_scheduled"] = scheduled_time
                         # Fallback logic: If expected is None, use scheduled. Otherwise use expected.
                         processed_data["next_train_expected"] = expected_time if expected_time is not None else scheduled_time

                         processed_data["next_train_terminus"] = first_valid_train.get("terminus")
                         processed_data["next_train_platform"] = first_valid_train.get("platform")
                         dest_list = first_valid_train.get("destinations", [])
                         if dest_list and isinstance(dest_list, list) and len(dest_list) > 0 and isinstance(dest_list[0], dict):
                              processed_data["next_train_destination"] = dest_list[0].get("name")
                     else:
                         _LOGGER.warning("FORCE: No valid (non-cancelled, parsable time) trains found for %s.", self.station_name)
                         processed_data["next_train_scheduled"] = "All Cancelled/Invalid"
                         processed_data["next_train_expected"] = "All Cancelled/Invalid"

                 _LOGGER.debug("Finished processing forced update for %s. Next Scheduled: %s, Next Expected: %s, NRCC Msgs: %s",
                               self.station_name, processed_data.get("next_train_scheduled"),
                               processed_data.get("next_train_expected"), processed_data.get("nrcc_messages")) # Added NRCC to log

                 # Assign to internal cache and return successful data
                 _LOGGER.debug("COORD [%s]: Assigning processed_data to self.data. Processed data keys: %s",
                           self.name, processed_data.keys() if processed_data else "None")
                 self.data = processed_data
                 return processed_data

            except Exception as processing_err: # <--- START EXCEPT BLOCK FOR PROCESSING
                 _LOGGER.exception("CRITICAL: Error during data processing phase for %s after FORCE fetch: %s", self.station_name, processing_err)
                 # If processing fails, return previous data if possible, otherwise raise UpdateFailed
                 if self.data:
                     _LOGGER.warning("Processing after FORCE failed for %s, returning previous data due to error.", self.station_name)
                     self.data["processing_error"] = f"Processing Failed (Force): {processing_err}" # Add error marker
                     self.data["last_update"] = dt_util.now().isoformat() # Mark attempt time
                     # Ensure nrcc_messages key exists even when returning old data after processing failure
                     if "nrcc_messages" not in self.data:
                         self.data["nrcc_messages"] = None # Or perhaps retain the last known good value if preferred? Setting to None is safer.
                     return self.data # Return existing data
                 else:
                     _LOGGER.error("Processing after FORCE failed for %s and no previous data exists.", self.station_name)
                     # Raise UpdateFailed so the coordinator framework handles the failure state correctly
                     raise UpdateFailed(f"Data processing failed for {self.station_name} after force fetch") from processing_err
            # --- END EXCEPT BLOCK FOR PROCESSING ---

# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# PLATFORM SETUP FUNCTION
# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform from a ConfigEntry."""
    _LOGGER.info("SENSOR: Setting up entities for entry %s (%s)", entry.entry_id, entry.title)
    _LOGGER.info("SENSOR: Running async_setup_entry with code version %s", COMPONENT_VERSION)

    coordinator: NationalRailScheduleCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    if not coordinator:
        _LOGGER.error("SENSOR: Coordinator not found in hass.data for entry %s. Setup failed.", entry.entry_id)
        return

    _LOGGER.debug("SENSOR: Retrieved coordinator for entry %s", entry.entry_id)

    entities_to_add = []

    # --- Create the main Coordinator Sensor Entity ---
    try:
        if coordinator.sensor_name is None:
             _LOGGER.warning("Coordinator sensor_name is None at entity creation time for %s. Using fallback.", coordinator.station_name)
             # Coordinator __init__ now ensures sensor_name is set, but keep check just in case

        coordinator_sensor = NationalRailSchedule(coordinator)
        entities_to_add.append(coordinator_sensor)
        _LOGGER.debug("SENSOR: Created NationalRailSchedule entity for %s", coordinator.station_name)
    except Exception as e:
         _LOGGER.exception("SENSOR: Failed to initialize NationalRailSchedule sensor for %s: %s", coordinator.station_name, e)
         return

    # --- Create Monitored Sensor (If configured) ---
    if entry.data.get(CONF_MONITOR_TRAIN, False):
        _LOGGER.info("SENSOR: Setting up monitored train sensor for station %s", coordinator.station_name)

        target_time_str = entry.data.get(CONF_TARGET_TIME)
        target_destination = entry.data.get(CONF_TARGET_DESTINATION)
        time_window = entry.data.get(CONF_TIME_WINDOW_MINUTES, DEFAULT_TIME_WINDOW)
        name = entry.data.get(CONF_MONITORED_TRAIN_NAME)

        if not all([target_time_str, target_destination, name]):
             _LOGGER.error("SENSOR: Missing required config for monitored train (name, time, dest) for station %s. Skipping.", coordinator.station_name)
        else:
            try:
                time_parts = target_time_str.split(":")
                target_time = time(
                    hour=int(time_parts[0]),
                    minute=int(time_parts[1]),
                    second=int(time_parts[2]) if len(time_parts) > 2 else 0
                )
            except (ValueError, IndexError, TypeError) as e:
                _LOGGER.error("SENSOR: Invalid target time format '%s' for monitored train: %s. Skipping.", target_time_str, e)
            else:
                try:
                    _LOGGER.debug("SENSOR: Creating monitored train sensor '%s' linked to coordinator for %s", name, coordinator.station_name)
                    monitored_train = MonitoredTrainSensor(
                        hass,
                        name,
                        coordinator,
                        target_time,
                        target_destination.upper(),
                        timedelta(minutes=float(time_window))
                    )
                    entities_to_add.append(monitored_train)
                    _LOGGER.debug("SENSOR: Created MonitoredTrainSensor entity '%s'", name)
                except Exception as monitor_init_err:
                     _LOGGER.exception("SENSOR: Failed to initialize MonitoredTrainSensor object '%s': %s. Skipping.", name, monitor_init_err)

    # --- Add entities ---
    if entities_to_add:
         _LOGGER.debug("SENSOR: Adding %d entities for entry %s", len(entities_to_add), entry.entry_id)
         async_add_entities(entities_to_add)
    else:
         _LOGGER.warning("SENSOR: No entities were added for entry %s.", entry.entry_id)


# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# ENTITY CLASSES
# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

# --- NationalRailSchedule Entity (Main Coordinator Sensor) ---
class NationalRailSchedule(CoordinatorEntity, SensorEntity):
    """An entity showing the next train schedule, linked to the coordinator."""
    _attr_attribution = "Data provided by National Rail Darwin Data Feeds"
    _attr_icon = "mdi:train"

    def __init__(self, coordinator: NationalRailScheduleCoordinator):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        if not coordinator.sensor_name:
             _LOGGER.error("NationalRailSchedule init: Coordinator sensor_name is None! Using fallback ID.")
             fallback_id = f"train_schedule_{coordinator.station.lower()}_error_{self.config_entry.entry_id[:8]}"
             self._attr_unique_id = fallback_id
             self.entity_id = f"sensor.{fallback_id}"
             self._attr_name = f"Train Schedule {coordinator.station_name} (Error)"
        else:
             safe_sensor_name = coordinator.sensor_name.replace('-', '_')
             self.entity_id = f"sensor.{safe_sensor_name}"
             self._attr_unique_id = coordinator.sensor_name
             self._attr_name = coordinator.friendly_name

        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.station)},
            "name": f"National Rail {coordinator.station_name}",
            "manufacturer": "National Rail",
            "model": "Darwin Departures",
            "entry_type": "service",
            "configuration_url": "https://www.nationalrail.co.uk/",
        }
        _LOGGER.debug("NationalRailSchedule '%s' initialized. Entity ID: %s, Unique ID: %s", self._attr_name, self.entity_id, self._attr_unique_id)

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if self.coordinator.data:
            # Return a copy to prevent modification? CoordinatorEntity might handle this.
            return self.coordinator.data.copy()
        return None

    @property
    def native_value(self):
        """Return the state of the sensor.

        Prioritizes returning a datetime object for HA timestamp handling.
        - If expected is a valid time, return that datetime.
        - If expected is 'On time', return the scheduled datetime.
        - If expected is 'Delayed', return the scheduled datetime (best guess).
        - If expected is 'Cancelled' or other status, return that string.
        """
        entity_id_log = self.entity_id or self._attr_unique_id

        # Handle coordinator not ready
        if not self.coordinator.last_update_success and self.coordinator.data is None:
            _LOGGER.debug("%s native_value: Coordinator initial update failed or no data yet.", entity_id_log)
            return STATE_UNAVAILABLE # Or STATE_UNKNOWN

        coordinator_data = self.coordinator.data

        # Handle case where coordinator data is missing or empty
        if not coordinator_data:
            _LOGGER.warning("%s native_value: coordinator.data is None/Falsy. Last success: %s. Returning UNKNOWN.",
                           entity_id_log, self.coordinator.last_update_success)
            return STATE_UNKNOWN

        next_expected_str = coordinator_data.get("next_train_expected")
        next_scheduled_str = coordinator_data.get("next_train_scheduled")
        api_error = coordinator_data.get("api_error")
        trains_list = coordinator_data.get("trains")

        #_LOGGER.debug("%s native_value: Expected='%s', Scheduled='%s'", entity_id_log, next_expected_str, next_scheduled_str)

        if next_expected_str is None:
             if api_error:
                 _LOGGER.debug("%s native_value: next_expected is None, returning API Error string.", entity_id_log)
                 return "API Error"
             if not isinstance(trains_list, list) or not trains_list: # Check list explicitly
                 _LOGGER.debug("%s native_value: next_expected is None and no trains, returning No Trains Found.", entity_id_log)
                 return "No Trains Found"
             # This case should be rarer now with fallbacks in client.py
             _LOGGER.warning("%s native_value: next_expected is None unexpectedly. Returning UNKNOWN.", entity_id_log)
             return STATE_UNKNOWN

        # --- Try to return a datetime object ---
        # 1. Try parsing the 'expected' time/status
        parsed_expected_dt = self.coordinator._parse_time_safely(next_expected_str)
        if parsed_expected_dt:
            # Successfully parsed expected time into a datetime
             _LOGGER.debug("%s native_value: Returning parsed expected datetime: %s", entity_id_log, parsed_expected_dt)
             return parsed_expected_dt

        # 2. If expected wasn't a parsable time, check common status strings
        if isinstance(next_expected_str, str):
             expected_lower = next_expected_str.lower()
             if expected_lower == "on time":
                 # Return the scheduled time
                 parsed_scheduled_dt = self.coordinator._parse_time_safely(next_scheduled_str)
                 if parsed_scheduled_dt:
                      _LOGGER.debug("%s native_value: Expected 'On time', returning scheduled datetime: %s", entity_id_log, parsed_scheduled_dt)
                      return parsed_scheduled_dt
                 else:
                      _LOGGER.warning("%s native_value: Expected 'On time' but failed to parse scheduled time '%s'. Returning 'On time' string.", entity_id_log, next_scheduled_str)
                      return "On time" # Fallback to string if scheduled fails
             elif expected_lower == "delayed":
                  # Return the scheduled time as the best available time estimate
                  parsed_scheduled_dt = self.coordinator._parse_time_safely(next_scheduled_str)
                  if parsed_scheduled_dt:
                       _LOGGER.debug("%s native_value: Expected 'Delayed', returning scheduled datetime: %s", entity_id_log, parsed_scheduled_dt)
                       return parsed_scheduled_dt
                  else:
                       _LOGGER.warning("%s native_value: Expected 'Delayed' but failed to parse scheduled time '%s'. Returning 'Delayed' string.", entity_id_log, next_scheduled_str)
                       return "Delayed" # Fallback to string
             else:
                  # It's some other status string (Cancelled, No Trains Found, API Error, etc.)
                  _LOGGER.debug("%s native_value: Returning status string: '%s'", entity_id_log, next_expected_str)
                  return next_expected_str # Return the status string directly
        else:
             # Should not happen if next_expected_str is not None and not string, but handle defensively
             _LOGGER.error("%s native_value: Unexpected type for next_expected_str: %s. Returning UNKNOWN.", entity_id_log, type(next_expected_str).__name__)
             return STATE_UNKNOWN


# --- MonitoredTrainSensor Entity ---
class MonitoredTrainSensor(SensorEntity):
    """Representation of a monitored specific train."""
    _attr_should_poll = False # Relies on source sensor updates and internal logic
    _attr_icon = "mdi:train-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        coordinator: NationalRailScheduleCoordinator,
        target_time: time,
        target_destination: str, # Expecting upper case code
        time_window: timedelta
    ):
        """Initialize the sensor."""
        # ... (Init remains the same, including version number update) ...
        self.hass = hass
        self.coordinator = coordinator
        self._target_time = target_time
        self._target_destination = target_destination
        self._time_window = time_window
        self._initialized = False
        self._tracked_train_id = None
        self._tracking_stopped = False
        self._stop_listeners = None # Stores listener cancel callbacks
        self._tracking_stopped_at: datetime | None = None # Track when tracking stopped
        self._reset_timer_cancel: callable | None = None # Handle for reset timer <<< NEW
        self._tracked_scheduled_departure_dt: datetime | None = None
        self._tracked_arrival_dt: datetime | None = None
        self._re_evaluation_performed: bool = False

        # --- Naming and ID ---
        self._attr_name = name
        base_source_name = coordinator.sensor_name or f"unknown_coord_{coordinator.station}"
        safe_base_name = base_source_name.replace('sensor.', '').replace('.', '_').replace('-', '_').lower()
        target_dest_safe = target_destination.replace('.', '_').replace('-', '_').lower()
        target_time_safe = target_time.strftime('%H%M%S')
        self._attr_unique_id = f"monitored_train_{safe_base_name}_{target_dest_safe}_{target_time_safe}"

        # Determine source sensor entity_id
        if coordinator.sensor_name:
             safe_sensor_name = coordinator.sensor_name.replace('-', '_')
             self._source_sensor_id = f"sensor.{safe_sensor_name}"
        else:
             self._source_sensor_id = f"sensor.train_schedule_{coordinator.station.lower()}_unknown"
             _LOGGER.error("Monitored sensor '%s': Coordinator sensor_name missing! Using fallback source ID: %s", name, self._source_sensor_id)

        # --- Device Info ---
        self._attr_device_info = { "identifiers": {(DOMAIN, coordinator.station)} }

        # --- Initial State and Attributes ---
        self._attr_native_value = "Waiting for Source"
        self._attr_extra_state_attributes = {
            "source_sensor": self._source_sensor_id,
            "target_destination": target_destination,
            "target_destination_name": STATION_MAP.get(target_destination, target_destination),
            "target_departure_time": target_time.strftime("%H:%M:%S"),
            "monitoring_window_minutes": round(time_window.total_seconds() / 60, 1),
            "status": "Initializing",
            "last_update_trigger": None,
            "last_update_time": None,
            "tracked_train_details": None,
            "change_flags": {},
            "re_evaluation_status": "Pending",
            "version": COMPONENT_VERSION, # Uses updated version
        }
        self._previous_attributes = self._attr_extra_state_attributes.copy()

        _LOGGER.info("Initialized MonitoredTrainSensor '%s' (Unique ID: %s)", self._attr_name, self._attr_unique_id)
        _LOGGER.debug("MonitoredTrainSensor '%s' config: Source Sensor=%s, Dest=%s, Time=%s, Window=%s min",
                      self._attr_name, self._source_sensor_id, self._target_destination,
                      self._target_time.strftime('%H:%M:%S'), self._time_window.total_seconds() / 60)


    # --- Helper Methods (Parsing, Matching, Finding Trains) ---
    # _parse_train_time, _is_destination_match, _get_effective_departure_time,
    # _calculate_arrival_time, _find_next_train, _find_best_usable_train
    # These methods remain unchanged from VER_25
    def _parse_train_time(self, time_input: str | datetime | None) -> datetime | None:
        """Parse train time using the coordinator's safe parsing method."""
        if isinstance(time_input, str) and time_input in ["No Report"]:
             return None
        return self.coordinator._parse_time_safely(time_input)

    def _is_destination_match(self, train: dict) -> bool:
        """Check if the train goes to the target destination (case-insensitive, checks code & name)."""
        if not train: return False

        target_dest_code_upper = self._target_destination
        target_dest_name = STATION_MAP.get(target_dest_code_upper)
        target_dest_name_upper = target_dest_name.upper() if target_dest_name else None
        reverse_map = {name.upper(): code for code, name in STATION_MAP.items()}

        terminus = train.get("terminus")
        if isinstance(terminus, str):
            terminus_upper = terminus.upper()
            if terminus_upper == target_dest_code_upper: return True
            if target_dest_name_upper and terminus_upper == target_dest_name_upper: return True
            terminus_code = reverse_map.get(terminus_upper)
            if terminus_code and terminus_code == target_dest_code_upper: return True

        destinations = train.get("destinations")
        if isinstance(destinations, list):
            for dest_point in destinations:
                if not isinstance(dest_point, dict): continue
                dest_name = dest_point.get("name")
                if isinstance(dest_name, str):
                    dest_name_upper = dest_name.upper()
                    if dest_name_upper == target_dest_code_upper: return True
                    if target_dest_name_upper and dest_name_upper == target_dest_name_upper: return True
                    dest_code = reverse_map.get(dest_name_upper)
                    if dest_code and dest_code == target_dest_code_upper: return True

        return False

    def _get_effective_departure_time(self, train: dict) -> datetime | None:
        """Calculate the effective departure time (Expected > On Time=Scheduled > Scheduled)."""
        expected_str = train.get("expected")
        scheduled_str = train.get("scheduled")
        expected_dt = self._parse_train_time(expected_str)
        if expected_dt:
            return expected_dt
        if isinstance(expected_str, str) and expected_str == "On time":
            scheduled_dt = self._parse_train_time(scheduled_str)
            if scheduled_dt:
                 return scheduled_dt
        scheduled_dt = self._parse_train_time(scheduled_str)
        if scheduled_dt:
            return scheduled_dt
        # _LOGGER.debug("Could not determine effective departure time for train: %s", train.get("serviceID", scheduled_str)) # DEBUG -> verbose
        return None

    def _calculate_arrival_time(self, train_data: dict) -> datetime | None:
        """Calculate the expected arrival time at the *target* destination."""
        entity_id_log = self.entity_id or self._attr_unique_id
        if not isinstance(train_data.get("destinations"), list):
            # _LOGGER.debug("%s: Train data missing 'destinations' list for arrival calculation.", entity_id_log) # DEBUG -> verbose
            return None

        target_dest_code_upper = self._target_destination
        target_dest_name = STATION_MAP.get(target_dest_code_upper)
        target_dest_name_upper = target_dest_name.upper() if target_dest_name else None
        reverse_map = {name.upper(): code for code, name in STATION_MAP.items()}

        for dest_info in train_data["destinations"]:
            if not isinstance(dest_info, dict): continue
            dest_name = dest_info.get("name")
            if not isinstance(dest_name, str): continue
            dest_name_upper = dest_name.upper()

            matches = False
            if dest_name_upper == target_dest_code_upper: matches = True
            elif target_dest_name_upper and dest_name_upper == target_dest_name_upper: matches = True
            else:
                dest_code = reverse_map.get(dest_name_upper)
                if dest_code and dest_code == target_dest_code_upper: matches = True

            if matches:
                expected_arrival_str = dest_info.get("time_at_destination")
                arrival_dt = self._parse_train_time(expected_arrival_str)
                if arrival_dt:
                    # _LOGGER.debug("%s: Using expected/actual arrival time '%s' for %s.", entity_id_log, expected_arrival_str, dest_name) # DEBUG -> verbose
                    return arrival_dt
                else:
                    scheduled_arrival_str = dest_info.get("scheduled_time_at_destination")
                    arrival_dt_sched = self._parse_train_time(scheduled_arrival_str)
                    if arrival_dt_sched:
                        # _LOGGER.debug("%s: Using scheduled arrival time '%s' for %s (Expected/Actual was '%s').",
                        #                 entity_id_log, scheduled_arrival_str, dest_name, expected_arrival_str) # DEBUG -> verbose
                        return arrival_dt_sched
                    else:
                        _LOGGER.warning("%s: Could not determine arrival time for %s. Expected/Actual: '%s', Scheduled: '%s'.",
                                        entity_id_log, dest_name, expected_arrival_str, scheduled_arrival_str)
                        return None

        # _LOGGER.debug("%s: Target destination %s not found in calling points list for arrival calculation.", entity_id_log, self._target_destination) # DEBUG -> verbose
        return None
    def _find_next_train(self, all_trains: list, after_time: datetime) -> tuple[dict | None, datetime | None]:
        """Find the next available (not cancelled) train to the destination after a given time.
           Returns (train_data, effective_departure_dt) or (None, None)."""
        entity_id_log = self.entity_id or self._attr_name # Use correct logging reference
        # _LOGGER.debug("%s: Searching for next available train to %s after %s", entity_id_log, self._target_destination, after_time)
        next_available_train = None
        earliest_next_departure_dt = None

        if not isinstance(all_trains, list): # Basic check
            _LOGGER.error("%s: Invalid 'all_trains' provided to _find_next_train", entity_id_log)
            return None, None

        for train in all_trains:
            # Use self methods for consistency
            expected_str = train.get("expected", "").lower()
            if expected_str == "cancelled": continue

            effective_departure_dt = self._get_effective_departure_time(train)
            if not effective_departure_dt or effective_departure_dt <= after_time: continue

            if not self._is_destination_match(train): continue

            if next_available_train is None or effective_departure_dt < earliest_next_departure_dt:
                 next_available_train = train
                 earliest_next_departure_dt = effective_departure_dt

        if next_available_train:
             sched = next_available_train.get("scheduled")
             next_id = next_available_train.get("serviceID") or f"{sched}_{next_available_train.get('terminus','?')}"
             # _LOGGER.debug("%s: Selected next available train: %s (ID: %s, Effective: %s)",
             #               entity_id_log, sched, next_id, earliest_next_departure_dt)
        # else: # Less verbose logging
             # _LOGGER.debug("%s: No subsequent train found to %s after %s", entity_id_log, self._target_destination, after_time)

        return next_available_train, earliest_next_departure_dt        

    def _find_best_usable_train(self, all_trains: list, exclude_train_id: str | None = None) -> tuple[dict | None, datetime | None]:
        """Find the best train within the time window and matching the destination."""
        entity_id_log = self.entity_id or self._attr_name or f"monitored_train_{self._attr_unique_id}"
        candidate_trains = []

        # --- Time Calculation ---
        try:
            now = dt_util.now()
            naive_target_dt = datetime.combine(now.date(), self._target_time)
            target_dt_base = dt_util.as_local(naive_target_dt)

            # Adjust base date for target time if near midnight
            if now.time() < time(4, 0, 0) and self._target_time > time(20, 0, 0):
                 target_dt_base = dt_util.as_local(datetime.combine(now.date() - timedelta(days=1), self._target_time))
            elif self._target_time < time(4, 0, 0) and now.time() > time(20, 0, 0):
                 target_dt_base = dt_util.as_local(datetime.combine(now.date() + timedelta(days=1), self._target_time))

            if not isinstance(self._time_window, timedelta):
                 _LOGGER.error("%s: _time_window is not a timedelta! Cannot calculate window.", entity_id_log)
                 return None, None
            window_start = target_dt_base
            window_end = target_dt_base + self._time_window
        except Exception as time_calc_err:
            _LOGGER.exception("%s: Error during time calculation in _find_best_usable_train: %s", entity_id_log, time_calc_err)
            return None, None

        # --- Train Iteration ---
        if not isinstance(all_trains, list):
            _LOGGER.error("%s: _find_best_usable_train: 'all_trains' is not a list! Type: %s.", entity_id_log, type(all_trains).__name__)
            return None, None

        _LOGGER.debug("%s: _find_best_usable_train: Checking %d trains. Window: %s to %s (Departing on or after target)",
                         entity_id_log, len(all_trains), window_start.strftime('%H:%M'), window_end.strftime('%H:%M'))


        for i, train in enumerate(all_trains):
            try:
                expected_value_raw = train.get("expected", "")
                sched_value_raw = train.get("scheduled")
                service_id = train.get("serviceID")
                terminus = train.get("terminus", "Unknown")
                current_train_id = service_id or f"{sched_value_raw}_{terminus}"

                # Skip if this is the excluded train ID
                if exclude_train_id and current_train_id == exclude_train_id:
                    # _LOGGER.debug("%s: Loop %d: Skipping excluded train ID %s", entity_id_log, i, exclude_train_id) # DEBUG -> verbose
                    continue

                expected_str_chk = ""
                if isinstance(expected_value_raw, str):
                    expected_str_chk = expected_value_raw.lower()
                if expected_str_chk == "cancelled":
                    # _LOGGER.debug("%s: Loop %d: Skipped train %s because cancelled.", entity_id_log, i, current_train_id) # DEBUG -> verbose
                    continue

                if not self._is_destination_match(train):
                    # _LOGGER.debug("%s: Loop %d: Skipped train %s, destination mismatch.", entity_id_log, i, current_train_id) # DEBUG -> verbose
                    continue

                effective_departure_dt = self._get_effective_departure_time(train)
                if not effective_departure_dt:
                    # _LOGGER.debug("%s: Loop %d: Skipped train %s, cannot determine effective departure.", entity_id_log, i, current_train_id) # DEBUG -> verbose
                    continue

                # Check if effective departure is within the calculated window
                if not (window_start <= effective_departure_dt <= window_end):
                     # _LOGGER.debug("%s: Loop %d: Skipped train %s, effective departure %s outside window %s - %s.",
                     #               entity_id_log, i, current_train_id, effective_departure_dt.strftime('%H:%M'),
                     #               window_start.strftime('%H:%M'), window_end.strftime('%H:%M')) # DEBUG -> verbose
                     continue

                expected_arrival_dt = self._calculate_arrival_time(train)
                if expected_arrival_dt is None:
                    # _LOGGER.debug("%s: Loop %d: Skipping candidate train %s, cannot calculate arrival time.", entity_id_log, i, current_train_id) # DEBUG -> verbose
                    continue

                # Calculate duration for sorting secondary criteria (prefer faster train if arrival is same)
                duration_td = timedelta.max
                if expected_arrival_dt >= effective_departure_dt:
                      duration_td = expected_arrival_dt - effective_departure_dt

                # Add candidate: Sort by arrival time (primary), then duration (secondary), then departure (tertiary)
                candidate_trains.append((expected_arrival_dt, duration_td, effective_departure_dt, train))
                # _LOGGER.debug("%s: Loop %d: Train %s added as candidate (Effective: %s, Arrival: %s).",
                #               entity_id_log, i, current_train_id, effective_departure_dt.strftime('%H:%M'), expected_arrival_dt.strftime('%H:%M')) # DEBUG -> verbose

            except Exception as processing_err:
                _LOGGER.exception("%s: Loop %d: Error processing train in _find_best_usable_train: %s", entity_id_log, i, processing_err)
                continue

        # --- Selection ---
        if not candidate_trains:
            _LOGGER.debug("%s: No usable trains found matching criteria (Dest: %s, Departing between %s and %s)%s.",
                            entity_id_log, self._target_destination,
                            window_start.strftime('%H:%M'), window_end.strftime('%H:%M'),
                            f" excluding ID {exclude_train_id}" if exclude_train_id else "")
            return None, None

        candidate_trains.sort() # Sort by arrival, then duration, then departure

        best_arrival_dt, _, best_dep_time, best_train_data = candidate_trains[0]
        best_sched = best_train_data.get('scheduled')
        best_term = best_train_data.get('terminus', 'Unknown')
        best_id = best_train_data.get('serviceID') or f"{best_sched}_{best_term}"

        _LOGGER.debug("%s: Selected best train (ID: %s, Sched: %s) arriving at %s, departing effectively at %s.",
                     entity_id_log, best_id, best_sched,
                     best_arrival_dt.strftime('%H:%M:%S'), best_dep_time.strftime('%H:%M:%S'))

        return best_train_data, best_arrival_dt
    # --- Lifecycle Methods ---
    # async_added_to_hass, _delayed_initial_update, async_will_remove_from_hass,
    # _async_schedule_reset, _update_state_and_attrs
    # These methods remain unchanged from VER_25
    async def async_added_to_hass(self) -> None:
        """Register callbacks and schedule initial check."""
        await super().async_added_to_hass()
        entity_id_log = self.entity_id or self._attr_name
        _LOGGER.debug("Setting up listeners for %s", entity_id_log)

        if not self._source_sensor_id:
            _LOGGER.error("%s: Source sensor ID not determined. Cannot set up listeners.", entity_id_log)
            self._update_state_and_attrs("Error: Setup Failed", {"status": "Error", "info": "Source sensor ID missing."})
            return

        listeners = []

        @callback
        def source_state_listener(event: Event) -> None:
            """Handle state changes on the source sensor."""
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            entity_id_log = self.entity_id or self._attr_name or f"monitored_train_{self._attr_unique_id}"

            if not new_state:
                 _LOGGER.debug("%s: Source sensor '%s' removed. Ignoring event.", entity_id_log, entity_id)
                 return
            _LOGGER.debug("%s LISTENER: Source '%s' state change. Old: '%s', New: '%s'. New Attrs: %s",
                          entity_id_log, entity_id,
                          old_state.state if old_state else 'None',
                          new_state.state,
                          # Log a subset of attributes, like 'trains' presence/type
                          f"Trains type: {type(new_state.attributes.get('trains')).__name__}" if new_state.attributes else "No Attrs")
            _LOGGER.debug("%s: Source sensor '%s' state change detected. New state: %s", entity_id_log, entity_id, new_state.state)

            # Check if the 'trains' attribute actually changed content
            old_trains_attr = old_state.attributes.get("trains") if old_state and old_state.attributes else None
            new_trains_attr = new_state.attributes.get("trains") if new_state.attributes else None
            trains_changed = new_trains_attr != old_trains_attr

            # Trigger update ONLY if the trains list content has changed
            # Or if the source recovers from an error state AND has trains data
            source_was_error = old_state and old_state.state in ERROR_STATES
            source_is_error = new_state.state in ERROR_STATES
            source_has_trains = isinstance(new_trains_attr, list)

            trigger_update = False
            trigger_reason = "No Trigger"

            if trains_changed and source_has_trains and not source_is_error:
                trigger_update = True
                trigger_reason = f"Source Trains Changed ({new_state.state})"
                _LOGGER.info("%s: Source '%s' trains attribute changed. Scheduling update.", entity_id_log, entity_id)
            elif source_was_error and not source_is_error and source_has_trains:
                trigger_update = True
                trigger_reason = f"Source Recovered ({new_state.state})"
                _LOGGER.info("%s: Source '%s' recovered from error state '%s' to '%s'. Scheduling update.",
                             entity_id_log, entity_id, old_state.state if old_state else 'None', new_state.state)
            elif source_is_error:
                 _LOGGER.debug("%s: Source '%s' changed to error state '%s'. Monitored sensor will update via its own logic.",
                              entity_id_log, entity_id, new_state.state)
            elif not source_has_trains:
                 _LOGGER.debug("%s: Source '%s' update has no 'trains' list (State: %s). Ignoring.",
                               entity_id_log, entity_id, new_state.state)
            else:
                 _LOGGER.debug("%s: Source '%s' state changed (%s -> %s) but trains data is identical or invalid. Ignoring trigger.",
                               entity_id_log, entity_id, old_state.state if old_state else 'None', new_state.state)

            if trigger_update:
                # Check if an update is already scheduled or running? (future enhancement)
                _LOGGER.debug("%s: Scheduling direct update. Trigger: %s", entity_id_log, trigger_reason)
                # Store trigger reason BEFORE creating task
                self._attr_extra_state_attributes["last_update_trigger"] = trigger_reason
                self.hass.async_create_task(self.async_update())
            else:
                 self._attr_extra_state_attributes["last_update_trigger"] = "No Trigger (Listener)"

        try:
            listeners.append(
                async_track_state_change_event(
                    self.hass, [self._source_sensor_id], source_state_listener
                )
            )
            _LOGGER.debug("%s: Added state change listener for %s", entity_id_log, self._source_sensor_id)

            listeners.append(
                async_call_later(self.hass, timedelta(seconds=15), self._delayed_initial_update)
            )
            _LOGGER.debug("%s: Scheduled delayed initial update check.", entity_id_log)

        except Exception as e:
             _LOGGER.exception("%s: Failed to add listener or schedule initial update for %s: %s",
                               entity_id_log, self._source_sensor_id, e)
             self._update_state_and_attrs("Error: Listener Failed", {"status": "Error", "info": f"Failed listener setup for {self._source_sensor_id}."})
             listeners = []

        @callback
        def stop_listeners():
            entity_id_log = self.entity_id or self._attr_name # Use instance var
            _LOGGER.debug("Executing stop_listeners callback for %s", entity_id_log)
            for cancel in listeners:
                try:
                    cancel()
                except Exception as e:
                    _LOGGER.warning("%s: Error cancelling listener/timer: %s", entity_id_log, e)
            listeners.clear()
        self._stop_listeners = stop_listeners

    @callback
    async def _delayed_initial_update(self, _now=None):
        """Run update shortly after startup if source is ready and not yet initialized."""
        entity_id_log = self.entity_id or self._attr_name

        if self._initialized:
             _LOGGER.debug("%s: Skipping delayed initial update, already initialized.", entity_id_log)
             return
        if self._tracking_stopped:
             _LOGGER.debug("%s: Skipping delayed initial update, tracking is stopped.", entity_id_log)
             return

        _LOGGER.debug("%s: Running delayed initial check/update using coordinator data", entity_id_log)

        source_ready = False
        coord_ok = False
        if self.coordinator:
             coord_ok = self.coordinator.last_update_success
             if coord_ok and self.coordinator.data and isinstance(self.coordinator.data.get("trains"), list):
                 source_ready = True
             else:
                 _LOGGER.debug("%s: Coordinator not ready for initial update (Success: %s, Data Type: %s, Trains Type: %s)",
                                entity_id_log, coord_ok, type(self.coordinator.data).__name__,
                                type(self.coordinator.data.get("trains")).__name__ if self.coordinator.data else 'N/A')
        else:
             _LOGGER.error("%s: Coordinator object missing during delayed initial update.", entity_id_log)

        if source_ready:
            _LOGGER.info("%s: Coordinator ready during initial check. Triggering initial update.", entity_id_log)
            self._attr_extra_state_attributes["last_update_trigger"] = "Initial Startup Check"
            await self.async_update()
        else:
            source_state_val = "N/A"
            if self._source_sensor_id:
                 source_state = self.hass.states.get(self._source_sensor_id)
                 source_state_val = source_state.state if source_state else "Not Found"

            _LOGGER.warning("%s: Coordinator or source sensor not ready (Coord OK: %s, Source State: %s) during initial check. Will rely on state changes.",
                            entity_id_log, coord_ok, source_state_val)
            # Update state only if still initializing
            if not self._initialized and self._attr_native_value == "Waiting for Source":
                self._update_state_and_attrs(
                    "Waiting for Source",
                    {"status": "Initializing",
                     "info": f"Waiting for source (Coord OK: {coord_ok}, State: {source_state_val})"}
                 )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        entity_id_log = self.entity_id or self._attr_name
        _LOGGER.debug("Cleaning up %s on removal.", entity_id_log)
        if self._stop_listeners:
            self._stop_listeners()
            _LOGGER.debug("Stopped listeners for %s.", entity_id_log)
        # Cancel the reset timer if it's active <<< NEW
        if self._reset_timer_cancel:
            _LOGGER.debug("Cancelling pending reset timer for %s.", entity_id_log)
            try:
                self._reset_timer_cancel()
                self._reset_timer_cancel = None
            except Exception as e:
                 _LOGGER.warning("%s: Error cancelling reset timer: %s", entity_id_log, e)
        await super().async_will_remove_from_hass()

    # --- Reset Timer Callback <<< NEW ---
    @callback
    async def _async_schedule_reset(self, _now=None):
        """Reset sensor state to start searching again after a delay."""
        entity_id_log = self.entity_id or self._attr_name
        _LOGGER.info("%s: Reset timer fired. Resetting sensor to search again.", entity_id_log)

        # Clear the timer handle first
        self._reset_timer_cancel = None

        # Reset state variables
        self._tracking_stopped = False
        self._tracked_train_id = None
        self._initialized = False # Crucial to trigger the initial find logic
        self._re_evaluation_performed = False
        self._tracking_stopped_at = None
        self._tracked_scheduled_departure_dt = None
        self._tracked_arrival_dt = None

        # Set state to indicate reset and trigger update
        # Set an intermediate state first
        intermediate_attrs = self._attr_extra_state_attributes.copy()
        intermediate_attrs["status"] = "Searching (Reset)"
        intermediate_attrs["info"] = f"Resetting after {RESET_DELAY_SECONDS / 3600:.1f} hours. Searching..."
        intermediate_attrs["tracked_train_details"] = None
        intermediate_attrs["last_update_trigger"] = "Scheduled Reset"
        intermediate_attrs["last_update_time"] = dt_util.now().isoformat(timespec='milliseconds')

        self._update_state_and_attrs("Searching (Reset)", intermediate_attrs, force_write=True)

        # Schedule the update to perform the actual search using current coordinator data
        _LOGGER.debug("%s: Triggering async_update after reset.", entity_id_log)
        self.hass.async_create_task(self.async_update())

    # --- Helper to Update State and Attributes Consistently ---
    def _update_state_and_attrs(self, native_value: str | datetime | None, new_attrs: dict, force_write: bool = False):
        """Update internal state and attributes, writing to HA if changed."""
        entity_id_log = self.entity_id or self._attr_name
        current_native_value = self._attr_native_value
        current_attributes = self._attr_extra_state_attributes

        # Ensure required base attributes are present if resetting completely
        new_attrs.setdefault("source_sensor", self._source_sensor_id)
        new_attrs.setdefault("target_destination", self._target_destination)
        new_attrs.setdefault("target_destination_name", STATION_MAP.get(self._target_destination, self._target_destination))
        new_attrs.setdefault("target_departure_time", self._target_time.strftime("%H:%M:%S"))
        new_attrs.setdefault("monitoring_window_minutes", round(self._time_window.total_seconds() / 60, 1))
        new_attrs.setdefault("version", COMPONENT_VERSION)
        new_attrs["last_update_time"] = dt_util.now().isoformat(timespec='milliseconds') # Always update time


        state_changed = native_value != current_native_value
        # Compare dicts carefully - ignore last_update_time for change detection? Maybe not necessary.
        attrs_changed = new_attrs != current_attributes

        if state_changed or attrs_changed or force_write:
            # _LOGGER.debug("%s: Change detected (State: %s, Attrs: %s, Force: %s). Writing HA state. New Value: '%s'",
            #              entity_id_log, state_changed, attrs_changed, force_write, native_value) # DEBUG -> verbose

            self._attr_native_value = native_value
            self._attr_extra_state_attributes = new_attrs
            self._previous_attributes = new_attrs.copy() # Update previous state reference
            self.async_write_ha_state()
        # else:
             # _LOGGER.debug("%s: No significant state/attribute change or force_write=False. Skipping HA write.", entity_id_log) # DEBUG -> verbose
             # Still update previous_attributes if nothing changed to keep it in sync
             # self._previous_attributes = new_attrs.copy() # No, only update previous if written

    async def async_update(self) -> None:
        """Fetch new state data for the sensor based on coordinator data."""
        update_start_time = dt_util.now()
        entity_id_log = self.entity_id or self._attr_name
        _LOGGER.debug("--- Monitored Train Update Start [%s] (%s) ---", update_start_time.isoformat(timespec='milliseconds'), entity_id_log)
        _LOGGER.debug("State before: Init=%s, Stopped=%s, TrackedID=%s, ReEval=%s, Value=%s, Status=%s",
                     self._initialized, self._tracking_stopped, self._tracked_train_id,
                     self._re_evaluation_performed, self._attr_native_value,
                     self._attr_extra_state_attributes.get("status"))

        # --- Stop processing if tracking stopped AND no reset pending ---
        if self._tracking_stopped and not self._reset_timer_cancel:
            # ... (Tracking stopped logic remains unchanged) ...
            _LOGGER.debug("%s: Tracking stopped and no reset scheduled. No update needed.", entity_id_log)
            # Ensure final state is consistent
            current_status = self._attr_extra_state_attributes.get("status")
            if current_status not in ["Departed", "Cancelled (Final)", "Lost Track", "Tracking Ended"]:
                _LOGGER.warning("%s: Tracking stopped but status is '%s'. Forcing 'Tracking Ended'.", entity_id_log, current_status)
                final_attrs = self._attr_extra_state_attributes.copy()
                final_attrs["status"] = "Tracking Ended"
                final_attrs["info"] = "Tracking ended (state inconsistency check)."
                self._update_state_and_attrs("Tracking Ended", final_attrs, force_write=True)
            return


        # --- If reset timer is running, don't process regular updates ---
        if self._reset_timer_cancel:
            # ... (Reset timer check remains unchanged) ...
             _LOGGER.debug("%s: Reset timer is active. Skipping regular update.", entity_id_log)
             # Maybe update 'last checked' time? For now, just skip.
             return


        # --- Store previous state BEFORE this update runs ---
        previous_native_value = self._previous_attributes.get("_native_value_copy_for_compare", self._attr_native_value)
        previous_attributes = self._previous_attributes.copy() if isinstance(self._previous_attributes, dict) else {}

        # --- Initialize variables for this run ---
        new_native_value = previous_native_value
        new_attrs = previous_attributes.copy()
        new_attrs["last_update_time"] = update_start_time.isoformat(timespec='milliseconds')
        new_attrs["last_update_trigger"] = self._attr_extra_state_attributes.get("last_update_trigger", "Periodic/Unknown")

        change_flags = {}
        event_to_fire = None
        train_id_for_event = self._tracked_train_id
        current_train_data = None
        source_error = None
        all_trains = None

        # --- Get Data from Coordinator & Handle Errors ---
        # ... (This section remains unchanged) ...
        if not self.coordinator:
             source_error = "Coordinator object not available."
        elif not self.coordinator.last_update_success and self.coordinator.data is None:
             source_error = "Coordinator has not had a successful update yet."
        elif self.coordinator.data is None:
             source_error = "Coordinator data is None (likely API error during fetch)."
        else:
            coord_data = self.coordinator.data
            if coord_data.get("api_error"):
                 source_error = f"Coordinator reported API error: {coord_data['api_error']}"
            else:
                trains_candidate = coord_data.get("trains")
                if trains_candidate is None:
                    source_error = "Coordinator data missing 'trains' list."
                elif not isinstance(trains_candidate, list):
                    source_error = f"Coordinator 'trains' attribute is invalid (type: {type(trains_candidate).__name__})."
                else:
                    all_trains = trains_candidate


        # --- Determine State Based on Source Status ---
        process_trains = False
        # ... (This section remains unchanged) ...
        if source_error:
             _LOGGER.warning("%s: Source coordinator error: %s", entity_id_log, source_error)
             current_status = new_attrs.get("status")
             if current_status not in ["Source Error", "Initializing", "Error"]: # Avoid error loops
                 new_native_value = "Source Error"
                 new_attrs["status"] = "Source Error"
                 new_attrs["info"] = source_error
                 new_attrs["tracked_train_details"] = None # Clear details on source error
             # Don't change state if already waiting/initializing due to error
             elif current_status == "Initializing" and not self._initialized:
                  new_native_value = "Waiting for Source"
                  new_attrs["status"] = "Initializing"
                  new_attrs["info"] = f"Source error during init: {source_error}"

        elif all_trains is not None: # implies coordinator data available, no errors
             process_trains = True
             # Clear previous source error info if present
             if new_attrs.get("status") == "Source Error":
                 new_attrs["info"] = "Source recovered." # Status will be updated by train logic
        else:
             _LOGGER.error("%s: Unexpected state - no source error but all_trains is None.", entity_id_log)
             new_native_value = "Error: Internal State"
             new_attrs["status"] = "Error"
             new_attrs["info"] = "Internal logic error processing source data."
             new_attrs["tracked_train_details"] = None


        # --- Constants for Re-evaluation ---
        REEVALUATION_WINDOW = timedelta(minutes=10)
        REEVALUATION_BETTER_THRESHOLD = timedelta(minutes=3)
        REEVALUATION_DELAY_THRESHOLD = timedelta(minutes=10)

        # ==============================================================
        # --- Core Logic: Initialization, Re-evaluation, or Tracking ---
        # ==============================================================
        if process_trains:
            # ... (Phase 1: Find Initial Train remains unchanged) ...
            if not self._initialized:
                _LOGGER.debug("%s: Not initialized. Searching for initial best train.", entity_id_log)
                initial_best_train, initial_arrival_dt = self._find_best_usable_train(all_trains)

                if initial_best_train and initial_arrival_dt:
                    current_train_data = initial_best_train
                    sched_str = initial_best_train.get("scheduled")
                    terminus = initial_best_train.get("terminus", "Unknown")
                    self._tracked_train_id = initial_best_train.get("serviceID") or f"{sched_str}_{terminus}"
                    self._tracked_scheduled_departure_dt = self._parse_train_time(sched_str)
                    self._tracked_arrival_dt = initial_arrival_dt
                    self._re_evaluation_performed = False
                    new_attrs["re_evaluation_status"] = "Pending"
                    train_id_for_event = self._tracked_train_id

                    _LOGGER.info("%s: Found and latched onto initial train ID: %s (Sched: %s, Exp Arrival: %s)",
                                 entity_id_log, self._tracked_train_id,
                                 self._tracked_scheduled_departure_dt.strftime('%H:%M') if self._tracked_scheduled_departure_dt else sched_str,
                                 self._tracked_arrival_dt.strftime('%H:%M') if self._tracked_arrival_dt else "N/A")

                    change_flags["first_valid_update"] = True
                    # Prepare 'found' event (fired after state update)
                    event_to_fire = {'name': f"{DOMAIN}_train_found", 'data': {}}
                    # State/Value determined in Process Update Data block below
                    # Initialized flag set AFTER this update cycle completes

                else:
                    # Initial search failed
                    _LOGGER.info("%s: Initial search did not find a matching train.", entity_id_log)
                    new_native_value = "Not Found"
                    new_attrs["status"] = "Not Found"
                    new_attrs["info"] = f"No train found matching Dest={self._target_destination}, Time={self._target_time.strftime('%H:%M')}, Window={self._time_window.seconds // 60}m"
                    new_attrs["tracked_train_details"] = None
                    # Ensure current_train_data is None so next block is skipped
                    current_train_data = None

            # ... (Phase 2: Tracking & Re-evaluation remains unchanged, including finding tracked train, triggers, comparison, switching, and vanishing logic) ...
            else: # self._initialized is True
                if not self._tracked_train_id:
                    _LOGGER.error("%s: Initialized but tracked_train_id is None! Resetting state.", entity_id_log)
                    self._initialized = False # Force re-initialization next cycle
                    new_native_value = "Error: Tracking ID Lost"
                    new_attrs["status"] = "Internal Error"
                    new_attrs["info"] = "Tracked ID lost after initialization."
                    new_attrs["tracked_train_details"] = None
                    current_train_data = None # Prevent further processing
                else:
                    # --- Find the currently tracked train ---
                    found_tracked_train = None
                    for train in all_trains:
                        sched_str_id = train.get("scheduled")
                        term_id = train.get("terminus", "Unknown")
                        current_id = train.get("serviceID") or f"{sched_str_id}_{term_id}"
                        if current_id == self._tracked_train_id:
                            found_tracked_train = train
                            break

                    if found_tracked_train:
                        # --- Tracked train still exists ---
                        current_train_data = found_tracked_train
                        train_id_for_event = self._tracked_train_id
                        # State determined in Process Update Data block below

                        # --- Re-evaluation Logic ---
                        trigger_reevaluation = False
                        reevaluation_reason = ""
                        current_sched_str = found_tracked_train.get("scheduled")
                        current_sched_dt = self._parse_train_time(current_sched_str)
                        current_effective_dt = self._get_effective_departure_time(found_tracked_train)
                        current_delay = timedelta(0)
                        if current_sched_dt and current_effective_dt and current_effective_dt > current_sched_dt:
                             current_delay = current_effective_dt - current_sched_dt

                        if not self._re_evaluation_performed:
                            time_window_reached = (self._tracked_scheduled_departure_dt is not None and
                                                   update_start_time >= (self._tracked_scheduled_departure_dt - REEVALUATION_WINDOW))
                            delay_threshold_met = current_delay >= REEVALUATION_DELAY_THRESHOLD

                            if time_window_reached:
                                trigger_reevaluation = True
                                reevaluation_reason = f"Departure window reached ({REEVALUATION_WINDOW.seconds // 60} min prior)"
                            elif delay_threshold_met:
                                trigger_reevaluation = True
                                reevaluation_reason = f"Delay threshold met ({int(current_delay.total_seconds() / 60)} min)"

                        if trigger_reevaluation:
                            _LOGGER.info("%s: Re-evaluation triggered for train %s. Reason: %s.",
                                         entity_id_log, self._tracked_train_id, reevaluation_reason)
                            new_attrs["re_evaluation_status"] = "Checking"
                            best_alternative_train, alt_arrival_dt = self._find_best_usable_train(
                                all_trains, exclude_train_id=self._tracked_train_id
                            )
                            switched_train = False
                            if best_alternative_train and alt_arrival_dt:
                                # Compare arrival times
                                current_tracked_arrival = self._tracked_arrival_dt # Use the stored arrival time
                                if not current_tracked_arrival: # If we couldn't calculate arrival before, try again now
                                     current_tracked_arrival = self._calculate_arrival_time(found_tracked_train)

                                if current_tracked_arrival and alt_arrival_dt < (current_tracked_arrival - REEVALUATION_BETTER_THRESHOLD):
                                    switched_train = True
                                    old_train_id = self._tracked_train_id
                                    old_sched_dt = self._tracked_scheduled_departure_dt
                                    old_arrival_dt = current_tracked_arrival # Use potentially recalculated arrival

                                    _LOGGER.info("%s: Switching tracked train! Found better alternative.", entity_id_log)
                                    _LOGGER.info("%s:   Old Train: %s (Sched: %s, Est Arrival: %s)", entity_id_log, old_train_id,
                                                 old_sched_dt.strftime('%H:%M') if old_sched_dt else '?',
                                                 old_arrival_dt.strftime('%H:%M') if old_arrival_dt else '?')

                                    # --- Update tracking to the new train ---
                                    current_train_data = best_alternative_train # Process NEW train this cycle
                                    new_sched_str = best_alternative_train.get("scheduled")
                                    new_terminus = best_alternative_train.get("terminus", "Unknown")
                                    self._tracked_train_id = best_alternative_train.get("serviceID") or f"{new_sched_str}_{new_terminus}"
                                    self._tracked_scheduled_departure_dt = self._parse_train_time(new_sched_str)
                                    self._tracked_arrival_dt = alt_arrival_dt # Store new arrival
                                    self._re_evaluation_performed = True # Mark as done for NEW train
                                    train_id_for_event = self._tracked_train_id

                                    _LOGGER.info("%s:   New Train: %s (Sched: %s, Est Arrival: %s)", entity_id_log, self._tracked_train_id,
                                                 self._tracked_scheduled_departure_dt.strftime('%H:%M') if self._tracked_scheduled_departure_dt else '?',
                                                 self._tracked_arrival_dt.strftime('%H:%M') if self._tracked_arrival_dt else '?')

                                    change_flags["switched_train"] = True
                                    new_attrs["re_evaluation_status"] = f"Switched to {self._tracked_train_id}"

                                    event_to_fire = {
                                        'name': f"{DOMAIN}_train_switched",
                                        'data': {
                                            "previous_train_id": old_train_id,
                                            "previous_scheduled_departure": old_sched_dt.isoformat() if old_sched_dt else None,
                                            "previous_expected_arrival": old_arrival_dt.isoformat() if old_arrival_dt else None,
                                            "reason": f"Alt arrives {alt_arrival_dt.strftime('%H:%M')} vs current {old_arrival_dt.strftime('%H:%M') if old_arrival_dt else 'N/A'}"
                                        }
                                    }
                                else:
                                     _LOGGER.debug("%s: Alt train not better (Alt: %s vs Current: %s) or comparison failed.", entity_id_log,
                                                   alt_arrival_dt.strftime('%H:%M') if alt_arrival_dt else 'N/A',
                                                   current_tracked_arrival.strftime('%H:%M') if current_tracked_arrival else 'N/A')
                                     self._re_evaluation_performed = True
                                     new_attrs["re_evaluation_status"] = "Checked - No Better Train Found"
                            else:
                                _LOGGER.debug("%s: Re-evaluation found no alternative trains.", entity_id_log)
                                self._re_evaluation_performed = True
                                new_attrs["re_evaluation_status"] = "Checked - No Alternatives Available"
                            # If switched, current_train_data is updated. If not, it remains the original tracked train.

                    else: # --- Tracked Train Vanished ---
                        _LOGGER.info("%s: Tracked train ID %s not found in current data feed. Stopping tracking.",
                                     entity_id_log, self._tracked_train_id)
                        self._tracking_stopped = True
                        self._tracking_stopped_at = update_start_time # Record stop time

                        # Determine final state based on *previous* known state
                        last_status = previous_attributes.get("status")
                        # Get previous details carefully
                        prev_details = previous_attributes.get("tracked_train_details") if isinstance(previous_attributes.get("tracked_train_details"), dict) else {}
                        last_expected_iso = prev_details.get("expected_departure_iso")
                        last_expected_time_str = prev_details.get("expected_departure_time", "?")
                        last_expected_dt = self._parse_train_time(last_expected_iso)

                        final_state = "Lost Track"
                        final_info = f"Tracked train ({self._tracked_train_id}) disappeared from feed."
                        final_status_attr = "Lost Track"

                        if last_status == "Cancelled":
                            final_state = "Cancelled (Final)"
                            final_info = "Tracked train was previously cancelled."
                            final_status_attr = "Cancelled (Final)"
                        elif last_expected_dt and update_start_time > (last_expected_dt + timedelta(minutes=2)):
                            final_state = f"Departed ~{last_expected_time_str}"
                            final_info = f"Tracked train likely departed around {last_expected_time_str}."
                            final_status_attr = "Departed"
                        elif last_status == "Departed":
                             final_state = f"Departed ~{last_expected_time_str}"
                             final_info = "Tracked train was previously marked as departed."
                             final_status_attr = "Departed"
                        elif last_expected_dt:
                            final_info = f"Tracked train vanished before expected departure ({last_expected_time_str})."

                        new_native_value = final_state
                        new_attrs["status"] = final_status_attr
                        new_attrs["info"] = final_info
                        new_attrs["tracked_train_details"] = None # Clear details
                        new_attrs["re_evaluation_status"] = "N/A (Tracking Stopped)"

                        _LOGGER.info("%s: Setting final state: %s. Scheduling reset timer.", entity_id_log, final_state)

                        # --- Schedule the Reset Timer <<< NEW ---
                        if self._reset_timer_cancel: # Cancel any existing timer first
                            _LOGGER.warning("%s: Existing reset timer found unexpectedly. Cancelling it.", entity_id_log)
                            try: self._reset_timer_cancel()
                            except: pass # Ignore errors cancelling
                        _LOGGER.info("%s: Scheduling reset in %s seconds.", entity_id_log, RESET_DELAY_SECONDS)
                        self._reset_timer_cancel = async_call_later(
                            self.hass,
                            timedelta(seconds=RESET_DELAY_SECONDS),
                            self._async_schedule_reset
                        )
                        # --- End Reset Timer Scheduling ---

                        current_train_data = None # Stop processing data for this cycle


        # ==============================================================
        # --- Process Update Data (if current_train_data exists) ---
        # ==============================================================
        if current_train_data:
            # This block runs if initializing, tracking, or just switched trains.

            # --- Extract Details ---
            details = {"train_id": self._tracked_train_id} # Use current tracked ID
            scheduled_str = current_train_data.get("scheduled")
            expected_str = current_train_data.get("expected")
            platform = current_train_data.get("platform")
            terminus = current_train_data.get("terminus", "Unknown")

            scheduled_dt = self._parse_train_time(scheduled_str)
            effective_departure_dt = self._get_effective_departure_time(current_train_data)

            # Calculate arrival for *this* train data (might be new if switched)
            current_arrival_dt = self._calculate_arrival_time(current_train_data)
            # If this IS the tracked train (not just switched to), update the stored arrival
            if self._tracked_train_id == (current_train_data.get("serviceID") or f"{scheduled_str}_{terminus}"):
                 self._tracked_arrival_dt = current_arrival_dt

            # --- Populate Detail Fields ---
            details["scheduled_departure_iso"] = scheduled_dt.isoformat() if scheduled_dt else scheduled_str
            details["scheduled_departure_time"] = scheduled_dt.strftime("%H:%M") if scheduled_dt else "?"
            details["expected_departure_iso"] = effective_departure_dt.isoformat() if effective_departure_dt else expected_str
            details["expected_departure_time"] = effective_departure_dt.strftime("%H:%M") if effective_departure_dt else (expected_str if isinstance(expected_str, str) else "?")
            details["platform"] = platform if platform else "TBC"
            details["terminus"] = terminus
            details["terminus_name"] = STATION_MAP.get(terminus, terminus) if terminus else "Unknown"
            details["expected_arrival_iso"] = current_arrival_dt.isoformat() if current_arrival_dt else None
            details["expected_arrival_time"] = current_arrival_dt.strftime("%H:%M") if current_arrival_dt else "N/A"

            # <<< CHANGE 4: Add New Fields to details dict >>>
            details["operator_name"] = current_train_data.get("operator", "N/A")
            details["operator_code"] = current_train_data.get("operatorCode")
            details["cancel_reason"] = current_train_data.get("cancelReason")
            details["delay_reason"] = current_train_data.get("delayReason")
            details["length"] = current_train_data.get("length")

            # Handle adhocAlerts (extract text safely)
            alerts_data = current_train_data.get("adhocAlerts")
            alerts_list = []
            if isinstance(alerts_data, dict):
                message_entries = alerts_data.get("message") # Assuming structure like nrccMessages
                if isinstance(message_entries, list):
                     for entry in message_entries:
                         if isinstance(entry, dict):
                              # Use the same key as nrccMessages based on your sample
                              msg_text = entry.get("_value_1")
                              if isinstance(msg_text, str) and msg_text.strip():
                                 alerts_list.append(msg_text.strip())
            elif isinstance(alerts_data, str): # Handle simple string alert
                alerts_list.append(alerts_data.strip())
            details["adhoc_alerts"] = alerts_list if alerts_list else None
            # <<< END CHANGE >>>


            journey_minutes = None
            if effective_departure_dt and current_arrival_dt and current_arrival_dt >= effective_departure_dt:
                duration = current_arrival_dt - effective_departure_dt
                journey_minutes = int(duration.total_seconds() / 60)
            details["expected_journey_minutes"] = journey_minutes

            # --- Determine Current Status and Native Value Display ---
            # ... (Status/Delay/NativeValue calculation remains unchanged) ...
            current_status = "Unknown"
            delay_minutes = None
            native_value_display = details["expected_departure_time"] # Default

            if isinstance(expected_str, str) and expected_str.lower() == "cancelled":
                current_status = "Cancelled"
                native_value_display = "Cancelled"
            elif effective_departure_dt and scheduled_dt and effective_departure_dt > scheduled_dt:
                current_status = "Delayed"
                delay_minutes = int((effective_departure_dt - scheduled_dt).total_seconds() / 60)
                native_value_display = f"{details['expected_departure_time']} (+{delay_minutes}m)"
            elif expected_str == "On time" or (effective_departure_dt and scheduled_dt and effective_departure_dt == scheduled_dt):
                current_status = "On Time"
                native_value_display = details["scheduled_departure_time"] if details["scheduled_departure_time"] != "?" else details["expected_departure_time"]
            elif effective_departure_dt: # If we have an effective time, use 'Scheduled' unless other status applies
                 current_status = "Scheduled"
                 native_value_display = details["expected_departure_time"]
            elif scheduled_dt: # Fallback if only scheduled time known
                 current_status = "Scheduled"
                 native_value_display = details["scheduled_departure_time"]

            details["status"] = current_status
            details["delay_minutes"] = delay_minutes

            # --- Set Native Value and Attributes ---
            new_native_value = native_value_display # This becomes the sensor state
            new_attrs["status"] = current_status # Update top-level status
            new_attrs["tracked_train_details"] = details # Store all current details
            new_attrs["info"] = None # Clear previous info message if we have details

            # --- Add calling points details ---
            # ... (Calling points logic remains unchanged) ...
            dest_list = current_train_data.get("destinations", [])
            if isinstance(dest_list, list):
                details["calling_points"] = []
                details["destination_details"] = {}
                for dest in dest_list:
                     if isinstance(dest, dict):
                         dest_name = dest.get("name")
                         sched_arrival = dest.get("scheduled_time_at_destination")
                         exp_arrival_str = dest.get("time_at_destination")
                         exp_arrival_dt = self._parse_train_time(exp_arrival_str)
                         sched_arrival_dt = self._parse_train_time(sched_arrival)
                         actual_arrival_dt = exp_arrival_dt or sched_arrival_dt

                         if dest_name:
                             dest_name_friendly = STATION_MAP.get(dest_name, dest_name)
                             details["calling_points"].append(dest_name_friendly)
                             details["destination_details"][dest_name_friendly] = {
                                 "scheduled_arrival": sched_arrival_dt.strftime("%H:%M") if sched_arrival_dt else sched_arrival,
                                 "expected_arrival": exp_arrival_dt.strftime("%H:%M") if exp_arrival_dt else (exp_arrival_str if isinstance(exp_arrival_str, str) else sched_arrival),
                                 "scheduled_arrival_iso": sched_arrival_dt.isoformat() if sched_arrival_dt else sched_arrival,
                                 "expected_arrival_iso": exp_arrival_dt.isoformat() if exp_arrival_dt else (exp_arrival_str if isinstance(exp_arrival_str, str) else sched_arrival),
                                 "actual_arrival_iso": actual_arrival_dt.isoformat() if actual_arrival_dt else None,
                             }


            # --- Perform Change Detection & Prep Event ---
            # Only compare/fire events if initialized AND not just switched
            if self._initialized and not change_flags.get("switched_train"):
                # ... (Setup for change detection remains unchanged) ...
                prev_tracked_details = previous_attributes.get("tracked_train_details") or {}
                prev_status = prev_tracked_details.get("status", previous_attributes.get("status", "Unknown"))
                prev_platform = prev_tracked_details.get("platform", "TBC")
                prev_expected_iso = prev_tracked_details.get("expected_departure_iso")
                prev_expected_time = prev_tracked_details.get("expected_departure_time", "?")

                current_platform = details["platform"] # "TBC" or platform number
                current_expected_iso = details["expected_departure_iso"]
                current_expected_time = details["expected_departure_time"]

                _LOGGER.debug("%s: Change detection: Current Plat=%s, Prev Plat=%s", entity_id_log, current_platform, prev_platform) # DEBUG Platform
                platform_event_data = None
                if current_platform != prev_platform:
                    # Condition: Fire only if the new platform is *not* TBC
                    # AND (either the old one was TBC OR the old one was different)
                    if current_platform != "TBC":
                        change_type = "confirmed" if prev_platform == "TBC" else "changed"
                        change_flags[f"platform_{change_type}"] = True
                        change_flags["previous_platform"] = prev_platform
                        _LOGGER.info("%s: Platform %s for train %s: %s (was %s)",
                                    entity_id_log, change_type, self._tracked_train_id, current_platform, prev_platform)

                        platform_event_data = {
                             "new_platform": current_platform,
                             "previous_platform": prev_platform,
                             "change_type": change_type
                         }
                    else:
                        _LOGGER.debug("%s: Platform changed TO TBC from %s. Not firing event.", entity_id_log, prev_platform)
                else:
                    _LOGGER.debug("%s: Platform (%s) unchanged from previous (%s).", entity_id_log, current_platform, prev_platform)


                # --- Check Cancellation FIRST ---
                if current_status == "Cancelled" and prev_status != "Cancelled":
                     change_flags["just_cancelled"] = True
                     change_flags["previous_status"] = prev_status
                     _LOGGER.info("%s: Tracked train (%s) has been Cancelled. Finding next and resetting immediately.", entity_id_log, self._tracked_train_id)

                     # Find next available train
                     next_available_train_info = "None Found"
                     last_effective_departure = self._parse_train_time(prev_tracked_details.get("expected_departure_iso"))
                     last_scheduled_departure = self._parse_train_time(prev_tracked_details.get("scheduled_departure_iso"))
                     after_time_for_next = last_effective_departure or last_scheduled_departure or update_start_time
                     next_train_data, next_dep_dt = self._find_next_train(all_trains, after_time_for_next)
                     if next_train_data and next_dep_dt:
                          next_plat = next_train_data.get("platform", "TBC")
                          next_term = next_train_data.get("terminus", "Unknown")
                          next_term_name = STATION_MAP.get(next_term, next_term)
                          next_time_str = next_dep_dt.strftime('%H:%M')
                          next_available_train_info = f"{next_time_str} to {next_term_name} (Plat: {next_plat})"
                     new_attrs["next_available_train"] = next_available_train_info
                     _LOGGER.debug("%s: Next available train found: %s", entity_id_log, next_available_train_info)

                     # Prepare cancellation event data
                     event_to_fire = {'name': f"{DOMAIN}_train_cancelled", 'data': {
                          "previous_status": prev_status,
                          "next_available_train": next_available_train_info,
                          # <<< CHANGE 5: Add cancel_reason to event >>>
                          "cancel_reason": details.get("cancel_reason")
                          }}

                     # --- IMMEDIATE RESET LOGIC (Remains unchanged) ---
                     # ... (Reset logic as before) ...
                     _LOGGER.debug("%s: Performing immediate reset logic due to cancellation.", entity_id_log)
                     # ... (keep existing reset timer cancellation if present) ...
                     # Reset state variables
                     self._tracked_train_id = None
                     self._initialized = False
                     self._re_evaluation_performed = False
                     self._tracking_stopped_at = None
                     self._tracked_scheduled_departure_dt = None
                     self._tracked_arrival_dt = None
                     self._tracking_stopped = False # Ensure tracking isn't stopped

                     # Set sensor state for this update to indicate searching
                     new_native_value = "Searching (Reset)"
                     new_attrs["status"] = "Searching (Reset)"
                     # <<< MODIFY INFO MESSAGE >>>
                     new_attrs["info"] = f"Cancelled. Immediately searching for replacement. Next possible: {next_available_train_info}"
                     new_attrs["tracked_train_details"] = None
                     new_attrs["last_update_trigger"] = "Cancellation Reset"
                     new_attrs["re_evaluation_status"] = "Pending"
                     new_attrs["last_update_time"] = update_start_time.isoformat(timespec='milliseconds')

                     # Schedule the *next* update cycle
                     _LOGGER.debug("%s: Triggering next async_update task after immediate reset.", entity_id_log)
                     self.hass.async_create_task(self.async_update())
                     # --- END IMMEDIATE RESET LOGIC ---


                # --- Check other changes ONLY if NOT just cancelled ---
                elif not change_flags.get("just_cancelled"):

                    # 0. Calculate if time changed meaningfully (add this back)
                    is_meaningful_time_change = False
                    if current_expected_iso != prev_expected_iso:
                        # Basic check: ISO strings differ. More robust: parse and compare datetimes if available.
                        # Let's stick to the ISO string check for simplicity unless None is involved.
                        if current_expected_iso is not None and prev_expected_iso is not None:
                             # Example threshold: more than 60 seconds difference
                             try:
                                 current_dt = self.coordinator._parse_time_safely(current_expected_iso)
                                 prev_dt = self.coordinator._parse_time_safely(prev_expected_iso)
                                 if current_dt and prev_dt and abs((current_dt - prev_dt).total_seconds()) > 60:
                                     is_meaningful_time_change = True
                                 elif current_dt and not prev_dt: # Gained an expected time
                                      is_meaningful_time_change = True
                             except Exception: # Ignore parsing errors for this check
                                 pass # Stick with basic ISO string compare if parsing fails
                        elif current_expected_iso != prev_expected_iso: # Handle cases where one might be None
                           is_meaningful_time_change = True


                    # 1. Platform Change Event
                    if platform_event_data and event_to_fire is None:
                        # ... (Platform event prep remains unchanged) ...
                        _LOGGER.debug("%s: Preparing platform_changed event.", entity_id_log)
                        event_to_fire = {'name': f"{DOMAIN}_platform_changed", 'data': platform_event_data}

                    # 2. Time Change Event
                    # Use the calculated flag VVV
                    elif is_meaningful_time_change and event_to_fire is None: # Check event_to_fire too
                         _LOGGER.info("%s: Time change detected for train %s: %s -> %s",
                                      entity_id_log, self._tracked_train_id, prev_expected_time, current_expected_time)
                         change_flags["time_changed"] = True
                         change_flags["previous_expected_departure"] = prev_expected_time
                         # No need to check event_to_fire again here VVV
                         _LOGGER.debug("%s: Preparing time_changed event.", entity_id_log)
                         event_to_fire = {'name': f"{DOMAIN}_time_changed", 'data': {
                              "expected_departure_time": current_expected_time,
                              "previous_expected_departure_time": prev_expected_time,
                              # <<< CHANGE 6: Add delay_reason to event >>>
                              "delay_reason": details.get("delay_reason")
                              }}

                    # 3. Status Change Event
                    # ...
                    elif current_status != prev_status and event_to_fire is None:
                        # Only log significant status changes (e.g., not just Scheduled -> On Time if times match)
                        is_significant_status_change = True # Add more nuanced check if needed
                        if is_significant_status_change:
                            change_flags["status_changed"] = True
                            change_flags["previous_status"] = prev_status
                            _LOGGER.info("%s: Status change detected for train %s: %s -> %s",
                                          entity_id_log, self._tracked_train_id, prev_status, current_status)
                            # Usually no dedicated event for simple status change, covered by time/cancel/platform


        # ==============================================================
        # --- Final State Update & Event Firing ---
        # ==============================================================

        # ... (Fallback logic remains unchanged) ...
        if process_trains and current_train_data is None and new_native_value == previous_native_value and not self._tracking_stopped:
            if self._initialized:
                 _LOGGER.warning("%s: Processed trains but ended with no current data and no state change. Setting to 'Lost Track?'", entity_id_log)
                 # This case might indicate the train vanished but wasn't caught correctly above
                 new_native_value = "Lost Track?"
                 new_attrs["status"] = "Lost Track"
                 new_attrs["info"] = "Train possibly lost track (fallback check)."
                 # Don't stop tracking here, let the next cycle confirm if it's truly gone
            else:
                 # If not initialized, this means the initial search found nothing
                 _LOGGER.debug("%s: Fallback check confirms initial search found no train.", entity_id_log)
                 # State should already be "Not Found" from PHASE 1 else block


        # Add change flags to attributes for debugging
        new_attrs["change_flags"] = change_flags

        # --- Store a copy of the native value for reliable comparison next time ---
        new_attrs["_native_value_copy_for_compare"] = str(new_native_value) if isinstance(new_native_value, datetime) else new_native_value

        # --- Update HA State (using helper) ---
        force_write = self._tracking_stopped or new_attrs.get("status") == "Searching (Reset)"
        self._update_state_and_attrs(new_native_value, new_attrs, force_write=force_write)
        _LOGGER.debug("%s UPDATE: PRE-WRITE STATE. Native Value: '%s' (Type: %s). Attributes: %s",
                              entity_id_log, new_native_value, type(new_native_value).__name__, new_attrs)
        #_LOGGER.info("%s UPDATE: Final state determined. About to write Native: '%s', Status: '%s'",
         #                     entity_id_log, new_native_value, new_attrs.get("status", "N/A"))        

        # --- Fire Event (if prepared and state was written) ---
        fire_this_event = event_to_fire

        # Special handling for the FIRST valid update to fire train_found
        if change_flags.get("first_valid_update") and current_train_data:
            # ... (train_found event logic remains unchanged) ...
             _LOGGER.debug("%s: Preparing 'train_found' event for first successful update.", entity_id_log)
             # Prioritize train_found for the first update
             fire_this_event = {'name': f"{DOMAIN}_train_found", 'data': {}}
             # Add platform info if available during first find
             initial_platform = new_attrs.get("tracked_train_details", {}).get("platform", "TBC")
             if initial_platform != "TBC":
                 fire_this_event['data']['initial_platform'] = initial_platform
                 _LOGGER.debug("%s: Added initial_platform '%s' to train_found event.", entity_id_log, initial_platform)


        # Only fire if an event was chosen AND state was actually updated (using VER_24 logic)
        if fire_this_event and (force_write or new_attrs != previous_attributes or new_native_value != previous_native_value):
             # Populate base event data from the *new* attributes
             base_event_data = {
                "entity_id": self.entity_id,
                "sensor_name": self._attr_name,
                "station_code": self.coordinator.station,
                "station_name": self.coordinator.station_name,
                "target_destination": self._target_destination,
                "target_destination_name": STATION_MAP.get(self._target_destination, self._target_destination),
                "tracked_train_id": train_id_for_event, # Use the relevant ID
             }
             # Add details from the current tracked_train_details
             tracked_details = new_attrs.get("tracked_train_details")
             if isinstance(tracked_details, dict):
                  base_event_data.update({
                     "scheduled_departure": tracked_details.get("scheduled_departure_time", "?"),
                     "expected_departure_time": tracked_details.get("expected_departure_time", "?"),
                     "platform": tracked_details.get("platform", "TBC"),
                     "status": tracked_details.get("status", "Unknown"),
                     "terminus": tracked_details.get("terminus", "Unknown"),
                     "terminus_name": tracked_details.get("terminus_name", "Unknown"),
                     "expected_arrival": tracked_details.get("expected_arrival_time", "N/A"),
                     "journey_minutes": tracked_details.get("expected_journey_minutes"),
                     "delay_minutes": tracked_details.get("delay_minutes"),
                     "operator_name": tracked_details.get("operator_name", "N/A"),
                     "operator_code": tracked_details.get("operator_code"),
                     "cancel_reason": tracked_details.get("cancel_reason"), 
                     "delay_reason": tracked_details.get("delay_reason"),   
                     "length": tracked_details.get("length"),
                     "adhoc_alerts": tracked_details.get("adhoc_alerts"),
                     # <<< END CHANGE >>>
                  })

             # Merge specific event data (will overwrite base data if keys conflict)
             final_event_data = {**base_event_data, **fire_this_event['data']}

             _LOGGER.info("%s: Firing event '%s'", entity_id_log, fire_this_event['name'])
             _LOGGER.debug("Event data for %s: %s", fire_this_event['name'], final_event_data)
             self.hass.bus.async_fire(fire_this_event['name'], final_event_data)
        elif fire_this_event:
            _LOGGER.debug("%s: Event '%s' was prepared, but state did not change. Event not fired.", entity_id_log, fire_this_event['name'])


        # --- Set Initialized Flag AFTER first successful update where a train was processed ---
        if not self._initialized and current_train_data:
            # ... (Initialization flag logic remains unchanged) ...
             _LOGGER.info("%s: Sensor is now initialized and tracking train %s.", entity_id_log, self._tracked_train_id)
             self._initialized = True # Set flag AFTER potential event firing


        _LOGGER.debug("--- Monitored Train Update End [%s] (%s) ---", dt_util.now().isoformat(timespec='milliseconds'), entity_id_log)