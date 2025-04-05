"""Config flow for National Rail UK integration."""

import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .client import NationalRailClient, NationalRailClientException, NationalRailClientInvalidToken
from .const import CONF_DESTINATIONS, CONF_STATION, CONF_TOKEN, DOMAIN
from .stations import STATIONS, STATION_MAP

_LOGGER = logging.getLogger(__name__)

# Helper function to convert comma-separated string to list of stations
def parse_destinations(destinations_str):
    """Parse comma-separated destination string to list."""
    if not destinations_str:
        return []
    return [d.strip() for d in destinations_str.split(",") if d.strip()]

async def validate_api_token(hass, token: str) -> bool:
    """Validate the API token by making a test API call."""
    try:
        client = NationalRailClient(token, "PAD", [])
        await client.async_get_data()
        return True
    except NationalRailClientInvalidToken:
        return False
    except Exception as err:
        _LOGGER.warning("Error validating API token: %s", err)
        # If there's another error, it might be due to API limits or temporary issues
        # Rather than rejecting the token, we'll assume it's valid
        return True

async def validate_station_input(hass, user_input: dict) -> dict:
    """Validate a station configuration.
    
    Raises:
        NationalRailClientInvalidToken: If the API token is invalid
        NationalRailClientInvalidInput: If the station or destination inputs are invalid
        NationalRailClientException: For other API errors
    """
    # Create a token to validate with
    token = user_input.get(CONF_TOKEN)
    if not token:
        # Try to get an existing token
        token = await get_global_api_token(hass)
        if not token:
            raise NationalRailClientException("No API token provided")
    
    station = user_input.get(CONF_STATION)
    if not station:
        raise NationalRailClientInvalidInput("No station provided")
    
    destinations = user_input.get(CONF_DESTINATIONS, [])
    
    # Convert destinations to comma-separated string if it's a list
    destination_list = destinations if isinstance(destinations, list) else parse_destinations(destinations)
    
    try:
        # Make a test API call
        client = NationalRailClient(token, station, destination_list)
        await client.async_get_data()
        
        # Return user_input to indicate success
        return user_input
    except NationalRailClientInvalidToken:
        _LOGGER.error("Invalid National Rail API token")
        raise
    except NationalRailClientInvalidInput:
        _LOGGER.error("Invalid station or destination input: %s -> %s", 
                     station, destinations)
        raise
    except Exception as err:
        _LOGGER.exception("Unexpected error during station validation: %s", err)
        raise NationalRailClientException("Failed to validate station configuration") from err

async def get_global_api_token(hass):
    """Get the global API token from any available source."""
    # First check entries already loaded in memory
    if DOMAIN in hass.data:
        for entry_id, entry in hass.data[DOMAIN].items():
            if isinstance(entry, config_entries.ConfigEntry) and CONF_TOKEN in entry.data:
                # Found a token, let's use it
                return entry.data[CONF_TOKEN]
    
    # Then check all entries in the config registry
    for entry in hass.config_entries.async_entries(DOMAIN):
        if CONF_TOKEN in entry.data:
            # Found a token, use it
            return entry.data[CONF_TOKEN]
    
    return None

async def ensure_global_token_entry(hass, token, source="migration"):
    """Ensure a global token entry exists."""
    # Check if we already have a global token entry
    has_token_entry = False
    for entry in hass.config_entries.async_entries(DOMAIN):
        if CONF_TOKEN in entry.data and CONF_STATION not in entry.data:
            has_token_entry = True
            break
    
    if not has_token_entry:
        # No dedicated token entry found, create one
        flow_handler = config_entries.HANDLERS.get(DOMAIN)
        if flow_handler:
            flow = flow_handler()
            flow.hass = hass
            flow.context = {"source": source}
            
            # Set a unique ID to prevent duplicates
            await flow.async_set_unique_id(f"{DOMAIN}_api_token")
            
            # Create the entry
            return await flow.async_create_entry(
                title="National Rail API Token",
                data={CONF_TOKEN: token}
            )
    
    return None    


class NationalRailConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for National Rail UK."""

    VERSION = 2  # Updated from 1 to 2

    def __init__(self):
        """Initialize the flow."""
        self._token = None

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the user's initial choices."""
        # Check if we need to set up the token first
        token = await get_global_api_token(self.hass)
        
        # If no global token, check if any entry has a token we can use
        if not token:
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if CONF_TOKEN in entry.data:
                    token = entry.data[CONF_TOKEN]
                    
                    # Create a global token entry with this token
                    await ensure_global_token_entry(self.hass, token, "user")
                    
                    # Once we create a token entry, get it again
                    token = await get_global_api_token(self.hass)
                    break
        
        if not token:
            # Still no token found, redirect to token setup
            return await self.async_step_api_token()
        
        # We have a token, proceed to station setup
        self._token = token
        return await self.async_step_station()

    async def async_step_api_token(self, user_input=None) -> FlowResult:
        """Handle the API token configuration step."""
        errors = {}
        
        if user_input is not None:
            try:
                # Validate the token
                if await validate_api_token(self.hass, user_input[CONF_TOKEN]):
                    # Token is valid, save it
                    self._token = user_input[CONF_TOKEN]
                    
                    # Create a global token entry
                    await self.async_set_unique_id(f"{DOMAIN}_api_token")
                    self._abort_if_unique_id_configured()
                    
                    return self.async_create_entry(
                        title="National Rail API Token",
                        data={CONF_TOKEN: user_input[CONF_TOKEN]}
                    )
                else:
                    errors["base"] = "invalid_token"
            except Exception as ex:
                _LOGGER.exception("Unexpected error during token validation")
                errors["base"] = "unknown"
        
        # Show form to enter API token
        return self.async_show_form(
            step_id="api_token",
            data_schema=vol.Schema({
                vol.Required(CONF_TOKEN): str,
            }),
            errors=errors,
            description_placeholders={
                "token_info": "You need to provide a valid National Rail Darwin API token"
            }
        )

    async def async_step_station(self, user_input=None) -> FlowResult:
        """Handle the station configuration step."""
        errors = {}
        
        # Create station options for dropdown
        station_options = {station["code"]: f"{station['name']} ({station['code']})" 
                         for station in STATIONS}
        
        # Create station options for multi-select dropdown
        station_select_options = [
            {"value": station["code"], 
             "label": f"{station['name']} ({station['code']})"} 
            for station in STATIONS
        ]
        
        if user_input is not None:
            try:
                # Add the token to the input
                user_input[CONF_TOKEN] = self._token
                
                # Convert multi-select list to comma-separated string if needed
                if isinstance(user_input.get(CONF_DESTINATIONS), list):
                    user_input[CONF_DESTINATIONS] = ",".join(user_input[CONF_DESTINATIONS])
                
                # Validate the input
                await validate_station_input(self.hass, user_input)
                
                # Set title based on station name and destination
                station_name = STATION_MAP.get(user_input[CONF_STATION], user_input[CONF_STATION])
                destination_names = []
                
                if user_input.get(CONF_DESTINATIONS):
                    destinations = parse_destinations(user_input[CONF_DESTINATIONS])
                    for dest in destinations:
                        destination_names.append(STATION_MAP.get(dest, dest))
                
                title = f"Train Schedule {user_input[CONF_STATION]}"
                if destination_names:
                    title += f" -> {', '.join(destination_names)}"
                
                return self.async_create_entry(title=title, data=user_input)
            except NationalRailClientException as ex:
                _LOGGER.exception("Error during station configuration: %s", ex)
                errors["base"] = "cannot_connect"
        
        # Data schema with dropdowns and multi-select
        data_schema = vol.Schema({
            vol.Required(CONF_STATION): vol.In(station_options),
            vol.Optional(CONF_DESTINATIONS, default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=station_select_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
        })
        
        return self.async_show_form(
            step_id="station", data_schema=data_schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for the National Rail integration."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            # Convert multi-select list to comma-separated string if needed
            if isinstance(user_input.get(CONF_DESTINATIONS), list):
                user_input[CONF_DESTINATIONS] = ",".join(user_input[CONF_DESTINATIONS])
                
            return self.async_create_entry(title="", data=user_input)

        # Create station options for multi-select dropdown
        station_select_options = [
            {"value": station["code"], 
             "label": f"{station['name']} ({station['code']})"} 
            for station in STATIONS
        ]
        
        # Get current destinations as a list
        current_destinations = parse_destinations(
            self.config_entry.data.get(CONF_DESTINATIONS, "")
        )
        
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_DESTINATIONS, default=current_destinations): 
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=station_select_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN
                    )
                ),
            })
        )