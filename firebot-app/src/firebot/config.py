"""All tunable numbers in one place. Thermal calibration values describe the SIMULATED sensor; replace with real
calibration data when real hardware exists."""
from __future__ import annotations

from dataclasses import dataclass

from firebot_infra.drishti import MeasurementSpec


@dataclass(frozen=True)
class Config:
    dt: float = 0.1
    # geometry
    robot_radius_m: float = 0.25
    probe_m: float = 0.6                 # thermal sensors sample a point this far ahead
    thermal_angle: float = 0.5           # rad, left/right thermal sensors
    range_max_m: float = 4.0
    # thermal behaviour
    ambient_c: float = 22.0
    detect_rise_c: float = 8.0
    lost_rise_c: float = 4.0
    lost_after_s: float = 3.0
    standoff_rise_c: float = 200.0       # stop when the centre probe is this much hotter than ambient (~1.7 m away)
    cal_amplitude_c: float = 300.0
    cal_sigma_m: float = 1.2
    # motion
    cruise_v: float = 0.4
    approach_v: float = 0.3
    max_v: float = 0.5
    max_w: float = 1.5
    max_accel: float = 1.0               # m/s^2 (guard rate limit on speeding up)
    avoid_travel_m: float = 0.6          # start steering away when free travel ahead is below this
    stop_travel_m: float = 0.15          # guard: never drive forward when free travel ahead is below this
    body_margin_m: float = 0.08          # safety margin added to the robot radius
    # mission rules
    max_mission_s: float = 300.0
    hold_timeout_s: float = 10.0
    resume_after_steps: int = 5
    low_battery_pct: float = 15.0
    cell_m: float = 0.5


THERMAL = ("thermal_l", "thermal_c", "thermal_r")
# seven range beams fanned over 180 degrees (left is positive), like a very small lidar
RANGE_ANGLES = {"range_m90": -1.5708, "range_m60": -1.0472, "range_m30": -0.5236, "range_0": 0.0,
                "range_p30": 0.5236, "range_p60": 1.0472, "range_p90": 1.5708}
RANGE = tuple(RANGE_ANGLES)
SENSOR_TYPES = {**{k: "temperature" for k in THERMAL}, **{k: "distance" for k in RANGE},
                "pose": "pose", "battery": "battery"}


def specs() -> list[MeasurementSpec]:
    return [
        MeasurementSpec("temperature", "degC", max_age_s=0.5, min_value=-40, max_value=1200,
                        flatline_window_s=1.0, flatline_min_samples=5),     # noisy analog probe: a frozen value is a fault
        MeasurementSpec("distance", "m", max_age_s=0.5, min_value=0.0, max_value=10.0),
        MeasurementSpec("pose", "pose_xyt", max_age_s=0.5, dims=3, frame_id="odom"),
        MeasurementSpec("battery", "%", max_age_s=2.0, min_value=0.0, max_value=100.0),
    ]
