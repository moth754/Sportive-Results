"""Background threads: Webscorer poller, SMS sender, results-page publisher and system meters."""

import logging
import threading

from . import events, processing, publish, settings, sms, webscorer
from .state import state
from .system import Sampler

log = logging.getLogger("raceresults.workers")


def poll_once(event_id):
    """Fetch the event from Webscorer and process it. Keeps the previous data if anything goes wrong."""
    event = events.get_event(event_id)
    if event is None:
        return
    s = settings.get_all()
    try:
        data = webscorer.fetch_race(event["race_id"], s["webscorer_api_id"], s["webscorer_token"])
        racers, meta = webscorer.extract(data)
    except webscorer.WebscorerError as exc:
        _fetch_failed(event_id, str(exc))
        return
    if not racers and processing.present_count(event_id):
        _fetch_failed(event_id, "Webscorer returned no racers; keeping the previous results")
        return
    processing.process_event(event_id, fetched=(racers, meta))
    was_failing = bool(state.fetch.get(event_id, {}).get("error"))
    state.set_fetch(event_id, ok=True)
    if was_failing:
        state.activity("Webscorer connection restored")


def _fetch_failed(event_id, message):
    previous = state.fetch.get(event_id, {}).get("error")
    state.set_fetch(event_id, ok=False, error=message)
    if message != previous:  # don't fill the activity log with the same error every 20 seconds
        state.activity(message, "error")


class Workers:
    def __init__(self):
        self.threads = []

    def start(self):
        for target, name in ((self._poller, "poller"), (self._sms_sender, "sms"),
                             (self._publisher, "publisher"), (self._stats, "stats")):
            thread = threading.Thread(target=self._guard(target), name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        state.stopping.set()
        for event in (state.wake_poller, state.wake_sms, state.wake_publisher):
            event.set()
        for thread in self.threads:
            thread.join(timeout=5)

    @staticmethod
    def _guard(loop):
        def run():
            while not state.stopping.is_set():
                try:
                    loop()
                    return
                except Exception:
                    log.exception("%s crashed; restarting in 5 s", loop.__name__)
                    state.activity(f"Internal error in {loop.__name__.strip('_')}; restarting it", "error")
                    state.stopping.wait(5)
        return run

    @staticmethod
    def _wait(event, seconds):
        event.wait(seconds)
        event.clear()

    def _poller(self):
        while not state.stopping.is_set():
            s = settings.get_all()
            event_id = s["active_event_id"]
            event = events.get_event(event_id) if event_id else None
            if event is not None and event["polling"]:
                poll_once(event_id)
            self._wait(state.wake_poller, max(5, int(s["refresh_seconds"])))

    def _sms_sender(self):
        while not state.stopping.is_set():
            if state.sms_enabled and sms.send_next():
                state.stopping.wait(0.2)  # gentle pacing for Twilio
                continue
            self._wait(state.wake_sms, 5)

    def _publisher(self):
        while not state.stopping.is_set():
            s = settings.get_all()
            if s["ftp_enabled"] and s["active_event_id"]:
                publish.publish_once()
            self._wait(state.wake_publisher, max(30, int(s["ftp_interval_seconds"])))

    def _stats(self):
        sampler = Sampler(publish.DATA_DIR or "/")
        while not state.stopping.is_set():
            sampler.sample()
            state.stopping.wait(2)
