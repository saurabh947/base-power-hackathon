"""battery_status codes and packing (docs/API.md). No services needed."""
import base64
import time

import numpy as np

from src.sim import battery_status as bs


def test_codes_are_the_contracts():
    assert (bs.OFFLINE, bs.WAITING, bs.SELLING, bs.CHARGING, bs.CONTROLLER_DOWN) == (0, 1, 2, 3, 4)


def test_packing_puts_even_batteries_in_the_low_nibble():
    codes = np.array([1, 2, 3, 4, 0, 1, 2], dtype=np.uint8)  # odd count: the last high nibble is 0
    raw = bs.pack(codes)
    assert raw == bytes([0x21, 0x43, 0x10, 0x02])
    for i, c in enumerate(codes):  # docs/API.md: byte i // 2, low nibble for even i, high for odd i
        b = raw[i // 2]
        assert ((b & 0x0F) if i % 2 == 0 else (b >> 4)) == c
    assert np.array_equal(bs.unpack(raw, len(codes)), codes)


def test_twenty_thousand_batteries_take_ten_thousand_bytes():
    codes = np.random.default_rng(0).integers(0, 5, 20000).astype(np.uint8)
    raw = bs.pack(codes)
    assert len(raw) == 10000 and len(base64.b64encode(raw)) == 13336
    assert np.array_equal(bs.unpack(raw), codes)


def test_tick_codes_precedence():
    sp = np.array([5.0, -3.0, 0.0, 1e-9, 7.0, -2.0, 4.0])
    offline = np.array([False, False, False, False, True, False, True])
    down = np.array([False, False, False, False, False, True, True])
    codes = bs.tick_codes(sp, offline, down)
    # selling, charging, waiting, waiting (dust), offline wins, controller down, offline wins over down
    assert codes.tolist() == [2, 3, 1, 1, 0, 4, 0]
    assert bs.idle_codes(offline).tolist() == [1, 1, 1, 1, 0, 1, 0]


def test_codes_for_twenty_thousand_batteries_take_well_under_a_millisecond():
    rng = np.random.default_rng(1)
    sp = rng.normal(0, 5, 20000)
    offline = rng.random(20000) < 0.1
    down = rng.random(20000) < 0.05
    t0 = time.perf_counter()
    for _ in range(100):
        bs.pack(bs.tick_codes(sp, offline, down))
    per_tick_ms = (time.perf_counter() - t0) * 1000 / 100
    assert per_tick_ms < 2.0, per_tick_ms  # measured ~0.1 ms on an M3 Pro
