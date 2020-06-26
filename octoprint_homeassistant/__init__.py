# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
from octoprint.server import user_permission
from octoprint.events import eventManager, Events
from octoprint.settings import settings
from octoprint.util import RepeatedTimer
import datetime
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
		lwTopic="mqtt",
		hassTopic="hass/{hass}",
		controlTopic="hassControl/{control}"
	)
)


class HomeassistantPlugin(octoprint.plugin.SettingsPlugin,
						  octoprint.plugin.TemplatePlugin,
						  octoprint.plugin.StartupPlugin,
						  octoprint.plugin.EventHandlerPlugin,
						  octoprint.plugin.ProgressPlugin,
						  octoprint.plugin.WizardPlugin):

	def __init__(self):
		self.mqtt_publish = None
		self.mqtt_publish_with_timestamp = None
		self.mqtt_subcribe = None
		self.update_timer = None

	def handle_timer(self):
		self._generate_printer_status()

	##~~ SettingsPlugin

	def get_settings_defaults(self):
		return SETTINGS_DEFAULTS

	##~~ StartupPlugin mixin

	def on_startup(self, host, port):
		self._logger.setLevel(logging.DEBUG)

	def on_after_startup(self):
		if self._settings.get(["unique_id"]) is None:
			import uuid
			_uid = str(uuid.uuid4())
			self._settings.set(["unique_id"], _uid)
			settings().save()

		helpers = self._plugin_manager.get_helpers("mqtt", "mqtt_publish", "mqtt_publish_with_timestamp",
												   "mqtt_subscribe")
		if helpers:
			if "mqtt_publish_with_timestamp" in helpers:
				self._logger.debug("Setup publish with timestamp helper")
				self.mqtt_publish_with_timestamp = helpers["mqtt_publish_with_timestamp"]

			if "mqtt_publish" in helpers:
				self._logger.debug("Setup publish helper")
				self.mqtt_publish = helpers["mqtt_publish"]

			if "mqtt_subscribe" in helpers:
				self._logger.debug("Setup subscribe helper")
				self.mqtt_subscribe = helpers["mqtt_subscribe"]
				self.mqtt_subscribe(self._generate_topic("lwTopic", "", full=True), self._on_mqtt_message)

		if not self.update_timer:
			self.update_timer = RepeatedTimer(60, self.handle_timer, None, None, False)

		# Since retain may not be used it's not always possible to simply tie this to the connected state
		self._generate_device_registration()
		self._generate_device_controls(subscribe=True)

		# For people who do not have retain setup, need to do this again to make sensors available
		_connected_topic = self._generate_topic('lwTopic', '', full=True)
		self.mqtt_publish(_connected_topic, 'connected', allow_queueing=True)

		# Setup the default printer states
		self.mqtt_publish(self._generate_topic("hassTopic", "is_printing", full=True), 'False', allow_queueing=True)
		self.mqtt_publish(self._generate_topic("hassTopic", "is_paused", full=True), 'False', allow_queueing=True)
		self.on_print_progress('', '', 0)

	def _get_mac_address(self):
		import uuid
		return ':'.join(re.findall('..', '%012x' % uuid.getnode()))

	def _on_mqtt_message(self, topic, message, retained=None, qos=None, *args, **kwargs):
		self._logger.info("Received MQTT message from " + topic)
		self._logger.info(message)

		# Don't rely on this, the message may be disabled.
		if message == "connected":
			self._generate_device_registration()
			self._generate_device_controls(sunscribe=False)

	def _generate_topic(self, topic_type, topic, full=False):
		self._logger.debug("Generating topic for " + topic_type + ", " + topic)
		mqtt_defaults = dict(plugins=dict(mqtt=MQTT_DEFAULTS))
		_topic = ""

		if topic_type != "baseTopic":
			_topic = settings().get(["plugins", "mqtt", "publish", topic_type], defaults=mqtt_defaults)
			_topic = re.sub(r'{.+}', '', _topic);

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

		_config_device = self._generate_device_config(_node_id, _node_name)

		##~~ Configure Connected Sensor
		self._generate_sensor(
			topic='homeassistant/binary_sensor/' + _node_id + '_CONNECTED/config',
			values={
				'name': _node_name + ' Connected',
				'uniq_id': _node_id + '_CONNECTED',
				'stat_t': '~' + self._generate_topic('eventTopic', 'Connected'),
				'json_attr_t': '~' + self._generate_topic('eventTopic', 'Connected'),
				'pl_on': 'Connected',
				'pl_off': 'Disconnected',
				'val_tpl': '{{value_json._event}}',
				'dev_cla': 'connectivity',
				'device': _config_device,
			}
		)

		##~~ Configure Printing Sensor
		self._generate_sensor(
			topic='homeassistant/binary_sensor/' + _node_id + '_PRINTING/config',
			values={
				'name': _node_name + ' Printing',
				'uniq_id': _node_id + '_PRINTING',
				'stat_t': '~' + self._generate_topic('hassTopic', 'printing'),
				'pl_on': 'True',
				'pl_off': 'False',
				'val_tpl': '{{value_json.state.flags.printing}}',
				'device': _config_device,
			}
		)

		##~~ Configure Last Event Sensor
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_EVENT/config',
			values={
				'name': _node_name + ' Last Event',
				'uniq_id': _node_id + '_EVENT',
				'stat_t': '~' + self._generate_topic('eventTopic', '+'),
				'val_tpl': '{{value_json._event}}',
				'device': _config_device
			}
		)

		##~~ Configure Print Status
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_PRINTING_S/config',
			values={
				'name': _node_name + ' Print Status',
				'uniq_id': _node_id + '_PRINTING_S',
				'stat_t': '~' + self._generate_topic('hassTopic', 'printing'),
				'json_attr_t': '~' + self._generate_topic('hassTopic', 'printing'),
				'json_attr_tpl': '{{value_json.state|tojson}}',
				'val_tpl': '{{value_json.state.text}}',
				'device': _config_device
			}
		)

		##~~ Configure Print Progress
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_PRINTING_P/config',
			values={
				'name': _node_name + ' Print Progress',
				'uniq_id': _node_id + '_PRINTING_P',
				'stat_t': '~' + self._generate_topic('progressTopic', 'printing'),
				'unit_of_meas': '%',
				'val_tpl': '{{value_json.progress|float|default(0,true)}}',
				'device': _config_device
			}
		)

		##~~ Configure Print File
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_PRINTING_F/config',
			values={
				'name': _node_name + ' Print File',
				'uniq_id': _node_id + '_PRINTING_F',
				'stat_t': '~' + self._generate_topic('progressTopic', 'printing'),
				'val_tpl': '{{value_json.path}}',
				'device': _config_device,
				'ic': 'mdi:file'
			}
		)

		##~~ Configure Print Time
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_PRINTING_T/config',
			values={
				'name': _node_name + ' Print Time',
				'uniq_id': _node_id + '_PRINTING_T',
				'stat_t': '~' + self._generate_topic('hassTopic', 'printing'),
				'val_tpl': '{{value_json.progress.printTimeFormatted}}',
				'device': _config_device,
				'ic': 'mdi:clock-start'
			}
		)

		##~~ Configure Print Time Left
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_PRINTING_E/config',
			values={
				'name': _node_name + ' Print Time Left',
				'uniq_id': _node_id + '_PRINTING_E',
				'stat_t': '~' + self._generate_topic('hassTopic', 'printing'),
				'val_tpl': '{{value_json.progress.printTimeLeftFormatted}}',
				'device': _config_device,
				'ic': 'mdi:clock-check-outline'
			}
		)

		##~~ Configure Print ETA
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_PRINTING_ETA/config',
			values={
				'name': _node_name + ' Print Estimated Time',
				'uniq_id': _node_id + '_PRINTING_ETA',
				'stat_t': '~' + self._generate_topic('hassTopic', 'printing'),
				'json_attr_t': '~' + self._generate_topic('hassTopic', 'printing'),
				'json_attr_tpl': '{{value_json.job|tojson}}',
				'val_tpl': '{{value_json.job.estimatedPrintTimeFormatted}}',
				'device': _config_device
			}
		)

		##~~ Configure Print Current Z
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_PRINTING_Z/config',
			values={
				'name': _node_name + ' Current Z',
				'uniq_id': _node_id + '_PRINTING_Z',
				'stat_t': '~' + self._generate_topic('hassTopic', 'printing'),
				'unit_of_meas': 'mm',
				'val_tpl': '{{value_json.currentZ|float}}',
				'device': _config_device,
				'ic': 'mdi:printer-3d-nozzle'
			}
		)

		##~~ Configure Slicing Status
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_SLICING_P/config',
			values={
				'name': _node_name + ' Slicing Progress',
				'uniq_id': _node_id + '_SLICING_P',
				'stat_t': '~' + self._generate_topic('progressTopic', 'slicing'),
				'unit_of_meas': '%',
				'val_tpl': '{{value_json.progress|float|default(0,true)}}',
				'device': _config_device
			}
		)

		##~~ Configure Slicing File
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_SLICING_F/config',
			values={
				'name': _node_name + ' Slicing File',
				'uniq_id': _node_id + '_SLICING_F',
				'stat_t': '~' + self._generate_topic('progressTopic', 'slicing'),
				'val_tpl': '{{value_json.source_path}}',
				'device': _config_device,
				'ic': 'mdi:file'
			}
		)

		##~~ Tool Temperature
		_e = self._printer_profile_manager.get_current_or_default()["extruder"]["count"]
		for x in range(_e):
			self._generate_sensor(
				topic='homeassistant/sensor/' + _node_id + '_TOOL' + str(x) + '/config',
				values={
					'name': _node_name + ' Tool ' + str(x) + ' Temperature',
					'uniq_id': _node_id + '_TOOL' + str(x),
					'stat_t': '~' + self._generate_topic('temperatureTopic', 'tool' + str(x)),
					'unit_of_meas': '°C',
					'val_tpl': '{{value_json.actual|float}}',
					'device': _config_device,
					'dev_cla': "temperature"
				}
			)
			self._generate_sensor(
				topic='homeassistant/sensor/' + _node_id + '_TOOL_TARGET' + str(x) + '/config',
				values={
					'name': _node_name + ' Tool ' + str(x) + ' Target',
					'uniq_id': _node_id + '_TOOL_TARGET' + str(x),
					'stat_t': '~' + self._generate_topic('temperatureTopic', 'tool' + str(x)),
					'unit_of_meas': '°C',
					'val_tpl': '{{value_json.target|float}}',
					'device': _config_device,
					'dev_cla': "temperature"
				}
			)

		##~~ Bed Temperature
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_BED/config',
			values={
				'name': _node_name + ' Bed Temperature',
				'uniq_id': _node_id + '_BED',
				'stat_t': '~' + self._generate_topic('temperatureTopic', 'bed'),
				'unit_of_meas': '°C',
				'val_tpl': '{{value_json.actual|float}}',
				'device': _config_device,
				'dev_cla': 'temperature'
			}
		)
		self._generate_sensor(
			topic='homeassistant/sensor/' + _node_id + '_BED_TARGET/config',
			values={
				'name': _node_name + ' Bed Target',
				'uniq_id': _node_id + '_BED_TARGET',
				'stat_t': '~' + self._generate_topic('temperatureTopic', 'bed'),
				'unit_of_meas': '°C',
				'val_tpl': '{{value_json.target|float}}',
				'device': _config_device,
				'dev_cla': 'temperature'
			}
		)

	def _generate_sensor(self, topic, values):
		payload = {
			'avty_t': '~' + self._generate_topic('lwTopic', ''),
			'pl_avail': 'connected',
			'pl_not_avail': 'disconnected',
			'~': self._generate_topic('baseTopic', '', full=True)
		}
		payload.update(values)
		self.mqtt_publish(topic, payload, allow_queueing=True)

	def _generate_device_config(self, _node_id, _node_name):
		_config_device = {
			'ids': [_node_id],
			'cns': [['mac', self._get_mac_address()]],
			'name': _node_name,
			'mf': 'Clifford Roche',
			'mdl': 'HomeAssistant Discovery for OctoPrint',
			'sw': self._plugin_version
		}
		return _config_device

	def _generate_printer_status(self):

		data = self._printer.get_current_data()
		try:
			data["progress"]["printTimeLeftFormatted"] = str(datetime.timedelta(seconds=int(data["progress"]["printTimeLeft"])))
		except:
			data["progress"]["printTimeLeftFormatted"] = None
		try:
			data["progress"]["printTimeFormatted"] = str(datetime.timedelta(seconds=data["progress"]["printTime"]))
		except:
			data["progress"]["printTimeFormatted"] = None
		try:
			data["job"]["estimatedPrintTimeFormatted"] = str(datetime.timedelta(seconds=data["job"]["estimatedPrintTime"]))
		except:
			data["job"]["estimatedPrintTimeFormatted"] = None

		if self.mqtt_publish_with_timestamp:
			self.mqtt_publish_with_timestamp(self._generate_topic("hassTopic", "printing", full=True), data,
										 	 allow_queueing=True)

	def _on_emergency_stop(self, topic, message, retained=None, qos=None, *args, **kwargs):
		self._logger.debug('Emergency stop message received: ' + str(message))
		if message:
			self._printer.commands('M112')

	def _on_cancel_print(self, topic, message, retained=None, qos=None, *args, **kwargs):
		self._logger.debug('Cancel print message received: ' + str(message))
		if message:
			self._printer.cancel_print()

	def _on_pause_print(self, topic, message, retained=None, qos=None, *args, **kwargs):
		self._logger.debug('Pause print message received: ' + str(message))
		if message:
			self._printer.pause_print()
		else:
			self._printer.resume_print()

	def _on_shutdown_system(self, topic, message, retained=None, qos=None, *args, **kwargs):
		self._logger.debug('Shutdown print message received: ' + str(message))
		if message:
			shutdown_command = self._settings.global_get(["server", "commands", "systemShutdownCommand"])
			try:
				import sarge
				params = {'async': True}
				sarge.run(shutdown_command, **params)
			except Exception as e:
				self._logger.info('Unable to run shutdown command: ' + e)
				pass

	def _generate_device_controls(self, subscribe=False):

		s = settings()
		name_defaults = dict(appearance=dict(name="OctoPrint"))

		_node_name = s.get(["appearance", "name"], defaults=name_defaults)
		_node_uuid = self._settings.get(["unique_id"])
		_node_id = (_node_uuid[:6]).upper()

		_config_device = self._generate_device_config(_node_id, _node_name)

		# Emergency stop
		if subscribe:
			self.mqtt_subscribe(self._generate_topic("controlTopic", "stop", full=True), self._on_emergency_stop)

		self._generate_sensor(
			topic='homeassistant/switch/' + _node_id + '_STOP/config',
			values={
				'name': _node_name + ' Emergency Stop',
				'uniq_id': _node_id + '_STOP',
				'cmd_t': '~' + self._generate_topic('controlTopic', 'stop'),
				'stat_t': '~' + self._generate_topic('controlTopic', 'stop'),
				'pl_off': 'False',
				'pl_on': 'True',
				'val_tpl': '{{False}}',
				'device': _config_device,
				'ic': 'mdi:alert-octagon'
			}
		)

		# Cancel print
		if subscribe:
			self.mqtt_subscribe(self._generate_topic("controlTopic", "cancel", full=True), self._on_cancel_print)

		self._generate_sensor(
			topic='homeassistant/switch/' + _node_id + '_CANCEL/config',
			values={
				'name': _node_name + ' Cancel Print',
				'uniq_id': _node_id + '_CANCEL',
				'cmd_t': '~' + self._generate_topic('controlTopic', 'cancel'),
				'stat_t': '~' + self._generate_topic('controlTopic', 'cancel'),
				'avty_t': '~' + self._generate_topic('hassTopic', 'is_printing'),
				'pl_avail': 'True',
				'pl_not_avail': 'False',
				'pl_off': 'False',
				'pl_on': 'True',
				'val_tpl': '{{False}}',
				'device': _config_device,
				'ic': 'mdi:cancel'
			}
		)

		# Pause / resume print
		if subscribe:
			self.mqtt_subscribe(self._generate_topic("controlTopic", "pause", full=True), self._on_pause_print)

		self._generate_sensor(
			topic='homeassistant/switch/' + _node_id + '_PAUSE/config',
			values={
				'name': _node_name + ' Pause Print',
				'uniq_id': _node_id + '_PAUSE',
				'cmd_t': '~' + self._generate_topic('controlTopic', 'pause'),
				'stat_t': '~' + self._generate_topic('hassTopic', 'is_paused'),
				'avty_t': '~' + self._generate_topic('hassTopic', 'is_printing'),
				'pl_avail': 'True',
				'pl_not_avail': 'False',
				'pl_off': 'False',
				'pl_on': 'True',
				'device': _config_device,
				'ic': 'mdi:pause'
			}
		)

		# Shutdown OctoPrint
		if subscribe:
			self.mqtt_subscribe(self._generate_topic("controlTopic", "shutdown", full=True), self._on_shutdown_system)

		self._generate_sensor(
			topic='homeassistant/switch/' + _node_id + '_SHUTDOWN/config',
			values={
				'name': _node_name + ' Shutdown System',
				'uniq_id': _node_id + '_SHUTDOWN',
				'cmd_t': '~' + self._generate_topic('controlTopic', 'shutdown'),
				'stat_t': '~' + self._generate_topic('controlTopic', 'shutdown'),
				'pl_off': 'False',
				'pl_on': 'True',
				'val_tpl': '{{False}}',
				'device': _config_device,
				'ic': 'mdi:power'
			}
		)

	##~~ EventHandlerPlugin API

	def on_event(self, event, payload):
		events = dict(comm=(Events.CONNECTING, Events.CONNECTED, Events.DISCONNECTING,
							Events.DISCONNECTED, Events.ERROR, Events.PRINTER_STATE_CHANGED),
					  files=(Events.FILE_SELECTED, Events.FILE_DESELECTED),
					  status=(Events.PRINT_STARTED, Events.PRINT_FAILED, Events.PRINT_DONE,
							 Events.PRINT_CANCELLED, Events.PRINT_PAUSED, Events.PRINT_RESUMED,
							 Events.Z_CHANGE))

		if event in events["comm"] or event in events["files"] or event in events["status"]:
			self._logger.debug("Received event " + event + ", updating status")
			self._generate_printer_status()

		if event == Events.PRINT_STARTED:
			if self.update_timer:
				self.mqtt_publish(self._generate_topic("hassTopic", "is_printing", full=True), 'True',
								  allow_queueing=True)
				self.update_timer.start()

		elif event in (Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED):
			if self.update_timer:
				self.mqtt_publish(self._generate_topic("hassTopic", "is_printing", full=True), 'False',
								  allow_queueing=True)
				self.update_timer.cancel()

		if event == Events.PRINT_PAUSED:
			self.mqtt_publish(self._generate_topic("hassTopic", "is_paused", full=True), 'True',
							  allow_queueing=True)

		elif event in (Events.PRINT_RESUMED, Events.PRINT_STARTED):
			self.mqtt_publish(self._generate_topic("hassTopic", "is_paused", full=True), 'False',
							  allow_queueing=True)

	##~~ ProgressPlugin API

	def on_print_progress(self, storage, path, progress):
		self._generate_printer_status()

	def on_slicing_progress(self, slicer, source_location, source_path, destination_location, destination_path,
							progress):
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
