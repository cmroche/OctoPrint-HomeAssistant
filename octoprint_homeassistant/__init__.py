# coding=utf-8
from __future__ import absolute_import

import datetime
import json
import logging
import re

import psutil
import octoprint.plugin
from octoprint.events import Events
from octoprint.settings import settings
from octoprint.util import RepeatedTimer

SETTINGS_DEFAULTS = dict(
    unique_id=None,
    node_id=None,
    discovery_topic="homeassistant",
    node_name="OctoPrint",
    device_manufacturer="Clifford Roche",
    device_model="HomeAssistant Discovery for OctoPrint",
)

MQTT_DEFAULTS = dict(
    publish=dict(
        baseTopic="octoPrint/",
        eventTopic="event/{event}",
        progressTopic="progress/{progress}",
        temperatureTopic="temperature/{temp}",
        lwTopic="mqtt",
        hassTopic="hass/{hass}",
        controlTopic="hassControl/{control}",
    )
)


class HomeassistantPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.ProgressPlugin,
    octoprint.plugin.WizardPlugin,
):
    def __init__(self):
        self._logger = logging.getLogger(__name__)
        self.mqtt_publish = None
        self.mqtt_publish_with_timestamp = None
        self.mqtt_subcribe = None
        self.update_timer = None
        self.constant_timer = None
        self.psucontrol_enabled = False

    def handle_timer(self):
        self._generate_printer_status()

    def handle_constant_timer(self):
        self._generate_status()

    ##~~ SettingsPlugin

    def get_settings_defaults(self):
        return SETTINGS_DEFAULTS

    def get_settings_version(self):
        return 2

    def on_settings_migrate(self, target, current):
        if target >= 1:  # This is the first version
            _node_uuid = self._settings.get(["unique_id"])
            if _node_uuid:
                _node_id = (_node_uuid[:6]).upper()
                self._settings.set(["node_id"], _node_id)

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        self._generate_device_registration()
        self._generate_device_controls(subscribe=True)
        self._generate_connection_status()

    ##~~ TemplatePlugin mixin

    def get_template_configs(self):
        return [dict(type="settings", custom_bindings=False)]

    ##~~ StartupPlugin mixin

    def on_after_startup(self):
        if self._settings.get(["unique_id"]) is None:
            import uuid

            _uuid = uuid.uuid4()
            _uid = str(_uuid)
            self._settings.set(["unique_id"], _uid)
            self._settings.set(["node_id"], _uuid.hex)
            settings().save()

        helpers = self._plugin_manager.get_helpers(
            "mqtt", "mqtt_publish", "mqtt_publish_with_timestamp", "mqtt_subscribe"
        )
        if helpers:
            if "mqtt_publish_with_timestamp" in helpers:
                self._logger.debug("Setup publish with timestamp helper")
                self.mqtt_publish_with_timestamp = helpers[
                    "mqtt_publish_with_timestamp"
                ]

            if "mqtt_publish" in helpers:
                self._logger.debug("Setup publish helper")
                self.mqtt_publish = helpers["mqtt_publish"]

            if "mqtt_subscribe" in helpers:
                self._logger.debug("Setup subscribe helper")
                self.mqtt_subscribe = helpers["mqtt_subscribe"]
                self.mqtt_subscribe(
                    self._generate_topic("lwTopic", "", full=True),
                    self._on_mqtt_message,
                )

        # PSUControl helpers
        psu_helpers = self._plugin_manager.get_helpers(
            "psucontrol", "turn_psu_on", "turn_psu_off", "get_psu_state"
        )

        self.psucontrol_enabled = True

        if psu_helpers:
            self._logger.info("PSUControl helpers found")

            if "get_psu_state" in psu_helpers:
                self.get_psu_state = psu_helpers["get_psu_state"]
                self._logger.debug("Setup get_psu_state helper")
            else:
                self._logger.error(
                    "Helper get_psu_state not found, disabling PSUControl integration"
                )
                self.psucontrol_enabled = False

            if "turn_psu_on" in psu_helpers:
                self.turn_psu_on = psu_helpers["turn_psu_on"]
                self._logger.debug("Setup turn_psu_on helper")
            else:
                self._logger.error(
                    "Helper turn_psu_on not found, disabling PSUControl integration"
                )
                self.psucontrol_enabled = False

            if "turn_psu_off" in psu_helpers:
                self.turn_psu_off = psu_helpers["turn_psu_off"]
                self._logger.debug("Setup turn_psu_off helper")
            else:
                self._logger.error(
                    "Helper turn_psu_on not found, disabling PSUControl integration"
                )
                self.psucontrol_enabled = False
        else:
            self._logger.info("PSUControl helpers not found")
            self.psucontrol_enabled = False

        self.snapshot_enabled = self._settings.global_get(
            ["webcam", "timelapseEnabled"]
        )
        if self.snapshot_enabled:
            self.snapshot_path = self._settings.global_get(["webcam", "snapshot"])
            if not self.snapshot_path:
                self.snapshot_enabled = False

        if not self.update_timer:
            self.update_timer = RepeatedTimer(60, self.handle_timer, None, None, False)

        if not self.constant_timer:
            self.constant_timer = RepeatedTimer(
                30, self.handle_constant_timer, None, None, False
            )
            self.constant_timer.start()

        # Since retain may not be used it's not always possible to simply tie this to the connected state
        self._generate_device_registration()
        self._generate_device_controls(subscribe=True)

        # For people who do not have retain setup, need to do this again to make sensors available
        _connected_topic = self._generate_topic("lwTopic", "", full=True)
        self.mqtt_publish(_connected_topic, "connected", allow_queueing=True)

        # Setup the default printer states
        self.mqtt_publish(
            self._generate_topic("hassTopic", "is_printing", full=True),
            "False",
            allow_queueing=True,
        )
        self.mqtt_publish(
            self._generate_topic("hassTopic", "is_paused", full=True),
            "False",
            allow_queueing=True,
        )
        self.on_print_progress("", "", 0)
        self._generate_connection_status()

        if self.psucontrol_enabled:
            self._generate_psu_state()

    def _get_mac_address(self):
        import uuid

        return ":".join(re.findall("..", "%012x" % uuid.getnode()))

    def _on_mqtt_message(
        self, topic, message, retained=None, qos=None, *args, **kwargs
    ):
        try:
            message = message.decode()
        except (UnicodeDecodeError, AttributeError):
            pass

        self._logger.info("Received MQTT message from " + topic)
        self._logger.info(message)

        # Don't rely on this, the message may be disabled.
        if message == "connected":
            self._generate_device_registration()
            self._generate_device_controls(subscribe=False)

    def _generate_topic(self, topic_type, topic, full=False):
        self._logger.debug("Generating topic for " + topic_type + ", " + topic)
        mqtt_defaults = dict(plugins=dict(mqtt=MQTT_DEFAULTS))
        _topic = ""

        if topic_type != "baseTopic":
            _topic = settings().get(
                ["plugins", "mqtt", "publish", topic_type], defaults=mqtt_defaults
            )
            _topic = re.sub(r"{.+}", "", _topic)

        if full or topic_type == "baseTopic":
            _topic = (
                settings().get(
                    ["plugins", "mqtt", "publish", "baseTopic"], defaults=mqtt_defaults
                )
                + _topic
            )

        _topic += topic
        self._logger.debug("Generated topic: " + _topic)
        return _topic

    def _generate_device_registration(self):

        _discovery_topic = self._settings.get(["discovery_topic"])

        _node_name = self._settings.get(["node_name"])
        _node_id = self._settings.get(["node_id"])

        _device_manufacturer = self._settings.get(["device_manufacturer"])
        _device_model = self._settings.get(["device_model"])

        _config_device = self._generate_device_config(
            _node_id, _node_name, _device_manufacturer, _device_model
        )

        ##~~ Configure Connected Sensor
        self._generate_sensor(
            topic=_discovery_topic + "/binary_sensor/" + _node_id + "_CONNECTED/config",
            values={
                "name": _node_name + " Connected",
                "uniq_id": _node_id + "_CONNECTED",
                "stat_t": "~" + self._generate_topic("hassTopic", "Connected"),
                "pl_on": "Connected",
                "pl_off": "Disconnected",
                "dev_cla": "connectivity",
                "device": _config_device,
            },
        )

        ##~~ Configure Printing Sensor
        self._generate_sensor(
            topic=_discovery_topic + "/binary_sensor/" + _node_id + "_PRINTING/config",
            values={
                "name": _node_name + " Printing",
                "uniq_id": _node_id + "_PRINTING",
                "stat_t": "~" + self._generate_topic("hassTopic", "printing"),
                "pl_on": "True",
                "pl_off": "False",
                "val_tpl": "{{value_json.state.flags.printing}}",
                "device": _config_device,
            },
        )

        ##~~ Configure Last Event Sensor
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_EVENT/config",
            values={
                "name": _node_name + " Last Event",
                "uniq_id": _node_id + "_EVENT",
                "stat_t": "~" + self._generate_topic("eventTopic", "+"),
                "val_tpl": "{{value_json._event}}",
                "device": _config_device,
            },
        )

        ##~~ Configure Print Status
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_PRINTING_S/config",
            values={
                "name": _node_name + " Print Status",
                "uniq_id": _node_id + "_PRINTING_S",
                "stat_t": "~" + self._generate_topic("hassTopic", "printing"),
                "json_attr_t": "~" + self._generate_topic("hassTopic", "printing"),
                "json_attr_tpl": "{{value_json.state|tojson}}",
                "val_tpl": "{{value_json.state.text}}",
                "device": _config_device,
            },
        )

        ##~~ Configure Print Progress
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_PRINTING_P/config",
            values={
                "name": _node_name + " Print Progress",
                "uniq_id": _node_id + "_PRINTING_P",
                "json_attr_t": "~" + self._generate_topic("hassTopic", "printing"),
                "json_attr_tpl": "{{value_json.progress|tojson}}",
                "stat_t": "~" + self._generate_topic("progressTopic", "printing"),
                "unit_of_meas": "%",
                "val_tpl": "{{value_json.progress|float(0)}}",
                "device": _config_device,
            },
        )

        ##~~ Configure Print File
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_PRINTING_F/config",
            values={
                "name": _node_name + " Print File",
                "uniq_id": _node_id + "_PRINTING_F",
                "stat_t": "~" + self._generate_topic("progressTopic", "printing"),
                "val_tpl": "{{value_json.path}}",
                "device": _config_device,
                "ic": "mdi:file",
            },
        )

        ##~~ Configure Print Time
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_PRINTING_T/config",
            values={
                "name": _node_name + " Print Time",
                "uniq_id": _node_id + "_PRINTING_T",
                "stat_t": "~" + self._generate_topic("hassTopic", "printing"),
                "avty": [
                    {
                        "t": "~" + self._generate_topic("hassTopic", "printing"),
                        "val_tpl": "{{'False' if not value_json.progress.printTime else 'True'}}",
                        "pl_avail": "True",
                        "pl_not_avail": "False",
                    }
                ],
                "val_tpl": "{{value_json.progress.printTime}}",
                "dev_cla": "duration",
                "unit_of_meas": "s",
                "device": _config_device,
                "ic": "mdi:clock-start",
            },
        )

        ##~~ Configure Print Time Left
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_PRINTING_E/config",
            values={
                "name": _node_name + " Print Time Left",
                "uniq_id": _node_id + "_PRINTING_E",
                "stat_t": "~" + self._generate_topic("hassTopic", "printing"),
                "avty": [
                    {
                        "t": "~" + self._generate_topic("hassTopic", "printing"),
                        "val_tpl": "{{'False' if not value_json.progress.printTimeLeft else 'True'}}",
                        "pl_avail": "True",
                        "pl_not_avail": "False",
                    }
                ],
                "val_tpl": "{{value_json.progress.printTimeLeft}}",
                "dev_cla": "duration",
                "unit_of_meas": "s",
                "device": _config_device,
                "ic": "mdi:clock-end",
            },
        )

        ##~~ Configure Print ETA
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_PRINTING_ETA/config",
            values={
                "name": _node_name + " Approximate Total Print Time",
                "uniq_id": _node_id + "_PRINTING_ETA",
                "stat_t": "~" + self._generate_topic("hassTopic", "printing"),
                "json_attr_t": "~" + self._generate_topic("hassTopic", "printing"),
                "json_attr_tpl": "{{value_json.job|tojson}}",
                "avty": [
                    {
                        "t": "~" + self._generate_topic("hassTopic", "printing"),
                        "val_tpl": "{{'False' if not value_json.job.estimatedPrintTime else 'True'}}",
                        "pl_avail": "True",
                        "pl_not_avail": "False",
                    },
                ],
                "val_tpl": "{{value_json.job.estimatedPrintTime}}",
                "dev_cla": "duration",
                "unit_of_meas": "s",
                "device": _config_device,
            },
        )

        ##~~ Configure Print Remaining Time
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_PRINTING_C/config",
            values={
                "name": _node_name + " Approximate Completion Time",
                "uniq_id": _node_id + "_PRINTING_C",
                "stat_t": "~" + self._generate_topic("hassTopic", "printing"),
                "avty": [
                    {
                        "t": "~" + self._generate_topic("hassTopic", "printing"),
                        "val_tpl": "{{'False' if not value_json.progress.printTimeLeft else 'True'}}",
                        "pl_avail": "True",
                        "pl_not_avail": "False",
                    },
                ],
                "val_tpl": "{{now() + timedelta(seconds=value_json.progress.printTimeLeft|int(default=0))}}",
                "dev_cla": "timestamp",
                "device": _config_device,
            },
        )

        ##~~ Configure Print Current Z
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_PRINTING_Z/config",
            values={
                "name": _node_name + " Current Z",
                "uniq_id": _node_id + "_PRINTING_Z",
                "stat_t": "~" + self._generate_topic("hassTopic", "printing"),
                "unit_of_meas": "mm",
                "val_tpl": "{{value_json.currentZ|float(0)}}",
                "device": _config_device,
                "ic": "mdi:axis-z-arrow",
            },
        )

        ##~~ Configure Slicing Status
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_SLICING_P/config",
            values={
                "name": _node_name + " Slicing Progress",
                "uniq_id": _node_id + "_SLICING_P",
                "stat_t": "~" + self._generate_topic("progressTopic", "slicing"),
                "unit_of_meas": "%",
                "val_tpl": "{{value_json.progress|float(0)}}",
                "device": _config_device,
            },
        )

        ##~~ Configure Slicing File
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_SLICING_F/config",
            values={
                "name": _node_name + " Slicing File",
                "uniq_id": _node_id + "_SLICING_F",
                "stat_t": "~" + self._generate_topic("progressTopic", "slicing"),
                "val_tpl": "{{value_json.source_path}}",
                "device": _config_device,
                "ic": "mdi:file",
            },
        )

        ##~~ Tool Temperature
        _e = self._printer_profile_manager.get_current_or_default()["extruder"]["count"]
        for x in range(_e):
            self._generate_sensor(
                topic=_discovery_topic
                + "/sensor/"
                + _node_id
                + "_TOOL"
                + str(x)
                + "/config",
                values={
                    "name": _node_name + " Tool " + str(x) + " Temperature",
                    "uniq_id": _node_id + "_TOOL" + str(x),
                    "stat_t": "~"
                    + self._generate_topic("temperatureTopic", "tool" + str(x)),
                    "unit_of_meas": "°C",
                    "val_tpl": "{{value_json.actual|float(0)}}",
                    "device": _config_device,
                    "dev_cla": "temperature",
                    "ic": "mdi:printer-3d-nozzle",
                },
            )
            self._generate_sensor(
                topic=_discovery_topic
                + "/sensor/"
                + _node_id
                + "_TOOL"
                + str(x)
                + "_TARGET"
                + "/config",
                values={
                    "name": _node_name + " Tool " + str(x) + " Target",
                    "uniq_id": _node_id + "_TOOL" + str(x) + "_TARGET",
                    "stat_t": "~"
                    + self._generate_topic("temperatureTopic", "tool" + str(x)),
                    "unit_of_meas": "°C",
                    "val_tpl": "{{value_json.target|float(0)}}",
                    "device": _config_device,
                    "dev_cla": "temperature",
                    "ic": "mdi:printer-3d-nozzle",
                },
            )

        ##~~ Bed Temperature
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_BED/config",
            values={
                "name": _node_name + " Bed Temperature",
                "uniq_id": _node_id + "_BED",
                "stat_t": "~" + self._generate_topic("temperatureTopic", "bed"),
                "unit_of_meas": "°C",
                "val_tpl": "{{value_json.actual|float(0)}}",
                "device": _config_device,
                "dev_cla": "temperature",
                "ic": "mdi:radiator",
            },
        )
        self._generate_sensor(
            topic=_discovery_topic + "/sensor/" + _node_id + "_BED_TARGET/config",
            values={
                "name": _node_name + " Bed Target",
                "uniq_id": _node_id + "_BED_TARGET",
                "stat_t": "~" + self._generate_topic("temperatureTopic", "bed"),
                "unit_of_meas": "°C",
                "val_tpl": "{{value_json.target|float(0)}}",
                "device": _config_device,
                "dev_cla": "temperature",
                "ic": "mdi:radiator",
            },
        )

        ##~~ Chamber Temperature
        _h = self._printer_profile_manager.get_current_or_default()["heatedChamber"]
        if _h:
            self._generate_sensor(
                topic=_discovery_topic + "/sensor/" + _node_id + "_CHAMBER/config",
                values={
                    "name": _node_name + " Chamber Temperature",
                    "uniq_id": _node_id + "_CHAMBER",
                    "stat_t": "~" + self._generate_topic("temperatureTopic", "chamber"),
                    "unit_of_meas": "°C",
                    "val_tpl": "{{value_json.actual|float(0)}}",
                    "device": _config_device,
                    "dev_cla": "temperature",
                    "ic": "mdi:radiator",
                },
            )
            self._generate_sensor(
                topic=_discovery_topic
                + "/sensor/"
                + _node_id
                + "_CHAMBER_TARGET/config",
                values={
                    "name": _node_name + " Chamber Target",
                    "uniq_id": _node_id + "_CHAMBER_TARGET",
                    "stat_t": "~" + self._generate_topic("temperatureTopic", "chamber"),
                    "unit_of_meas": "°C",
                    "val_tpl": "{{value_json.target|float(0)}}",
                    "device": _config_device,
                    "dev_cla": "temperature",
                    "ic": "mdi:radiator",
                },
            )

        ##~~ SoC Temperature (if supported)
        self._generate_sensor(
            topic="homeassistant/sensor/" + _node_id + "_SOC/config",
            values={
                "name": _node_name + " SoC Temperature",
                "uniq_id": _node_id + "_SOC",
                "stat_t": "~" + self._generate_topic("temperatureTopic", "soc"),
                "unit_of_meas": "°C",
                "val_tpl": "{{value_json.temperature|float(0)|round(1)}}",
                "device": _config_device,
                "dev_cla": "temperature",
                "ic": "mdi:radiator",
            },
        )

    def _generate_sensor(self, topic, values):
        payload={}
        payload.update({
            "avty": [],
            "~": self._generate_topic("baseTopic", "", full=True),
        })

        # Add in set values
        payload.update(values)

        # Append default availability topic
        payload["avty"].append({
            "t": "~" + self._generate_topic("lwTopic", ""),
            "pl_avail": "connected",
            "pl_not_avail": "disconnected",
        })

        self.mqtt_publish(topic, payload, allow_queueing=True)

    def _generate_device_config(
        self, _node_id, _node_name, _device_manufacturer, _device_model
    ):
        _config_device = {
            "ids": _node_id,
            "name": _node_name,
            "mf": _device_manufacturer,
            "mdl": _device_model,
            "sw": "HomeAssistant Discovery for OctoPrint " + self._plugin_version,
        }
        return _config_device

    def _get_cpu_temp(self):
        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                if "coretemp" in temps:
                    return temps["coretemp"][0].current
                if "cpu-thermal" in temps:
                    return temps["cpu-thermal"][0].current
                if "cpu_thermal" in temps:
                    return temps["cpu_thermal"][0].current
        return None

    def _generate_status(self):

        data = {"temperature": self._get_cpu_temp()}

        if self.mqtt_publish_with_timestamp:
            self.mqtt_publish_with_timestamp(
                self._generate_topic("temperatureTopic", "soc", full=True),
                data,
                allow_queueing=True,
            )

    def _generate_printer_status(self):

        data = self._printer.get_current_data()
        try:
            data["progress"]["printTimeLeftFormatted"] = str(
                datetime.timedelta(seconds=int(data["progress"]["printTimeLeft"]))
            ).split(".")[0]
        except:
            data["progress"]["printTimeLeftFormatted"] = None
        try:
            data["progress"]["printTimeFormatted"] = str(
                datetime.timedelta(seconds=data["progress"]["printTime"])
            ).split(".")[0]
        except:
            data["progress"]["printTimeFormatted"] = None
        try:
            data["job"]["estimatedPrintTimeFormatted"] = str(
                datetime.timedelta(seconds=data["job"]["estimatedPrintTime"])
            ).split(".")[0]
        except:
            data["job"]["estimatedPrintTimeFormatted"] = None

        if self.mqtt_publish_with_timestamp:
            self.mqtt_publish_with_timestamp(
                self._generate_topic("hassTopic", "printing", full=True),
                data,
                allow_queueing=True,
            )

    def _generate_connection_status(self):

        state, _, _, _ = self._printer.get_current_connection()
        state_connected = "Disconnected" if state == "Closed" else "Connected"
        # Function can be called by on_event before on_after_startup has run.
        # This will throw a TypeError since self.mqtt_publish is still null.
        if self.mqtt_publish:
            self.mqtt_publish(
                self._generate_topic("hassTopic", "Connected", full=True),
                state_connected,
                allow_queueing=True,
            )

    def _generate_psu_state(self, psu_state=None):
        if self.psucontrol_enabled:
            if psu_state is None:
                psu_state = self.get_psu_state()
                self._logger.debug(
                    "No psu_state specified, state retrieved from helper: "
                    + str(psu_state)
                )
            self.mqtt_publish(
                self._generate_topic("hassTopic", "psu_on", full=True),
                str(psu_state),
                allow_queueing=True,
            )

    def _on_emergency_stop(
        self, topic, message, retained=None, qos=None, *args, **kwargs
    ):
        # In Home Assistant, MQTT buttons send the message 'PRESS' when pressed.
        self._logger.debug("Emergency stop message received: " + str(message))
        if message == b"PRESS":
            self._printer.commands("M112")
        else:
            self._logger.error("Unknown message received: " + str(message))

    def _on_cancel_print(
        self, topic, message, retained=None, qos=None, *args, **kwargs
    ):
        self._logger.debug("Cancel print message received: " + str(message))
        if message == b"PRESS":
            self._printer.cancel_print()
        else:
            self._logger.error("Unknown message received: " + str(message))

    def _on_pause_print(self, topic, message, retained=None, qos=None, *args, **kwargs):
        # In Home Assistant, MQTT switches send the message 'True' when turned on and 'False' when turned off.
        self._logger.debug("Pause print message received: " + str(message))
        if message == b"True":
            self._printer.pause_print()
        elif message == b"False":
            self._printer.resume_print()
        else:
            self._logger.error("Unknown message received: " + str(message))

    def _on_shutdown_system(self, topic, message, retained=None, qos=None, *args, **kwargs):
        self._logger.debug("Shutdown print message received: " + str(message))
        if message == b"PRESS":
            shutdown_command = self._settings.global_get(
                ["server", "commands", "systemShutdownCommand"]
            )
            try:
                import sarge

                sarge.run(shutdown_command, async_=True)
            except Exception as e:
                self._logger.info("Unable to run shutdown command: " + str(e))
        else:
            self._logger.error("Unknown message received: " + str(message))

    def _on_restart_system(self, topic, message, retained=None, qos=None, *args, **kwargs):
        self._logger.debug("Reboot print message received: " + str(message))
        if message == b"PRESS":
            _command = self._settings.global_get(
                ["server", "commands", "systemRestartCommand"]
            )
            try:
                import sarge

                sarge.run(_command, async_=True)
            except Exception as e:
                self._logger.info("Unable to run system reboot command: " + str(e))
        else:
            self._logger.error("Unknown message received: " + str(message))

    def _on_restart_server(self, topic, message, retained=None, qos=None, *args, **kwargs):
        self._logger.debug("Restart print message received: " + str(message))
        if message == b"PRESS":
            _command = self._settings.global_get(
                ["server", "commands", "serverRestartCommand"]
            )
            try:
                import sarge

                sarge.run(_command, async_=True)
            except Exception as e:
                self._logger.info("Unable to run system reboot command: " + str(e))
        else:
            self._logger.error("Unknown message received: " + str(message))

    def _on_psu(self, topic, message, retained=None, qos=None, *args, **kwargs):
        message = message.decode()
        self._logger.debug("PSUControl message received: " + message)
        if message == "True":
            self._logger.info("Turning on PSU")
            self.turn_psu_on()
        elif message == "False":
            self._logger.info("Turning off PSU")
            self.turn_psu_off()
        else:
            self._logger.error("Unknown message received: " + str(message))

    def _on_camera(self, topic, message, retained=None, qos=None, *args, **kwargs):
        self._logger.debug("Camera snapshot message received: " + str(message))
        if self.snapshot_enabled and message == b"PRESS":
            import urllib.request as urlreq

            url_handle = urlreq.urlopen(self.snapshot_path)
            file_content = url_handle.read()
            url_handle.close()
            self.mqtt_publish(
                self._generate_topic("baseTopic", "camera", full=True),
                file_content,
                allow_queueing=False,
                raw_data=True,
            )
        elif self.snapshot_enabled and not message == "PRESS":
            self._logger.error("Unknown message received: " + str(message))

    def _on_connect_printer(self, topic, message, retained=None, qos=None, *args, **kwargs):
        self._logger.debug("(Dis)Connecting to printer" + str(message))
        try:
          if message == b'True':
            self._printer.connect()
          elif message == b'False':
            self._printer.disconnect()
          else:
            self._logger.error("Unknown message received: " + str(message))
        except Exception as e:
          self._logger.error("Unable to run connect command: " + str(e))

    def _on_home(self, topic, message, retained=None, qos=None, *args, **kwargs):
        self._logger.debug("Homing printer: " + str(message))
        if message:
            try:
                home_payload = json.loads(message)
                axes = set(home_payload) & set(["x", "y", "z", "e"])
                self._printer.home(list(axes))
            except Exception as e:
                self._logger.error("Unable to run home command: " + str(e))

    def _on_jog(self, topic, message, retained=None, qos=None, *args, **kwargs):
        self._logger.debug("Jogging printer: " + str(message))
        if message:
            try:
                jog_payload = json.loads(message)
                axes_keys = set(jog_payload.keys()) & set(["x", "y", "z"])
                axes = {k: v for (k, v) in jog_payload.items() if k in axes_keys}
                self._printer.jog(axes, jog_payload.get("speed"))
            except Exception as e:
                self._logger.error("Unable to run jog command: " + str(e))

    def _on_command(self, topic, message, retained=None, qos=None, *args, **kwargs):
        self._logger.debug("Received gcode commands %s", message)
        try:
            try:
                self._printer.commands(json.loads(message))
            except ValueError:
                self._printer.commands(message)
        except Exception as e:
            self._logger.error("Unable to run printer commands: " + str(e))

    def _generate_device_controls(self, subscribe=False):

        _discovery_topic = self._settings.get(["discovery_topic"])

        _node_name = self._settings.get(["node_name"])
        _node_id = self._settings.get(["node_id"])

        _device_manufacturer = self._settings.get(["device_manufacturer"])
        _device_model = self._settings.get(["device_model"])

        _config_device = self._generate_device_config(
            _node_id, _node_name, _device_manufacturer, _device_model
        )

        # Connect printer
        if subscribe:
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "connect", full=True),
                self._on_connect_printer,
            )

        self._generate_sensor(
            topic=_discovery_topic + "/switch/" + _node_id + "_CONNECT/config",
            values={
                "name": _node_name + " Connect to printer",
                "uniq_id": _node_id + "_CONNECT",
                "cmd_t": "~" + self._generate_topic("controlTopic", "connect"),
                "stat_t": self._generate_topic("hassTopic", "Connected", full=True),
                "pl_off": "False",
                "pl_on": "True",
                "stat_on": "Connected",
                "stat_off": "Disconnected",
                "device": _config_device,
                "ic": "mdi:lan-connect",
            },
        )

        # Emergency stop
        if subscribe:
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "stop", full=True),
                self._on_emergency_stop,
            )

        self._generate_sensor(
            topic=_discovery_topic + "/button/" + _node_id + "_STOP/config",
            values={
                "name": _node_name + " Emergency Stop",
                "uniq_id": _node_id + "_STOP",
                "cmd_t": "~" + self._generate_topic("controlTopic", "stop"),
                "device": _config_device,
                "ic": "mdi:alert-octagon",
            },
        )

        # Cancel print
        if subscribe:
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "cancel", full=True),
                self._on_cancel_print,
            )

        self._generate_sensor(
            topic=_discovery_topic + "/button/" + _node_id + "_CANCEL/config",
            values={
                "name": _node_name + " Cancel Print",
                "uniq_id": _node_id + "_CANCEL",
                "cmd_t": "~" + self._generate_topic("controlTopic", "cancel"),
                "avty": [
                    {
                        "t": "~" + self._generate_topic("hassTopic", "is_printing"),
                        "pl_avail": "True",
                        "pl_not_avail": "False",
                    },
                ],
                "device": _config_device,
                "ic": "mdi:cancel",
            },
        )

        # Pause / resume print
        if subscribe:
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "pause", full=True),
                self._on_pause_print,
            )

        self._generate_sensor(
            topic=_discovery_topic + "/switch/" + _node_id + "_PAUSE/config",
            values={
                "name": _node_name + " Pause Print",
                "uniq_id": _node_id + "_PAUSE",
                "cmd_t": "~" + self._generate_topic("controlTopic", "pause"),
                "stat_t": "~" + self._generate_topic("hassTopic", "is_paused"),
                "avty": [
                    {
                        "t": "~" + self._generate_topic("hassTopic", "is_printing"),
                        "pl_avail": "True",
                        "pl_not_avail": "False",
                    },
                ],
                "pl_off": "False",
                "pl_on": "True",
                "device": _config_device,
                "ic": "mdi:pause",
            },
        )

        # Shutdown, Reboot and Restart OctoPrint
        if subscribe:
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "shutdown", full=True),
                self._on_shutdown_system,
            )
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "reboot", full=True),
                self._on_restart_system,
            )
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "restart", full=True),
                self._on_restart_server,
            )

        self._generate_sensor(
            topic=_discovery_topic + "/button/" + _node_id + "_SHUTDOWN/config",
            values={
                "name": _node_name + " Shutdown System",
                "uniq_id": _node_id + "_SHUTDOWN",
                "cmd_t": "~" + self._generate_topic("controlTopic", "shutdown"),
                "device": _config_device,
                "ic": "mdi:power",
            },
        )

        self._generate_sensor(
            topic=_discovery_topic + "/button/" + _node_id + "_REBOOT/config",
            values={
                "name": _node_name + " Reboot System",
                "uniq_id": _node_id + "_REBOOT",
                "cmd_t": "~" + self._generate_topic("controlTopic", "reboot"),
                "device": _config_device,
                "ic": "mdi:restart-alert",
            },
        )

        self._generate_sensor(
            topic=_discovery_topic + "/button/" + _node_id + "_RESTART/config",
            values={
                "name": _node_name + " Restart Server",
                "uniq_id": _node_id + "_RESTART",
                "cmd_t": "~" + self._generate_topic("controlTopic", "restart"),
                "device": _config_device,
                "ic": "mdi:restart",
            },
        )

        # PSUControl
        if self.psucontrol_enabled:
            if subscribe:
                self.mqtt_subscribe(
                    self._generate_topic("controlTopic", "psu", full=True),
                    self._on_psu,
                )

            self._generate_sensor(
                topic=_discovery_topic + "/switch/" + _node_id + "_PSU/config",
                values={
                    "name": _node_name + " PSU",
                    "uniq_id": _node_id + "_PSU",
                    "cmd_t": "~" + self._generate_topic("controlTopic", "psu"),
                    "stat_t": "~" + self._generate_topic("hassTopic", "psu_on"),
                    "pl_on": "True",
                    "pl_off": "False",
                    "device": _config_device,
                    "ic": "mdi:flash",
                },
            )

        # Camera output
        if self.snapshot_enabled:
            if subscribe:
                self.mqtt_subscribe(
                    self._generate_topic("controlTopic", "camera_snapshot", full=True),
                    self._on_camera,
                )

            self._generate_sensor(
                topic=_discovery_topic
                + "/switch/"
                + _node_id
                + "_CAMERA_SNAPSHOT/config",
                values={
                    "name": _node_name + " Camera snapshot",
                    "uniq_id": _node_id + "_CAMERA_SNAPSHOT",
                    "cmd_t": "~"
                    + self._generate_topic("controlTopic", "camera_snapshot"),
                    "stat_t": "~"
                    + self._generate_topic("controlTopic", "camera_snapshot"),
                    "pl_off": "False",
                    "pl_on": "True",
                    "val_tpl": "{{False}}",
                    "device": _config_device,
                    "ic": "mdi:camera-iris",
                },
            )

        # Command topics that don't have a suitable sensor configuration. These can be used
        # through the MQTT.publish service call though.
        if subscribe:
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "jog", full=True), self._on_jog
            )
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "connect", full=True), self._on_connect_printer
            )
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "home", full=True), self._on_home
            )
            self.mqtt_subscribe(
                self._generate_topic("controlTopic", "commands", full=True),
                self._on_command,
            )

    ##~~ EventHandlerPlugin API

    def on_event(self, event, payload):
        events = dict(
            comm=(
                Events.CONNECTING,
                Events.CONNECTED,
                Events.DISCONNECTING,
                Events.DISCONNECTED,
                Events.ERROR,
                Events.PRINTER_STATE_CHANGED,
            ),
            files=(
                Events.FILE_SELECTED,
                Events.FILE_DESELECTED,
                Events.CAPTURE_DONE,
            ),
            status=(
                Events.PRINT_STARTED,
                Events.PRINT_FAILED,
                Events.PRINT_DONE,
                Events.PRINT_CANCELLED,
                Events.PRINT_PAUSED,
                Events.PRINT_RESUMED,
                Events.Z_CHANGE,
            ),
        )

        # Printer connectivity status events
        if event in events["comm"]:
            self._generate_connection_status()

        # Print job status events
        if (
            event in events["comm"]
            or event in events["files"]
            or event in events["status"]
        ):
            self._logger.debug("Received event " + event + ", updating status")
            self._generate_printer_status()

        if event == Events.PRINT_STARTED:
            if self.update_timer:
                self.mqtt_publish(
                    self._generate_topic("hassTopic", "is_printing", full=True),
                    "True",
                    allow_queueing=True,
                )

                try:
                    self.update_timer.start()
                except RuntimeError:
                    # May already be running, it's ok
                    pass

        elif event in (Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED):
            if self.update_timer:
                self.mqtt_publish(
                    self._generate_topic("hassTopic", "is_printing", full=True),
                    "False",
                    allow_queueing=True,
                )

                try:
                    self.update_timer.cancel()
                except RuntimeError:
                    # May already be stopped, it's ok
                    pass

        if event == Events.PRINT_PAUSED:
            self.mqtt_publish(
                self._generate_topic("hassTopic", "is_paused", full=True),
                "True",
                allow_queueing=True,
            )

        elif event in (Events.PRINT_RESUMED, Events.PRINT_STARTED):
            self.mqtt_publish(
                self._generate_topic("hassTopic", "is_paused", full=True),
                "False",
                allow_queueing=True,
            )


        if (
            self.psucontrol_enabled and
            event == Events.PLUGIN_PSUCONTROL_PSU_STATE_CHANGED
        ):
            self._generate_psu_state(payload["isPSUOn"])

        if event == Events.CAPTURE_DONE:
            file_handle = open(payload["file"], "rb")
            file_content = file_handle.read()
            file_handle.close()
            self.mqtt_publish(
                self._generate_topic("baseTopic", "camera", full=True),
                file_content,
                allow_queueing=False,
                raw_data=True,
            )

    ##~~ ProgressPlugin API

    def on_print_progress(self, storage, path, progress):
        self._generate_printer_status()

    def on_slicing_progress(
        self,
        slicer,
        source_location,
        source_path,
        destination_location,
        destination_path,
        progress,
    ):
        pass

    ##~~ WizardPlugin mixin

    def is_wizard_required(self):
        helpers = self._plugin_manager.get_helpers("mqtt")
        if helpers:
            return False

        mqtt_defaults = dict(plugins=dict(mqtt=MQTT_DEFAULTS))
        _retain = settings().get_boolean(
            ["plugins", "mqtt", "broker", "retain"], defaults=mqtt_defaults
        )
        if not _retain:
            return False

        return True

    ##~~ Softwareupdate hook

    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
        # for details.
        return dict(
            homeassistant=dict(
                displayName="HomeAssistant Discovery Plugin",
                displayVersion=self._plugin_version,
                # version check: github repository
                type="github_release",
                user="cmroche",
                repo="OctoPrint-HomeAssistant",
                current=self._plugin_version,
                # update method: pip
                pip="https://github.com/cmroche/OctoPrint-HomeAssistant/archive/{target_version}.zip",
            )
        )


__plugin_name__ = "HomeAssistant Discovery"
__plugin_pythoncompat__ = ">=2.7,<4"  # python 2 and 3


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = HomeassistantPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
