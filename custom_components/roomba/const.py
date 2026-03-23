"""The roomba constants."""

from homeassistant.const import Platform

DOMAIN = "roomba"
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SELECT, Platform.SENSOR, Platform.VACUUM]
CONF_CERT = "certificate"
CONF_CONTINUOUS = "continuous"
CONF_BLID = "blid"
DEFAULT_CERT = "/etc/ssl/certs/ca-certificates.crt"
DEFAULT_CONTINUOUS = True
DEFAULT_DELAY = 30
ROOMBA_SESSION = "roomba_session"
CONF_CLOUD_EMAIL = "cloud_email"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_ROBOT_ID = "robot_id"
