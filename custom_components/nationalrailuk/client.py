"""Client for the National Rail API"""

from datetime import datetime, timedelta
import logging

import httpx
from zeep import AsyncClient, Settings, xsd
from zeep.exceptions import Fault
from zeep.plugins import HistoryPlugin
from zeep.transports import AsyncTransport

from .const import WSDL
from .stations import STATION_MAP # Import if needed

_LOGGER = logging.getLogger(__name__)


class NationalRailClientException(Exception):
    """Base exception class."""


class NationalRailClientInvalidToken(NationalRailClientException):
    """Token is Invalid"""


class NationalRailClientInvalidInput(NationalRailClientException):
    """Invalid input parameters such as station codes"""


def rebuild_date(base, time):
    """
    Rebuild a date time object from the simplified representation returned by the api.
    Handles HH:MM strings and potential midnight wrap-around.
    Returns datetime object or None if parsing fails.
    """
    if isinstance(time, datetime): return time # Already datetime
    if not isinstance(time, str) or not time or ":" not in time:
        _LOGGER.debug("Invalid time format for rebuild_date: %s (%s)", time, type(time).__name__)
        return None

    try:
        time_parts = time.split(":")
        hour = int(time_parts[0])
        minute = int(time_parts[1])

        date_object = datetime(
            base.year, base.month, base.day, hour, minute, tzinfo=base.tzinfo
        )

        # Handle midnight wrap-around
        if (date_object - datetime.now(tz=base.tzinfo)).total_seconds() < -4 * 60 * 60:
            new_base = base + timedelta(days=1)
            date_object = datetime(
                new_base.year, new_base.month, new_base.day,
                hour, minute, tzinfo=new_base.tzinfo,
            )
        return date_object
    except (ValueError, IndexError) as e:
        _LOGGER.warning("Failed to rebuild date for time '%s': %s", time, e)
        return None

class NationalRailClient:
    """Client for the National Rail API"""

    def __init__(self, api_token, station, destinations) -> None:
        """Initialize the client."""
        self.station = station
        self.api_token = api_token
        # Ensure destinations is always a list, even if None passed
        self.destinations = destinations if isinstance(destinations, list) else []
        self.client = None
        self.header_value = None

        # Prepackage the authorization token structure
        self.header = xsd.Element(
            "{http://thalesgroup.com/RTTI/2013-11-28/Token/types}AccessToken",
            xsd.ComplexType(
                [
                    xsd.Element(
                        "{http://thalesgroup.com/RTTI/2013-11-28/Token/types}TokenValue",
                        xsd.String(),
                    ),
                ]
            ),
        )

    async def setup_client(self):
        """Set up the SOAP client asynchronously."""
        if self.client is not None:
            return

        import asyncio

        # Use recommended settings from previous attempts
        settings = Settings(strict=False, xml_huge_tree=True)
        history = HistoryPlugin()

        def create_transport():
            # Use reasonable timeouts
            wsdl_client = httpx.Client(verify=True, timeout=60)
            httpx_client = httpx.AsyncClient(verify=True, timeout=60)
            return AsyncTransport(client=httpx_client, wsdl_client=wsdl_client, operation_timeout=60)

        transport = await asyncio.to_thread(create_transport)

        self.client = AsyncClient(
            wsdl=WSDL,
            transport=transport,
            settings=settings,
            plugins=[history]
        )
        self.header_value = self.header(TokenValue=self.api_token)

    async def get_raw_departures(self):
        """Get the raw data from the api using GetDepBoardWithDetails."""
        if self.client is None: await self.setup_client()

        _LOGGER.debug("Fetching departures for %s using GetDepBoardWithDetails. Destinations: %s", self.station, self.destinations)

        res = None # Initialize res
        try:
            if not self.destinations:
                _LOGGER.debug("No filter destination specified for %s.", self.station)
                res = await self.client.service.GetDepBoardWithDetails(
                    numRows=15,
                    crs=self.station,
                    _soapheaders=[self.header_value]
                )
            else:
                # Initialize combined_res to hold the structure
                # We need to know the base structure - assuming it has locationName, trainServices etc.
                # Let's fetch the first batch to establish the base structure
                first_dest = self.destinations[0]
                _LOGGER.debug("Fetching initial batch for %s to %s.", self.station, first_dest)
                combined_res = await self.client.service.GetDepBoardWithDetails(
                        numRows=10, crs=self.station, filterCrs=first_dest,
                        filterType="to", _soapheaders=[self.header_value],
                    )

                # Ensure trainServices.service exists and is a list
                if not hasattr(combined_res, 'trainServices') or combined_res.trainServices is None:
                    combined_res.trainServices = self.client.get_type('ns0:ArrayOfServiceItemsWithCallingPoints')() # Create appropriate type
                if not hasattr(combined_res.trainServices, 'service') or combined_res.trainServices.service is None:
                     combined_res.trainServices.service = [] # Initialize as empty list

                # Fetch remaining batches
                for dest_crs in self.destinations[1:]:
                    _LOGGER.debug("Fetching batch for %s to %s.", self.station, dest_crs)
                    try:
                        batch = await self.client.service.GetDepBoardWithDetails(
                            numRows=10, crs=self.station, filterCrs=dest_crs,
                            filterType="to", _soapheaders=[self.header_value],
                        )
                        # Merge services if batch has them
                        if hasattr(batch, 'trainServices') and batch.trainServices and hasattr(batch.trainServices, 'service') and batch.trainServices.service:
                            # Ensure the service list in combined_res is actually a list
                            if not isinstance(combined_res.trainServices.service, list):
                                 # If it was a single object, put it in a list first
                                 combined_res.trainServices.service = [combined_res.trainServices.service]
                            combined_res.trainServices.service.extend(
                                batch.trainServices.service if isinstance(batch.trainServices.service, list) else [batch.trainServices.service]
                            )
                        # Merge NRCC messages (using getattr for safety)
                        batch_nrcc = getattr(batch, 'nrccMessages', None)
                        if batch_nrcc and getattr(batch_nrcc, 'message', None):
                             combined_nrcc = getattr(combined_res, 'nrccMessages', None)
                             if not combined_nrcc:
                                  # Need to create the correct type if it doesn't exist
                                  # Find the type name by inspecting the WSDL or a successful response
                                  # Example: NRCCMessageType = self.client.get_type('ns0:NRCCMessages')
                                  # combined_res.nrccMessages = NRCCMessageType(message=[]) # Create instance
                                  # For now, let's handle simple append if possible, might need refinement
                                  _LOGGER.warning("Cannot reliably merge NRCC messages across batches without knowing the exact type name.")
                                  pass # Skip complex merge for now
                             elif hasattr(combined_nrcc, 'message') and combined_nrcc.message is not None:
                                  # Append messages if possible
                                   batch_messages = getattr(batch_nrcc, 'message', [])
                                   if not isinstance(batch_messages, list): batch_messages = [batch_messages]
                                   if not isinstance(combined_nrcc.message, list): combined_nrcc.message = [combined_nrcc.message]
                                   combined_nrcc.message.extend(batch_messages)

                    except Fault as batch_err:
                         _LOGGER.error("SOAP Fault fetching batch for %s to %s: %s", self.station, dest_crs, batch_err.message)
                         continue
                    except Exception as batch_err:
                         _LOGGER.error("Error fetching batch for %s to %s: %s", self.station, dest_crs, batch_err, exc_info=True)
                         continue
                res = combined_res # Final result is the combined one

        except Fault as err:
             _LOGGER.error("SOAP Fault during GetDepBoardWithDetails: %s", err.message)
             raise err # Re-raise to be caught in async_get_data
        except Exception as e:
             _LOGGER.error("Error during GetDepBoardWithDetails call: %s", e, exc_info=True)
             raise NationalRailClientException("Error calling GetDepBoardWithDetails") from e

        # --- Validation using getattr ---
        if not res or not hasattr(res, 'locationName'): # Check attribute existence
             _LOGGER.warning("GetDepBoardWithDetails response seems invalid or empty. Object type: %s", type(res).__name__)
             return None

        _LOGGER.debug("GetDepBoardWithDetails call successful for %s.", self.station)
        return res

    def process_data(self, station_board):
        """
        Unpack the data return by the api (expects Zeep object like StationBoardWithDetails).
        Uses getattr for safe access.
        """
        status = {}
        status["trains"] = []
        status["nrccMessages"] = None

        # Use getattr with defaults
        status["station"] = getattr(station_board, 'locationName', self.station)
        _LOGGER.debug("Processing data for station: %s", status["station"])
        time_base = getattr(station_board, 'generatedAt', datetime.now().astimezone())
        _LOGGER.debug("Time base for processing: %s", time_base)

        # --- NRCC Messages ---
        raw_nrcc = getattr(station_board, 'nrccMessages', None)
        if raw_nrcc:
            messages_list = []
            # Access message list safely
            message_entries = getattr(raw_nrcc, 'message', [])
            # Handle case where API might return single message not in a list
            if message_entries and not isinstance(message_entries, list):
                message_entries = [message_entries]
            if message_entries: # Check if list has content
                for entry in message_entries:
                    msg_text = None
                    if isinstance(entry, str): msg_text = entry
                    elif hasattr(entry, '_value_1'): msg_text = entry._value_1 # Common Zeep pattern
                    elif hasattr(entry, 'value'): msg_text = entry.value # Another pattern
                    if isinstance(msg_text, str) and msg_text.strip():
                        cleaned_msg = ' '.join(msg_text.strip().split())
                        messages_list.append(cleaned_msg)
            status["nrccMessages"] = messages_list if messages_list else None
        _LOGGER.debug("Extracted NRCC Messages: %s", status["nrccMessages"])


        # --- Train Services ---
        train_services_container = getattr(station_board, 'trainServices', None)
        services_list = [] # Default to empty list
        if train_services_container:
            services_list = getattr(train_services_container, 'service', [])
            # Handle case where API returns single service not in list
            if services_list and not isinstance(services_list, list):
                services_list = [services_list]

        if not services_list:
             _LOGGER.info("No train services found or service list empty for %s.", status["station"])
             return status # Return status with potentially NRCC messages but no trains

        processed_serviceIDs = set()

        for service in services_list:
            final_destinations_for_output = [] # Initialize here per service

            # --- Basic Service Info & Duplicate Check ---
            service_id = getattr(service, 'serviceID', None)
            if not service_id or service_id in processed_serviceIDs: continue
            processed_serviceIDs.add(service_id)

            # --- Departure Times ---
            std_str = getattr(service, 'std', None)
            scheduled_time = rebuild_date(time_base, std_str)
            if scheduled_time is None: continue

            etd_str = getattr(service, 'etd', 'On time')
            expected_time_value = None
            expected_time_status = None
            parsed_etd_dt = rebuild_date(time_base, etd_str)

            if isinstance(parsed_etd_dt, datetime):
                 expected_time_value = parsed_etd_dt
            elif isinstance(parsed_etd_dt, str):
                 expected_time_status = parsed_etd_dt
                 if expected_time_status == "On time": expected_time_value = scheduled_time
            else:
                 expected_time_value = scheduled_time

            # --- Platform & Terminus ---
            platform = getattr(service, 'platform', None)
            terminus_name = "Unknown"
            destination_info = getattr(service, 'destination', None)
            if destination_info:
                 location_list = getattr(destination_info, 'location', [])
                 if location_list and not isinstance(location_list, list): location_list = [location_list] # Ensure list
                 if location_list and hasattr(location_list[0], 'locationName'):
                      terminus_name = getattr(location_list[0], 'locationName', "Unknown")

            # --- NEW Fields Extraction ---
            operator_name = getattr(service, 'operator', None)
            operator_code = getattr(service, 'operatorCode', None)
            length = getattr(service, 'length', None)
            is_cancelled_raw = getattr(service, 'isCancelled', False)
            is_cancelled_bool = is_cancelled_raw is True
            cancel_reason = getattr(service, 'cancelReason', None)
            delay_reason = getattr(service, 'delayReason', None)
            raw_adhoc = getattr(service, 'adhocAlerts', None)
            final_adhoc_alerts = None
            if raw_adhoc: # Simplified adhoc processing - improve if needed
                 adhoc_msg = getattr(raw_adhoc, 'message', None)
                 if isinstance(adhoc_msg, str) and adhoc_msg.strip():
                     final_adhoc_alerts = ' '.join(adhoc_msg.strip().split())

            # --- Perturbation Flag ---
            perturbation = False
            final_expected_output = expected_time_value if expected_time_value else expected_time_status
            if is_cancelled_bool:
                 perturbation = True
                 final_expected_output = "Cancelled"
            elif expected_time_status in ["Delayed", "Cancelled"] or cancel_reason or delay_reason or final_adhoc_alerts:
                 perturbation = True
                 if expected_time_status == "Cancelled": final_expected_output = "Cancelled"
            elif isinstance(expected_time_value, datetime) and (expected_time_value - scheduled_time).total_seconds() > 90:
                 perturbation = True


            # --- Calling Points Processing & Filtering ---
            calling_points_list_container = getattr(service, 'subsequentCallingPoints', None)
            calling_point_list = []
            target_destination_found_in_cps = False
            terminus_cp_data_for_output = None
            temp_processed_cps = []

            if calling_points_list_container:
                 cp_list_wrapper = getattr(calling_points_list_container, 'callingPointList', [])
                 if cp_list_wrapper and not isinstance(cp_list_wrapper, list): cp_list_wrapper = [cp_list_wrapper] # Ensure list
                 if cp_list_wrapper and hasattr(cp_list_wrapper[0], 'callingPoint'):
                      calling_point_list = getattr(cp_list_wrapper[0], 'callingPoint', [])
                      if calling_point_list and not isinstance(calling_point_list, list): calling_point_list = [calling_point_list] # Ensure list

            if calling_point_list:
                 # --- Find & Process Terminus Info ---
                 raw_terminus_cp = calling_point_list[-1]
                 terminus_sched_arrival = rebuild_date(time_base, getattr(raw_terminus_cp, 'st', None))
                 terminus_exp_arrival_raw = getattr(raw_terminus_cp, 'et', 'On time')
                 terminus_exp_arrival_dt = rebuild_date(time_base, terminus_exp_arrival_raw)
                 final_terminus_expected = terminus_exp_arrival_dt if terminus_exp_arrival_dt else terminus_exp_arrival_raw
                 if terminus_sched_arrival:
                      terminus_cp_data_for_output = {
                           "name": getattr(raw_terminus_cp, 'locationName', terminus_name),
                           "crs": getattr(raw_terminus_cp, 'crs', None),
                           "scheduled_time_at_destination": terminus_sched_arrival,
                           "expected_time_at_destination": final_terminus_expected,
                      }

                 # --- Process all CPs and check against filter ---
                 for cp in calling_point_list:
                     cp_crs = getattr(cp, 'crs', None)
                     st_str = getattr(cp, 'st', None)
                     if not cp_crs or not st_str: continue

                     is_target = self.destinations and cp_crs in self.destinations
                     if is_target: target_destination_found_in_cps = True

                     # Process the CP data
                     cp_name = getattr(cp, 'locationName', 'Unknown')
                     et_str = getattr(cp, 'et', 'On time')
                     scheduled_arrival_at_cp = rebuild_date(time_base, st_str)
                     if scheduled_arrival_at_cp is None: continue

                     expected_arrival_at_cp_dt = rebuild_date(time_base, et_str)
                     final_cp_expected = expected_arrival_at_cp_dt if expected_arrival_at_cp_dt else et_str

                     temp_processed_cps.append({
                         "name": cp_name, "crs": cp_crs,
                         "scheduled_time_at_destination": scheduled_arrival_at_cp,
                         "expected_time_at_destination": final_cp_expected,
                     })

            # --- Filter Check & Build Final Destination List ---
            include_train = True
            terminus_matches_filter = False
            if self.destinations:
                include_train = False # Assume excluded
                if target_destination_found_in_cps:
                    include_train = True
                    for cp_data in temp_processed_cps:
                         if cp_data.get("crs") in self.destinations:
                             final_destinations_for_output.append(cp_data)
                elif terminus_cp_data_for_output and terminus_cp_data_for_output.get("crs") in self.destinations:
                     include_train = True
                     final_destinations_for_output.append(terminus_cp_data_for_output) # Add only terminus if it matches


            # --- Skip or Build Train ---
            if not include_train:
                 _LOGGER.debug("FILTER: Skipping service %s based on destination filter %s.", service_id, self.destinations)
                 continue

            destinations_list_for_train = final_destinations_for_output if self.destinations else ([terminus_cp_data_for_output] if terminus_cp_data_for_output else [])

            train = {
                "scheduled": scheduled_time,
                "expected": final_expected_output,
                "terminus": terminus_name,
                "destinations": destinations_list_for_train,
                "platform": platform,
                "perturbation": perturbation,
                "operator": operator_name,
                "operatorCode": operator_code,
                "length": length,
                "isCancelled": is_cancelled_bool,
                "cancelReason": cancel_reason,
                "delayReason": delay_reason,
                "adhocAlerts": final_adhoc_alerts,
                "serviceId": service_id,
            }
            status["trains"].append(train)


        # --- Sort Trains ---
        def sort_key(train_dict):
           # ... sort key logic ...
            expected_val = train_dict.get("expected") # Use .get for safety
            scheduled_val = train_dict.get("scheduled")
            # Fallback if scheduled somehow missing (shouldn't happen)
            if not isinstance(scheduled_val, datetime): return datetime.max.astimezone()

            if isinstance(expected_val, datetime): return expected_val
            if isinstance(expected_val, str):
                 if expected_val.lower() == "cancelled": return scheduled_val + timedelta(hours=24)
                 if expected_val.lower() == "delayed": return scheduled_val + timedelta(hours=23)
            # Fallback for "On time", None, or other strings
            return scheduled_val
            
        try:
            status["trains"] = sorted(status["trains"], key=sort_key)
            _LOGGER.debug("Sorted %d final trains.", len(status["trains"]))
        except Exception as sort_err:
            _LOGGER.error("Error sorting final trains list: %s", sort_err, exc_info=True)

        return status

    async def async_get_data(self):
        """Data refresh function called by the coordinator"""
        if self.client is None: await self.setup_client()

        raw_data = None
        try:
            _LOGGER.info("Requesting departure data for %s", self.station)
            raw_data = await self.get_raw_departures()
            if raw_data is None:
                _LOGGER.error("get_raw_departures returned None for %s", self.station)
                return {"trains": [], "station": STATION_MAP.get(self.station, self.station), "error": "API call failed or returned invalid data"}

        except Fault as err:
            _LOGGER.exception("SOAP Fault whilst fetching data: ")
            if "Invalid Access Token" in err.message: raise NationalRailClientInvalidToken("Invalid API token") from err
            if "Could not resolve CRS code" in err.message: raise NationalRailClientInvalidInput(f"Invalid station input: {self.station}") from err
            raise NationalRailClientException(f"Unknown SOAP Fault: {err.message}") from err
        except httpx.RequestError as err:
             _LOGGER.error("HTTP error occurred while fetching data: %s", err)
             raise NationalRailClientException("HTTP Connection Error") from err
        except Exception as err:
             _LOGGER.exception("Unexpected error whilst fetching data: ")
             raise NationalRailClientException("Unknown Error during fetch") from err

        try:
            _LOGGER.info("Processing station schedule for %s", self.station)
            data = self.process_data(raw_data) # Pass the Zeep object directly
        except Exception as err:
            _LOGGER.exception("Exception whilst processing data: ")
            raise NationalRailClientException("Unexpected error processing API data") from err

        # Ensure station name is present
        if "station" not in data:
             # Use getattr on the raw_data object here
             data["station"] = getattr(raw_data, 'locationName', STATION_MAP.get(self.station, self.station))

        return data