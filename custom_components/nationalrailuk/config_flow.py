import logging
import voluptuous as vol
from typing import Any # Add Any

from homeassistant import config_entries
from homeassistant.core import callback, HomeAssistant # Added HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
import homeassistant.helpers.config_validation as cv
from datetime import time, timedelta # Added timedelta for validation context

from .client import (
    NationalRailClient,
    NationalRailClientException,
    NationalRailClientInvalidToken,
    NationalRailClientInvalidInput
)
from .const import (
    CONF_DESTINATIONS,
    CONF_STATION,
    CONF_TOKEN,
    DOMAIN,
    CONF_MONITOR_TRAIN,
    CONF_TARGET_TIME,
    CONF_TARGET_DESTINATION,
    CONF_TIME_WINDOW_MINUTES,
    CONF_MONITORED_TRAIN_NAME,
    DEFAULT_TIME_WINDOW
)
from .stations import STATIONS, STATION_MAP

_LOGGER = logging.getLogger(__name__)

# --- Helper functions (parse_destinations, parse_time_string, validate_api_token) remain the same ---
def parse_destinations(destinations_str):
    """Parse comma-separated destination string or list to list."""
    if not destinations_str:
        return []
    if isinstance(destinations_str, list):
        return [d.strip() if isinstance(d, str) else d for d in destinations_str]
    return [d.strip() for d in destinations_str.split(",") if d.strip()]

def parse_time_string(time_str):
    """Parse HH:MM or HH:MM:SS string into time object."""
    if not time_str:
        return None
    parts = time_str.split(":")
    if len(parts) == 2:
        return time(int(parts[0]), int(parts[1]))
    elif len(parts) == 3:
        return time(int(parts[0]), int(parts[1]), int(parts[2]))
    else:
        raise ValueError(f"Invalid time format: {time_str}")

async def validate_api_token(hass: HomeAssistant, token: str) -> bool:
    """Validate the API token by making a test API call."""
    # <<< NOTE: This still creates a temporary client. If you refactor
    #     client to be stateless later, this needs updating too. >>>
    try:
        # Use a common known station like Paddington for validation
        client = NationalRailClient(token, "PAD", [])
        await client.setup_client()
        # Use a minimal fetch - maybe GetDepBoardWithDetails is too much?
        # If Darwin has a simpler 'ping' or 'check token' call, use that.
        # Otherwise, this check is okay.
        await client.async_get_data()
        _LOGGER.info("API Token validation successful.")
        return True
    except NationalRailClientInvalidToken:
        _LOGGER.error("API Token validation failed: Invalid Token.")
        return False
    except Exception as err:
        # Log the specific error for better debugging
        _LOGGER.warning("API Token validation failed with unexpected error: %s", err, exc_info=True)
        # Decide if you want to allow proceeding on other errors (e.g., network timeout)
        # For now, let's treat other errors as failures too for simplicity.
        # return True # Original logic allowed proceeding
        return False # Stricter: only proceed if token confirmed good or definitively known bad

# --- Validation function (validate_station_input) - slightly adjusted ---
async def validate_station_input(hass: HomeAssistant, config_data: dict, token: str) -> None:
    """
    Validate station and destination input by making a test API call.
    Raises exceptions on failure.
    """
    station = config_data.get(CONF_STATION)
    destinations_raw = config_data.get(CONF_DESTINATIONS, [])
    destination_list = parse_destinations(destinations_raw)

    if not station:
        raise NationalRailClientInvalidInput("Station code is required.")
    if not token:
         raise NationalRailClientInvalidInput("API token missing for validation.") # Should not happen with flow logic

    _LOGGER.debug("Validating station '%s' with destinations %s using provided token.",
                  station, destination_list)

    try:
        # <<< NOTE: Still creates temporary client here too >>>
        client = NationalRailClient(token, station, destination_list)
        await client.setup_client()
        await client.async_get_data() # Fetch data for the specific station/dest
        _LOGGER.info("Station input validation successful for %s.", station)
        # No need to return data, just raise exception on failure
    except NationalRailClientInvalidToken:
        _LOGGER.error("Station validation failed: Invalid API token used.")
        raise # Re-raise specific token error
    except NationalRailClientInvalidInput as err:
         _LOGGER.error("Station validation failed: Invalid station/destination input for %s: %s", station, err)
         raise # Re-raise specific input error
    except NationalRailClientException as err:
        _LOGGER.error("Station validation failed: API client error for %s: %s", station, err)
        raise # Re-raise general client error
    except Exception as err:
        _LOGGER.exception("Station validation failed: Unexpected error for %s: %s", station, err)
        # Wrap unexpected errors
        raise NationalRailClientException(f"Unexpected error during station validation for {station}") from err


# --- Helper function (get_global_api_token) remains the same ---
async def get_global_api_token(hass):
    """Get the global API token from any available source."""
    # First look for dedicated token entries (those with only CONF_TOKEN)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if CONF_TOKEN in entry.data and CONF_STATION not in entry.data:
            _LOGGER.debug("Found token in dedicated token entry: %s...", entry.data[CONF_TOKEN][:5])
            return entry.data[CONF_TOKEN]

    # Then check all entries in the config registry for any token
    for entry in hass.config_entries.async_entries(DOMAIN):
        if CONF_TOKEN in entry.data:
            _LOGGER.debug("Found token in regular entry: %s...", entry.data[CONF_TOKEN][:5])
            return entry.data[CONF_TOKEN]

    # Finally check temporary storage
    if f"{DOMAIN}_token_to_save" in hass.data and hass.data[f"{DOMAIN}_token_to_save"]:
        token = hass.data[f"{DOMAIN}_token_to_save"]
        _LOGGER.debug("Found token in temporary storage: %s...", token[:5])
        return token

    _LOGGER.debug("No token found in any location")
    return None

# --- Helper function (ensure_global_token_entry) remains the same ---
async def ensure_global_token_entry(hass, token, source="migration"):
    """Ensure a global token entry exists."""
    # ... (implementation unchanged) ...
    return None


class NationalRailConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for National Rail UK."""

    VERSION = 2

    def __init__(self):
        """Initialize the flow."""
        self._token: str | None = None
        # Store data collected across steps
        self._station_data: dict[str, Any] = {} # MODIFIED: Init empty dict

    # --- async_step_system remains the same ---
    async def async_step_system(self, user_input=None) -> FlowResult:
        """Handle a flow initialized by the system (for token entry)."""
        # ... (implementation unchanged) ...
        _LOGGER.info("Handling system step.") # Add some logging
        if user_input and CONF_TOKEN in user_input:
            _LOGGER.info("Creating token-only entry from system step.")
            # Check for existing token entries first
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if CONF_TOKEN in entry.data and CONF_STATION not in entry.data:
                    _LOGGER.info("Token-only entry already exists, updating it.")
                    new_data = dict(entry.data)
                    new_data[CONF_TOKEN] = user_input[CONF_TOKEN]
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data=new_data
                    )
                    return self.async_abort(reason="already_configured") # Use reason for abort

            # Set a unique ID - use a consistent one
            await self.async_set_unique_id(f"{DOMAIN}_api_token") # Changed ID slightly
            self._abort_if_unique_id_configured()

            # Create new entry
            return self.async_create_entry(
                title="National Rail API Token",
                data={CONF_TOKEN: user_input[CONF_TOKEN]}
            )

        # If no token provided, abort
        _LOGGER.warning("System step called without token.")
        return self.async_abort(reason="missing_token") # Use reason for abort

    # --- async_step_user remains mostly the same (redirects to token or station) ---
    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the user's initial choices."""
        _LOGGER.debug("Handling user step. Input: %s", user_input) # Add logging
        errors = {} # Keep errors dict

        # Check if we need to set up the token first
        token = await get_global_api_token(self.hass)

        # --- Logic to handle potential token in user_input if coming from reauth/other flows ---
        # (This part of original code seemed geared towards saving token from user step,
        #  which might conflict with separate token entry. Let's stick to finding existing token first.)
        # if not token and user_input and CONF_TOKEN in user_input:
        #     # Maybe validate and save token here? Let's enforce step_api_token for clarity.
        #     token = user_input[CONF_TOKEN] # Tentatively use it
        #     # Validate it?
        #     if not await validate_api_token(self.hass, token):
        #          errors["base"] = "invalid_token" # Add error if invalid
        #          token = None # Reset token if invalid

        # Check temporary storage (useful if token step completed but flow restarted?)
        if not token and f"{DOMAIN}_token_to_save" in self.hass.data and self.hass.data[f"{DOMAIN}_token_to_save"]:
            token = self.hass.data[f"{DOMAIN}_token_to_save"]
            _LOGGER.info("Using token from temporary storage: %s...", token[:5])
            # Ensure token entry exists
            await ensure_global_token_entry(self.hass, token, source="user_flow_temp")


        if not token:
            # Still no token found, redirect to token setup
            _LOGGER.debug("No token found, proceeding to api_token step.")
            return await self.async_step_api_token()

        # We have a token, store it and proceed to station setup
        _LOGGER.debug("Token found, proceeding to station step.")
        self._token = token
        # Clear any previously stored station data if restarting user step
        self._station_data = {}
        return await self.async_step_station()

    # --- async_step_api_token remains mostly the same (validates and creates token entry) ---
    async def async_step_api_token(self, user_input=None) -> FlowResult:
        """Handle the API token configuration step."""
        _LOGGER.debug("Handling api_token step. Input: %s", user_input)
        errors = {}

        if user_input is not None:
            token_to_validate = user_input[CONF_TOKEN]
            try:
                if await validate_api_token(self.hass, token_to_validate):
                    _LOGGER.info("API token validated successfully.")
                    self._token = token_to_validate # Store validated token locally

                    # Check for existing token entries first
                    existing_entry = None
                    for entry in self.hass.config_entries.async_entries(DOMAIN):
                        if CONF_TOKEN in entry.data and CONF_STATION not in entry.data:
                            existing_entry = entry
                            break

                    if existing_entry:
                        _LOGGER.info("Token-only entry already exists, updating it.")
                        if existing_entry.data[CONF_TOKEN] != self._token:
                             new_data = dict(existing_entry.data)
                             new_data[CONF_TOKEN] = self._token
                             self.hass.config_entries.async_update_entry(entry, data=new_data)
                        else:
                             _LOGGER.debug("Existing token entry has the same token, no update needed.")
                        # Proceed to station step *after* ensuring token entry exists/is updated
                        return await self.async_step_station()
                    else:
                        # Create a new global token entry
                        _LOGGER.info("Creating new token-only entry.")
                        await self.async_set_unique_id(f"{DOMAIN}_api_token") # Use consistent ID
                        # self._abort_if_unique_id_configured() # Abort might be too aggressive if user explicitly adds again
                        # Instead, let's allow creating the entry, then move to station step
                        # Store token temporarily in case flow is interrupted before station step
                        self.hass.data[f"{DOMAIN}_token_to_save"] = self._token
                        entry = self.async_create_entry(
                            title="National Rail API Token",
                            data={CONF_TOKEN: self._token}
                        )
                        # After creating token entry, proceed to station step
                        # We need to return the FlowResult from create_entry first,
                        # then HA will likely call async_step_user again, which will find the token.
                        # This seems a bit complex. Let's try creating the entry AND proceeding.
                        # Create the entry first, then proceed.
                        # NOTE: This might cause issues if the create_entry FlowResult is not returned immediately.
                        # Safer approach: Create entry, return FlowResult, let HA restart flow at user step.
                        return entry # Return the result of creating the token entry

                        # Alternative (might not work as expected):
                        # await self.async_create_entry(...) # Create entry
                        # return await self.async_step_station() # Immediately proceed? Risky.

                else:
                    _LOGGER.warning("API token validation failed.")
                    errors["base"] = "invalid_token"
            except Exception as ex:
                _LOGGER.exception("Unexpected error during token validation")
                errors["base"] = "unknown"

        # Show form to enter API token
        _LOGGER.debug("Showing api_token form. Errors: %s", errors)
        return self.async_show_form(
            step_id="api_token",
            data_schema=vol.Schema({
                vol.Required(CONF_TOKEN): str,
            }),
            errors=errors,
            description_placeholders={
                "token_info": "Enter your National Rail Darwin API token." # Simpler placeholder
            }
        )


    # --- async_step_station - MODIFIED ---
    async def async_step_station(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the base station configuration step."""
        _LOGGER.debug("Handling station step. Input: %s", user_input)
        errors = {}

        # Create station options
        station_options = {station["code"]: f"{station['name']} ({station['code']})"
                         for station in STATIONS}
        station_select_options = [
            {"value": station["code"], "label": f"{station['name']} ({station['code']})"}
            for station in STATIONS
        ]

        if user_input is not None:
            # Store the input from this step
            self._station_data.update(user_input)
            monitor_train = self._station_data.get(CONF_MONITOR_TRAIN, False)

            # Perform base validation (station code format, etc. - API call later)
            # (Add any simple voluptuous checks here if needed)

            if monitor_train:
                # If monitoring, proceed to the next step to gather details
                _LOGGER.debug("Monitor train selected, proceeding to monitor_details step.")
                return await self.async_step_monitor_details()
            else:
                # If not monitoring, validate the base station info now
                _LOGGER.debug("Monitor train not selected. Validating base station input.")
                try:
                    # Validate using the stored token and current station data
                    await validate_station_input(self.hass, self._station_data, self._token)

                    # If validation passes, create the entry with only base data
                    title = self._generate_title(self._station_data)
                    _LOGGER.info("Validation successful for non-monitored entry. Creating entry '%s'.", title)
                    return self.async_create_entry(title=title, data=self._station_data)

                except (NationalRailClientInvalidInput, NationalRailClientException) as ex:
                    _LOGGER.warning("Validation failed for base station: %s", ex)
                    errors["base"] = "cannot_connect" # Or map more specific errors if needed
                except NationalRailClientInvalidToken:
                     _LOGGER.error("Invalid token detected during base station validation.")
                     errors["base"] = "invalid_token" # Should ideally be caught earlier
                except Exception as ex:
                     _LOGGER.exception("Unexpected validation error for base station")
                     errors["base"] = "unknown"
                # If errors occurred, fall through to re-show the form

        # --- Schema for this step (Base station + Monitor Toggle) ---
        schema = vol.Schema({
            vol.Required(CONF_STATION, default=self._station_data.get(CONF_STATION)): vol.In(station_options),
            vol.Optional(CONF_DESTINATIONS, default=self._station_data.get(CONF_DESTINATIONS, [])): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=station_select_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Optional(CONF_MONITOR_TRAIN, default=self._station_data.get(CONF_MONITOR_TRAIN, False)): selector.BooleanSelector(),
        })

        _LOGGER.debug("Showing station form. Errors: %s", errors)
        return self.async_show_form(
            step_id="station",
            data_schema=schema,
            errors=errors
        )

    # --- NEW step: async_step_monitor_details ---
    async def async_step_monitor_details(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle gathering details for a monitored train."""
        _LOGGER.debug("Handling monitor_details step. Input: %s", user_input)
        errors = {}

        # Create station options (needed again for target destination)
        station_options = {station["code"]: f"{station['name']} ({station['code']})"
                         for station in STATIONS}

        if user_input is not None:
            # Combine base data with monitor details
            final_data = self._station_data.copy()
            final_data.update(user_input)

            # --- Add Validation for Monitor Details ---
            # Check if target time/dest are provided
            if not final_data.get(CONF_TARGET_TIME):
                errors[CONF_TARGET_TIME] = "required"
            if not final_data.get(CONF_TARGET_DESTINATION):
                errors[CONF_TARGET_DESTINATION] = "required"

            # --- Reformat Time (must happen *before* validation if validation uses it) ---
            target_time_input = final_data.get(CONF_TARGET_TIME)
            if target_time_input and isinstance(target_time_input, str): # Check if it's string from form
                 try:
                     target_time_obj = parse_time_string(target_time_input)
                     # Store standardized HH:MM:SS string
                     final_data[CONF_TARGET_TIME] = target_time_obj.strftime("%H:%M:00")
                 except ValueError:
                      _LOGGER.warning("Invalid time format received: %s", target_time_input)
                      errors[CONF_TARGET_TIME] = "invalid_time_format"
            elif target_time_input: # If already parsed (e.g., from options flow default?)
                try:
                    # Ensure it's stored consistently
                    final_data[CONF_TARGET_TIME] = time(target_time_input.hour, target_time_input.minute).strftime("%H:%M:00")
                except AttributeError:
                    _LOGGER.warning("Invalid time object type for target_time: %s", type(target_time_input))
                    errors[CONF_TARGET_TIME] = "invalid_time_format"


            # If basic validation passes, perform the API validation
            if not errors:
                _LOGGER.debug("Basic monitor details seem OK. Performing full validation.")
                try:
                    # Validate the *combined* data using the stored token
                    await validate_station_input(self.hass, final_data, self._token)

                    # Generate title using combined data
                    title = self._generate_title(final_data)
                    _LOGGER.info("Validation successful for monitored entry. Creating entry '%s'.", title)

                    # Add default name if not provided
                    if not final_data.get(CONF_MONITORED_TRAIN_NAME):
                        target_time_str = final_data[CONF_TARGET_TIME] # Use formatted time
                        target_dest_code = final_data[CONF_TARGET_DESTINATION]
                        target_dest_name = STATION_MAP.get(target_dest_code, target_dest_code)
                        final_data[CONF_MONITORED_TRAIN_NAME] = f"Train to {target_dest_name} @ {target_time_str[:5]}" # Use HH:MM

                    return self.async_create_entry(title=title, data=final_data)

                except (NationalRailClientInvalidInput, NationalRailClientException) as ex:
                    _LOGGER.warning("Validation failed for monitored station: %s", ex)
                    errors["base"] = "cannot_connect"
                except NationalRailClientInvalidToken:
                     _LOGGER.error("Invalid token detected during monitored station validation.")
                     errors["base"] = "invalid_token"
                except Exception as ex:
                     _LOGGER.exception("Unexpected validation error for monitored station")
                     errors["base"] = "unknown"

        # --- Schema for this step (Monitor Details Only) ---
        # Provide defaults from user_input if re-showing form due to error
        current_values = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_TARGET_TIME, default=current_values.get(CONF_TARGET_TIME)): selector.TimeSelector(),
            vol.Required(CONF_TARGET_DESTINATION, default=current_values.get(CONF_TARGET_DESTINATION)): vol.In(station_options),
            vol.Optional(CONF_TIME_WINDOW_MINUTES, default=current_values.get(CONF_TIME_WINDOW_MINUTES, DEFAULT_TIME_WINDOW)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=5, max=60, step=5, unit_of_measurement="minutes")
            ),
            vol.Optional(CONF_MONITORED_TRAIN_NAME, default=current_values.get(CONF_MONITORED_TRAIN_NAME, "")): cv.string,
        })

        _LOGGER.debug("Showing monitor_details form. Errors: %s", errors)
        return self.async_show_form(
            step_id="monitor_details",
            data_schema=schema,
            errors=errors,
            description_placeholders={ # Add description if needed
                "station_info": f"Configure monitoring for station: {STATION_MAP.get(self._station_data.get(CONF_STATION), self._station_data.get(CONF_STATION))}"
            }
        )

    # --- Helper to generate title ---
    def _generate_title(self, config_data: dict) -> str:
        """Generate the config entry title based on collected data."""
        station_code = config_data.get(CONF_STATION, "Unknown")
        station_name = STATION_MAP.get(station_code, station_code)
        monitor_train = config_data.get(CONF_MONITOR_TRAIN, False)

        if monitor_train:
            name_override = config_data.get(CONF_MONITORED_TRAIN_NAME)
            if name_override:
                return name_override # Use user-provided name directly as title

            # Construct title if name not overridden
            target_time = config_data.get(CONF_TARGET_TIME, "??:??")[:5] # HH:MM
            target_dest_code = config_data.get(CONF_TARGET_DESTINATION, "?")
            target_dest_name = STATION_MAP.get(target_dest_code, target_dest_code)
            return f"Train {station_name} -> {target_dest_name} @ {target_time}"
        else:
            # Title for non-monitored entry
            destination_names = []
            dest_codes_raw = config_data.get(CONF_DESTINATIONS, [])
            dest_codes = parse_destinations(dest_codes_raw) # Ensure list
            for dest in dest_codes:
                destination_names.append(STATION_MAP.get(dest, dest))

            title = f"Schedule {station_name}" # Simpler base title
            if destination_names:
                title += f" -> {', '.join(sorted(destination_names))}" # Sort for consistency
            return title

    # --- Options Flow Handler ---
    # This should remain largely unchanged as it operates on the existing entry data
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for the National Rail integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry): # Added type hint
        """Initialize options flow."""
        super().__init__(config_entry)
        # Store entry data for easier access
        self._entry_data = config_entry.data
        # Determine if this is a monitored entry based on saved data
        self._monitor_train = config_entry.data.get(CONF_MONITOR_TRAIN, False)

    async def async_step_init(self, user_input: dict[str, Any] | None = None): # Added type hint
        """Manage the options."""
        errors = {} # Add error handling

        if user_input is not None:
             # Merge new options with existing data that isn't configurable here
             # (like station code, monitor_train toggle itself)
             new_data = self._entry_data.copy()
             new_data.update(user_input) # Update with user's choices

             # --- Reformat Destinations ---
             dest_input = new_data.get(CONF_DESTINATIONS)
             if isinstance(dest_input, list):
                 # Keep as list for consistency if SelectSelector used?
                 # Or convert back to string if needed elsewhere?
                 # Let's keep as list internally if schema uses list.
                 # Ensure elements are strings
                 new_data[CONF_DESTINATIONS] = [str(d) for d in dest_input]
                 # If you need string format elsewhere:
                 # new_data[CONF_DESTINATIONS] = ",".join(str(d) for d in dest_input)


             # --- Reformat Time (if monitored) ---
             if self._monitor_train and CONF_TARGET_TIME in new_data:
                 target_time_input = new_data[CONF_TARGET_TIME]
                 if isinstance(target_time_input, str):
                      try:
                          target_time_obj = parse_time_string(target_time_input)
                          new_data[CONF_TARGET_TIME] = target_time_obj.strftime("%H:%M:00")
                      except ValueError:
                          _LOGGER.warning("OptionsFlow: Invalid time format %s", target_time_input)
                          errors[CONF_TARGET_TIME] = "invalid_time_format"
                 elif isinstance(target_time_input, time): # Handle if TimeSelector returns time object
                      new_data[CONF_TARGET_TIME] = target_time_input.strftime("%H:%M:00")
                 else:
                      _LOGGER.warning("OptionsFlow: Unexpected type for target time %s", type(target_time_input))
                      errors[CONF_TARGET_TIME] = "invalid_time_format"

             # --- Add default name if monitored and name cleared ---
             if self._monitor_train and CONF_MONITORED_TRAIN_NAME in new_data and not new_data[CONF_MONITORED_TRAIN_NAME]:
                 target_time_str = new_data.get(CONF_TARGET_TIME, "??:??")[:5] # Use updated time
                 target_dest_code = new_data.get(CONF_TARGET_DESTINATION, "?")
                 target_dest_name = STATION_MAP.get(target_dest_code, target_dest_code)
                 new_data[CONF_MONITORED_TRAIN_NAME] = f"Train to {target_dest_name} @ {target_time_str}"
                 _LOGGER.debug("OptionsFlow: Resetting monitored train name.")


             if not errors:
                 _LOGGER.debug("OptionsFlow: Updating entry %s with data: %s", self.config_entry.entry_id, new_data)
                 # Use async_update_entry which handles data updates correctly
                 self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
                 return self.async_create_entry(title="", data={}) # data={} indicates success, no further steps

             # If errors, fall through to re-show form

        # --- Build Schema Based on Entry Type ---
        station_select_options = [
            {"value": station["code"], "label": f"{station['name']} ({station['code']})"}
            for station in STATIONS
        ]
        station_options = {station["code"]: f"{station['name']} ({station['code']})"
                         for station in STATIONS}

        schema_dict = {}
        if self._monitor_train:
            _LOGGER.debug("OptionsFlow: Showing schema for monitored train.")
            # Schema for monitored train entry
            schema_dict = {
                # Cannot change target station or toggle monitor here
                vol.Optional(CONF_TARGET_TIME, default=self._entry_data.get(CONF_TARGET_TIME)): selector.TimeSelector(),
                vol.Optional(CONF_TARGET_DESTINATION, default=self._entry_data.get(CONF_TARGET_DESTINATION)): vol.In(station_options),
                vol.Optional(CONF_TIME_WINDOW_MINUTES, default=self._entry_data.get(CONF_TIME_WINDOW_MINUTES, DEFAULT_TIME_WINDOW)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=60, step=5, unit_of_measurement="minutes")
                ),
                vol.Optional(CONF_MONITORED_TRAIN_NAME, default=self._entry_data.get(CONF_MONITORED_TRAIN_NAME, "")): cv.string,
                # Add destinations here too? Or assume they are only for non-monitored?
                # If monitored trains *also* use the destination filter for the source sensor, include it.
                # Let's assume destinations are primarily for the general schedule sensor for now.
                # vol.Optional(CONF_DESTINATIONS, default=parse_destinations(self._entry_data.get(CONF_DESTINATIONS, ""))): ...
            }
        else:
            _LOGGER.debug("OptionsFlow: Showing schema for schedule-only entry.")
            # Schema for schedule-only entry
            schema_dict = {
                # Cannot change target station or toggle monitor here
                vol.Optional(CONF_DESTINATIONS, default=parse_destinations(self._entry_data.get(CONF_DESTINATIONS, ""))):
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=station_select_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        # Use custom_value=False if you only want predefined stations
                        # custom_value=True # Allows typing CRS codes not in the list
                    )
                ),
            }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            errors=errors # Pass errors back to the form
        )