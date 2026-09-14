"""Walk-burst approach gating: walk -> settle -> sense phase cycle.

TonyPi's camera bounces badly while walking (29% detection rate on captured
venue frames mid-walk, versus reliable detection once still), so modes 1-3
move in bursts: repeat the last sensed motion intent for a while, stop and
let the frame settle, then run detection for a sense window before choosing
the next intent.  The walk burst shrinks as the ball grows in frame, so the
robot re-senses more and more often as it closes in.
"""

from __future__ import annotations

from collections.abc import Callable

from .controller import ControllerConfig
from .model import RobotCommand


class ApproachGate:
    """Phase clock for burst-walking approaches; read config live each call.

    Starts in ``sense`` so a freshly armed session looks before it leaps.
    ``advance`` auto-rotates phases when their window expires and returns the
    current phase; ``hold_settle`` forces a re-settle (e.g. after an obstacle
    maneuver aborted a walk burst).
    """

    def __init__(self, config_provider: Callable[[], ControllerConfig], clock: Callable[[], float]):
        self._config = config_provider
        self._clock = clock
        self._phase = "sense"
        self._phase_until: float | None = None  # first advance opens the window

    @property
    def phase(self) -> str:
        return self._phase

    def advance(
        self,
        radius_ratio: float | None,
        command: RobotCommand | None = None,
    ) -> str:
        config = self._config()
        now = self._clock()
        if self._phase_until is None:
            self._phase_until = now + config.sense_seconds
            return self._phase
        if now < self._phase_until:
            return self._phase
        if self._phase == "walk":
            self._phase, duration = "settle", config.settle_seconds
        elif self._phase == "settle":
            self._phase, duration = "sense", config.sense_seconds
        else:
            if command == RobotCommand.stop():
                # Keep sensing through pending alignment/arrival or a missed
                # target, instead of spending a blind cycle repeating STOP.
                self._phase_until = now + config.sense_seconds
                return self._phase
            duration = self.walk_duration(radius_ratio, config)
            if command is not None and (command.steer or command.lateral):
                duration = self.turn_duration(command, config)
            self._phase = "walk"
        self._phase_until = now + duration
        return self._phase

    def hold_settle(self) -> None:
        """Abort the current walk burst: stand still and re-sense next."""
        self._phase = "settle"
        self._phase_until = self._clock() + self._config().settle_seconds

    @staticmethod
    def turn_duration(command: RobotCommand, config: ControllerConfig) -> float:
        """Fixed request window per gait tier, NOT a fractional action step.

        Used only by the explicit legacy fallback. STEP_V1 corrections in
        run_loop wait for completion instead of this deadline. A shorter
        legacy window cannot shrink the step angle or guarantee one execution.
        """
        magnitude = abs(command.steer or command.lateral)
        return config.turn_seconds_near if magnitude <= 0.45 else config.turn_seconds_far

    @staticmethod
    def walk_duration(radius_ratio: float | None, config: ControllerConfig) -> float:
        """Linear shrink from walk_seconds_far (ball small) to _near (ball big)."""
        if radius_ratio is None:
            return config.walk_seconds_far
        span = config.arrival_radius_ratio - config.slow_radius_ratio
        if span <= 0 or radius_ratio <= config.slow_radius_ratio:
            return config.walk_seconds_far
        if radius_ratio >= config.arrival_radius_ratio:
            return config.walk_seconds_near
        share = (config.arrival_radius_ratio - radius_ratio) / span
        return config.walk_seconds_near + share * (
            config.walk_seconds_far - config.walk_seconds_near
        )
