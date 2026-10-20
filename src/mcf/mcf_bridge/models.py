"""Domain model for the storefront side of the MCF bridge."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum


class OrderState(StrEnum):
    RECEIVED = "RECEIVED"  # order-paid event accepted, not yet sent to MCF
    SUBMITTED = "SUBMITTED"  # createFulfillmentOrder accepted
    SHIPPED = "SHIPPED"  # at least one package has a tracking number
    COMPLETE = "COMPLETE"  # every package reported DELIVERED
    EXCEPTION = "EXCEPTION"  # needs a human (alarm raised)
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {OrderState.COMPLETE, OrderState.CANCELLED}


class MappingStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    VALID = "VALID"  # MSKU found in FBA inventory for the marketplace
    INVALID = "INVALID"  # MSKU not found for the marketplace


# Storefront shipping options -> Fulfillment Outbound ShippingSpeedCategory enum.
SHIPPING_SPEED = {
    "standard": "Standard",
    "expedited": "Expedited",
    "priority": "Priority",
}

FULFILLMENT_POLICIES = {"FillOrKill", "FillAll", "FillAllAvailable"}

_SAFE_ID = re.compile(r"[^A-Za-z0-9-]")


class ValidationError(ValueError):
    """Input that can never succeed; not retried."""


@dataclass(frozen=True)
class Address:
    name: str
    address_line1: str
    city: str
    state_or_region: str
    postal_code: str
    country_code: str
    address_line2: str = ""
    phone: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Address:
        try:
            return cls(
                name=d["name"],
                address_line1=d["addressLine1"],
                address_line2=d.get("addressLine2", ""),
                city=d.get("city", ""),
                state_or_region=d.get("stateOrRegion", ""),
                postal_code=d["postalCode"],
                country_code=d["countryCode"],
                phone=d.get("phone", ""),
            )
        except KeyError as exc:  # required by the SP-API Address model
            raise ValidationError(f"address missing field {exc}") from exc

    def to_spapi(self) -> dict:
        out = {
            "name": self.name,
            "addressLine1": self.address_line1,
            "city": self.city,
            "stateOrRegion": self.state_or_region,
            "postalCode": self.postal_code,
            "countryCode": self.country_code,
        }
        if self.address_line2:
            out["addressLine2"] = self.address_line2
        if self.phone:
            out["phone"] = self.phone
        return out


@dataclass(frozen=True)
class OrderLine:
    storefront_sku: str
    quantity: int


@dataclass(frozen=True)
class PaidOrder:
    """Payload of the storefront `Order Paid` event."""

    storefront_order_id: str
    marketplace: str  # logical key, e.g. "US"; resolved to a marketplaceId via config
    placed_at: str  # ISO-8601
    shipping_speed: str
    address: Address
    lines: tuple[OrderLine, ...]
    comment: str = "Thank you for your order."
    fulfillment_policy: str | None = None

    @classmethod
    def from_event_detail(cls, detail: dict) -> PaidOrder:
        try:
            lines = tuple(
                OrderLine(storefront_sku=str(x["sku"]), quantity=int(x["quantity"])) for x in detail["lines"]
            )
            order = cls(
                storefront_order_id=str(detail["orderId"]),
                marketplace=str(detail["marketplace"]),
                placed_at=str(detail["placedAt"]),
                shipping_speed=str(detail.get("shippingSpeed", "standard")).lower(),
                address=Address.from_dict(detail["shippingAddress"]),
                lines=lines,
                comment=str(detail.get("comment") or "Thank you for your order."),
                fulfillment_policy=detail.get("fulfillmentPolicy"),
            )
        except (KeyError, TypeError) as exc:
            raise ValidationError(f"malformed order event: {exc}") from exc
        order.validate()
        return order

    def validate(self) -> None:
        if not self.lines:
            raise ValidationError("order has no lines")
        if any(line.quantity <= 0 for line in self.lines):
            raise ValidationError("quantity must be positive")
        if self.shipping_speed not in SHIPPING_SPEED:
            raise ValidationError(f"unsupported shipping speed {self.shipping_speed!r}")
        if self.fulfillment_policy and self.fulfillment_policy not in FULFILLMENT_POLICIES:
            raise ValidationError(f"unsupported fulfillment policy {self.fulfillment_policy!r}")

    @property
    def seller_fulfillment_order_id(self) -> str:
        """Deterministic id (idempotency key on the MCF side), max 40 chars per the model."""
        return seller_fulfillment_order_id(self.storefront_order_id)


def seller_fulfillment_order_id(storefront_order_id: str) -> str:
    safe = _SAFE_ID.sub("-", storefront_order_id)
    candidate = f"SF-{safe}"
    if len(candidate) <= 40:
        return candidate
    digest = hashlib.sha256(storefront_order_id.encode()).hexdigest()[:12]
    return f"SF-{safe[:24]}-{digest}"


@dataclass
class OrderRecord:
    storefront_order_id: str
    state: OrderState
    seller_fulfillment_order_id: str
    marketplace: str
    attempts: int = 0
    last_error: str = ""
    packages: list[dict] = field(default_factory=list)
    updated_at: str = ""
