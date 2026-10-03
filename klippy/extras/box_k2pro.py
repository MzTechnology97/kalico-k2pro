# Copyright (C) 2026 MzTechnology97 and contributors
# Protocol lineage: Jacob10383/k2-plus-custom-firmware (GPLv3) plus
# K2-OpenHost clean-room validation against Creality's published CFS 1.1.3 image.
# This file is distributed under the terms of the GNU GPLv3 license.
"""K2 Pro compatibility adapter for Jacob's generic Box backend.

CFS firmware 1.1.3 returns a four-byte command-0x0A state payload instead of
the six-byte payload used by the original K2 Plus implementation.  Firmware
analysis and live captures establish the layout as:

    signed temperature C | humidity % | event byte | box state

The downstream/hub mask is omitted from 0x0A, but is available through the
already-supported command-0x08 hub-mask query.

This module keeps those K2-Pro-specific differences outside Jacob's generic
protocol implementation.  It installs the adapter during config loading,
before Box enumeration and polling begin.
"""

import struct
from dataclasses import replace

from extras import box_protocol


class BoxK2Pro:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.box = self.printer.lookup_object("box", None)
        if self.box is None:
            raise config.error("[box_k2pro] requires [box] first")
        self._install_decoder()
        self._install_snapshot_adapter()

    @staticmethod
    def _install_decoder():
        if getattr(box_protocol, "_k2pro_state_decoder_installed", False):
            return
        original = box_protocol.decode_box_state

        def decode_box_state(frame, address):
            reply = box_protocol.decode_reply(
                frame, address, box_protocol.CMD_BOX_STATE)

            # Keep Jacob's wire-error and four-slot event handling unchanged.
            if (reply.status in box_protocol.WIRE_ERROR_STATUSES
                    or reply.status == box_protocol.STATUS_SLOT_EVENT
                    or len(reply.payload) != 4):
                return original(frame, address)

            state = reply.payload[3]
            if state not in range(6):
                raise box_protocol.ProtocolError(
                    "unknown K2 Pro box-state value %d" % state)
            if state == box_protocol.BOX_STATE_PRINT:
                if reply.status not in (
                        box_protocol.STATUS_OK, box_protocol.STATUS_RUNOUT):
                    raise box_protocol.ProtocolError(
                        "K2 Pro print-state response has invalid status")
            elif (state != box_protocol.BOX_STATE_ERROR
                    and reply.status != box_protocol.STATUS_OK):
                raise box_protocol.ProtocolError(
                    "K2 Pro non-error state has nonzero status")

            return box_protocol.BoxStateReply(
                reply.address,
                reply.command,
                reply.status,
                reply.payload,
                reply.raw,
                struct.unpack("b", reply.payload[:1])[0],
                reply.payload[1],
                state,
                None,
                None,
            )

        box_protocol.decode_box_state = decode_box_state
        box_protocol._k2pro_state_decoder_installed = True

    def _install_snapshot_adapter(self):
        if getattr(self.box, "_k2pro_snapshot_adapter_installed", False):
            return
        base = self.box._query_box_snapshot

        def query_box_snapshot(address, driver, include_topology):
            reply = base(address, driver, include_topology)
            if (reply is None
                    or reply.slot_events is not None
                    or reply.downstream_mask is not None):
                return reply

            hub = driver.query_hub_mask(timeout=0.5)
            if (hub is not None
                    and hub.status == box_protocol.STATUS_OK
                    and hub.value is not None):
                # K2 Pro firmware keeps the hub selector latched on the last
                # channel even after the filament path is completely empty.
                # Therefore CMD_SLOT_MASK(1) is a route/selector mask, not by
                # itself proof that a filament is loaded.  Only promote that
                # mask to Jacob's generic downstream_mask when the printhead
                # sensor confirms filament or the CFS is in an active path
                # state.  This prevents a failed/finished load from leaving a
                # phantom loaded slot (for example T3 while state=IDLE,
                # buffer=empty and the printhead sensor is clear).
                detected, _sensor_error = self.box.get_filament_sensor_state()
                active_path = (
                    detected is True
                    or reply.box_state in (
                        box_protocol.BOX_STATE_PRELOAD,
                        box_protocol.BOX_STATE_PRINT,
                        box_protocol.BOX_STATE_RELOAD,
                    )
                )
                return replace(
                    reply,
                    downstream_mask=hub.value if active_path else 0,
                )
            return reply

        self.box._query_box_snapshot = query_box_snapshot
        self.box._k2pro_snapshot_adapter_installed = True


def load_config(config):
    return BoxK2Pro(config)
