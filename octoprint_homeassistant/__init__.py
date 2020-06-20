# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
from octoprint.server import user_permission
from octoprint.events import eventManager, Events
from octoprint.settings import settings
import threading
import time
import os
import re
import logging
import json

### (Don't forget to remove me)
# This is a basic skeleton for your plugin's __init__.py. You probably want to adjust the class name of your plugin
# as well as the plugin mixins it's subclassing from. This is really just a basic skeleton to get you started,
# defining your plugin as a template plugin, settings and asset plugin. Feel free to add or remove mixins
# as necessary.
#
# Take a look at the documentation on what other plugin mixins are available.

import octoprint.plugin

SETTINGS_DEFAULTS = dict(unique_id=None)
MQTT_DEFAULTS = dict(
	publish=dict(
		baseTopic="octoPrint/",
		eventTopic="event/{event}",
		progressTopic="progress/{progress}",
		temperatureTopic="temperature/{temp}",
		hassTopic="hass/{hass}",
		lwTopic="mqtt"
	)
)


class HomeassistantPlugin(octoprint.plugin.SettingsPlugin,
						  octoprint.plugin.TemplatePlugin,
						  octoprint.plugin.StartupPlugin,
						  octoprint.plugin.ProgressPlugin,
						  octoprint.plugin.WizardPlugin):

	def __init__(self):
		self.mqtt_publish = None
		self.mqtt_publish_with_timestamp = None
		self.mqtt_subcribe = None

	##~~ SettingsPlugin

	def get_settings_defaults(self):
		return SETTINGS_DEFAULTS

	##~~ StartupPlugin mixin

	def on_startup(self, host, port):
		self._logger.setLevel(logging.INFO)

	def on_after_startup(self):
		if self._settings.get(["unique_id"]) is None:
			import uuid
			_uid = str(uuid.uuid4())
			self._settings.set(["unique_id"], _uid)
			settings().save()

		helpers = self._plugin_manager.get_helpers("mqtt", "mqtt_publish", "mqtt_publish_with_timestamp", "mqtt_subscribe")
		if helpers:
			if "mqtt_publish_with_timestamp" in helpers:
				self._logger.debug("Setup publish with timestamp helper")
				self.mqtt_publish_with_timestamp = helpers["mqtt_publish_with_timestamp"]

			if "mqtt_publish" in helpers:
				self._logger.debug("Setup publish helper")
				self.mqtt_publish = helpers["mqtt_publish"]

				# By default retain isn't used, so it's not possible to get a callback
				# from the MQTT plugin to trigger device registration, we have to queue
				self._generate_device_registration()

			if "mqtt_subscribe" in helpers:
				self._logger.debug("Setup subscribe helper")
				self.mqtt_subscribe = helpers["mqtt_subscribe"]
				self.mqtt_subscribe(self._generate_topic("baseTopic", "mqtt", full=True), self._on_mqtt_message)

	def _get_mac_address(self):
		import uuid
		return ':'.join(re.findall('..', '%012x' % uuid.getnode()))

	def _on_mqtt_message(self, topic, message, retained=None, qos=None, *args, **kwargs):
		self._logger.info("Received MQTT message from " + topic)
		self._logger.info(message)

		if message == "connected":
			self._generate_device_registration()

	def _generate_topic(self, topic_type, topic, full=False):
		mqtt_defaults = dict(plugins=dict(mqtt=MQTT_DEFAULTS))
		_topic = ""

		if topic_type != "baseTopic":
			_topic = settings().get(["plugins", "mqtt", "publish", topic_type], defaults=mqtt_defaults)
			_topic = _topic[:_topic.rfind('{')]

		if full or topic_type == "baseTopic":
			_topic = settings().get(["plugins", "mqtt", "publish", "baseTopic"], defaults=mqtt_defaults) + _topic

		_topic += topic
		self._logger.debug("Generated topic: " + _topic)
		return _topic

	def _generate_device_registration(self):

		s = settings()
		name_defaults = dict(appearance=dict(name="OctoPrint"))

		_node_name = s.get(["appearance", "name"], defaults=name_defaults)
		_node_uuid = self._settings.get(["unique_id"])
		_node_id = (_node_uuid[:6]).upper()

		_config_device = {
			"ids": [_node_id],
			"cns": [["mac", self._get_mac_address()]],
			"name": _node_name,
			"mf": "Clifford Roche",
			"mdl": "HomeAssistant Discovery for OctoPrint",
			"sw": self._plugin_version
		}

		##~~ Configure Connected Sensor

		_topic_connected = "homeassistant/binary_sensor/" + _node_id + "_CONNECTED/config"
		_config_connected = {
			"name": _node_name + " Connected",
			"uniq_id": _node_id + "_CONNECTED",
			"stat_t": "~" + self._generate_topic("eventTopic", "Connected"),
			"json_attr_t": "~" + self._generate_topic("eventTopic", "Connected"),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"pl_on": "Connected",
			"pl_off": "Disconnected",
			"val_tpl": '{{value_json._event}}',
			"device": _config_device,
			"dev_cla": "connectivity",
			"~": self._generate_topic("baseTopic", "", full=True)
		}

		self.mqtt_publish(_topic_connected, _config_connected, allow_queueing=True)

		##~~ Configure Printing Sensor

		_topic_printing = "homeassistant/binary_sensor/" + _node_id + "_PRINTING/config"
		_config_printing = {
			"name": _node_name + " Printing",
			"uniq_id": _node_id + "_PRINTING",
			"stat_t": "~" + self._generate_topic("progressTopic", "printing"),
			"json_attr_t": "~" + self._generate_topic("progressTopic", "printing"),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"pl_on": "True",
			"pl_off": "False",
			"val_tpl": '{{value_json.progress > 0}}',
			"device": _config_device,
			"~": self._generate_topic("baseTopic", "", full=True)
		}

		self.mqtt_publish(_topic_printing, _config_printing, allow_queueing=True)

		##~~ Configure Last Event Sensor

		_topic_last_event = "homeassistant/sensor/" + _node_id + "_EVENT/config"
		_config_last_event = {
			"name": _node_name + " Last Event",
			"uniq_id": _node_id + "_EVENT",
			"stat_t": "~" + self._generate_topic("eventTopic", "+"),
			"json_attr_t": "~" + self._generate_topic("eventTopic", "+"),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"val_tpl": "{{value_json._event}}",
			"device": _config_device,
			"~": self._generate_topic("baseTopic", "", full=True)
		}

		self.mqtt_publish(_topic_last_event, _config_last_event, allow_queueing=True)

		##~~ Configure Print Status

		_topic_printing_p = "homeassistant/sensor/" + _node_id + "_PRINTING_P/config"
		_config_printing_p = {
			"name": _node_name + " Print Progress",
			"uniq_id": _node_id + "_PRINTING_P",
			"stat_t": "~" + self._generate_topic("progressTopic", "printing"),
			"json_attr_t": "~" + self._generate_topic("progressTopic", "printing"),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"unit_of_meas": "%",
			"val_tpl": "{{value_json.progress|float|default(0,true)}}",
			"device": _config_device,
			"~": self._generate_topic("baseTopic", "", full=True)
		}

		self.mqtt_publish(_topic_printing_p, _config_printing_p, allow_queueing=True)

		##~~ Configure Print File

		_topic_printing_f = "homeassistant/sensor/" + _node_id + "_PRINTING_F/config"
		_config_printing_f = {
			"name": _node_name + " Print File",
			"uniq_id": _node_id + "_PRINTING_F",
			"stat_t": "~" + self._generate_topic("progressTopic", "printing"),
			"json_attr_t": "~" + self._generate_topic("progressTopic", "printing"),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"val_tpl": "{{value_json.path}}",
			"device": _config_device,
			"~": self._generate_topic("baseTopic", "", full=True)
		}

		self.mqtt_publish(_topic_printing_f, _config_printing_f, allow_queueing=True)

		##~~ Configure Slicing Status

		_topic_slicing_p = "homeassistant/sensor/" + _node_id + "_SLICING_P/config"
		_config_slicing_p = {
			"name": _node_name + " Slicing Progress",
			"uniq_id": _node_id + "_SLICING_P",
			"stat_t": "~" + self._generate_topic("progressTopic", "slicing"),
			"json_attr_t": "~" + self._generate_topic("progressTopic", "slicing"),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"unit_of_meas": "%",
			"val_tpl": "{{value_json.progress|float|default(0,true)}}",
			"device": _config_device,
			"~": self._generate_topic("baseTopic", "", full=True)
		}

		self.mqtt_publish(_topic_slicing_p, _config_slicing_p, allow_queueing=True)

		##~~ Configure Slicing File

		_topic_slicing_f = "homeassistant/sensor/" + _node_id + "_SLICING_F/config"
		_config_slicing_f = {
			"name": _node_name + " Slicing File",
			"uniq_id": _node_id + "_SLICING_F",
			"stat_t": "~" + self._generate_topic("progressTopic", "slicing"),
			"json_attr_t": "~" + self._generate_topic("progressTopic", "slicing"),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"val_tpl": "{{value_json.source_path}}",
			"device": _config_device,
			"~": self._generate_topic("baseTopic", "", full=True)
		}

		self.mqtt_publish(_topic_slicing_f, _config_slicing_f, allow_queueing=True)

		##~~ Tool Temperature
		_e = self._printer_profile_manager.get_current_or_default()["extruder"]["count"]
		for x in range(_e):
			_topic_e_temp = "homeassistant/sensor/" + _node_id + "_TOOL" + str(x) + "/config"
			_config_e_temp = {
				"name": _node_name + " Tool " + str(x) + " Temperature",
				"uniq_id": _node_id + "_TOOL" + str(x),
				"stat_t": "~" + self._generate_topic("temperatureTopic", "tool" + str(x)),
				"json_attr_t": "~" + self._generate_topic("temperatureTopic", "tool" + str(x)),
				"avty_t": "~mqtt",
				"pl_avail": "connected",
				"pl_not_avail": "disconnected",
				"unit_of_meas": "°C",
				"val_tpl": "{{value_json.actual|float}}",
				"device": _config_device,
				"dev_cla": "temperature",
				"~": self._generate_topic("baseTopic", "", full=True)
			}

			self.mqtt_publish(_topic_e_temp, _config_e_temp, allow_queueing=True)

		##~~ Bed Temperature

		_topic_bed_temp = "homeassistant/sensor/" + _node_id + "_BED/config"
		_config_bed_temp = {
			"name": _node_name + " Bed Temperature",
			"uniq_id": _node_id + "_BED",
			"stat_t": "~" + self._generate_topic("temperatureTopic", "bed"),
			"json_attr_t": "~" + self._generate_topic("temperatureTopic", "bed"),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"unit_of_meas": "°C",
			"val_tpl": "{{value_json.actual|float}}",
			"device": _config_device,
			"dev_cla": "temperature",
			"~": self._generate_topic("baseTopic", "", full=True)
		}

		self.mqtt_publish(_topic_bed_temp, _config_bed_temp, allow_queueing=True)

		##~~ For people who do not have retain setup, need to do this again to make sensors available
		_connected_topic = self._generate_topic("lwTopic", "", full=True)
		self.mqtt_publish(_connected_topic, "connected", allow_queueing=True)

		##~~ Setup the default printer states
		self.on_print_progress("", "", 0)

	##~~ ProgressPlugin API

	def on_print_progress(self, storage, path, progress):

		data = self._printer.get_current_data()
		self.mqtt_publish_with_timestamp(self._generate_topic("hassTopic", "printing", full=True), data, allow_queueing=True)

	def on_slicing_progress(self, slicer, source_location, source_path, destination_location, destination_path, progress):

		pass

	##~~ WizardPlugin mixin

	def is_wizard_required(self):
		helpers = self._plugin_manager.get_helpers("mqtt")
		if helpers:
			return False

		mqtt_defaults = dict(plugins=dict(mqtt=MQTT_DEFAULTS))
		_retain = settings().get_boolean(["plugins", "mqtt", "broker", "retain"], defaults=mqtt_defaults)
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
				pip="https://github.com/cmroche/OctoPrint-HomeAssistant/archive/{target_version}.zip"
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
