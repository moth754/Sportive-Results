"""In-memory runtime state shared by the web app and the background workers."""

import collections
import itertools
import logging
import threading
import time
import uuid

log = logging.getLogger("raceresults")

_LEVELS = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


class State:
    def __init__(self):
        self._lock = threading.Lock()
        # SMS sending always starts OFF and is never saved as on.
        self.sms_enabled = False
        self.boot_id = uuid.uuid4().hex[:8]
        self._versions = collections.defaultdict(int)
        self._activity = collections.deque(maxlen=300)
        self._activity_ids = itertools.count(1)
        self.fetch = {}        # event_id -> {"at", "ok_at", "error"}
        self.publish = {"at": None, "ok_at": None, "error": None, "message": ""}
        self.stats = {}
        self.wake_poller = threading.Event()
        self.wake_sms = threading.Event()
        self.wake_publisher = threading.Event()
        self.stopping = threading.Event()

    # Results change counter, so browsers only download results when something changed.
    def bump(self, event_id):
        with self._lock:
            self._versions[event_id] += 1

    def version(self, event_id):
        with self._lock:
            return f"{self.boot_id}-{self._versions[event_id]}"

    # Short activity log shown in the Results tab.
    def activity(self, message, level="info"):
        entry = {"id": next(self._activity_ids), "at": time.time(), "level": level, "message": message}
        with self._lock:
            self._activity.append(entry)
        log.log(_LEVELS.get(level, logging.INFO), message)

    def activity_since(self, since_id):
        with self._lock:
            return [e for e in self._activity if e["id"] > since_id]

    def set_fetch(self, event_id, ok, error=None):
        now = time.time()
        with self._lock:
            entry = self.fetch.setdefault(event_id, {"at": None, "ok_at": None, "error": None})
            entry["at"] = now
            if ok:
                entry["ok_at"], entry["error"] = now, None
            else:
                entry["error"] = error


state = State()
