"""Typed dispatch for durable pipeline outbox events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"outbox payload field {key!r} must be non-empty text")
    return value


def _required_positive_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"outbox event field {key!r} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class CityActivationNotification:
    """Validated payload for one user/city activation delivery."""

    banana: str
    city_name: str
    state: str
    user_id: str
    email: str
    user_name: str

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "CityActivationNotification":
        return cls(
            banana=_required_text(payload, "banana"),
            city_name=_required_text(payload, "city_name"),
            state=_required_text(payload, "state"),
            user_id=_required_text(payload, "user_id"),
            email=_required_text(payload, "email"),
            user_name=_required_text(payload, "user_name"),
        )

    async def publish(self) -> None:
        from userland.email.transactional import send_city_available_email

        sent = await send_city_available_email(
            email=self.email,
            user_name=self.user_name,
            city_name=self.city_name,
            state=self.state,
            banana=self.banana,
        )
        if not sent:
            raise RuntimeError(
                f"city activation delivery failed for user {self.user_id}"
            )


async def dispatch_outbox_event(db: Any, event: Mapping[str, Any]) -> None:
    """Publish one claimed event; raise so the lease owner can retry it."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("outbox payload must be an object")

    event_type = event.get("event_type")
    if event_type == "queue.enqueue":
        queue_payload = dict(payload)
        queue_payload["desired_generation"] = _required_positive_int(
            event, "work_generation"
        )
        await db.queue.enqueue_job(**queue_payload)
        return
    if event_type == "notification.city_activated":
        await CityActivationNotification.from_payload(payload).publish()
        return
    raise ValueError(f"unsupported outbox event type: {event_type!r}")
