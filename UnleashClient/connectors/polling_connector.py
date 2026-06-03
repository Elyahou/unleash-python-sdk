import json
import uuid
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from UnleashClient.api import get_feature_toggles
from UnleashClient.cache import BaseCache
from UnleashClient.constants import ETAG, FEATURES_URL
from UnleashClient.engine import UnleashEngine
from UnleashClient.events import UnleashEventType, UnleashFetchedEvent
from UnleashClient.utils import LOGGER

from .base_connector import BaseConnector


def _is_usable_features_body(body: str) -> bool:
    """Return whether ``body`` is a structurally usable ClientFeatures payload.

    Guards the cache against a *total* parse failure — invalid JSON, or valid
    JSON that is not an object (``null`` / ``[]`` / ``42`` / ``"oops"``), which
    the engine would refuse to load. A JSON object (flat ``{features}`` or delta
    ``{events}`` form) is considered usable; per-feature degradation is handled
    downstream by ``take_state`` without discarding the whole body.
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    # Must be an actual ClientFeatures document (flat or delta), not an HTTP-200
    # error envelope such as ``{"error": ..., "statusCode": 502}``.
    return "features" in payload or "events" in payload


class PollingConnector(BaseConnector):
    def __init__(
        self,
        engine: UnleashEngine,
        cache: BaseCache,
        scheduler: BackgroundScheduler,
        url: str,
        app_name: str,
        instance_id: str,
        headers: Optional[dict] = None,
        custom_options: Optional[dict] = None,
        request_timeout: int = 30,
        request_retries: int = 3,
        project: Optional[str] = None,
        scheduler_executor: str = "default",
        refresh_interval: int = 15,
        refresh_jitter: int = None,
        event_callback: Optional[Callable] = None,
        ready_callback: Optional[Callable] = None,
    ):
        self.engine = engine
        self.cache = cache
        self.scheduler = scheduler
        self.url = url
        self.app_name = app_name
        self.instance_id = instance_id
        self.headers = headers or {}
        self.custom_options = custom_options or {}
        self.request_timeout = request_timeout
        self.request_retries = request_retries
        self.project = project
        self.scheduler_executor = scheduler_executor
        self.refresh_interval = refresh_interval
        self.refresh_jitter = refresh_jitter
        self.event_callback = event_callback
        self.ready_callback = ready_callback
        self.job = None

    def _fetch_and_load(self):
        state, etag = get_feature_toggles(
            url=self.url,
            app_name=self.app_name,
            instance_id=self.instance_id,
            headers={
                **self.headers,
                "unleash-interval": str(self.refresh_interval * 1000),
            },
            custom_options=self.custom_options,
            request_timeout=self.request_timeout,
            request_retries=self.request_retries,
            project=self.project,
            cached_etag=self.cache.get(ETAG),
        )

        # Validate the freshly fetched body BEFORE caching it, so a corrupt /
        # truncated / non-ClientFeatures HTTP 200 body (e.g. a proxy error
        # page or ``null``) cannot poison the cache and persist across
        # restarts. Only cache the body when it parses into usable state.
        usable = bool(state) and _is_usable_features_body(state)

        if usable:
            self.cache.set(FEATURES_URL, state)
        elif state:
            LOGGER.warning(
                "Server returned an unparseable feature provisioning body; "
                "discarding it and keeping previously cached provisioning."
            )
        else:
            LOGGER.debug(
                "No feature provisioning returned from server, using cached provisioning."
            )

        # Only advance the ETag when we actually accepted the body. Storing the
        # ETag of a discarded body would make the next request send
        # ``If-None-Match`` with it, receive a 304, and keep the stale flags
        # indefinitely — re-introducing the cache poisoning the check above
        # prevents.
        if etag and usable:
            self.cache.set(ETAG, etag)

        self.load_features()

        if usable:
            if self.event_callback:
                event = UnleashFetchedEvent(
                    event_type=UnleashEventType.FETCHED,
                    event_id=uuid.uuid4(),
                    raw_features=state,
                )
                self.event_callback(event)
            if self.ready_callback:
                self.ready_callback()

    def start(self):
        self._fetch_and_load()

        self.job = self.scheduler.add_job(
            self._fetch_and_load,
            trigger=IntervalTrigger(
                seconds=self.refresh_interval,
                jitter=self.refresh_jitter,
            ),
            executor=self.scheduler_executor,
        )

    def stop(self):
        if self.job:
            self.job.remove()
            self.job = None
