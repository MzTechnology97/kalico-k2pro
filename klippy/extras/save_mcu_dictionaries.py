# Save the data dictionary of each connected MCU to a file
#
# The K2 MCUs run Creality firmware: their dictionaries (the commands and
# constants each firmware offers) exist only on the printer. Saved copies
# let tests load the real K2 Pro configuration without the hardware, so an
# upstream change that breaks the profile shows up in CI.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import json
import logging
import os

DEFAULT_PATH = "~/printer_data/mcu_dictionaries"


class SaveMcuDictionaries:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.path = config.get("path", DEFAULT_PATH)
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "SAVE_MCU_DICTIONARIES",
            self.cmd_SAVE_MCU_DICTIONARIES,
            desc=self.cmd_SAVE_MCU_DICTIONARIES_help,
        )

    def _dictionaries(self):
        for name, obj in self.printer.lookup_objects("mcu"):
            serial = getattr(obj, "_serial", None)
            if serial is None:
                continue
            raw = serial.get_msgparser().get_raw_data_dictionary()
            if not raw:
                continue
            yield obj.get_name(), raw

    cmd_SAVE_MCU_DICTIONARIES_help = (
        "Save the data dictionary of each connected MCU as <name>.dict"
    )

    def cmd_SAVE_MCU_DICTIONARIES(self, gcmd):
        path = os.path.expanduser(gcmd.get("PATH", self.path))
        os.makedirs(path, exist_ok=True)
        saved = []
        for name, raw in self._dictionaries():
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)
            fname = os.path.join(path, "%s.dict" % (name,))
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
                f.write("\n")
            app = data.get("app", "Klipper")
            saved.append(
                "%s: %s %s, %d commands"
                % (name, app, data.get("version", "?"), len(data["commands"]))
            )
            logging.info("Saved MCU dictionary %s", fname)
        if not saved:
            raise gcmd.error("No MCU data dictionary available")
        gcmd.respond_info(
            "MCU dictionaries saved in %s\n%s" % (path, "\n".join(saved))
        )


def load_config(config):
    return SaveMcuDictionaries(config)
