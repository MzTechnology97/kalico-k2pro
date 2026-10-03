# Copyright (C) 2026 MzTechnology97 and contributors
# Portions of the print-mapping workflow are derived from Jacob10383's
# k2-plus-custom-firmware (GPLv3). This file is distributed under GPLv3.
"""K2-OpenHost additions to the native Box print mapping.

The tool -> slot map itself, BOX_PRINT_INFO / BOX_PRINT_START and the
logical-tool aware purge/temperature handling live in ``box`` and
``BoxChangeEngine`` (Jacob's upstream implementation). This module only adds
what K2-OpenHost needs on top of it:

* automatic mapping of ordinary Moonraker/Orca starts (``auto_map_prints``),
  installed into the native engine map when the file is loaded;
* the mapping suggestion shown by the Mainsail dialog after BOX_PRINT_INFO;
* two extra ``box.get_status()`` fields:

    print_mapping_enabled
    auto_mapping
"""

import os

from extras.box_auto_mapping import suggest_mapping
from extras.box_gcode import read_metadata


class BoxPrintMapping:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.box = self.printer.lookup_object("box", None)
        if self.box is None:
            raise config.error("[box_print_mapping] requires [box] to be loaded first")

        self.change_engine = self.box.change_engine
        self.auto_map_prints = config.getboolean("auto_map_prints", False)
        self.auto_map_block_unresolved = config.getboolean("auto_map_block_unresolved", True)
        self.auto_mapping = {"state": "idle", "map": {}, "unresolved": []}
        self._explicit_start_in_progress = False

        # Extend the canonical box Moonraker object instead of publishing a
        # second competing CFS state object.
        self._base_get_status = self.box.get_status
        self.box.get_status = self._box_get_status

        # Box registers no commands in observation mode; nothing to wrap then.
        self._base_print_info = self.gcode.register_command("BOX_PRINT_INFO", None)
        if self._base_print_info is not None:
            self.gcode.register_command(
                "BOX_PRINT_INFO", self.cmd_print_info,
                desc="Inspect tools used by a print")
        self._base_print_start = self.gcode.register_command("BOX_PRINT_START", None)
        if self._base_print_start is not None:
            self.gcode.register_command(
                "BOX_PRINT_START", self.cmd_print_start,
                desc="Start a print with a tool map")

        # Box resets the native map on these events first (it registered its
        # handlers earlier), so an automatic map installed on load survives.
        for event in (
                "print_stats:complete_printing",
                "print_stats:error_printing",
                "print_stats:cancelled_printing",
                "print_stats:reset",
                "virtual_sdcard:reset_file"):
            self.printer.register_event_handler(event, self._reset_auto_mapping)
        self.printer.register_event_handler(
            "virtual_sdcard:load_file", self._handle_file_loaded)

    # ------------------------------------------------------------------
    # Moonraker status contract
    # ------------------------------------------------------------------

    def _box_get_status(self, eventtime):
        status = dict(self._base_get_status(eventtime))
        status["print_mapping_enabled"] = not bool(
            getattr(self.box, "observation_mode", False))
        status["auto_mapping"] = dict(self.auto_mapping)
        return status

    # ------------------------------------------------------------------
    # Automatic mapping
    # ------------------------------------------------------------------

    def _reset_auto_mapping(self, *args):
        self.auto_mapping = {"state": "idle", "map": {}, "unresolved": []}

    def _suggest_mapping(self, tools):
        if not tools:
            self.auto_mapping = {
                "state": "no_tools", "map": {}, "unresolved": []}
            return {}, []
        status = self._base_get_status(
            self.printer.get_reactor().monotonic())
        slots = [
            slot for slot in status.get("slots", [])
            if slot.get("external") or slot.get("present")
        ]
        mapping, unresolved = suggest_mapping(tools, slots)
        self.auto_mapping = {
            "state": "unresolved" if unresolved else "ready",
            "map": {str(tool): slot for tool, slot in mapping.items()},
            "unresolved": unresolved,
        }
        return mapping, unresolved

    def _recovery_in_progress(self):
        # PLR_RECOVER loads the file and then restores the saved map.
        plr = self.printer.lookup_object("power_loss_recovery", None)
        return bool(getattr(plr, "recovering", False))

    def _handle_file_loaded(self, *args):
        self._reset_auto_mapping()
        if (self._explicit_start_in_progress or not self.auto_map_prints
                or getattr(self.box, "observation_mode", False)
                or self._recovery_in_progress()):
            return
        sd = self.printer.lookup_object("virtual_sdcard", None)
        if sd is None or sd.current_file is None:
            return
        path = os.path.realpath(getattr(sd.current_file, "name", ""))
        root = os.path.realpath(sd.sdcard_dirname)
        if not path or os.path.commonpath((root, path)) != root:
            return
        filename = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            metadata = read_metadata(path)
        except OSError:
            return
        tools = metadata.get("tools", [])
        self.box.print_info = {"filename": filename, "tools": tools}
        mapping, unresolved = self._suggest_mapping(tools)
        if not tools:
            return
        if unresolved:
            if self.auto_map_block_unresolved:
                raise self.gcode.error(
                    "[BOX]: Automatic CFS mapping unresolved for %s"
                    % ", ".join("T%d" % tool for tool in unresolved))
            return
        self.box._register_tools(mapping)
        engine = self.change_engine
        engine.tool_map = dict(mapping)
        engine.mapping_filename = filename
        engine.active_tool = engine.active_slot = None
        self.auto_mapping["state"] = "active"

    # ------------------------------------------------------------------
    # Wrapped native commands
    # ------------------------------------------------------------------

    def cmd_print_info(self, gcmd):
        self._base_print_info(gcmd)
        info = self.box.print_info or {}
        self._suggest_mapping(info.get("tools", []))

    def cmd_print_start(self, gcmd):
        # An explicit map from the dialog replaces the automatic one, and an
        # unresolved automatic map must not block it.
        self._explicit_start_in_progress = True
        try:
            self._base_print_start(gcmd)
        finally:
            self._explicit_start_in_progress = False


def load_config(config):
    return BoxPrintMapping(config)
