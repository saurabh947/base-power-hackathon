"""Per-battery status codes for the live map (docs/API.md `battery_status`).

Codes (4 bits each):
  0 offline          taken offline by a visitor (an availability override)
  1 waiting          online, 0 kW this tick
  2 selling          commanded to discharge this tick
  3 charging         commanded to charge this tick
  4 controller down  its controller is suspect or dead (missed this tick, or was already suspect when the
                     tick was sent) and the battery hasn't been handed over to a healthy one yet

Packing: battery i is in byte i // 2, the low nibble for even i and the high nibble for odd i, so 20,000
batteries take 10,000 bytes. The coordinator writes the packed bytes to one Redis key every tick; the API
sends them base64-encoded in the snapshot and in every frame.
"""
from __future__ import annotations

import numpy as np

OFFLINE, WAITING, SELLING, CHARGING, CONTROLLER_DOWN = 0, 1, 2, 3, 4
NAMES = {OFFLINE: "offline", WAITING: "waiting", SELLING: "selling", CHARGING: "charging",
         CONTROLLER_DOWN: "controller_down"}
SETPOINT_EPS_KW = 1e-6  # |setpoint| at or below this is "waiting"


def tick_codes(setpoints_kw: np.ndarray, offline: np.ndarray, controller_down: np.ndarray) -> np.ndarray:
    """uint8 code per battery for one tick, vectorized: offline wins, then controller down, then the setpoint."""
    sp = np.asarray(setpoints_kw)
    codes = np.full(sp.shape[0], WAITING, dtype=np.uint8)
    codes[sp > SETPOINT_EPS_KW] = SELLING
    codes[sp < -SETPOINT_EPS_KW] = CHARGING
    codes[controller_down] = CONTROLLER_DOWN
    codes[offline] = OFFLINE
    return codes


def idle_codes(offline: np.ndarray) -> np.ndarray:
    """No run: every battery is waiting (1) or offline (0)."""
    return np.where(np.asarray(offline, dtype=bool), OFFLINE, WAITING).astype(np.uint8)


def pack(codes: np.ndarray) -> bytes:
    c = np.asarray(codes, dtype=np.uint8)
    if c.size and c.max() > 15:
        raise ValueError("status codes must fit in 4 bits")
    if len(c) % 2:
        c = np.append(c, np.uint8(0))
    return (c[0::2] | (c[1::2] << 4)).astype(np.uint8).tobytes()


def unpack(data: bytes, n: int | None = None) -> np.ndarray:
    b = np.frombuffer(data, dtype=np.uint8)
    out = np.empty(2 * len(b), dtype=np.uint8)
    out[0::2] = b & 0x0F
    out[1::2] = b >> 4
    return out if n is None else out[:n]
