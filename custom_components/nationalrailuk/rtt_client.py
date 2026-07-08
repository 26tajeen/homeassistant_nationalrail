"""RealTimeTrains data.rtt.io API client for live in-journey tracking.

Replaces the old HTML-scraping approach (rtt_scraper.py + rtt_resolver.py) with
the official data.rtt.io API. The portal-issued token is a *refresh* token:
GET /api/get_access_token with it as bearer returns a short-lived access token,
which we cache and re-exchange on expiry. Data calls use the gb-nr namespace.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

_LOGGER = logging.getLogger(__name__)

RTT_BASE = "https://data.rtt.io"


class RTTError(Exception):
    """Raised when the RTT API returns an error we can't recover from."""


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class RTTClient:
    """Minimal async client for the data.rtt.io API (gb-nr namespace)."""

    def __init__(self, refresh_token: str, namespace: str = "gb-nr") -> None:
        self._refresh = refresh_token
        self._ns = namespace
        self._access: str | None = None
        self._access_expiry: datetime | None = None

    async def _ensure_access(self, client: httpx.AsyncClient) -> str:
        """Return a valid access token, exchanging the refresh token if needed."""
        now = datetime.now(timezone.utc)
        if self._access and self._access_expiry and now < self._access_expiry:
            return self._access

        resp = await client.get(
            f"{RTT_BASE}/api/get_access_token",
            headers={"Authorization": f"Bearer {self._refresh}"},
        )
        if resp.status_code != 200:
            raise RTTError(f"token exchange failed: HTTP {resp.status_code}")
        data = resp.json()
        token = data.get("token")
        if not token:
            raise RTTError("no access token in exchange response")
        self._access = token
        expiry = _parse_iso(data.get("validUntil"))
        # Refresh a minute early; fall back to a conservative 5 min if absent.
        self._access_expiry = (
            expiry - timedelta(seconds=60) if expiry else now + timedelta(minutes=5)
        )
        _LOGGER.debug("RTT access token obtained, valid until %s", self._access_expiry)
        return token

    async def _get(self, path: str, params: dict) -> dict | None:
        """GET a data endpoint, transparently refreshing the token on a 401."""
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._ensure_access(client)
            resp = await client.get(
                f"{RTT_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401:
                # Access token may have expired early; force one re-exchange.
                self._access = None
                token = await self._ensure_access(client)
                resp = await client.get(
                    f"{RTT_BASE}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code == 204:
                return None  # valid query, no services found
            if resp.status_code != 200:
                raise RTTError(f"{path} returned HTTP {resp.status_code}")
            return resp.json()

    async def resolve_identity(
        self, origin_crs: str, dest_crs: str, scheduled_dt: datetime
    ) -> str | None:
        """Resolve a tracked train to an RTT reference "identity:departureDate".

        Queries the origin location board (filtered to the destination) around the
        scheduled departure and matches by advertised departure time. Returns a
        reference string usable with get_service(), or None if no match.
        """
        time_from = scheduled_dt.strftime("%Y-%m-%dT%H:%M:%S")
        board = await self._get(
            f"/{self._ns}/location",
            {
                "code": origin_crs,
                "filterTo": dest_crs,
                "timeFrom": time_from,
                "timeWindow": 30,
            },
        )
        if not board:
            return None
        target = scheduled_dt.strftime("%H:%M")
        for svc in board.get("services") or []:
            departure = (svc.get("temporalData") or {}).get("departure") or {}
            sched = departure.get("scheduleAdvertised") or departure.get(
                "scheduleInternal"
            )
            if sched and sched[11:16] == target:
                meta = svc.get("scheduleMetadata") or {}
                identity = meta.get("identity")
                dep_date = meta.get("departureDate")
                if identity and dep_date:
                    return f"{identity}:{dep_date}"
        return None

    async def get_service(
        self, identity: str, departure_date: str
    ) -> dict | None:
        """Fetch a specific service's live running.

        Args:
            identity: train identity / headcode, e.g. "C03158".
            departure_date: origin departure date, "YYYY-MM-DD".

        Returns a normalised dict (see _parse_service) or None if not found.
        """
        data = await self._get(
            f"/{self._ns}/service",
            {"identity": identity, "departureDate": departure_date, "detailed": "true"},
        )
        if not data or not data.get("service"):
            return None
        return self._parse_service(data["service"])

    @staticmethod
    def _parse_service(svc: dict) -> dict:
        """Normalise a gb-nr service into calling points + live position.

        The actual/forecast boundary in the calling-point list *is* the live
        position: every stop with a realtimeActual has been passed; the first
        stop with only a realtimeForecast is where the train is heading next.
        """
        calling: list[dict] = []
        last_actual_idx = -1
        for idx, loc in enumerate(svc.get("locations") or []):
            location = loc.get("location") or {}
            name = location.get("description") or (
                (location.get("longCodes") or [None])[0]
            )
            temporal = loc.get("temporalData") or {}
            arrival = temporal.get("arrival") or {}
            departure = temporal.get("departure") or {}
            actual = arrival.get("realtimeActual") or departure.get("realtimeActual")
            forecast = departure.get("realtimeForecast") or arrival.get(
                "realtimeForecast"
            )
            lateness = arrival.get("realtimeAdvertisedLateness")
            if lateness is None:
                lateness = departure.get("realtimeInternalLateness")
            platform = (loc.get("locationMetadata") or {}).get("platform") or {}
            if actual:
                last_actual_idx = idx
            calling.append(
                {
                    "name": name,
                    "code": (location.get("longCodes") or [None])[0],
                    "scheduled": arrival.get("scheduleAdvertised")
                    or departure.get("scheduleAdvertised"),
                    "actual": actual,
                    "forecast": forecast,
                    "lateness": lateness,
                    "cancelled": bool(
                        arrival.get("isCancelled") or departure.get("isCancelled")
                    ),
                    "platform": platform.get("actual") or platform.get("planned"),
                }
            )

        position = None
        if last_actual_idx >= 0:
            passed = calling[last_actual_idx]
            nxt = (
                calling[last_actual_idx + 1]
                if last_actual_idx + 1 < len(calling)
                else None
            )
            position = {
                "last_reported": passed["name"],
                "last_reported_time": passed["actual"],
                "next_stop": nxt["name"] if nxt else None,
                "next_stop_forecast": nxt["forecast"] if nxt else None,
            }

        meta = svc.get("scheduleMetadata") or {}
        origin = ((svc.get("origin") or [{}])[0].get("location") or {}).get(
            "description"
        )
        destination = ((svc.get("destination") or [{}])[0].get("location") or {}).get(
            "description"
        )
        return {
            "identity": meta.get("identity"),
            "unique_identity": meta.get("uniqueIdentity"),
            "operator": (meta.get("operator") or {}).get("name"),
            "origin": origin,
            "destination": destination,
            "calling_points": calling,
            "position": position,
            "lateness": calling[last_actual_idx]["lateness"]
            if last_actual_idx >= 0
            else None,
            "arrived": bool(calling) and last_actual_idx == len(calling) - 1,
        }
