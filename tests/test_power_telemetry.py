from __future__ import annotations

import asyncio
import unittest

from live_site.app import _normalize_frontend_telemetry
from live_site.schemas import JetsonTelemetryEnvelope
from live_site.telemetry import TelemetryHub


HEALTHY_POWER = {
    "battery_channel": "CH1",
    "pack_voltage_v": 12.84,
    "shutdown_threshold_v": 12.0,
    "power_action": "stay_on",
    "will_shutdown": False,
    "status": "monitoring",
    "message": "Battery voltage is healthy.",
    "low_voltage_duration_sec": 0.0,
}


class PowerTelemetryTests(unittest.TestCase):
    def test_jetson_envelope_accepts_power_payload(self) -> None:
        envelope = JetsonTelemetryEnvelope.model_validate(
            {
                "device_id": "jetson-01",
                "cpu": 0.41,
                "memory": 0.72,
                "temp_c": 55.0,
                "robot_status": "Patrol",
                "timestamp": "2026-04-21T15:20:00Z",
                "power": HEALTHY_POWER,
                "plate_detections": [],
            }
        )

        self.assertIsNotNone(envelope.power)
        self.assertEqual(envelope.power.battery_channel, "CH1")
        self.assertEqual(envelope.power.pack_voltage_v, 12.84)
        self.assertFalse(envelope.power.will_shutdown)

    def test_frontend_normalization_preserves_power_and_plate_fields(self) -> None:
        normalized = _normalize_frontend_telemetry(
            {
                "device_id": "jetson-01",
                "robot_status": "Patrol",
                "timestamp": "2026-04-21T15:20:00Z",
                "power": HEALTHY_POWER,
                "plate_detections": [
                    {
                        "plate_read": "ABC1234",
                        "time": "2026-04-21T15:19:55Z",
                        "location": {"lat": 43.123456, "lon": -79.123456},
                        "confidence_level": 0.94,
                    }
                ],
            }
        )

        self.assertEqual(normalized["detected_plate"], "ABC1234")
        self.assertEqual(normalized["latitude"], 43.123456)
        self.assertEqual(normalized["power"]["power_action"], "stay_on")
        self.assertEqual(normalized["power"]["pack_voltage_v"], 12.84)

    def test_null_voltage_is_preserved_as_unavailable(self) -> None:
        normalized = _normalize_frontend_telemetry(
            {
                "device_id": "jetson-01",
                "timestamp": "2026-04-21T15:20:00Z",
                "power": {
                    "battery_channel": "CH1",
                    "pack_voltage_v": None,
                    "shutdown_threshold_v": 12.0,
                    "power_action": "unknown",
                    "will_shutdown": False,
                    "status": "waiting_for_battery",
                    "message": "Battery telemetry is not present in the Arduino status JSON yet.",
                    "low_voltage_duration_sec": 0.0,
                },
                "plate_detections": [],
            }
        )

        self.assertIsNone(normalized["power"]["pack_voltage_v"])
        self.assertEqual(normalized["power"]["power_action"], "unknown")
        self.assertEqual(normalized["power"]["status"], "waiting_for_battery")

    def test_missing_or_explicit_null_power_clears_live_snapshot(self) -> None:
        normalized_missing = _normalize_frontend_telemetry(
            {
                "device_id": "jetson-01",
                "timestamp": "2026-04-21T15:20:00Z",
                "plate_detections": [],
            }
        )
        self.assertIsNone(normalized_missing["power"])

        async def scenario() -> None:
            hub = TelemetryHub()
            await hub.publish({"timestamp": "2026-04-21T15:20:00Z", "power": HEALTHY_POWER})
            snapshot = await hub.publish({"timestamp": "2026-04-21T15:20:05Z", "power": None})
            self.assertIsNone(snapshot["power"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
