"""RealTimeTrains UID resolver - match National Rail trains to RTT UIDs"""
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
import httpx
from bs4 import BeautifulSoup

from .stations import STATION_MAP

_LOGGER = logging.getLogger(__name__)


async def get_rtt_uid(
    origin_crs: str,
    destination_crs: str,
    scheduled_departure: datetime,
    operator_code: Optional[str] = None
) -> Optional[str]:
    """
    Resolve a National Rail train to its RealTimeTrains UID by querying RTT web interface.

    Args:
        origin_crs: Origin station code (e.g., "YRK")
        destination_crs: Destination station code (e.g., "EDB")
        scheduled_departure: Scheduled departure time as datetime
        operator_code: Optional operator code for better matching (e.g., "GR")

    Returns:
        UID string (e.g., "C41093") or None if not found
    """
    try:
        # Format time window (±15 minutes around scheduled departure)
        start_time = scheduled_departure - timedelta(minutes=15)
        end_time = scheduled_departure + timedelta(minutes=15)

        date_str = scheduled_departure.strftime("%Y-%m-%d")
        start_time_str = start_time.strftime("%H%M")
        end_time_str = end_time.strftime("%H%M")

        # Construct RealTimeTrains URL
        url = (
            f"https://www.realtimetrains.co.uk/search/detailed/"
            f"gb-nr:{origin_crs}/{date_str}/{start_time_str}-{end_time_str}"
            f"?stp=WVS&show=all&order=wtt"
        )

        _LOGGER.debug(
            "Querying RTT for %s->%s at %s: %s",
            origin_crs,
            destination_crs,
            scheduled_departure.strftime("%H:%M"),
            url
        )

        # Fetch the page
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()

        # Parse HTML
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find all service links
        # RTT now uses <a class="service"> for each service
        service_links = soup.find_all('a', class_='service')

        _LOGGER.debug("Found %d service links on RTT page", len(service_links))

        if not service_links:
            _LOGGER.warning("No services found on RTT page")
            return None

        # Match by destination and time
        target_time_str = scheduled_departure.strftime("%H:%M")
        _LOGGER.debug("Target: %s->%s at %s", origin_crs, destination_crs, target_time_str)

        # Fallback candidates when RTT destination text doesn't match the requested stop,
        # but departure time does (common for intermediate-stop tracking).
        fallback_time_matches = []
        fallback_operator_matches = []

        for idx, service_link in enumerate(service_links):
            # Extract UID from href
            href = service_link.get('href', '')
            uid_match = re.search(r'/service/gb-nr:([A-Z0-9]+)/', href)
            if not uid_match:
                _LOGGER.debug("Service %d: No UID found in href: %s", idx, href)
                continue

            uid = uid_match.group(1)

            # Check destination (look for div.location.d span)
            dest_div = service_link.find('div', class_='location d')
            if not dest_div:
                _LOGGER.debug("Service %d (UID %s): No destination div found", idx, uid)
                continue

            dest_span = dest_div.find('span')
            if not dest_span:
                # Could be "Terminates here" or "Starts here"
                _LOGGER.debug("Service %d (UID %s): Destination div exists but no span (terminus/origin)", idx, uid)
                continue

            destination = dest_span.text.strip()
            _LOGGER.debug("Service %d: UID=%s, Destination=%s", idx, uid, destination)

            # Check if destination matches (by name or CRS code)
            # Note: RTT shows full station names, not CRS codes
            dest_name = STATION_MAP.get(destination_crs, destination_crs)
            destination_matches = (
                destination_crs.lower() in destination.lower()
                or destination.lower() in dest_name.lower()
                or dest_name.lower() in destination.lower()
            )

            # Check departure time - look for the ORIGIN departure time
            # On RTT search results, the origin time is in a div with class "time plan d o gbtt" (or wtt)
            # The "o" stands for "origin". If that doesn't exist, fall back to any "time plan d"
            # First try to find origin-specific departure time
            origin_time_divs = service_link.find_all('div', class_='time plan d o gbtt')
            if not origin_time_divs:
                origin_time_divs = service_link.find_all('div', class_='time plan d o wtt')

            # If no origin-specific time, fall back to any departure time (take first one)
            if not origin_time_divs:
                origin_time_divs = service_link.find_all('div', class_='time plan d gbtt')
                if not origin_time_divs:
                    origin_time_divs = service_link.find_all('div', class_='time plan d wtt')

            _LOGGER.debug("Service %d (UID %s to %s): Found %d time divs", idx, uid, destination, len(origin_time_divs))
            service_time = None
            for time_idx, time_div in enumerate(origin_time_divs):
                time_classes = time_div.get('class', [])
                time_text = time_div.text.strip()
                _LOGGER.debug("  Time div %d: classes=%s, text='%s'", time_idx, time_classes, time_text)
                # Time should be like "1036" (4 digits)
                if time_text and len(time_text) == 4 and time_text.isdigit():
                    service_time = f"{time_text[:2]}:{time_text[2:]}"
                    _LOGGER.debug("  -> Matched! service_time=%s", service_time)
                    break

            if not service_time:
                _LOGGER.debug("Service %d (UID %s to %s): No departure time found", idx, uid, destination)
                continue

            _LOGGER.debug("Service %d (UID %s to %s): Departure time %s", idx, uid, destination, service_time)

            # Check time match with a small tolerance. RTT search listings can be off by a minute
            # versus the source feed for some services.
            time_diff = abs_time_diff(service_time, target_time_str)
            if time_diff > 1:
                _LOGGER.debug("Service %d (UID %s): Time mismatch - %s vs %s (diff: %d min)",
                            idx, uid, service_time, target_time_str, time_diff)
                continue

            # Check operator if provided
            operator_text = None
            if operator_code:
                toc_div = service_link.find('div', class_='toc')
                if toc_div:
                    operator_text = toc_div.text.strip()
                    if operator_code.lower() != operator_text.lower():
                        # Operator mismatch, but don't skip - might still be correct
                        _LOGGER.debug("Operator mismatch: expected %s, found %s", operator_code, operator_text)

            if destination_matches:
                _LOGGER.info(
                    "Matched RTT service: %s->%s at %s = UID %s",
                    origin_crs,
                    destination,
                    service_time,
                    uid
                )
                return uid

            # Destination mismatch fallback: keep exact-time candidates in case this is
            # an intermediate stop train (e.g. train terminates beyond requested stop).
            _LOGGER.debug(
                "Service %d (UID %s): Destination mismatch - want %s/%s, got %s",
                idx,
                uid,
                destination_crs,
                dest_name,
                destination,
            )
            fallback_time_matches.append((uid, destination, operator_text))
            if operator_code and operator_text and operator_text.lower() == operator_code.lower():
                fallback_operator_matches.append((uid, destination, operator_text))

        # Prefer a unique operator+time match if destination didn't match.
        if len(fallback_operator_matches) == 1:
            uid, destination, _ = fallback_operator_matches[0]
            _LOGGER.warning(
                "Using RTT fallback match by operator+time for %s->%s at %s: UID %s (RTT destination: %s)",
                origin_crs,
                destination_crs,
                target_time_str,
                uid,
                destination,
            )
            return uid

        # If there is only one exact-time candidate, accept it as fallback.
        if len(fallback_time_matches) == 1:
            uid, destination, _ = fallback_time_matches[0]
            _LOGGER.warning(
                "Using RTT fallback match by exact departure time for %s->%s at %s: UID %s (RTT destination: %s)",
                origin_crs,
                destination_crs,
                target_time_str,
                uid,
                destination,
            )
            return uid

        _LOGGER.warning(
            "No matching RTT service found for %s->%s at %s",
            origin_crs,
            destination_crs,
            target_time_str
        )
        return None

    except httpx.HTTPError as err:
        _LOGGER.error("HTTP error fetching RTT data: %s", err)
        return None
    except Exception as err:
        _LOGGER.error("Error resolving RTT UID: %s", err, exc_info=True)
        return None


def abs_time_diff(time1_str: str, time2_str: str) -> int:
    """Calculate absolute difference in minutes between two HH:MM time strings"""
    try:
        h1, m1 = map(int, time1_str.split(':'))
        h2, m2 = map(int, time2_str.split(':'))

        min1 = h1 * 60 + m1
        min2 = h2 * 60 + m2

        return abs(min1 - min2)
    except (ValueError, IndexError):
        return 999  # Large number to indicate parsing error
