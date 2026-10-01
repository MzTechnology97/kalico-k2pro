# Copyright (C) 2026 MzTechnology97 and contributors
# Portions of the print-mapping workflow are derived from Jacob10383's
# k2-plus-custom-firmware (GPLv3). This file is distributed under GPLv3.
"""K2-OpenHost CFS print mapping compatibility layer.

This module brings Jacob's BOX_PRINT_INFO / BOX_PRINT_START workflow to the
K2-OpenHost branch without replacing the hardware-validated Box transport.
It deliberately composes the existing ``box`` and ``BoxChangeEngine`` objects.

The frontend contract is exposed by extending ``box.get_status()`` with:

    print_mapping_version
    print_mapping_enabled
    print_info
    print_mapping

Use this together with the native Mainsail K2-OpenHost CFS mapping dialog.
"""

import os

from extras.box_gcode import read_metadata


PRINT_MAPPING_VERSION = 1


class BoxPrintMapping:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.box = self.printer.lookup_object("box", None)
        if self.box is None:
            raise config.error("[box_print_mapping] requires [box] to be loaded first")

        self.change_engine = self.box.change_engine
        self.print_info = None
        self.metadata = None
        self.mapping_filename = None
        self.tool_map = {}
        self.active_tool = None
        self.active_slot = None
        self._fallback_tools = {}
        self._wrapped_tools = set()

        # Extend the canonical box Moonraker object instead of publishing a
        # second competing CFS state object.
        self._base_get_status = self.box.get_status
        self.box.get_status = self._box_get_status

        self.gcode.register_command(
            "BOX_PRINT_INFO", self.cmd_print_info,
            desc="Inspect tools used by a print")
        self.gcode.register_command(
            "BOX_PRINT_START", self.cmd_print_start,
            desc="Start a print with a CFS tool-to-slot map")

        # START_PRINT in the current K2 profile calls PARSE_FLUSH_VOLUMES. The
        # older OpenHost BoxChangeEngine parses that metadata by physical slot,
        # while the new UI mapping is logical-tool -> physical-slot. Wrap the
        # command so an active mapped print keeps the translated metadata.
        self._base_parse_flush = self.gcode.register_command(
            "PARSE_FLUSH_VOLUMES", None)
        if self._base_parse_flush is not None:
            self.gcode.register_command(
                "PARSE_FLUSH_VOLUMES", self.cmd_parse_flush_volumes,
                desc="Parse slicer flush metadata with CFS tool mapping")

        self.printer.register_event_handler("box:ready", self._box_ready)
        for event in (
                "print_stats:complete_printing",
                "print_stats:error_printing",
                "print_stats:cancelled_printing",
                "print_stats:reset",
                "virtual_sdcard:load_file",
                "virtual_sdcard:reset_file"):
            self.printer.register_event_handler(event, self._reset_mapping)

    # ------------------------------------------------------------------
    # Moonraker status contract
    # ------------------------------------------------------------------

    def _box_get_status(self, eventtime):
        status = dict(self._base_get_status(eventtime))
        status.update({
            "print_mapping_version": PRINT_MAPPING_VERSION,
            "print_mapping_enabled": not bool(
                getattr(self.box, "observation_mode", False)),
            "print_info": self.print_info,
            "print_mapping": {
                "filename": self.mapping_filename,
                "map": {str(tool): slot for tool, slot in self.tool_map.items()},
                "active_tool": self.active_tool,
                "active_slot": self.active_slot,
            },
        })
        return status

    # ------------------------------------------------------------------
    # Tool command wrapping
    # ------------------------------------------------------------------

    def _box_ready(self, *args):
        for tool in self.box.physical_slots + (self.box.external_slot,):
            self._wrap_tool(tool)

    def _wrap_tool(self, tool):
        tool = int(tool)
        if tool in self._wrapped_tools:
            return
        name = "T%d" % tool
        old_handler = self.gcode.register_command(name, None)
        self._fallback_tools[tool] = old_handler
        self.gcode.register_command(
            name,
            lambda gcmd, tool=tool: self.select_tool(gcmd, tool),
            desc="Select logical tool T%d through CFS mapping" % tool,
        )
        self._wrapped_tools.add(tool)

    def select_tool(self, gcmd, tool):
        if self.mapping_filename is None:
            fallback = self._fallback_tools.get(tool)
            if fallback is None:
                raise gcmd.error("[BOX]: T%d is not available" % tool)
            return fallback(gcmd)

        if tool not in self.tool_map:
            reason = "T%d has no slot in this print's mapping" % tool
            sd = self.printer.lookup_object("virtual_sdcard", None)
            if sd is not None and sd.is_active():
                try:
                    self.change_engine.block_resume(reason)
                except Exception:
                    pass
                self.box.pause_print()
                return False
            raise gcmd.error("[BOX]: " + reason)

        target = self.tool_map[tool]
        self._install_engine_metadata(self.active_tool, tool)
        result = self.change_engine.change(
            gcmd, target, bool(gcmd.get_int("FLUSH", 1)))
        if result:
            self.active_tool = tool
            self.active_slot = target
        return result

    # ------------------------------------------------------------------
    # Metadata / mapping setup
    # ------------------------------------------------------------------

    def _reset_mapping(self, *args):
        self.mapping_filename = None
        self.tool_map = {}
        self.active_tool = None
        self.active_slot = None
        self.metadata = None

    def _print_idle(self, gcmd):
        if (self.change_engine._is_print_active()
                or self.change_engine._is_print_paused()):
            raise gcmd.error("[BOX]: Finish or cancel the current print first")
        if getattr(self.box, "operation_depth", 0):
            raise gcmd.error("[BOX]: Wait for the current Box operation to finish")
        return self.printer.lookup_object("virtual_sdcard")

    def _print_path(self, gcmd, sd):
        filename = gcmd.get("FILENAME")
        if filename.startswith("/"):
            filename = filename[1:]
        root = os.path.realpath(sd.sdcard_dirname)
        path = os.path.realpath(os.path.join(root, filename))
        if (not filename or os.path.isabs(filename)
                or os.path.commonpath((root, path)) != root
                or not filename.lower().endswith((".gcode", ".gco", ".g"))):
            raise gcmd.error(
                "[BOX]: Select a text G-code file inside Virtual SD")
        return filename, path

    def _inspect_print(self, gcmd, sd):
        filename, path = self._print_path(gcmd, sd)
        try:
            metadata = read_metadata(path)
        except OSError as exc:
            raise gcmd.error("[BOX]: Unable to inspect print: %s" % exc)
        self.metadata = metadata
        self.print_info = {"filename": filename, "tools": metadata["tools"]}
        return self.print_info

    @staticmethod
    def _logical_value(values, tool):
        if values is None or tool is None or tool < 0 or tool >= len(values):
            return None
        return values[tool]

    def _fallback_slot_temp(self, slot):
        try:
            value = self.box.slot_target_temp(slot)
        except Exception:
            value = None
        if value is None:
            value = self.change_engine.default_temp
        return int(value)

    def _install_engine_metadata(self, source_tool=None, target_tool=None):
        """Translate logical-tool metadata to physical-slot indices.

        The currently validated K2-OpenHost BoxChangeEngine predates Jacob's
        logical-tool-aware ChangeRequest fields and indexes its purge/temperature
        arrays by physical slot. Build a slot-indexed view here so the existing
        hardware-tested engine can consume the newer print-mapping contract.
        """
        if not self.metadata or not self.tool_map:
            return

        size = max([self.box.external_slot] + list(self.tool_map.values())) + 1
        matrix = [[None for _ in range(size)] for _ in range(size)]
        temp_print = [self._fallback_slot_temp(slot) for slot in range(size)]
        temp_initial = [self._fallback_slot_temp(slot) for slot in range(size)]

        logical_matrix = self.metadata.get("matrix")
        logical_print = self.metadata.get("temp_print")
        logical_initial = self.metadata.get("temp_initial_layer")

        for logical, slot in self.tool_map.items():
            value = self._logical_value(logical_print, logical)
            if value is not None:
                temp_print[slot] = int(value)
            value = self._logical_value(logical_initial, logical)
            if value is not None:
                temp_initial[slot] = int(value)

        if logical_matrix:
            for source_logical, source_slot in self.tool_map.items():
                if source_logical >= len(logical_matrix):
                    continue
                row = logical_matrix[source_logical]
                for target_logical, target_slot in self.tool_map.items():
                    if target_logical < len(row):
                        matrix[source_slot][target_slot] = row[target_logical]

        # If more than one logical tool intentionally maps to one physical slot,
        # ensure the active transition uses the right logical temperatures and
        # matrix entry rather than whichever mapping happened to be iterated last.
        if target_tool in self.tool_map:
            target_slot = self.tool_map[target_tool]
            value = self._logical_value(logical_print, target_tool)
            if value is not None:
                temp_print[target_slot] = int(value)
            value = self._logical_value(logical_initial, target_tool)
            if value is not None:
                temp_initial[target_slot] = int(value)
        if (source_tool in self.tool_map and target_tool in self.tool_map
                and logical_matrix and source_tool < len(logical_matrix)):
            row = logical_matrix[source_tool]
            if target_tool < len(row):
                matrix[self.tool_map[source_tool]][self.tool_map[target_tool]] = row[target_tool]

        self.change_engine.matrix = matrix
        self.change_engine.temp_print = temp_print
        self.change_engine.temp_initial_layer = temp_initial
        try:
            self.change_engine.parsed_epoch = self.change_engine._print_epoch()
        except Exception:
            self.change_engine.parsed_epoch = None

    # ------------------------------------------------------------------
    # Public G-code API used by Mainsail / Fluidd-style frontends
    # ------------------------------------------------------------------

    def cmd_print_info(self, gcmd):
        self._inspect_print(gcmd, self._print_idle(gcmd))

    def cmd_parse_flush_volumes(self, gcmd):
        if self.mapping_filename is not None and self.metadata is not None:
            self._install_engine_metadata(self.active_tool, self.active_tool)
            return
        if self._base_parse_flush is None:
            raise gcmd.error("[BOX]: PARSE_FLUSH_VOLUMES is unavailable")
        return self._base_parse_flush(gcmd)

    def cmd_print_start(self, gcmd):
        if getattr(self.box, "observation_mode", False):
            raise gcmd.error(
                "[BOX]: Print mapping is disabled while observation_mode is enabled")

        sd = self._print_idle(gcmd)
        info = self._inspect_print(gcmd, sd)
        if not info["tools"]:
            raise gcmd.error(
                "[BOX]: No filament usage metadata; start this file normally")

        try:
            mapping = {}
            for entry in gcmd.get("MAP", "").split(","):
                if not entry.strip():
                    continue
                tool, slot = entry.split(":")
                tool, slot = int(tool), int(slot)
                if tool in mapping or not 0 <= tool <= 255 or slot < 0:
                    raise ValueError()
                mapping[tool] = slot
        except ValueError:
            raise gcmd.error(
                "[BOX]: MAP must contain unique tool:slot pairs, e.g. 0:1,1:3")

        used = {item["tool"] for item in info["tools"]}
        if set(mapping) != used:
            raise gcmd.error(
                "[BOX]: Map every tool used by this file: %s" %
                ", ".join("T%d" % tool for tool in sorted(used)))
        if not self.box.drivers_ready:
            raise gcmd.error("[BOX]: Filament slots are not ready")

        try:
            live = self.box.read_live_state()
        except Exception as exc:
            raise gcmd.error("[BOX]: Unable to read CFS state: %s" % exc)

        for slot in mapping.values():
            if not self.box.is_valid_slot(slot):
                raise gcmd.error("[BOX]: T%d is offline" % slot)
            if self.box.is_physical_slot(slot) and not live.slot_mask & (1 << slot):
                raise gcmd.error("[BOX]: T%d has no filament" % slot)

        for tool in used:
            self._wrap_tool(tool)

        # Match Jacob's ordering: reset/load events clear any old map, then the
        # new map is installed before Virtual SD schedules the first G-code.
        sd._reset_file()
        try:
            sd._load_file(gcmd, info["filename"], check_subdirs=True)
            self.tool_map = mapping
            self.mapping_filename = info["filename"]
            self.active_tool = None
            self.active_slot = None
            self._install_engine_metadata()
            sd.do_resume()
        except Exception as exc:
            self._reset_mapping()
            sd._reset_file()
            if isinstance(exc, self.gcode.error):
                raise
            raise gcmd.error(
                "[BOX]: Unable to start %s: %s" % (info["filename"], exc))


def load_config(config):
    return BoxPrintMapping(config)
