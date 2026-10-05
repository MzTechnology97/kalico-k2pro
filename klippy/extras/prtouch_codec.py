# Codec for the K2 nozzle firmware's prtouch_v3 messages.
#
# Copyright (C) 2026  MzTechnology97
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Shared by prtouch (probing) and k2_load_cell_pa (APAX capture).

The nozzle firmware packs every series (ticks, sensor counts, E-step
intervals) the same way (prtouch_write_zip/prtouch_read_zip in the stock
prtouch_v3 object):

    [count][descriptors, last byte first][first value][delta][delta]...

Each descriptor byte holds four 2-bit width codes (width = code + 1 bytes).
The first value is the absolute value (a delta from 0), the others are
signed deltas from the previous value. Values are little endian.
"""

MASK32 = 0xFFFFFFFF


class FrameError(ValueError):
    pass


def decode_frame(payload, signed_first):
    """Decode one delta-packed integer series."""
    payload = bytes(payload)
    if not payload:
        return []
    count = payload[0]
    descriptor_len = (count + 3) // 4
    data_pos = 1 + descriptor_len
    if len(payload) < data_pos:
        raise FrameError("truncated descriptor")
    descriptors = payload[1:data_pos]
    values = []
    for index in range(count):
        descriptor = descriptors[-1 - index // 4]
        width = ((descriptor >> (2 * (index % 4))) & 3) + 1
        if data_pos + width > len(payload):
            raise FrameError("truncated data")
        raw = payload[data_pos : data_pos + width]
        data_pos += width
        value = int.from_bytes(
            raw, "little", signed=signed_first if index == 0 else True
        )
        if index:
            value += values[-1]
        values.append(value)
    return values


def encode_frame(values):
    """Encode a series like the firmware does (used by tests and replay)."""
    values = list(values)
    if len(values) > 255:
        raise FrameError("too many values for one frame")
    data = bytearray()
    codes = []
    last = 0
    for value in values:
        # The firmware subtracts in int32 arithmetic (ticks wrap at 2^32).
        delta = ((value - last + (1 << 31)) & MASK32) - (1 << 31)
        last = value
        for width in (1, 2, 3, 4):
            limit = 1 << (8 * width - 1)
            if -limit <= delta < limit:
                break
        else:
            raise FrameError("delta does not fit 32 bits")
        codes.append(width - 1)
        data += (delta & ((1 << (8 * width)) - 1)).to_bytes(width, "little")
    descriptors = bytearray((len(values) + 3) // 4)
    for index, code in enumerate(codes):
        descriptors[-1 - index // 4] |= code << (2 * (index % 4))
    return bytes([len(values)]) + bytes(descriptors) + bytes(data)


def decode_ticks(payload):
    """Decode a tick series as unsigned 32-bit values.

    The firmware picks the width of the first value from the int32 delta
    to 0, so a tick just below 2^32 (e.g. 0xFFFFF894) is sent in fewer
    bytes and must be sign-extended before masking. Later values can pass
    2^32 inside a block; they are masked too.
    """
    return [value & MASK32 for value in decode_frame(payload, True)]


class TickUnwrapper:
    """Extend 32-bit MCU ticks to a monotonic 64-bit clock.

    seed(clock64) anchors the first tick near a known 64-bit clock (from
    the MCU clock sync); later ticks only need to be within half the 32-bit
    range of the previous one.
    """

    def __init__(self):
        self.last = None

    def seed(self, clock64):
        self.last = clock64

    def unwrap(self, tick32):
        tick32 &= MASK32
        if self.last is None:
            self.last = tick32
            return tick32
        delta = (tick32 - self.last) & MASK32
        if delta >= 1 << 31:
            delta -= 1 << 32
        self.last += delta
        return self.last
