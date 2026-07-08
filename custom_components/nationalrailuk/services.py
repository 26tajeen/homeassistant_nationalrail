"""Services for National Rail UK integration."""
import logging
from datetime import datetime, timedelta, time
import uuid

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later
from homeassistant.exceptions import ServiceValidationError
import voluptuous as vol

from .const import (
    DOMAIN,
    CONF_STATION,
    CONF_MONITOR_TRAIN,
    CONF_TARGET_TIME,
    CONF_TARGET_DESTINATION,
    CONF_TIME_WINDOW_MINUTES,
    CONF_MONITORED_TRAIN_NAME,
    DEFAULT_TIME_WINDOW,
)

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_CREATE_TEMP_SENSOR = "create_temporary_sensor"
SERVICE_DELETE_TEMP_SENSOR = "delete_temporary_sensor"
SERVICE_LIST_TEMP_SENSORS = "list_temporary_sensors"
SERVICE_QUERY_TRAINS = "query_trains"
SERVICE_STATION_NAME_TO_CODE = "station_name_to_code"
SERVICE_TRACK_TRAIN_ENROUTE = "track_train_enroute"

# Service parameters
ATTR_STATION = "station"
ATTR_DESTINATION = "destination"
ATTR_TIME = "time"
ATTR_DURATION_MINUTES = "duration_minutes"
ATTR_SENSOR_ID = "sensor_id"
ATTR_TIME_WINDOW = "time_window_minutes"
ATTR_SERVICE_ID = "service_id"

# Default duration for temporary sensors (2 hours)
DEFAULT_DURATION_MINUTES = 120

# Store temporary sensors in hass.data
TEMP_SENSORS_KEY = f"{DOMAIN}_temp_sensors"


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for National Rail UK integration."""

    async def handle_create_temp_sensor(call: ServiceCall) -> dict:
        """Handle creating a temporary train monitoring sensor.

        Returns a dict with sensor information for service response.
        """
        station = call.data.get(ATTR_STATION, "").strip().upper()
        destination = call.data.get(ATTR_DESTINATION, "").strip().upper()
        target_time = call.data.get(ATTR_TIME)
        duration_minutes = call.data.get(ATTR_DURATION_MINUTES, DEFAULT_DURATION_MINUTES)
        time_window = call.data.get(ATTR_TIME_WINDOW, DEFAULT_TIME_WINDOW)
        sensor_id = call.data.get(ATTR_SENSOR_ID)

        # Validate inputs
        if not station or len(station) != 3:
            raise ServiceValidationError(f"station must be a 3-letter code (got: '{station}')")
        if not destination or len(destination) != 3:
            raise ServiceValidationError(f"destination must be a 3-letter code (got: '{destination}')")
        if not target_time:
            raise ServiceValidationError("time is required")

        # Generate unique sensor ID if not provided
        if not sensor_id:
            sensor_id = f"temp_{station.lower()}_{destination.lower()}_{str(uuid.uuid4())[:8]}"
        else:
            sensor_id = f"temp_{sensor_id}"

        _LOGGER.info(
            "Creating temporary sensor %s: %s to %s at %s (duration: %d min)",
            sensor_id,
            station,
            destination,
            target_time,
            duration_minutes
        )

        # Initialize temp sensors storage if needed
        if TEMP_SENSORS_KEY not in hass.data:
            hass.data[TEMP_SENSORS_KEY] = {}

        # Check if sensor already exists
        if sensor_id in hass.data[TEMP_SENSORS_KEY]:
            raise ServiceValidationError(f"Sensor {sensor_id} already exists")

        try:
            # Import here to avoid circular dependency
            from .sensor import NationalRailScheduleCoordinator, MonitoredTrainSensor
            from .config_flow import get_global_api_token

            # Get API token
            token = await get_global_api_token(hass)
            if not token:
                raise ServiceValidationError("No API token found. Please configure a token first.")

            # Create a coordinator for this temporary sensor
            coordinator = NationalRailScheduleCoordinator(
                hass,
                token,
                station,
                [destination]
            )

            # Setup the coordinator
            await coordinator.my_api.setup_client()
            # Use async_request_refresh instead of async_config_entry_first_refresh
            # since we're not in config entry setup phase
            await coordinator.async_request_refresh()

            # Parse target_time string (HH:MM) to time object
            try:
                time_parts = target_time.split(":")
                hour = int(time_parts[0])
                minute = int(time_parts[1])
                target_time_obj = time(hour=hour, minute=minute)
            except (ValueError, IndexError) as err:
                raise ServiceValidationError(f"Invalid time format '{target_time}'. Use HH:MM") from err

            # Convert time_window minutes to timedelta
            time_window_delta = timedelta(minutes=time_window)

            # Create the monitored train sensor with correct signature
            sensor = MonitoredTrainSensor(
                hass=hass,
                name=sensor_id,
                coordinator=coordinator,
                target_time=target_time_obj,
                target_destination=destination,
                time_window=time_window_delta
            )

            # Set entity_id manually (sensor doesn't auto-generate it in this context)
            sensor.entity_id = f"sensor.{sensor_id}"
            sensor._attr_unique_id = f"{DOMAIN}_{sensor_id}"

            # Register and add the entity via entity component
            from homeassistant.helpers.entity_component import EntityComponent
            from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN

            # Get or create the sensor component
            if SENSOR_DOMAIN not in hass.data:
                hass.data[SENSOR_DOMAIN] = EntityComponent(_LOGGER, SENSOR_DOMAIN, hass)

            component = hass.data[SENSOR_DOMAIN]

            # Add the entity to the component
            await component.async_add_entities([sensor])

            _LOGGER.info("Added temporary sensor entity: %s", sensor.entity_id)

            # Calculate expiry time
            expiry_time = datetime.now() + timedelta(minutes=duration_minutes)

            # Store temp sensor info
            hass.data[TEMP_SENSORS_KEY][sensor_id] = {
                "sensor": sensor,
                "coordinator": coordinator,
                "created_at": datetime.now(),
                "expires_at": expiry_time,
                "station": station,
                "destination": destination,
                "target_time": target_time,
                "entity_id": sensor.entity_id,
            }

            # Schedule automatic deletion
            async def _delete_temp_sensor(_now):
                """Delete the temporary sensor after duration expires."""
                await _cleanup_temp_sensor(sensor_id)

            async_call_later(
                hass,
                timedelta(minutes=duration_minutes),
                _delete_temp_sensor
            )

            _LOGGER.info(
                "Temporary sensor %s created successfully (entity_id: %s, expires: %s)",
                sensor_id,
                sensor.entity_id,
                expiry_time.strftime("%Y-%m-%d %H:%M:%S")
            )

            # Return sensor information
            return {
                "sensor_id": sensor_id,
                "entity_id": sensor.entity_id,
                "station": station,
                "destination": destination,
                "target_time": target_time,
                "expires_at": expiry_time.isoformat(),
                "duration_minutes": duration_minutes,
            }

        except Exception as err:
            _LOGGER.error("Error creating temporary sensor %s: %s", sensor_id, err, exc_info=True)
            raise ServiceValidationError(f"Failed to create temporary sensor: {err}") from err

    async def handle_delete_temp_sensor(call: ServiceCall) -> None:
        """Handle deleting a temporary sensor."""
        sensor_id = call.data.get(ATTR_SENSOR_ID)

        if not sensor_id:
            raise ServiceValidationError("sensor_id is required")

        # Ensure temp_ prefix
        if not sensor_id.startswith("temp_"):
            sensor_id = f"temp_{sensor_id}"

        await _cleanup_temp_sensor(sensor_id)

    async def handle_list_temp_sensors(call: ServiceCall) -> dict:
        """List all active temporary sensors."""
        if TEMP_SENSORS_KEY not in hass.data:
            return {"sensors": [], "count": 0}

        sensors_info = []
        for sensor_id, info in hass.data[TEMP_SENSORS_KEY].items():
            sensors_info.append({
                "sensor_id": sensor_id,
                "entity_id": info["entity_id"],
                "station": info["station"],
                "destination": info["destination"],
                "target_time": info["target_time"],
                "created_at": info["created_at"].isoformat(),
                "expires_at": info["expires_at"].isoformat(),
            })

        return {"sensors": sensors_info, "count": len(sensors_info)}

    async def handle_query_trains(call: ServiceCall) -> dict:
        """Query trains without creating a sensor.

        Returns departure information for trains matching the criteria.
        """
        station = call.data.get(ATTR_STATION, "").strip().upper()
        destination = call.data.get(ATTR_DESTINATION, "").strip().upper()
        target_time = call.data.get(ATTR_TIME)  # Optional

        # Validate inputs
        if not station or len(station) != 3:
            raise ServiceValidationError(f"station must be a 3-letter code (got: '{station}')")
        if not destination or len(destination) != 3:
            raise ServiceValidationError(f"destination must be a 3-letter code (got: '{destination}')")

        _LOGGER.info(
            "Querying trains: %s to %s%s",
            station,
            destination,
            f" at {target_time}" if target_time else ""
        )

        try:
            # Import here to avoid circular dependency
            from .sensor import NationalRailScheduleCoordinator
            from .config_flow import get_global_api_token

            # Get API token
            token = await get_global_api_token(hass)
            if not token:
                raise ServiceValidationError("No API token found. Please configure a token first.")

            # Create a temporary coordinator for this query
            coordinator = NationalRailScheduleCoordinator(
                hass,
                token,
                station,
                [destination]
            )

            # Setup the coordinator and fetch data
            await coordinator.my_api.setup_client()
            # Use async_request_refresh instead of async_config_entry_first_refresh
            # since we're not in config entry setup phase
            await coordinator.async_request_refresh()

            # Get departures from coordinator data
            data = coordinator.data
            _LOGGER.info(
                "[QUERY_TRAINS DEBUG] Data received: %s",
                "None" if data is None else f"{len(str(data))} chars, keys={list(data.keys()) if isinstance(data, dict) else 'not a dict'}"
            )

            if not data:
                _LOGGER.warning("[QUERY_TRAINS] No data from coordinator for %s to %s", station, destination)
                return {
                    "success": False,
                    "message": f"No data available for {station} to {destination}",
                    "departures": [],
                    "count": 0
                }

            # The coordinator stores trains under "trains" key, not "departures"
            trains = data.get("trains", [])
            _LOGGER.info("[QUERY_TRAINS DEBUG] Found %d trains in data", len(trains))

            # Clean up coordinator
            if hasattr(coordinator.my_api, 'close_session'):
                try:
                    await coordinator.my_api.close_session()
                except Exception as err:
                    _LOGGER.debug("Error closing coordinator session: %s", err)

            # Format departure information from trains
            # Import STATION_MAP for destination name lookups
            from .stations import STATION_MAP

            formatted_departures = []
            for idx, train in enumerate(trains):
                if idx == 0:  # Log first train to see structure
                    _LOGGER.info("[QUERY_TRAINS DEBUG] First train keys: %s", list(train.keys()) if isinstance(train, dict) else "Not a dict")
                    _LOGGER.info("[QUERY_TRAINS DEBUG] First train data: %s", train)

                # Extract raw data from API response
                scheduled_raw = train.get("scheduled")  # datetime object or string
                expected_raw = train.get("expected")  # datetime object or string like "On time", "Cancelled"
                terminus = train.get("terminus", "")  # Full destination name
                operator = train.get("operator", "")
                platform = train.get("platform", "TBC")
                length = train.get("length")
                is_cancelled = train.get("isCancelled", False)
                cancel_reason = train.get("cancelReason")
                delay_reason = train.get("delayReason")
                adhoc_alerts = train.get("adhocAlerts")

                # Format scheduled departure time
                scheduled_time_str = ""
                if isinstance(scheduled_raw, datetime):
                    scheduled_time_str = scheduled_raw.strftime("%H:%M")
                elif isinstance(scheduled_raw, str):
                    scheduled_time_str = scheduled_raw

                # Format expected departure time and determine status
                expected_time_str = ""
                status = "Unknown"
                delay_minutes = None

                if is_cancelled or (isinstance(expected_raw, str) and expected_raw.lower() == "cancelled"):
                    expected_time_str = "Cancelled"
                    status = "Cancelled"
                elif isinstance(expected_raw, datetime):
                    expected_time_str = expected_raw.strftime("%H:%M")
                    # Calculate delay
                    if isinstance(scheduled_raw, datetime):
                        delay_seconds = (expected_raw - scheduled_raw).total_seconds()
                        if delay_seconds > 60:  # More than 1 minute
                            delay_minutes = int(delay_seconds / 60)
                            status = f"Delayed {delay_minutes}m"
                        else:
                            status = "On Time"
                    else:
                        status = "Scheduled"
                elif isinstance(expected_raw, str):
                    expected_time_str = expected_raw
                    # Status strings from API: "On time", "Delayed", etc.
                    if expected_raw.lower() == "on time":
                        status = "On Time"
                        expected_time_str = scheduled_time_str  # Show scheduled time for on-time trains
                    elif expected_raw.lower() == "delayed":
                        status = "Delayed"
                    else:
                        status = expected_raw
                else:
                    expected_time_str = scheduled_time_str
                    status = "Scheduled"

                # Get arrival times at destination from destinations list
                expected_arrival_str = ""
                scheduled_arrival_str = ""
                destinations_list = train.get("destinations", [])
                if destinations_list and isinstance(destinations_list, list):
                    for dest in destinations_list:
                        if isinstance(dest, dict) and dest.get("crs") == destination:
                            # Get expected arrival
                            arrival_raw = dest.get("expected_time_at_destination")
                            if isinstance(arrival_raw, datetime):
                                expected_arrival_str = arrival_raw.strftime("%H:%M")
                            elif isinstance(arrival_raw, str):
                                expected_arrival_str = arrival_raw

                            # Get scheduled arrival
                            sched_arrival_raw = dest.get("scheduled_time_at_destination")
                            if isinstance(sched_arrival_raw, datetime):
                                scheduled_arrival_str = sched_arrival_raw.strftime("%H:%M")
                            elif isinstance(sched_arrival_raw, str):
                                scheduled_arrival_str = sched_arrival_raw
                            break

                # If no matching destination found in list, check if terminus matches
                if not expected_arrival_str:
                    # Get the last destination in the list as arrival time
                    if destinations_list and len(destinations_list) > 0:
                        last_dest = destinations_list[-1]
                        if isinstance(last_dest, dict):
                            # Get expected arrival
                            arrival_raw = last_dest.get("expected_time_at_destination")
                            if isinstance(arrival_raw, datetime):
                                expected_arrival_str = arrival_raw.strftime("%H:%M")
                            elif isinstance(arrival_raw, str):
                                expected_arrival_str = arrival_raw

                            # Get scheduled arrival
                            sched_arrival_raw = last_dest.get("scheduled_time_at_destination")
                            if isinstance(sched_arrival_raw, datetime):
                                scheduled_arrival_str = sched_arrival_raw.strftime("%H:%M")
                            elif isinstance(sched_arrival_raw, str):
                                scheduled_arrival_str = sched_arrival_raw

                # Get full destination name from STATION_MAP
                destination_name = STATION_MAP.get(destination, destination)

                formatted_departures.append({
                    "operator_name": operator,
                    "destination_name": terminus or destination_name,  # Use terminus (full name) or fallback
                    "scheduled_departure_time": scheduled_time_str,
                    "expected_departure_time": expected_time_str,
                    "platform": platform if platform else "TBC",
                    "status": status,
                    "delay_minutes": delay_minutes,
                    "length": length,
                    "scheduled_arrival_time": scheduled_arrival_str if scheduled_arrival_str else "N/A",
                    "expected_arrival_time": expected_arrival_str if expected_arrival_str else "N/A",
                    "cancel_reason": cancel_reason,
                    "delay_reason": delay_reason,
                    "adhoc_alerts": adhoc_alerts,
                })

            _LOGGER.info(
                "Query successful: found %d departures from %s to %s",
                len(formatted_departures),
                station,
                destination
            )

            return {
                "success": True,
                "station": station,
                "destination": destination,
                "departures": formatted_departures,
                "count": len(formatted_departures)
            }

        except Exception as err:
            _LOGGER.error("Error querying trains %s to %s: %s", station, destination, err, exc_info=True)
            raise ServiceValidationError(f"Failed to query trains: {err}") from err

    async def handle_station_name_to_code(call: ServiceCall) -> dict:
        """Convert station name to 3-letter code with fuzzy matching.

        Returns the station code for a given station name.
        Uses rapidfuzz for fuzzy matching if exact match fails.
        """
        station_name = call.data.get("name", "").strip()

        if not station_name:
            raise ServiceValidationError("name is required")

        try:
            from .stations import STATIONS

            # Create lookup dict
            station_map = {s["name"].lower(): s["code"] for s in STATIONS}
            station_names = list(station_map.keys())

            # Try exact match first
            name_lower = station_name.lower()
            code = station_map.get(name_lower)

            if code:
                return {
                    "success": True,
                    "name": station_name,
                    "code": code,
                    "match_type": "exact"
                }

            # Try if it's already a code
            if len(station_name) == 3 and station_name.isalpha():
                return {
                    "success": True,
                    "name": station_name,
                    "code": station_name.upper(),
                    "match_type": "code"
                }

            # Try fuzzy matching with rapidfuzz
            try:
                from rapidfuzz import process, fuzz

                # Get top 5 matches to choose best one
                # Use token_set_ratio which is better for partial matches
                # and handles word order differences
                results = process.extract(
                    name_lower,
                    station_names,
                    scorer=fuzz.token_set_ratio,
                    score_cutoff=80,  # Higher threshold for better accuracy
                    limit=5
                )

                if results:
                    # Prefer matches where the input is the start of the station name
                    # This handles "london" better (prefer "London Kings Cross" over "West London")
                    best_match = None
                    best_score = 0

                    for matched_name, score, _ in results:
                        # Boost score if query is at start of station name
                        if matched_name.startswith(name_lower):
                            score += 15  # Significant boost for prefix matches
                        # Boost if it's a word boundary match (whole word)
                        elif f" {name_lower}" in matched_name or f"{name_lower} " in matched_name:
                            score += 10

                        if score > best_score:
                            best_score = score
                            best_match = matched_name

                    if best_match:
                        matched_code = station_map[best_match]

                        # Get the proper case station name from STATIONS
                        proper_name = next((s["name"] for s in STATIONS if s["code"] == matched_code), best_match)

                        _LOGGER.info(
                            "Fuzzy matched '%s' to '%s' (code: %s, score: %.1f%%)",
                            station_name,
                            proper_name,
                            matched_code,
                            best_score
                        )

                        return {
                            "success": True,
                            "name": station_name,
                            "code": matched_code,
                            "matched_name": proper_name,
                            "match_type": "fuzzy",
                            "confidence": round(best_score, 1)
                        }

            except ImportError:
                _LOGGER.warning("rapidfuzz not available, falling back to exact match only")

            # Not found
            return {
                "success": False,
                "name": station_name,
                "code": None,
                "message": f"Station '{station_name}' not found"
            }

        except Exception as err:
            _LOGGER.error("Error converting station name '%s': %s", station_name, err)
            raise ServiceValidationError(f"Failed to convert station name: {err}") from err

    async def handle_track_train_enroute(call: ServiceCall) -> dict:
        """Track a train's journey using its service ID.

        Returns current location, previous stops, and upcoming stops.
        Fires events when train passes stations or gets delayed.
        """
        service_id = call.data.get(ATTR_SERVICE_ID, "").strip()
        origin_station = call.data.get("origin_station", "").strip().upper()  # Optional origin station

        if not service_id:
            raise ServiceValidationError("service_id is required")

        _LOGGER.info("Tracking train en-route with service ID: %s", service_id)

        try:
            # Import here to avoid circular dependency
            from .client import NationalRailClient
            from .config_flow import get_global_api_token
            from .stations import STATION_MAP

            # Get API token
            token = await get_global_api_token(hass)
            if not token:
                raise ServiceValidationError("No API token found. Please configure a token first.")

            # Use origin station if provided, otherwise default to LDS for backwards compatibility
            # NOTE: GetServiceDetails returns data "relative to the station board from which
            # the serviceID field value was generated" according to National Rail API docs
            station_for_query = origin_station if origin_station else "LDS"
            if not origin_station:
                _LOGGER.warning(
                    "No origin_station provided for service %s, using default station LDS. "
                    "This may result in incomplete calling point data. Consider passing origin_station parameter.",
                    service_id
                )

            client = NationalRailClient(token, station_for_query, [])
            await client.setup_client()

            # Get service details
            raw_service_details = await client.get_service_details(service_id)

            if not raw_service_details:
                return {
                    "success": False,
                    "message": (
                        f"Service {service_id} not found or no data available. "
                        "The train may have completed its journey, or the service is no longer "
                        "tracked in live departure data. For trains that have departed, "
                        "consider using arrival board tracking at the destination station instead."
                    ),
                    "service_id": service_id,
                    "reason": "service_not_available"
                }

            # Process the service details
            service_data = client.process_service_details(raw_service_details)

            if not service_data:
                return {
                    "success": False,
                    "message": f"Failed to process service data for {service_id}",
                    "service_id": service_id
                }

            # Fire an event with the en-route data
            hass.bus.async_fire(
                "nationalrailuk_train_enroute_update",
                {
                    "service_id": service_id,
                    "operator": service_data.get("operator"),
                    "operator_code": service_data.get("operator_code"),
                    "is_cancelled": service_data.get("is_cancelled"),
                    "cancel_reason": service_data.get("cancel_reason"),
                    "delay_reason": service_data.get("delay_reason"),
                    "last_station": service_data.get("last_station"),
                    "last_station_crs": service_data.get("last_station_crs"),
                    "next_station": service_data.get("next_station"),
                    "next_station_crs": service_data.get("next_station_crs"),
                    "next_station_scheduled": service_data.get("next_station_scheduled").isoformat() if service_data.get("next_station_scheduled") else None,
                    "next_station_expected": service_data.get("next_station_expected").isoformat() if isinstance(service_data.get("next_station_expected"), datetime) else str(service_data.get("next_station_expected")),
                    "nrcc_messages": service_data.get("nrcc_messages"),
                }
            )

            # Format previous stops for response
            previous_stops = []
            for stop in service_data.get("previous_stops", []):
                previous_stops.append({
                    "name": stop["name"],
                    "crs": stop["crs"],
                    "scheduled_time": stop["scheduled_time"].strftime("%H:%M") if stop["scheduled_time"] else "N/A",
                    "actual_time": stop["actual_time"].strftime("%H:%M") if stop["actual_time"] else "N/A",
                })

            # Format upcoming stops for response
            upcoming_stops = []
            for stop in service_data.get("upcoming_stops", []):
                expected_val = stop["expected_time"]
                if isinstance(expected_val, datetime):
                    expected_str = expected_val.strftime("%H:%M")
                else:
                    expected_str = str(expected_val)

                upcoming_stops.append({
                    "name": stop["name"],
                    "crs": stop["crs"],
                    "scheduled_time": stop["scheduled_time"].strftime("%H:%M") if stop["scheduled_time"] else "N/A",
                    "expected_time": expected_str,
                })

            _LOGGER.info(
                "En-route tracking successful for service %s - Last: %s, Next: %s",
                service_id,
                service_data.get("last_station"),
                service_data.get("next_station")
            )

            return {
                "success": True,
                "service_id": service_id,
                "operator": service_data.get("operator"),
                "is_cancelled": service_data.get("is_cancelled"),
                "cancel_reason": service_data.get("cancel_reason"),
                "delay_reason": service_data.get("delay_reason"),
                "last_station": service_data.get("last_station"),
                "next_station": service_data.get("next_station"),
                "next_station_scheduled": service_data.get("next_station_scheduled").strftime("%H:%M") if service_data.get("next_station_scheduled") else None,
                "next_station_expected": service_data.get("next_station_expected").strftime("%H:%M") if isinstance(service_data.get("next_station_expected"), datetime) else str(service_data.get("next_station_expected")),
                "previous_stops": previous_stops,
                "upcoming_stops": upcoming_stops,
                "nrcc_messages": service_data.get("nrcc_messages"),
            }

        except Exception as err:
            _LOGGER.error("Error tracking train en-route for service %s: %s", service_id, err, exc_info=True)
            raise ServiceValidationError(f"Failed to track train en-route: {err}") from err

    async def _cleanup_temp_sensor(sensor_id: str) -> None:
        """Clean up a temporary sensor."""
        if TEMP_SENSORS_KEY not in hass.data:
            _LOGGER.warning("No temporary sensors found")
            return

        sensor_info = hass.data[TEMP_SENSORS_KEY].get(sensor_id)
        if not sensor_info:
            _LOGGER.warning("Temporary sensor %s not found", sensor_id)
            return

        try:
            # Remove the entity properly via entity component
            sensor = sensor_info["sensor"]
            entity_id = sensor.entity_id

            # Get the sensor component
            from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
            if SENSOR_DOMAIN in hass.data:
                component = hass.data[SENSOR_DOMAIN]
                # Remove entity from component
                await component.async_remove_entity(entity_id)
            else:
                # Fallback: remove directly
                await sensor.async_will_remove_from_hass()
                hass.states.async_remove(entity_id)

            # Clean up coordinator if needed
            coordinator = sensor_info.get("coordinator")
            if coordinator and hasattr(coordinator.my_api, 'close_session'):
                try:
                    await coordinator.my_api.close_session()
                except Exception as err:
                    _LOGGER.debug("Error closing coordinator session: %s", err)

            # Remove from storage
            del hass.data[TEMP_SENSORS_KEY][sensor_id]

            _LOGGER.info("Temporary sensor %s deleted successfully", sensor_id)

        except Exception as err:
            _LOGGER.error("Error cleaning up temporary sensor %s: %s", sensor_id, err)

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_TEMP_SENSOR,
        handle_create_temp_sensor,
        schema=vol.Schema({
            vol.Required(ATTR_STATION): cv.string,
            vol.Required(ATTR_DESTINATION): cv.string,
            vol.Required(ATTR_TIME): cv.string,
            vol.Optional(ATTR_DURATION_MINUTES, default=DEFAULT_DURATION_MINUTES): cv.positive_int,
            vol.Optional(ATTR_TIME_WINDOW, default=DEFAULT_TIME_WINDOW): cv.positive_int,
            vol.Optional(ATTR_SENSOR_ID): cv.string,
        }),
        supports_response=True,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_TEMP_SENSOR,
        handle_delete_temp_sensor,
        schema=vol.Schema({
            vol.Required(ATTR_SENSOR_ID): cv.string,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_TEMP_SENSORS,
        handle_list_temp_sensors,
        schema=vol.Schema({}),
        supports_response=True,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY_TRAINS,
        handle_query_trains,
        schema=vol.Schema({
            vol.Required(ATTR_STATION): cv.string,
            vol.Required(ATTR_DESTINATION): cv.string,
            vol.Optional(ATTR_TIME): cv.string,
        }),
        supports_response=True,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_STATION_NAME_TO_CODE,
        handle_station_name_to_code,
        schema=vol.Schema({
            vol.Required("name"): cv.string,
        }),
        supports_response=True,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_TRACK_TRAIN_ENROUTE,
        handle_track_train_enroute,
        schema=vol.Schema({
            vol.Required(ATTR_SERVICE_ID): cv.string,
        }),
        supports_response=True,
    )

    _LOGGER.info("National Rail UK services registered")
