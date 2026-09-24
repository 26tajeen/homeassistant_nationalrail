"""Constants for the National Rail UK integration."""

DOMAIN = "nationalrailuk"
DOMAIN_DATA = f"{DOMAIN}_data"

# Platforms
SENSOR = "sensor"
PLATFORMS = [SENSOR]

WSDL = "https://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-11-01"


CONF_TOKEN = "api_token"
CONF_STATION = "station"
CONF_DESTINATIONS = "destinations"

# RealTimeTrains (data.rtt.io) refresh token, used for live in-journey tracking
CONF_RTT_TOKEN = "rtt_refresh_token"

# New configuration keys for specific train monitoring
CONF_MONITOR_TRAIN = "monitor_train"
CONF_TARGET_TIME = "target_time"
CONF_TARGET_DESTINATION = "target_destination"
CONF_TIME_WINDOW_MINUTES = "time_window_minutes"
CONF_MONITORED_TRAIN_NAME = "monitored_train_name"
CONF_EARLIER_TOLERANCE_MINUTES = "earlier_tolerance_minutes"
CONF_LATER_TOLERANCE_MINUTES = "later_tolerance_minutes"
CONF_SELECTION_STRATEGY = "selection_strategy"
CONF_LATCH_MINUTES = "latch_minutes_before_departure"
CONF_SWITCH_POLICY = "switch_policy"
CONF_SWITCH_ADVANTAGE_MINUTES = "switch_advantage_minutes"
CONF_DELAY_REEVALUATION_MINUTES = "delay_reevaluation_minutes"


# Default values
DEFAULT_TIME_WINDOW = 30
DEFAULT_EARLIER_TOLERANCE = 0
DEFAULT_SELECTION_STRATEGY = "earliest_arrival"
DEFAULT_LATCH_MINUTES = 10
DEFAULT_SWITCH_POLICY = "better_arrival"
DEFAULT_SWITCH_ADVANTAGE_MINUTES = 3
DEFAULT_DELAY_REEVALUATION_MINUTES = 10

SELECTION_STRATEGIES = {
    "closest_to_usual": "Closest to usual departure",
    "earliest_arrival": "Earliest arrival",
    "first_after_usual": "First departure after usual time",
}

SWITCH_POLICIES = {
    "stick_until_cancelled": "Stay with selected train unless cancelled",
    "better_arrival": "Switch when another train arrives meaningfully earlier",
    "next_viable": "Switch to the next viable train when disrupted",
}

# Refresh frequency for the sensor (in minutes)
# This controls how often the coordinator fetches new data from the API
REFRESH = 10

# Polling interval (in minutes) - kept for backwards compatibility
POLLING_INTERVALE = 10

# Increase polling frequency if within X minutes of next departure or if train is late
HIGH_FREQUENCY_REFRESH = 10  # Trigger high-frequency mode when train is within 10 minutes

# High-frequency polling interval (in minutes) - how often to poll when train is imminent
HIGH_FREQUENCY_INTERVAL = 1  # Poll every 1 minute when train is within 10 minutes


ATTR_ENTITY_ID = "entity_id"
