"""SP-API access layer: a small client interface, a mock (default) and a live client.

Operation names, paths and field names follow the official Swagger models:
- Fulfillment Outbound 2020-07-01: createFulfillmentOrder, getFulfillmentOrder,
  getPackageTrackingDetails (GET /fba/outbound/2020-07-01/...)
- FBA Inventory v1: getInventorySummaries (GET /fba/inventory/v1/summaries)
The live client has NOT been run against a real seller account (see VERIFICATION.md).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Protocol

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"  # noqa: S105 - public endpoint, not a secret
SPAPI_HOSTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SpApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"SP-API {status}: {message}")
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS or self.status == 0


class SpApiClient(Protocol):
    def create_fulfillment_order(self, body: dict) -> dict: ...

    def get_fulfillment_order(self, seller_fulfillment_order_id: str) -> dict: ...

    def get_package_tracking_details(self, package_number: int) -> dict: ...

    def get_inventory_summaries(
        self, marketplace_id: str, seller_skus: list[str], next_token: str | None = None
    ) -> dict: ...


# --------------------------------------------------------------------------- mock


class MockStateBackend(Protocol):
    def get(self, key: str) -> dict | None: ...

    def put(self, key: str, value: dict) -> None: ...


class InMemoryMockState:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        self._data[key] = json.loads(json.dumps(value))


def _now() -> datetime:
    return datetime.now(UTC)


class MockSpApiClient:
    """Deterministic stand-in for SP-API. No network access.

    Behaviour is driven by synthetic data only:
    - recipient name ``FAIL_TRANSIENT``: first two create calls return 503, then succeed
    - recipient name ``FAIL_ALWAYS``: every create call returns 503
    - recipient name ``INVALID_ADDRESS``: create returns 400
    - an order "ships" ``ship_after`` seconds after creation and is "delivered" after
      ``2 * ship_after`` seconds
    - seller SKUs containing ``OOS`` have zero fulfillable quantity; containing ``MISSING``
      are absent from FBA inventory
    """

    def __init__(self, state: MockStateBackend | None = None, ship_after_seconds: int = 0, clock=_now):
        self.state = state or InMemoryMockState()
        self.ship_after = timedelta(seconds=ship_after_seconds)
        self.clock = clock
        self.calls: list[str] = []

    def create_fulfillment_order(self, body: dict) -> dict:
        self.calls.append("createFulfillmentOrder")
        sfo_id = body["sellerFulfillmentOrderId"]
        name = body["destinationAddress"]["name"]
        counter_key = f"attempts#{sfo_id}"
        attempts = (self.state.get(counter_key) or {"n": 0})["n"] + 1
        self.state.put(counter_key, {"n": attempts})
        if name == "INVALID_ADDRESS":
            raise SpApiError(400, "InvalidInput: destinationAddress")
        if name == "FAIL_ALWAYS" or (name == "FAIL_TRANSIENT" and attempts <= 2):
            raise SpApiError(503, "ServiceUnavailable (mock)")
        if self.state.get(f"order#{sfo_id}") is None:
            self.state.put(
                f"order#{sfo_id}",
                {"request": body, "createdAt": self.clock().isoformat()},
            )
        return {}  # CreateFulfillmentOrderResponse carries only optional `errors`

    def get_fulfillment_order(self, seller_fulfillment_order_id: str) -> dict:
        self.calls.append("getFulfillmentOrder")
        rec = self.state.get(f"order#{seller_fulfillment_order_id}")
        if rec is None:
            raise SpApiError(404, "NotFound (mock)")
        req = rec["request"]
        age = self.clock() - datetime.fromisoformat(rec["createdAt"])
        shipped = age >= self.ship_after
        status = "Complete" if shipped else "Processing"
        shipments = []
        if shipped:
            pkg_no = _package_number(seller_fulfillment_order_id)
            self.state.put(f"pkg#{pkg_no}", {"sfoId": seller_fulfillment_order_id})
            shipments.append(
                {
                    "amazonShipmentId": f"MOCK-SHIP-{pkg_no}",
                    "fulfillmentCenterId": "MOCK1",
                    "fulfillmentShipmentStatus": "SHIPPED",
                    "fulfillmentShipmentPackage": [
                        {"packageNumber": pkg_no, "carrierCode": "MOCKCARRIER", "trackingNumber": f"MOCKTRK{pkg_no}"}
                    ],
                }
            )
        return {
            "payload": {
                "fulfillmentOrder": {
                    "sellerFulfillmentOrderId": seller_fulfillment_order_id,
                    "displayableOrderId": req["displayableOrderId"],
                    "fulfillmentOrderStatus": status,
                    "shippingSpeedCategory": req["shippingSpeedCategory"],
                },
                "fulfillmentOrderItems": [
                    {"sellerSku": i["sellerSku"], "quantity": i["quantity"]} for i in req["items"]
                ],
                "fulfillmentShipments": shipments,
            }
        }

    def get_package_tracking_details(self, package_number: int) -> dict:
        self.calls.append("getPackageTrackingDetails")
        for_order = self.state.get(f"pkg#{package_number}")
        delivered = False
        if for_order:
            rec = self.state.get(f"order#{for_order['sfoId']}")
            if rec:
                age = self.clock() - datetime.fromisoformat(rec["createdAt"])
                delivered = age >= 2 * self.ship_after
        return {
            "payload": {
                "packageNumber": package_number,
                "trackingNumber": f"MOCKTRK{package_number}",
                "carrierCode": "MOCKCARRIER",
                "customerTrackingLink": f"https://tracking.example.com/MOCKTRK{package_number}",
                "currentStatus": "DELIVERED" if delivered else "IN_TRANSIT",
            }
        }

    def get_inventory_summaries(self, marketplace_id: str, seller_skus: list[str], next_token=None) -> dict:
        self.calls.append("getInventorySummaries")
        summaries = []
        for sku in seller_skus:
            if "MISSING" in sku:
                continue
            qty = 0 if "OOS" in sku else 25 + (sum(map(ord, sku)) % 50)
            summaries.append(
                {
                    "sellerSku": sku,
                    "fnSku": f"X00{abs(hash(sku)) % 10**7:07d}",
                    "condition": "NewItem",
                    "inventoryDetails": {"fulfillableQuantity": qty},
                    "totalQuantity": qty,
                }
            )
        return {
            "payload": {
                "granularity": {"granularityType": "Marketplace", "granularityId": marketplace_id},
                "inventorySummaries": summaries,
            },
            "pagination": {},
        }


def _package_number(sfo_id: str) -> int:
    return 100000 + sum((i + 1) * ord(c) for i, c in enumerate(sfo_id)) % 900000


# --------------------------------------------------------------------------- live


class LiveSpApiClient:
    """Minimal SP-API client using an LWA refresh token.

    Credentials are read by the caller from AWS Secrets Manager and passed in;
    nothing is cached on disk. Not exercised against a real account in this prototype.
    """

    def __init__(self, region_key: str, client_id: str, client_secret: str, refresh_token: str, opener=None):
        if region_key not in SPAPI_HOSTS:
            raise ValueError(f"unknown SP-API region {region_key!r}")
        self.host = SPAPI_HOSTS[region_key]
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token: str | None = None
        self._token_expiry = 0.0
        self._open = opener or urllib.request.urlopen

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        data = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        ).encode()
        req = urllib.request.Request(LWA_TOKEN_URL, data=data, method="POST")  # noqa: S310 - fixed https URL
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        body = self._send(req)
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 0))
        return self._token

    def _send(self, req: urllib.request.Request) -> dict:
        try:
            with self._open(req, timeout=20) as resp:  # noqa: S310
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise SpApiError(exc.code, exc.read().decode(errors="replace")[:500]) from exc
        except urllib.error.URLError as exc:
            raise SpApiError(0, str(exc.reason)) from exc

    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = self.host + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=False)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - https host from allowlist
        req.add_header("x-amz-access-token", self._access_token())
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "ecommerce-graviton-cicd-prototype/0.1 (Language=Python)")
        return self._send(req)

    def create_fulfillment_order(self, body: dict) -> dict:
        return self._call("POST", "/fba/outbound/2020-07-01/fulfillmentOrders", body=body)

    def get_fulfillment_order(self, seller_fulfillment_order_id: str) -> dict:
        sfo = urllib.parse.quote(seller_fulfillment_order_id, safe="")
        return self._call("GET", f"/fba/outbound/2020-07-01/fulfillmentOrders/{sfo}")

    def get_package_tracking_details(self, package_number: int) -> dict:
        return self._call("GET", "/fba/outbound/2020-07-01/tracking", query={"packageNumber": int(package_number)})

    def get_inventory_summaries(self, marketplace_id: str, seller_skus: list[str], next_token=None) -> dict:
        query = {
            "details": "true",
            "granularityType": "Marketplace",
            "granularityId": marketplace_id,
            "marketplaceIds": marketplace_id,
            "sellerSkus": ",".join(seller_skus),
        }
        if next_token:
            query["nextToken"] = next_token
        return self._call("GET", "/fba/inventory/v1/summaries", query=query)
