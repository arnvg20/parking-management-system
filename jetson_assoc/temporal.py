from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from .models import InternalDecision, SpaceStatus, TemporalDecision


@dataclass(frozen=True)
class TemporalStabilizerConfig:
    max_history: int = 12
    min_frames_for_confirmation: int = 3
    min_vote_share: float = 0.58
    min_stable_confidence: float = 0.82


class TemporalStabilizer:
    def __init__(self, config: TemporalStabilizerConfig | None = None) -> None:
        self.config = config or TemporalStabilizerConfig()
        self._history: dict[str, deque[InternalDecision]] = defaultdict(
            lambda: deque(maxlen=self.config.max_history)
        )

    @staticmethod
    def _normalized_plate(plate_text: str | None) -> str | None:
        if not plate_text:
            return None
        normalized = "".join(character for character in plate_text.upper() if character.isalnum())
        return normalized or None

    def update(self, decision: InternalDecision) -> TemporalDecision:
        history = self._history[decision.parking_space_id]
        history.append(decision)

        recent = list(history)
        occupied_frames = [item for item in recent if item.status == SpaceStatus.OCCUPIED and item.vehicle]
        uncertain_frames = [item for item in recent if item.status == SpaceStatus.UNCERTAIN]
        empty_frames = [item for item in recent if item.status == SpaceStatus.EMPTY]

        if occupied_frames:
            plate_votes: dict[str, float] = defaultdict(float)
            supporting_frames: dict[str, int] = defaultdict(int)
            best_decision_by_plate: dict[str, InternalDecision] = {}

            for item in occupied_frames:
                if not item.plate or not item.plate.plate_text:
                    continue
                normalized_plate = self._normalized_plate(item.plate.plate_text)
                if not normalized_plate:
                    continue

                vote_weight = max(0.0, item.association_confidence) * max(0.0, item.plate.ocr_confidence)
                plate_votes[normalized_plate] += vote_weight
                supporting_frames[normalized_plate] += 1

                best_existing = best_decision_by_plate.get(normalized_plate)
                if best_existing is None or item.association_confidence > best_existing.association_confidence:
                    best_decision_by_plate[normalized_plate] = item

            if plate_votes:
                total_vote = sum(plate_votes.values()) or 1.0
                winner_plate, winner_vote = max(plate_votes.items(), key=lambda item: item[1])
                winner_share = winner_vote / total_vote
                confirmed_frames = supporting_frames[winner_plate]
                winning_decision = best_decision_by_plate[winner_plate]
                stabilized_confidence = min(
                    1.0,
                    (winning_decision.association_confidence * 0.65) + (winner_share * 0.35),
                )
                should_send = (
                    confirmed_frames >= self.config.min_frames_for_confirmation
                    and winner_share >= self.config.min_vote_share
                    and stabilized_confidence >= self.config.min_stable_confidence
                )

                return TemporalDecision(
                    parking_space_id=decision.parking_space_id,
                    status=SpaceStatus.OCCUPIED,
                    stable_plate_read=winner_plate,
                    confidence_level=stabilized_confidence,
                    confirmed_frames=confirmed_frames,
                    should_send=should_send,
                    timestamp=winning_decision.timestamp,
                    location=winning_decision.gps_location,
                    base_decision=winning_decision,
                    debug={
                        "vote_share": winner_share,
                        "plate_vote_weights": dict(plate_votes),
                        "occupied_frames_considered": len(occupied_frames),
                        "uncertain_frames_considered": len(uncertain_frames),
                    },
                )

            strongest_occupied = max(occupied_frames, key=lambda item: item.association_confidence)
            return TemporalDecision(
                parking_space_id=decision.parking_space_id,
                status=SpaceStatus.UNCERTAIN,
                stable_plate_read=None,
                confidence_level=strongest_occupied.association_confidence * 0.5,
                confirmed_frames=len(occupied_frames),
                should_send=False,
                timestamp=strongest_occupied.timestamp,
                location=strongest_occupied.gps_location,
                base_decision=strongest_occupied,
                debug={
                    "reason": "vehicle_present_but_plate_not_stable",
                    "occupied_frames_considered": len(occupied_frames),
                    "uncertain_frames_considered": len(uncertain_frames),
                },
            )

        if empty_frames and len(empty_frames) >= self.config.min_frames_for_confirmation:
            strongest_empty = empty_frames[-1]
            return TemporalDecision(
                parking_space_id=decision.parking_space_id,
                status=SpaceStatus.EMPTY,
                stable_plate_read=None,
                confidence_level=min(1.0, 0.55 + (0.1 * min(len(empty_frames), 4))),
                confirmed_frames=len(empty_frames),
                should_send=False,
                timestamp=strongest_empty.timestamp,
                location=strongest_empty.gps_location,
                base_decision=strongest_empty,
                debug={
                    "reason": "consistent_empty_frames",
                    "empty_frames_considered": len(empty_frames),
                },
            )

        return TemporalDecision(
            parking_space_id=decision.parking_space_id,
            status=SpaceStatus.UNCERTAIN,
            stable_plate_read=None,
            confidence_level=decision.association_confidence * 0.5,
            confirmed_frames=len(recent),
            should_send=False,
            timestamp=decision.timestamp,
            location=decision.gps_location,
            base_decision=decision,
            debug={
                "reason": "insufficient_temporal_consistency",
                "history_size": len(recent),
            },
        )
