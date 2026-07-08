"""Real Time Trains web scraper for en-route position tracking."""
import logging
import re
from datetime import datetime
from typing import Optional, Dict, List
import httpx
from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)


class RTTScraper:
    """Scraper for Real Time Trains website."""

    def __init__(self):
        """Initialize the scraper."""
        self.base_url = "https://www.realtimetrains.co.uk"

    async def get_train_position(
        self,
        rtt_uid: str,
        date: str = None
    ) -> Optional[Dict]:
        """Get current position of train from RTT simple page.

        Args:
            rtt_uid: RTT UID (e.g., "Y11923")
            date: Date in YYYY-MM-DD format (defaults to today)

        Returns:
            Dict with:
                - last_station: Name of last station passed
                - last_station_crs: CRS code of last station
                - next_station: Name of next station
                - next_station_crs: CRS code of next station
                - calling_points: List of all calling points with status
                - is_delayed: Boolean if train is delayed
                - url: The RTT URL used
        """
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        url = f"{self.base_url}/service/gb-nr:{rtt_uid}/{date}"

        try:
            _LOGGER.debug("Fetching RTT data from: %s", url)

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                response.raise_for_status()

            html = response.text
            soup = BeautifulSoup(html, 'html.parser')

            # Find all calling point divs
            calling_points_divs = soup.find_all('div', class_='location call public')

            if not calling_points_divs:
                _LOGGER.warning("No calling points found in RTT page: %s", url)
                return None

            calling_points = []
            last_passed_station = None
            next_upcoming_station = None

            for div in calling_points_divs:
                # Extract station name and CRS
                station_link = div.find('a', class_='name')
                if not station_link:
                    continue

                crs_span = station_link.find('span', class_='crs')
                if not crs_span:
                    continue

                crs = crs_span.text.strip()
                # Station name is the text after the CRS span
                station_name = station_link.text.replace(crs, '').strip()

                # Check if train has passed this station
                # Look for realtime times with 'act' class (actual/passed)
                realtime_divs = div.find_all('div', class_=re.compile(r'realtime.*act'))
                has_passed = len(realtime_divs) > 0

                # Check if station is upcoming
                # Look for realtime times with 'exp' class (expected) or without 'act'
                is_upcoming = not has_passed

                # Extract times
                realtime_arr_div = div.find('div', class_=re.compile(r'realtime arr'))
                realtime_dep_div = div.find('div', class_=re.compile(r'realtime dep'))

                realtime_arr = realtime_arr_div.text.strip() if realtime_arr_div else None
                realtime_dep = realtime_dep_div.text.strip() if realtime_dep_div else None

                # Extract delay info
                delay_div = div.find('div', class_='realtime delay')
                delay = delay_div.text.strip() if delay_div and delay_div.text.strip() else None

                calling_point = {
                    "crs": crs,
                    "name": station_name,
                    "has_passed": has_passed,
                    "realtime_arr": realtime_arr,
                    "realtime_dep": realtime_dep,
                    "delay": delay,
                }
                calling_points.append(calling_point)

                # Track last passed and next upcoming
                if has_passed:
                    last_passed_station = {
                        "crs": crs,
                        "name": station_name,
                        "time": realtime_dep or realtime_arr
                    }
                elif is_upcoming and not next_upcoming_station:
                    next_upcoming_station = {
                        "crs": crs,
                        "name": station_name,
                        "time": realtime_arr or realtime_dep
                    }

            # Check if train is delayed (any calling point has delay indicator)
            is_delayed = any(cp.get("delay") for cp in calling_points)

            result = {
                "last_station": last_passed_station["name"] if last_passed_station else None,
                "last_station_crs": last_passed_station["crs"] if last_passed_station else None,
                "last_station_time": last_passed_station["time"] if last_passed_station else None,
                "next_station": next_upcoming_station["name"] if next_upcoming_station else None,
                "next_station_crs": next_upcoming_station["crs"] if next_upcoming_station else None,
                "next_station_time": next_upcoming_station["time"] if next_upcoming_station else None,
                "calling_points": calling_points,
                "is_delayed": is_delayed,
                "url": url,
            }

            _LOGGER.info(
                "RTT scraper: Train between %s and %s (delayed: %s)",
                result["last_station"],
                result["next_station"],
                is_delayed
            )

            return result

        except httpx.HTTPError as err:
            _LOGGER.error("HTTP error fetching RTT data from %s: %s", url, err)
            return None
        except Exception as err:
            _LOGGER.error("Error parsing RTT data from %s: %s", url, err, exc_info=True)
            return None
