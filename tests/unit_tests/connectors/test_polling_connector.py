import responses
from apscheduler.schedulers.background import BackgroundScheduler

from tests.utilities.mocks.mock_features import (
    MOCK_FEATURE_RESPONSE,
    MOCK_FEATURE_RESPONSE_PROJECT,
)
from tests.utilities.testing_constants import (
    APP_NAME,
    CUSTOM_HEADERS,
    CUSTOM_OPTIONS,
    ETAG_VALUE,
    INSTANCE_ID,
    PROJECT_NAME,
    PROJECT_URL,
    REQUEST_RETRIES,
    REQUEST_TIMEOUT,
    URL,
)
from UnleashClient.connectors import PollingConnector
from UnleashClient.constants import ETAG, FEATURES_URL
from UnleashClient.engine import UnleashEngine

FULL_FEATURE_URL = URL + FEATURES_URL


@responses.activate
def test_polling_connector_fetch_and_load(cache_empty):
    engine = UnleashEngine()
    scheduler = BackgroundScheduler()
    responses.add(
        responses.GET,
        FULL_FEATURE_URL,
        json=MOCK_FEATURE_RESPONSE,
        status=200,
        headers={"etag": ETAG_VALUE},
    )
    temp_cache = cache_empty

    connector = PollingConnector(
        engine=engine,
        cache=temp_cache,
        scheduler=scheduler,
        url=URL,
        app_name=APP_NAME,
        instance_id=INSTANCE_ID,
        headers=CUSTOM_HEADERS,
        custom_options=CUSTOM_OPTIONS,
        request_timeout=REQUEST_TIMEOUT,
        request_retries=REQUEST_RETRIES,
    )

    connector._fetch_and_load()

    assert engine.is_enabled("testFlag", {})
    assert temp_cache.get(ETAG) == ETAG_VALUE


@responses.activate
def test_polling_connector_fetch_and_load_project(cache_empty):
    engine = UnleashEngine()
    scheduler = BackgroundScheduler()
    responses.add(
        responses.GET, PROJECT_URL, json=MOCK_FEATURE_RESPONSE_PROJECT, status=200
    )
    temp_cache = cache_empty

    connector = PollingConnector(
        engine=engine,
        cache=temp_cache,
        scheduler=scheduler,
        url=URL,
        app_name=APP_NAME,
        instance_id=INSTANCE_ID,
        headers=CUSTOM_HEADERS,
        custom_options=CUSTOM_OPTIONS,
        request_timeout=REQUEST_TIMEOUT,
        request_retries=REQUEST_RETRIES,
        project=PROJECT_NAME,
    )

    connector._fetch_and_load()

    assert engine.is_enabled("ivan-project", {})


@responses.activate
def test_polling_connector_fetch_and_load_failure(cache_empty):
    engine = UnleashEngine()
    scheduler = BackgroundScheduler()
    responses.add(
        responses.GET, FULL_FEATURE_URL, json=MOCK_FEATURE_RESPONSE, status=200
    )
    temp_cache = cache_empty

    connector = PollingConnector(
        engine=engine,
        cache=temp_cache,
        scheduler=scheduler,
        url=URL,
        app_name=APP_NAME,
        instance_id=INSTANCE_ID,
        headers=CUSTOM_HEADERS,
        custom_options=CUSTOM_OPTIONS,
        request_timeout=REQUEST_TIMEOUT,
        request_retries=REQUEST_RETRIES,
    )

    connector._fetch_and_load()

    responses.reset()
    responses.add(responses.GET, FULL_FEATURE_URL, json={}, status=500)

    connector._fetch_and_load()

    assert engine.is_enabled("testFlag", {})


@responses.activate
def test_polling_connector_start_stop(cache_empty):
    engine = UnleashEngine()
    scheduler = BackgroundScheduler()
    scheduler.start()

    responses.add(
        responses.GET,
        FULL_FEATURE_URL,
        json=MOCK_FEATURE_RESPONSE,
        status=200,
        headers={"etag": ETAG_VALUE},
    )
    temp_cache = cache_empty

    connector = PollingConnector(
        engine=engine,
        cache=temp_cache,
        scheduler=scheduler,
        url=URL,
        app_name=APP_NAME,
        instance_id=INSTANCE_ID,
        headers=CUSTOM_HEADERS,
        custom_options=CUSTOM_OPTIONS,
        request_timeout=REQUEST_TIMEOUT,
        request_retries=REQUEST_RETRIES,
        refresh_interval=1,
    )

    connector.start()
    assert connector.job is not None

    connector.stop()
    assert connector.job is None

    scheduler.shutdown()


@responses.activate
def test_polling_connector_corrupt_200_does_not_poison_cache_or_etag(cache_empty):
    # A corrupt HTTP-200 body must be discarded AND must not advance the ETag.
    # Storing the bad body's ETag would make the next request send If-None-Match,
    # receive a 304, and keep stale flags indefinitely (regression guard).
    engine = UnleashEngine()
    scheduler = BackgroundScheduler()
    responses.add(
        responses.GET,
        FULL_FEATURE_URL,
        json=MOCK_FEATURE_RESPONSE,
        status=200,
        headers={"etag": ETAG_VALUE},
    )

    connector = PollingConnector(
        engine=engine,
        cache=cache_empty,
        scheduler=scheduler,
        url=URL,
        app_name=APP_NAME,
        instance_id=INSTANCE_ID,
        headers=CUSTOM_HEADERS,
        custom_options=CUSTOM_OPTIONS,
        request_timeout=REQUEST_TIMEOUT,
        request_retries=REQUEST_RETRIES,
    )
    connector._fetch_and_load()
    good_state = cache_empty.get(FEATURES_URL)
    assert cache_empty.get(ETAG) == ETAG_VALUE

    # Now a corrupt 200 with a NEW etag.
    responses.reset()
    responses.add(
        responses.GET,
        FULL_FEATURE_URL,
        body="null",
        status=200,
        content_type="application/json",
        headers={"etag": 'W/"poison-etag"'},
    )
    connector._fetch_and_load()

    # Body discarded, prior good state preserved, and the poison ETag NOT stored.
    assert cache_empty.get(FEATURES_URL) == good_state
    assert cache_empty.get(ETAG) == ETAG_VALUE
