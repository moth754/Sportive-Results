"""Events: a Webscorer race plus its medal configuration, organiser logo and saved results."""

import datetime
import re
import time
import unicodedata

from . import db, settings, sms
from .state import state

_PAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,98}\.html?", re.IGNORECASE)


class EventError(ValueError):
    pass


def clean_page_filename(value):
    """The event's results page file name, e.g. landsend2026.html ("" if none given).

    ".html" is added when there's no extension; anything that could escape the FTP folder is refused.
    """
    name = str(value or "").strip()
    if not name:
        return ""
    if not re.search(r"\.html?$", name, re.IGNORECASE):
        if re.search(r"\.[A-Za-z0-9]{1,5}$", name):
            raise EventError("The results page file name must end in .html, e.g. landsend2026.html")
        name += ".html"
    if not _PAGE_NAME_RE.fullmatch(name):
        raise EventError("The results page file name can only use letters, numbers, dots, dashes and "
                         "underscores, e.g. landsend2026.html")
    return name


def _taken_page_names(exclude_id=None):
    return {row["page_filename"].lower(): row["name"] for row in db.query("SELECT id, name, page_filename FROM events")
            if row["id"] != exclude_id and row["page_filename"]}


def default_page_filename(event_name, exclude_id=None):
    """A suggestion from the event name ("Lands End 2026" -> landsend2026.html), unique among events."""
    ascii_name = unicodedata.normalize("NFKD", event_name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]", "", ascii_name.lower())[:60] or "results"
    taken = _taken_page_names(exclude_id)
    candidate, number = f"{base}.html", 2
    while candidate in taken:
        candidate, number = f"{base}-{number}.html", number + 1
    return candidate


def page_url(base_url, filename):
    """Full web address of an event's page, from the results folder address in Configuration."""
    base = (base_url or "").strip()
    if not base or not filename:
        return ""
    return base.rstrip("/") + "/" + filename


def _clean(fields, partial=False, event_id=None, current_name=""):
    out = {}
    if "name" in fields or not partial:
        name = " ".join(str(fields.get("name") or "").split())
        if not name:
            raise EventError("Give the event a name")
        out["name"] = name
    if "race_id" in fields or not partial:
        race_id = str(fields.get("race_id") or "").strip()
        if not re.fullmatch(r"\d{1,12}", race_id):
            raise EventError("The Webscorer race ID is the number at the end of the race's web address, e.g. 447118")
        out["race_id"] = race_id
    if "event_date" in fields:
        date = str(fields.get("event_date") or "").strip()
        if date:
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                raise EventError("Date must be YYYY-MM-DD") from None
        out["event_date"] = date
    if "medal_config_id" in fields:
        config_id = fields.get("medal_config_id") or None
        if config_id is not None:
            config_id = int(config_id)
            if db.query_one("SELECT 1 FROM medal_configs WHERE id = ?", (config_id,)) is None:
                raise EventError("That medal configuration no longer exists")
        out["medal_config_id"] = config_id
    for flag in ("medal_colours", "info_on"):
        if flag in fields:
            value = fields[flag]
            out[flag] = 1 if (value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes", "on")) else 0
    if "medal_colours" not in out and not partial:
        out["medal_colours"] = 1 if fields.get("medal_config_id") else 0
    if "info_message" in fields:
        out["info_message"] = str(fields.get("info_message") or "").strip()
    if "page_filename" in fields or not partial:
        page = clean_page_filename(fields.get("page_filename"))
        if not page:
            page = default_page_filename(out.get("name") or current_name, exclude_id=event_id)
        other = _taken_page_names(exclude_id=event_id).get(page.lower())
        if other:
            raise EventError(f"“{other}” already uses {page}. Each event needs its own results page file name.")
        out["page_filename"] = page
    return out


def fill_missing_page_filenames():
    """After upgrading from version 1: give every event its own page name (the old global setting is gone)."""
    for row in db.query("SELECT id, name FROM events WHERE page_filename = '' ORDER BY id"):
        db.execute("UPDATE events SET page_filename = ? WHERE id = ?",
                   (default_page_filename(row["name"], exclude_id=row["id"]), row["id"]))
    db.execute("DELETE FROM settings WHERE key = 'ftp_filename'")


def list_events():
    rows = db.query(
        "SELECT e.*, m.name AS medal_config_name, "
        "(SELECT COUNT(*) FROM racers r WHERE r.event_id = e.id AND r.present = 1) AS entrants, "
        "(SELECT COUNT(*) FROM racers r WHERE r.event_id = e.id AND r.present = 1 AND r.result_secs IS NOT NULL) "
        "AS finishers "
        "FROM events e LEFT JOIN medal_configs m ON m.id = e.medal_config_id "
        "ORDER BY e.event_date DESC, e.id DESC"
    )
    base_url = settings.get("public_url")
    return [_public(r, base_url) for r in rows]


def _public(row, base_url):
    return {
        "id": row["id"], "name": row["name"], "event_date": row["event_date"], "race_id": row["race_id"],
        "medal_config_id": row["medal_config_id"], "medal_config_name": row["medal_config_name"],
        "page_filename": row["page_filename"], "page_url": page_url(base_url, row["page_filename"]),
        "medal_colours": bool(row["medal_colours"]), "info_on": bool(row["info_on"]), "info_message": row["info_message"],
        "organiser_logo": row["organiser_logo"], "polling": bool(row["polling"]),
        "adjusted_field": row["adjusted_field"], "data_updated_at": row["data_updated_at"],
        "entrants": row["entrants"], "finishers": row["finishers"],
    }


def get_event(event_id):
    row = db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
    return row


def get_public(event_id):
    return next((e for e in list_events() if e["id"] == event_id), None)


def create_event(fields):
    data = _clean(fields)
    data.setdefault("event_date", "")
    data.setdefault("medal_config_id", None)
    data.setdefault("info_on", 0)
    data.setdefault("info_message", "")
    now = time.time()
    cur = db.execute(
        "INSERT INTO events(name, event_date, race_id, medal_config_id, medal_colours, info_on, info_message, "
        "page_filename, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (data["name"], data["event_date"], data["race_id"], data["medal_config_id"], data["medal_colours"],
         data["info_on"], data["info_message"], data["page_filename"], now, now),
    )
    state.activity(f"Event created: {data['name']}")
    if not settings.get("active_event_id"):
        activate(cur.lastrowid)
    return cur.lastrowid


def update_event(event_id, fields):
    event = get_event(event_id)
    if event is None:
        raise EventError("Event not found")
    data = _clean(fields, partial=True, event_id=event_id, current_name=event["name"])
    if not data:
        return False
    data["updated_at"] = time.time()
    db.execute("UPDATE events SET " + ", ".join(f"{k} = ?" for k in data) + " WHERE id = ?",
               (*data.values(), event_id))
    return True


def set_organiser_logo(event_id, filename):
    db.execute("UPDATE events SET organiser_logo = ?, updated_at = ? WHERE id = ?", (filename, time.time(), event_id))


def delete_event(event_id):
    event = get_event(event_id)
    if event is None:
        raise EventError("Event not found")
    if settings.get("active_event_id") == event_id:
        sms.set_enabled(False, event_id)
        settings.set_internal("active_event_id", None)
    db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    state.activity(f"Event deleted: {event['name']}")
    return event


def activate(event_id):
    """Make an event the one being polled. Text messages are always switched off on a change."""
    event = get_event(event_id)
    if event is None:
        raise EventError("Event not found")
    previous = settings.get("active_event_id")
    if state.sms_enabled:
        sms.set_enabled(False, previous)
    settings.set_internal("active_event_id", event_id)
    state.activity(f"Active event: {event['name']} (Webscorer race {event['race_id']})")
    state.wake_poller.set()
    state.wake_publisher.set()


def set_polling(event_id, on):
    db.execute("UPDATE events SET polling = ? WHERE id = ?", (1 if on else 0, event_id))
    state.activity("Live updates " + ("resumed" if on else "paused"))
    state.wake_poller.set()
