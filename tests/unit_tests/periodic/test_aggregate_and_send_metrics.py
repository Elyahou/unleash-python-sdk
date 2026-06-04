import json

import responses

from ct_unleash.constants import (
    CLIENT_SPEC_VERSION,
    METRICS_URL,
)
from ct_unleash.engine import UnleashEngine
from ct_unleash.periodic_tasks import aggregate_and_send_metrics
from tests.utilities.testing_constants import (
    APP_NAME,
    CONNECTION_ID,
    CUSTOM_HEADERS,
    CUSTOM_OPTIONS,
    INSTANCE_ID,
    REQUEST_TIMEOUT,
    URL,
)

FULL_METRICS_URL = URL + METRICS_URL
print(FULL_METRICS_URL)


@responses.activate
def test_no_metrics():
    responses.add(responses.POST, FULL_METRICS_URL, json={}, status=200)

    engine = UnleashEngine()

    aggregate_and_send_metrics(
        URL,
        APP_NAME,
        INSTANCE_ID,
        CONNECTION_ID,
        CUSTOM_HEADERS,
        CUSTOM_OPTIONS,
        REQUEST_TIMEOUT,
        engine,
    )

    assert len(responses.calls) == 0


@responses.activate
def test_metrics_metadata_is_sent():
    responses.add(responses.POST, FULL_METRICS_URL, json={}, status=200)

    engine = UnleashEngine()
    engine.count_toggle("something-to-make-sure-metrics-get-sent", True)

    aggregate_and_send_metrics(
        URL,
        APP_NAME,
        INSTANCE_ID,
        CONNECTION_ID,
        CUSTOM_HEADERS,
        CUSTOM_OPTIONS,
        REQUEST_TIMEOUT,
        engine,
    )

    assert len(responses.calls) == 1
    request = json.loads(responses.calls[0].request.body)

    assert "yggdrasilVersion" not in request
    assert request["specVersion"] == CLIENT_SPEC_VERSION
    assert request["platformName"] is not None
    assert request["platformVersion"] is not None
