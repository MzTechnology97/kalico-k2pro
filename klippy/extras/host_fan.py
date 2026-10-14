# Host fan: the CPU fan of the host (Raspberry Pi 5 / CM5 fan header)
# controlled from Klipper.
#
# The CM5 fan header is a PWM channel of the RP1, driven by the kernel's
# pwm-fan driver (hwmon "pwmfan") from the CPU thermal zone's trip points.
# This module takes it over: it disables the kernel's automatic fan control
# (the "mode" of the CPU thermal zone), writes the hwmon pwm1 and reads
# fan1_input. CPU throttling is done by the firmware and is not affected.
# Klipper hands the fan back to the kernel when it disconnects, and when it
# shuts down unless shutdown_speed is set.
#
# The fan is published as "temperature_fan <name>", so Mainsail, Fluidd and
# Moonraker's history show it like any temperature fan (temperature, target,
# speed, rpm) and SET_TEMPERATURE_FAN_TARGET works on it.
#
# Copyright (C) 2026  K2-OpenHost
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import os

MAX_PWM = 255
PWM_DEADBAND = 3


def _read(path):
    with open(path, "r") as handle:
        return handle.read().strip()


def _write(path, value):
    with open(path, "w") as handle:
        handle.write(str(value))


def find_hwmon(sysfs_root, name):
    base = os.path.join(sysfs_root, "class", "hwmon")
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for entry in entries:
        path = os.path.join(base, entry)
        try:
            if _read(os.path.join(path, "name")) == name:
                return path
        except OSError:
            continue
    return None


def find_thermal_zone(sysfs_root, zone_type):
    base = os.path.join(sysfs_root, "class", "thermal")
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for entry in entries:
        if not entry.startswith("thermal_zone"):
            continue
        path = os.path.join(base, entry)
        try:
            if _read(os.path.join(path, "type")) == zone_type:
                return path
        except OSError:
            continue
    return None


def target_speed(temp, target, ramp, min_speed, max_speed):
    """min_speed up to target, linear to max_speed at target + ramp."""
    if temp <= target:
        return min_speed
    if temp >= target + ramp:
        return max_speed
    return min_speed + (max_speed - min_speed) * (temp - target) / ramp


class HostFan:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.name = config.get_name().split()[-1]
        self.object_name = "temperature_fan " + self.name
        sysfs_root = config.get("sysfs_root", "/sys")
        self.hwmon_name = config.get("hwmon_name", "pwmfan")
        self.zone_type = config.get("thermal_zone", "cpu-thermal")
        self.min_temp = config.getfloat("min_temp", 0.0)
        self.max_temp = config.getfloat("max_temp", 100.0, above=self.min_temp)
        self.target = config.getfloat(
            "target_temp", 55.0, minval=self.min_temp, maxval=self.max_temp
        )
        self.ramp = config.getfloat("ramp_delta", 15.0, above=0.0)
        self.min_speed = config.getfloat(
            "min_speed", 0.3, minval=0.0, maxval=1.0
        )
        self.max_speed = config.getfloat(
            "max_speed", 1.0, above=0.0, maxval=1.0
        )
        if self.min_speed > self.max_speed:
            raise config.error(
                "[%s] min_speed is above max_speed" % (config.get_name(),)
            )
        self.full_speed_temp = config.getfloat(
            "full_speed_temp", 75.0, above=self.min_temp
        )
        self.shutdown_speed = config.getfloat(
            "shutdown_speed", None, minval=0.0, maxval=1.0
        )
        self.kick_start_time = config.getfloat(
            "kick_start_time", 0.5, minval=0.0
        )
        self.update_interval = config.getfloat(
            "update_interval", 2.0, minval=0.5
        )
        self.stall_time = config.getfloat("stall_time", 10.0, minval=0.0)
        self.mode = config.getchoice(
            "control",
            {"target": "target", "manual": "manual", "kernel": "kernel"},
            "target",
        )
        self.manual_speed = config.getfloat(
            "manual_speed", self.max_speed, minval=0.0, maxval=1.0
        )

        self.hwmon = find_hwmon(sysfs_root, self.hwmon_name)
        self.zone = find_thermal_zone(sysfs_root, self.zone_type)
        self.available = self.hwmon is not None and self.zone is not None
        self.writable = self.available and all(
            os.access(path, os.W_OK)
            for path in (
                os.path.join(self.hwmon, "pwm1"),
                os.path.join(self.zone, "mode"),
            )
        )
        self.problem = None
        if not self.available:
            self.problem = "no hwmon '%s' or thermal zone '%s'" % (
                self.hwmon_name,
                self.zone_type,
            )
        elif not self.writable:
            self.problem = (
                "no write access to %s/pwm1 or %s/mode "
                "(installer helper: ./helper.sh install host-fan)"
                % (self.hwmon, self.zone)
            )
        if self.problem:
            logging.warning(
                "host_fan %s: %s; monitoring only", self.name, self.problem
            )
            self.mode = "kernel"

        self.temperature = 0.0
        self.measured_min = None
        self.measured_max = None
        self.pwm = 0
        self.rpm = 0
        self.kernel_control = True
        self.full_speed = False
        self.stalled = False
        self.stall_since = None
        self.kick_until = 0.0
        self.shutdown = False
        self.timer = None

        self.printer.add_object(self.object_name, self)
        pheaters = self.printer.load_object(config, "heaters")
        pheaters.available_sensors.append(self.object_name)
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler(
            "klippy:shutdown", self._handle_shutdown
        )
        self.printer.register_event_handler(
            "klippy:disconnect", self._handle_disconnect
        )
        self.gcode.register_mux_command(
            "SET_TEMPERATURE_FAN_TARGET",
            "TEMPERATURE_FAN",
            self.name,
            self.cmd_SET_TEMPERATURE_FAN_TARGET,
            desc=self.cmd_SET_TEMPERATURE_FAN_TARGET_help,
        )
        # The first host fan also answers without FAN=.
        for value in (self.name, None):
            try:
                self.gcode.register_mux_command(
                    "SET_HOST_FAN",
                    "FAN",
                    value,
                    self.cmd_SET_HOST_FAN,
                    desc=self.cmd_SET_HOST_FAN_help,
                )
                self.gcode.register_mux_command(
                    "HOST_FAN_STATUS",
                    "FAN",
                    value,
                    self.cmd_HOST_FAN_STATUS,
                    desc=self.cmd_HOST_FAN_STATUS_help,
                )
            except self.printer.config_error:
                if value is not None:
                    raise

    # Kernel side
    def _path(self, base, leaf):
        return os.path.join(base, leaf)

    def _read_state(self):
        try:
            self.temperature = (
                int(_read(self._path(self.zone, "temp"))) / 1000.0
            )
            self.pwm = int(_read(self._path(self.hwmon, "pwm1")))
            self.rpm = int(_read(self._path(self.hwmon, "fan1_input")))
            self.kernel_control = (
                _read(self._path(self.zone, "mode")) == "enabled"
            )
        except (OSError, ValueError) as exc:
            logging.warning("host_fan %s: read error: %s", self.name, exc)
            return False
        if self.measured_min is None or self.temperature < self.measured_min:
            self.measured_min = self.temperature
        if self.measured_max is None or self.temperature > self.measured_max:
            self.measured_max = self.temperature
        return True

    def _set_kernel_control(self, enabled):
        if not self.writable:
            return
        value = "enabled" if enabled else "disabled"
        try:
            _write(self._path(self.zone, "mode"), value)
            self.kernel_control = enabled
        except OSError as exc:
            logging.warning(
                "host_fan %s: cannot set zone %s: %s", self.name, value, exc
            )

    def _write_pwm(self, pwm):
        try:
            _write(self._path(self.hwmon, "pwm1"), pwm)
            self.pwm = pwm
        except OSError as exc:
            logging.warning(
                "host_fan %s: cannot set pwm %d: %s", self.name, pwm, exc
            )

    def _apply_speed(self, speed, eventtime):
        pwm = int(round(max(0.0, min(1.0, speed)) * MAX_PWM))
        if self.kick_until > eventtime:
            return
        if self.kernel_control:
            self._set_kernel_control(False)
        if pwm and not self.pwm and self.kick_start_time and pwm < MAX_PWM:
            self._write_pwm(MAX_PWM)
            self.kick_until = eventtime + self.kick_start_time
            return
        if abs(pwm - self.pwm) >= PWM_DEADBAND or (
            pwm in (0, MAX_PWM) and pwm != self.pwm
        ):
            self._write_pwm(pwm)

    def _release(self):
        if self.writable and not self.kernel_control:
            self._set_kernel_control(True)

    # Control loop
    def _wanted_speed(self):
        if self.mode == "manual":
            speed = self.manual_speed
        else:
            speed = target_speed(
                self.temperature,
                self.target,
                self.ramp,
                self.min_speed,
                self.max_speed,
            )
        full = self.temperature >= self.full_speed_temp
        if full and not self.full_speed:
            self.gcode.respond_info(
                "host_fan %s: %.1f C, fan at full speed until it drops below %.1f C"
                % (self.name, self.temperature, self.full_speed_temp)
            )
        self.full_speed = full
        if full:
            return max(speed, self.max_speed)
        return speed

    def _check_stall(self, eventtime):
        if not self.stall_time or self.pwm == 0 or self.rpm > 0:
            self.stall_since = None
            self.stalled = False
            return
        if self.stall_since is None:
            self.stall_since = eventtime
        elif (
            not self.stalled and eventtime - self.stall_since >= self.stall_time
        ):
            self.stalled = True
            self.gcode.respond_raw(
                "!! host_fan %s: no tachometer pulses at %d%% for %.0f s"
                % (
                    self.name,
                    round(100.0 * self.pwm / MAX_PWM),
                    self.stall_time,
                )
            )

    def _update(self, eventtime):
        if not self._read_state():
            return eventtime + self.update_interval
        if self.shutdown:
            return self.reactor.NEVER
        if self.mode != "kernel":
            self._apply_speed(self._wanted_speed(), eventtime)
        elif not self.kernel_control:
            self._release()
        self._check_stall(eventtime)
        if self.kick_until > eventtime:
            return self.kick_until
        return eventtime + self.update_interval

    # Events
    def _handle_connect(self):
        # Mainsail reads min_temp/max_temp of a temperature_fan from the
        # config settings; this object has no section of that name.
        configfile = self.printer.lookup_object("configfile")
        configfile.status_settings.setdefault(
            self.object_name,
            {
                "min_temp": self.min_temp,
                "max_temp": self.max_temp,
                "target_temp": self.target,
                "min_speed": self.min_speed,
                "max_speed": self.max_speed,
                "control": self.mode,
            },
        )
        self._read_state()

    def _handle_ready(self):
        if self.problem:
            self.gcode.respond_info(
                "host_fan %s: %s; monitoring only" % (self.name, self.problem)
            )
        self.timer = self.reactor.register_timer(self._update, self.reactor.NOW)

    def _handle_shutdown(self):
        self.shutdown = True
        if self.shutdown_speed is None or not self.writable:
            self._release()
            return
        self._set_kernel_control(False)
        self._write_pwm(int(round(self.shutdown_speed * MAX_PWM)))

    def _handle_disconnect(self):
        self._release()

    # Status and commands
    def get_temp(self, eventtime):
        target = self.target if self.mode == "target" else 0.0
        return self.temperature, target

    def get_status(self, eventtime):
        return {
            "temperature": round(self.temperature, 2),
            "target": self.target if self.mode == "target" else 0.0,
            "speed": round(float(self.pwm) / MAX_PWM, 3),
            "rpm": self.rpm,
            "pwm": self.pwm,
            "control": self.mode,
            "kernel_control": self.kernel_control,
            "full_speed": self.full_speed,
            "stalled": self.stalled,
            "available": self.available,
            "writable": self.writable,
            "measured_min_temp": self.measured_min,
            "measured_max_temp": self.measured_max,
        }

    def _require_control(self, gcmd):
        if self.problem:
            raise gcmd.error("host_fan %s: %s" % (self.name, self.problem))

    def _kick(self):
        if self.timer is not None and not self.shutdown:
            self.reactor.update_timer(self.timer, self.reactor.NOW)

    cmd_SET_TEMPERATURE_FAN_TARGET_help = "Sets a temperature fan target"

    def cmd_SET_TEMPERATURE_FAN_TARGET(self, gcmd):
        self._require_control(gcmd)
        target = gcmd.get_float(
            "TARGET", self.target, minval=self.min_temp, maxval=self.max_temp
        )
        min_speed = gcmd.get_float(
            "MIN_SPEED", self.min_speed, minval=0.0, maxval=1.0
        )
        max_speed = gcmd.get_float(
            "MAX_SPEED", self.max_speed, above=0.0, maxval=1.0
        )
        if min_speed > max_speed:
            raise gcmd.error("MIN_SPEED is above MAX_SPEED")
        self.target = target
        self.min_speed = min_speed
        self.max_speed = max_speed
        self.mode = "target"
        self._kick()

    cmd_SET_HOST_FAN_help = (
        "Host fan: MODE=target|manual|kernel, SPEED=0..1 (manual), TARGET=<C>"
    )

    def cmd_SET_HOST_FAN(self, gcmd):
        self._require_control(gcmd)
        mode = gcmd.get("MODE", None)
        speed = gcmd.get_float("SPEED", None, minval=0.0, maxval=1.0)
        target = gcmd.get_float(
            "TARGET", None, minval=self.min_temp, maxval=self.max_temp
        )
        if mode is not None:
            mode = mode.lower()
            if mode not in ("target", "manual", "kernel"):
                raise gcmd.error("MODE must be target, manual or kernel")
        elif speed is not None:
            mode = "manual"
        elif target is not None:
            mode = "target"
        if speed is not None:
            self.manual_speed = speed
        if target is not None:
            self.target = target
        if mode is not None:
            self.mode = mode
        if self.mode == "kernel":
            self._release()
        self._kick()
        self.cmd_HOST_FAN_STATUS(gcmd)

    cmd_HOST_FAN_STATUS_help = "Report the host fan state"

    def cmd_HOST_FAN_STATUS(self, gcmd):
        self._read_state()
        if self.mode == "manual":
            how = "manual %d%%" % round(100.0 * self.manual_speed)
        elif self.mode == "target":
            how = "target %.1f C (%d-%d%%, full speed over %.1f C)" % (
                self.target,
                round(100.0 * self.min_speed),
                round(100.0 * self.max_speed),
                min(self.target + self.ramp, self.full_speed_temp),
            )
        else:
            how = "kernel (automatic)"
        msg = (
            "host_fan %s: %s; %.1f C, %d%% (pwm %d), %d RPM, kernel control %s"
            % (
                self.name,
                how,
                self.temperature,
                round(100.0 * self.pwm / MAX_PWM),
                self.pwm,
                self.rpm,
                "on" if self.kernel_control else "off",
            )
        )
        if self.problem:
            msg += "; " + self.problem
        gcmd.respond_info(msg)


def load_config_prefix(config):
    return HostFan(config)
