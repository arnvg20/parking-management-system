from __future__ import annotations

from dataclasses import dataclass

from .models import Location, TemporalDecision, WebsitePayload


@dataclass(frozen=True)
class WebsitePayloadFormatterConfig:
    preserve_existing_location_mapping: bool = True


class WebsitePayloadFormatter:
    def __init__(self, config: WebsitePayloadFormatterConfig | None = None) -> None:
        self.config = config or WebsitePayloadFormatterConfig()

    def _format_location(self, location: Location | None) -> dict[str, float] | None:
        if location is None:
            return None
        # Preserve the existing lat/lon object format by default.
        if self.config.preserve_existing_location_mapping:
            return {
                "lat": float(location.lat),
                "lon": float(location.lon),
            }
        return location.to_dict()

    def format(self, decision: TemporalDecision) -> WebsitePayload | None:
        if not decision.should_send or not decision.stable_plate_read or not decision.location:
            return None

        return WebsitePayload(
            plate_read=decision.stable_plate_read,
            time=decision.timestamp,
            location=self._format_location(decision.location),
            confidence_level=decision.confidence_level,
        )
