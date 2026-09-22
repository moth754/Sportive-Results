"""Webscorer JSON API: fetch a race and turn it into a flat list of racers."""

import datetime
import os
import re

import requests

BASE_URL = os.environ.get("RACERESULTS_WEBSCORER_URL", "https://www.webscorer.com/json/race")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RaceProcessor/1.0)",
    "Accept": "application/json",
    "Referer": "https://www.webscorer.com/",
}
_PREFERRED_ADJUSTED = ("adjustedtime", "adjtime", "penaltyadjustedtime", "handicapadjustedtime", "timeadjusted")


class WebscorerError(Exception):
    pass


def fetch_race(race_id, api_id, token, timeout=15):
    """Return Webscorer's JSON for a race, or raise WebscorerError.

    Error messages never include the request URL, because it carries the API token.
    """
    if not api_id:
        raise WebscorerError("Webscorer API ID is not set (Configuration tab)")
    params = {"raceid": race_id, "apiid": api_id}
    if token:
        params["apipriv"] = token
    try:
        response = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=timeout)
    except requests.Timeout:
        raise WebscorerError("Webscorer did not respond in time") from None
    except requests.ConnectionError:
        raise WebscorerError("Can't reach Webscorer: check the internet connection") from None
    except requests.RequestException as exc:
        raise WebscorerError(f"Webscorer request failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise WebscorerError(f"Webscorer returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise WebscorerError("Webscorer returned something that isn't JSON") from None
    if isinstance(data, dict) and data.get("Error"):
        raise WebscorerError(f"Webscorer: {data['Error']}")
    if not isinstance(data, dict) or not isinstance(data.get("Results"), list):
        raise WebscorerError("Unexpected response from Webscorer")
    return data


def _text(value):
    return "" if value is None else str(value).strip()


def _gender(value):
    value = _text(value)
    return "Open" if value == "Female/Male" else value


def find_adjusted_field(keys):
    """Pick the racer field holding the adjusted time (Webscorer's "Adjusted time" column), if any."""
    normalised = {key: re.sub(r"[^a-z]", "", str(key).lower()) for key in keys}
    for wanted in _PREFERRED_ADJUSTED:
        for key, norm in normalised.items():
            if norm == wanted:
                return key
    for key, norm in normalised.items():
        if "adj" in norm and "time" in norm and not any(x in norm for x in ("diff", "back", "percent", "pct", "place")):
            return key
    return None


def racer_key(bib, name, distance):
    """Stable identity for a racer: the bib, or name + distance when there's no bib."""
    if bib and bib != "-":
        return bib
    return f"~{name.casefold()}|{distance.casefold()}"


def extract(data):
    """Racers from the Overall groupings (as terminal_results.extract_racers did), plus race metadata.

    Returns (racers, meta). Each racer is a dict of plain strings; meta holds the race info,
    Webscorer's grouping order (for the public page) and the adjusted-time field name.
    """
    results = [g for g in data.get("Results") or [] if isinstance(g, dict)]

    keys = set()
    for group in results:
        for racer in group.get("Racers") or []:
            if isinstance(racer, dict):
                keys.update(racer.keys())
    adjusted_field = find_adjusted_field(keys)

    group_order = []
    for group in results:
        grouping = group.get("Grouping")
        if isinstance(grouping, dict):
            entry = [_text(grouping.get("Distance")),
                     "" if grouping.get("Overall") else _gender(grouping.get("Gender")),
                     _text(grouping.get("Category"))]
            if entry not in group_order:
                group_order.append(entry)

    racers, seen = [], set()
    for group in results:
        grouping = group.get("Grouping")
        if not (isinstance(grouping, dict) and grouping.get("Overall") is True):
            continue
        distance = _text(grouping.get("Distance"))
        for racer in group.get("Racers") or []:
            if not isinstance(racer, dict):
                continue
            combined = f"{_text(racer.get('FirstName'))} {_text(racer.get('LastName'))}".strip()
            record = {
                "bib": _text(racer.get("Bib")),
                "name": combined or _text(racer.get("Name")),
                "distance": distance,
                "category": _text(racer.get("Category")),
                "gender": _gender(racer.get("Gender")),
                "time": _text(racer.get("Time")),
                "adjusted": _text(racer.get(adjusted_field)) if adjusted_field else "",
                "phone": _text(racer.get("Info1")),
                "purchases": _text(racer.get("Info2") or racer.get("Info 2")),
                "start": _text(racer.get("StartTime")),
            }
            key = racer_key(record["bib"], record["name"], record["distance"])
            if key in seen:  # first entry wins, as before
                continue
            seen.add(key)
            record["key"] = key
            racers.append(record)

    info = data.get("RaceInfo") if isinstance(data.get("RaceInfo"), dict) else {}
    race_info = {k: info.get(k) for k in ("Name", "Date", "OrganizerName", "City", "Country", "CompletionState", "DisplayURL")}
    return racers, {"race_info": race_info, "group_order": group_order, "adjusted_field": adjusted_field}


def parse_date(text):
    """Webscorer's "Aug 23, 2026" -> "2026-08-23" (or "" if it can't be read)."""
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(_text(text), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def lookup(race_id, api_id, token):
    """Race name, date and organiser for the New event form."""
    data = fetch_race(race_id, api_id, token)
    info = data.get("RaceInfo") or {}
    racers, _ = extract(data)
    return {
        "name": _text(info.get("Name")),
        "date": parse_date(info.get("Date")),
        "organiser": _text(info.get("OrganizerName")),
        "racers": len(racers),
    }
