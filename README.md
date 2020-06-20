# OctoPrint-HomeAssistant

Enable MQTT based discovery of your OctoPrint server with Home Assistant.

## Setup

Install via the bundled [Plugin Manager](https://docs.octoprint.org/en/master/bundledplugins/pluginmanager.html)
or manually using this URL:

    https://github.com/cmroche/OctoPrint-HomeAsisstant/archive/master.zip

You will also need the [OctoPrint-MQTT plugin](https://github.com/OctoPrint/OctoPrint-MQTT) installed and configured to connected to your Home Assistant MQTT service, and MQTT discovery enabled (should be the default). With these, by using the OctoPrint-HomeAssistant plugin your OctoPrint instance will automatically register a device and several sensors to follow your printer status, printing and slicing progress.

***NOTE*** OctoPrint-MQTT works best with HomeAssistant if you leave the default "retain" option enabled. Remember to restart OctoPrint after configuring your MQTT broker settings or installing OctoPrint-HomeAssistant to properly register.

## Why use this plugin?

* MQTT updates are faster, and smaller than querying the Web API
* MQTT updates won't generate errors when OctoPrint isn't running
* MQTT is a local-push implementation, the HomeAssistant native OctoPrint integration uses local-polling
* No need to set static IP addresses or add manual configurations, it just works.

### Benefit from more sensors

* Reliable printer status, and is printing sensors.
* Current Z height
* Formatted print time, and print time remaining
