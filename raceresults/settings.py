"""Global configuration (persists across events). Secrets are stored locally and never sent to the browser."""

import csv
import io
import json
import threading

from . import db

DEFAULTS = {
    # Webscorer
    "webscorer_api_id": "",
    "webscorer_token": "",
    "refresh_seconds": 20,
    # Text messages (Twilio)
    "twilio_account_sid": "",
    "twilio_auth_token": "",
    "twilio_messaging_service_sid": "",
    "twilio_from": "",
    "phone_region": "GB",
    # Results website (FTP)
    "ftp_enabled": False,
    "ftp_protocol": "ftp",
    "ftp_host": "",
    "ftp_port": 0,
    "ftp_user": "",
    "ftp_password": "",
    "ftp_remote_dir": "",
    "ftp_passive": True,
    "ftp_verify_tls": True,
    "ftp_interval_seconds": 120,
    "public_url": "",  # web address of the remote folder; each event's page is <public_url>/<page file name>
    # Branding
    "timing_company_name": "",
    "timing_logo": "",
    # Security / internal
    "ui_password_hash": "",
    "active_event_id": None,
}

LABELS = {
    "refresh_seconds": "Refresh interval",
    "ftp_port": "FTP port",
    "ftp_interval_seconds": "Upload interval",
}

SECRETS = {"webscorer_token", "twilio_auth_token", "ftp_password", "ui_password_hash"}
# Changed through dedicated actions, not the Configuration form.
INTERNAL = {"ui_password_hash", "active_event_id", "timing_logo"}
INT_LIMITS = {"refresh_seconds": (5, 3600), "ftp_port": (0, 65535), "ftp_interval_seconds": (30, 86400)}

# Old Config.csv parameter -> new setting. RACEID, MedalColours, InfoOn and InfoMessage belong to an event now;
# TextSend is deliberately ignored.
LEGACY_MAP = {
    "APIID": "webscorer_api_id",
    "WEBSCORERTOKEN": "webscorer_token",
    "RefreshTime": "refresh_seconds",
    "TSID": "twilio_account_sid",
    "Ttoken": "twilio_auth_token",
    "MessagingServiceSID": "twilio_messaging_service_sid",
    "TFrom": "twilio_from",
}


class SettingsError(ValueError):
    pass


_cache = None
_cache_lock = threading.Lock()


def reset_cache():
    global _cache
    with _cache_lock:
        _cache = None


def get_all():
    global _cache
    with _cache_lock:
        if _cache is None:
            values = dict(DEFAULTS)
            for row in db.query("SELECT key, value FROM settings"):
                if row["key"] in DEFAULTS:
                    try:
                        values[row["key"]] = json.loads(row["value"])
                    except ValueError:
                        pass
            _cache = values
        return dict(_cache)


def get(key):
    return get_all()[key]


def _store(values):
    with db.transaction() as c:
        for key, value in values.items():
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
    reset_cache()


def set_internal(key, value):
    if key not in INTERNAL:
        raise KeyError(key)
    _store({key: value})


def coerce(key, value):
    """Validate one user-supplied value and convert it to the stored type."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    if key in INT_LIMITS:
        try:
            number = int(float(str(value).strip() or 0))
        except ValueError:
            raise SettingsError(f"{LABELS[key]} must be a whole number") from None
        low, high = INT_LIMITS[key]
        if not low <= number <= high:
            raise SettingsError(f"{LABELS[key]} must be between {low} and {high}")
        return number
    text = "" if value is None else str(value).strip()
    if key == "ftp_protocol":
        text = text.lower()
        if text not in ("ftp", "ftps", "sftp"):
            raise SettingsError("FTP protocol must be FTP, FTPS or SFTP")
    elif key == "public_url":
        if text and not text.lower().startswith(("http://", "https://")):
            text = "https://" + text
    elif key == "phone_region":
        import phonenumbers

        text = text.upper() or "GB"
        if text not in phonenumbers.SUPPORTED_REGIONS:
            raise SettingsError(f"Unknown country code '{text}' (use a two-letter code such as GB, IE or US)")
    return text


def update(values, clear=()):
    """Apply a partial update from the Configuration tab. Returns the set of keys that changed.

    Blank secret fields mean "keep the saved value"; secrets are only removed via `clear`.
    """
    current = get_all()
    changes = {}
    for key, value in values.items():
        if key not in DEFAULTS or key in INTERNAL:
            continue
        if key in SECRETS:
            if value is None or str(value).strip() == "":
                continue
            new = str(value).strip()
        else:
            new = coerce(key, value)
        if current.get(key) != new:
            changes[key] = new
    for key in clear:
        if key in SECRETS and key not in INTERNAL and current.get(key):
            changes[key] = ""
    if changes:
        _store(changes)
    return set(changes)


def public_view():
    """Settings for the browser: secret values replaced by *_set flags."""
    values = get_all()
    out = {k: v for k, v in values.items() if k not in SECRETS and k != "active_event_id"}
    for key in SECRETS - {"ui_password_hash"}:
        out[key + "_set"] = bool(values.get(key))
    out["ui_password_set"] = bool(values.get("ui_password_hash"))
    return out


LEGACY_EVENT_MAP = {"MedalColours": "medal_colours", "InfoOn": "info_on", "InfoMessage": "info_message"}


def parse_legacy_config(text):
    """Parse the old Parameter,Value Config.csv. Returns (settings, event fields, race_id or None)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SettingsError("The file is empty")
    header = [h.strip().lower() for h in rows[0]]
    param_col = next((i for i, h in enumerate(header) if h in ("parameter", "param", "name")), None)
    value_col = next((i for i, h in enumerate(header) if h in ("value", "val")), None)
    if param_col is None or value_col is None:
        raise SettingsError("Config.csv needs 'Parameter' and 'Value' columns")
    values, event_values, race_id = {}, {}, None
    for row in rows[1:]:
        if len(row) <= max(param_col, value_col):
            continue
        param = row[param_col].strip()
        if value_col == len(header) - 1:
            value = ",".join(row[value_col:])  # tolerate unquoted commas in a message
        else:
            value = row[value_col]
        value = value.strip()
        if param == "RACEID":
            race_id = value or None
        elif param in LEGACY_MAP:
            values[LEGACY_MAP[param]] = value
        elif param in LEGACY_EVENT_MAP:
            event_values[LEGACY_EVENT_MAP[param]] = value
    return values, event_values, race_id


def import_legacy_config(text):
    """Apply an old Config.csv's settings. Returns (settings imported, event fields for the caller, race_id)."""
    values, event_values, race_id = parse_legacy_config(text)
    update(values)
    return sorted(values), event_values, race_id
