"""LiveSpApiClient against a fake transport (no network, no real credentials)."""

import io
import json
import urllib.error
import urllib.parse

import pytest
from mcf_bridge.spapi import LWA_TOKEN_URL, LiveSpApiClient, SpApiError


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, fail_with=None):
        self.requests = []
        self.fail_with = fail_with

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.full_url == LWA_TOKEN_URL:
            return FakeResp(json.dumps({"access_token": "test-access", "expires_in": 3600}).encode())
        if self.fail_with:
            raise urllib.error.HTTPError(req.full_url, self.fail_with, "err", {}, io.BytesIO(b'{"errors":[]}'))
        return FakeResp(b'{"payload": {}}')


def client(opener):
    return LiveSpApiClient("na", "test-client", "test-secret", "test-refresh", opener=opener)


def test_token_exchange_and_header():
    op = FakeOpener()
    c = client(op)
    c.get_fulfillment_order("SF-1")
    c.get_fulfillment_order("SF-2")
    token_reqs = [r for r in op.requests if r.full_url == LWA_TOKEN_URL]
    assert len(token_reqs) == 1  # cached
    form = urllib.parse.parse_qs(token_reqs[0].data.decode())
    assert form["grant_type"] == ["refresh_token"] and form["client_id"] == ["test-client"]
    api = [r for r in op.requests if r.full_url != LWA_TOKEN_URL]
    assert api[0].get_header("X-amz-access-token") == "test-access"
    assert api[0].full_url == "https://sellingpartnerapi-na.amazon.com/fba/outbound/2020-07-01/fulfillmentOrders/SF-1"


def test_paths_and_queries():
    op = FakeOpener()
    c = client(op)
    c.create_fulfillment_order({"sellerFulfillmentOrderId": "SF-1"})
    c.get_package_tracking_details(123)
    c.get_inventory_summaries("ATVPDKIKX0DER", ["A", "B"], next_token="t")
    api = [r for r in op.requests if r.full_url != LWA_TOKEN_URL]
    assert api[0].get_method() == "POST" and api[0].full_url.endswith("/fba/outbound/2020-07-01/fulfillmentOrders")
    assert api[1].full_url.endswith("/fba/outbound/2020-07-01/tracking?packageNumber=123")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(api[2].full_url).query)
    assert q["granularityType"] == ["Marketplace"] and q["marketplaceIds"] == ["ATVPDKIKX0DER"]
    assert q["sellerSkus"] == ["A,B"] and q["nextToken"] == ["t"] and q["details"] == ["true"]


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (400, False), (403, False)])
def test_http_errors_map_to_retryability(status, retryable):
    with pytest.raises(SpApiError) as exc:
        client(FakeOpener(fail_with=status)).get_fulfillment_order("SF-1")
    assert exc.value.status == status and exc.value.retryable is retryable


def test_unknown_region_rejected():
    with pytest.raises(ValueError):
        LiveSpApiClient("xx", "a", "b", "c")
