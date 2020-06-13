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
		lwTopic="mqtt"
	)
)


class HomeassistantPlugin(octoprint.plugin.SettingsPlugin,
						  octoprint.plugin.AssetPlugin,
						  octoprint.plugin.TemplatePlugin,
						  octoprint.plugin.StartupPlugin,
						  octoprint.plugin.WizardPlugin):

	##~~ SettingsPlugin

	def get_settings_defaults(self):
		return SETTINGS_DEFAULTS

	##~~ AssetPlugin mixin

	def get_assets(self):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/OctoPrint-HomeAssistant.js"],
			css=["css/OctoPrint-HomeAssistant.css"],
			less=["less/OctoPrint-HomeAssistant.less"]
		)

	##~~ StartupPlugin mixin

	def on_after_startup(self):
		if self._settings.get(["unique_id"]) is None:
			import uuid
			_uid = str(uuid.uuid4())
			self._settings.set(["unique_id"], _uid)
			settings().save()

		helpers = self._plugin_manager.get_helpers("mqtt", "mqtt_publish")
		if helpers:
			if "mqtt_publish" in helpers:
				self.mqtt_publish = helpers["mqtt_publish"]
				self._generate_device_registration()

	def _get_mac_address(self):
		import uuid
		return ':'.join(re.findall('..', '%012x' % uuid.getnode()))

	def _generate_device_registration(self):

		s = settings()
		mqtt_defaults = dict(plugins=dict(mqtt=MQTT_DEFAULTS))
		name_defaults = dict(appearance=dict(name="OctoPrint"))

		_node_name = s.get(["appearance", "name"], defaults=name_defaults)
		_node_uuid = self._settings.get(["unique_id"])
		_node_id = (_node_uuid[:6]).upper()

		_base_topic = s.get(["plugins", "mqtt", "publish", "baseTopic"], defaults=mqtt_defaults)
		_event_topic = s.get(["plugins", "mqtt", "publish", "eventTopic"], defaults=mqtt_defaults)
		_progress_topic = s.get(["plugins", "mqtt", "publish", "progressTopic"], defaults=mqtt_defaults)
		_temperature_topic = s.get(["plugins", "mqtt", "publish", "temperatureTopic"], defaults=mqtt_defaults)

		_config_device = {
			"ids": [_node_id],
			"cns": [["mac", self._get_mac_address()]]
		}

		##~~ Configure Connected Sensor

		_topic_connected = "homeassistant/binary_sensor/" + _node_id + "_CONNECTED/config"
		_config_connected = {
			"name": _node_name + " Connected",
			"uniq_id": _node_id + "_CONNECTED",
			"stat_t": "~" + _event_topic.replace('{event}', 'Connected'),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"unit_of_meas": " ",
			"val_tpl": "{{value_json._event}}",
			"device": _config_device,
			"~": _base_topic
		}

		self.mqtt_publish(_topic_connected, _config_connected)

		##~~ Configure Printing Sensor

		_topic_printing = "homeassistant/binary_sensor/" + _node_id + "_STATUS/config"
		_config_printing = {
			"name": _node_name + " Status",
			"uniq_id": _node_id + "_STATUS",
			"stat_t": "~" + _event_topic.replace('{event}', 'PrintStarted'),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"unit_of_meas": " ",
			"val_tpl": "{{value_json._event}}",
			"device": _config_device,
			"~": _base_topic
		}

		self.mqtt_publish(_topic_printing, _config_printing)

		##~~ Configure Print Status

		_topic_progress = "homeassistant/sensor/" + _node_id + "_PROGRESS/config"
		_config_progress = {
			"name": _node_name + " Print Progress",
			"uniq_id": _node_id + "_PROGRESS",
			"stat_t": "~" + _progress_topic.replace('{progress}', 'printing'),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"unit_of_meas": "%",
			"val_tpl": "{{int(value_json.progress)}}",
			"device": _config_device,
			"~": _base_topic
		}

		self.mqtt_publish(_topic_progress, _config_progress)

		##~~ Tool Temperature
		_e = self._printer_profile_manager.get_current_or_default()["extruder"]["count"]
		for x in range(_e):
			_topic_e_temp = "homeassistant/sensor/" + _node_id + "_TOOL" + str(x) + "/config"
			_config_e_temp = {
				"name": _node_name + " Tool " + str(x) + " Temperature",
				"uniq_id": _node_id + "_TOOL" + str(x),
				"stat_t": "~" + _temperature_topic.replace('{temperature}', 'tool' + str(x)),
				"avty_t": "~mqtt",
				"pl_avail": "connected",
				"pl_not_avail": "disconnected",
				"unit_of_meas": "degrees",
				"val_tpl": "{{float(value_json.actual)}}",
				"device": _config_device,
				"~": _base_topic
			}

			self.mqtt_publish(_topic_e_temp, _config_e_temp)

		##~~ Bed Temperature

		_topic_bed_temp = "homeassistant/sensor/" + _node_id + "_BED/config"
		_config_bed_temp = {
			"name": _node_name + " Bed Temperature",
			"uniq_id": _node_id + "_BED",
			"stat_t": "~" + _temperature_topic.replace('{temperature}', 'bed'),
			"avty_t": "~mqtt",
			"pl_avail": "connected",
			"pl_not_avail": "disconnected",
			"unit_of_meas": "degrees",
			"val_tpl": "{{float(value_json.actual)}}",
			"device": _config_device,
			"~": _base_topic
		}

		self.mqtt_publish(_topic_bed_temp, _config_bed_temp)

	##~~ WizardPlugin mixin

	def is_wizard_required(self):
		helpers = self._plugin_manager.get_helpers("mqtt")
		if helpers:
			return False
		return True

	##~~ Softwareupdate hook

	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
		# for details.
		return dict(
			homeassistant=dict(
				displayName="Octoprint-homeassistant Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="cmroche",
				repo="OctoPrint-HomeAsisstant",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/cmroche/OctoPrint-HomeAsisstant/archive/{target_version}.zip"
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
