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
* warnings for the chosen map: a spool that may run out (RFID estimate below
  the slicer length) or a base material on a filled variant (PETG on
  PETG-CF). They are printed in the console and published, never block;
* a check every minute during the print: the filament each tool still
  needs against what its spool (and the identical spools runout swap would
  continue on) has left. A newly short tool is reported once in the console
  and added to ``mapping_warnings`` as ``low_filament_live``; it never
  pauses the print;
* four extra ``box.get_status()`` fields:

    print_mapping_enabled
    auto_mapping
    mapping_warnings
    filament_check
"""

import logging
import os

from extras.box_auto_mapping import (
    evaluate_mapping, live_filament_check, suggest_mapping_report)
from extras.box_gcode import read_metadata

LIVE_CHECK_INTERVAL = 60.0


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
        self.auto_mapping = {"state": "idle", "map": {}, "unresolved": [], "warnings": []}
        # Warnings of the map used by the current print (automatic or chosen).
        self.mapping_warnings = []
        self._explicit_start_in_progress = False
        # Live filament check during the print.
        self.filament_check = {"active": False, "tools": []}
        self._live_warned = set()
        self._live_tools_cache = (None, [])
        self.reactor = self.printer.get_reactor()
        self._live_timer = self.reactor.register_timer(self._live_check)
        self.printer.register_event_handler("klippy:ready", self._start_live_check)

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
        status["mapping_warnings"] = list(self.mapping_warnings)
        status["filament_check"] = {
            "active": self.filament_check["active"],
            "tools": [dict(item) for item in self.filament_check["tools"]],
        }
        return status

    # ------------------------------------------------------------------
    # Automatic mapping
    # ------------------------------------------------------------------

    def _reset_auto_mapping(self, *args):
        self.auto_mapping = {"state": "idle", "map": {}, "unresolved": [], "warnings": []}
        self.mapping_warnings = []
        self.filament_check = {"active": False, "tools": []}
        self._live_warned = set()

    # ------------------------------------------------------------------
    # Live filament check
    # ------------------------------------------------------------------

    def _start_live_check(self):
        self.reactor.update_timer(
            self._live_timer, self.reactor.monotonic() + LIVE_CHECK_INTERVAL)

    def _current_tools(self, sd):
        """Tools of the file being printed (the dialog's print_info only
        when it describes that file; otherwise read the file once)."""
        current = getattr(sd, "current_file", None) if sd else None
        path = getattr(current, "name", None)
        if not path:
            return []
        filename = os.path.basename(path)
        info = self.box.print_info or {}
        if os.path.basename(str(info.get("filename", ""))) == filename:
            return info.get("tools", []) or []
        cached_path, tools = self._live_tools_cache
        if cached_path != path:
            try:
                tools = read_metadata(path).get("tools", [])
            except (OSError, ValueError):
                tools = []
            self._live_tools_cache = (path, tools)
        return tools

    def _live_check(self, eventtime):
        try:
            self._run_live_check(eventtime)
        except Exception:
            logging.exception("box_print_mapping: live filament check failed")
        return eventtime + LIVE_CHECK_INTERVAL

    def _run_live_check(self, eventtime):
        stats = self.printer.lookup_object("print_stats", None)
        state = getattr(stats, "state", None)
        if state != "printing":
            if state != "paused":
                self.filament_check = {"active": False, "tools": []}
            return
        sd = self.printer.lookup_object("virtual_sdcard", None)
        tools = self._current_tools(sd)
        if not tools:
            self.filament_check = {"active": False, "tools": []}
            return
        mapping = dict(getattr(self.change_engine, "tool_map", {}) or {})
        if not mapping and len(tools) == 1:
            loaded = getattr(getattr(self.box, "snapshot", None), "loaded_slot", None)
            if isinstance(loaded, int) and loaded >= 0:
                mapping = {int(tools[0]["tool"]): loaded}
        stats_status = stats.get_status(eventtime)
        used_m = float(stats_status.get("filament_used", 0.0) or 0.0) / 1000.0
        progress = sd.get_status(eventtime).get("progress", 0.0) if sd else 0.0
        results = live_filament_check(
            tools, self._slots(), mapping, self._swap_enabled(), used_m, progress)
        self.filament_check = {"active": True, "tools": results}
        live = []
        for item in results:
            if not item["short"]:
                continue
            warning = {
                "kind": "low_filament_live", "tool": item["tool"],
                "slot": item["slot"], "needed_m": item["needed_m"],
                "remaining_m": item["available_m"],
                "includes_swap": item["includes_swap"],
                "estimated": item["estimated"],
            }
            live.append(warning)
            key = (item["tool"], item["slot"])
            if key not in self._live_warned:
                self._live_warned.add(key)
                self.gcode.respond_info("[BOX]: Warning: " + self._warning_text(warning))
        self.mapping_warnings = [
            w for w in self.mapping_warnings if w.get("kind") != "low_filament_live"
        ] + live

    def _slots(self):
        status = self._base_get_status(
            self.printer.get_reactor().monotonic())
        return [
            slot for slot in status.get("slots", [])
            if slot.get("external") or slot.get("present")
        ]

    def _swap_enabled(self):
        return bool(getattr(self.box, "runout_swap_enabled", False))

    def _suggest_mapping(self, tools):
        if not tools:
            self.auto_mapping = {
                "state": "no_tools", "map": {}, "unresolved": [], "warnings": []}
            return {}, []
        report = suggest_mapping_report(tools, self._slots(), self._swap_enabled())
        mapping, unresolved = report["map"], report["unresolved"]
        self.auto_mapping = {
            "state": "unresolved" if unresolved else "ready",
            "map": {str(tool): slot for tool, slot in mapping.items()},
            "unresolved": unresolved,
            "warnings": report["warnings"],
        }
        return mapping, unresolved

    def _warning_text(self, warning):
        tool = "T%d" % warning["tool"]
        where = self.box.slot_label(warning["slot"])
        if warning["kind"] == "low_filament_live":
            return ("%s still needs about %.1f m%s, %s has about %.1f m left%s. "
                    "Load more filament or the print pauses at runout." % (
                        tool, warning["needed_m"],
                        " (estimated)" if warning.get("estimated") else "",
                        where, warning["remaining_m"],
                        " including identical spools" if warning.get("includes_swap") else ""))
        if warning["kind"] == "low_filament":
            return ("%s needs about %.1f m of filament, %s has about %.1f m left%s. "
                    "The print continues and pauses at runout unless more filament is loaded." % (
                        tool, warning["needed_m"], where, warning["remaining_m"],
                        " including identical spools" if warning.get("includes_swap") else ""))
        if warning["kind"] == "material_variant":
            return ("%s is %s but %s holds %s (different variant). Check that the "
                    "nozzle and temperatures suit it." % (
                        tool, warning["tool_material"] or "?", where,
                        warning["slot_material"] or "?"))
        return "%s (%s) uses %s (%s)." % (
            tool, warning["tool_material"] or "?", where, warning["slot_material"] or "not set")

    def _notify(self, warnings):
        self.mapping_warnings = list(warnings)
        for warning in warnings:
            self.gcode.respond_info("[BOX]: Warning: " + self._warning_text(warning))

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
        self._notify(self.auto_mapping.get("warnings", []))

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
        # The user chose the map; still report spools that may run out.
        tools = (self.box.print_info or {}).get("tools", [])
        mapping = dict(getattr(self.change_engine, "tool_map", {}) or {})
        self._notify(evaluate_mapping(tools, self._slots(), mapping, self._swap_enabled()))


def load_config(config):
    return BoxPrintMapping(config)
