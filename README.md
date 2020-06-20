# OctoPrint-HomeAssistant

Enable MQTT based discovery of your OctoPrint server with Home Assistant.

## Setup

Install via the bundled [Plugin Manager](https://docs.octoprint.org/en/master/bundledplugins/pluginmanager.html)
or manually using this URL:

    https://github.com/cmroche/OctoPrint-HomeAsisstant/archive/master.zip

You will also need the [OctoPrint-MQTT plugin](https://github.com/OctoPrint/OctoPrint-MQTT) installed and configured to connected to your Home Assistant MQTT service, and MQTT discovery enabled (should be the default). With these, by using the OctoPrint-HomeAssistant plugin your OctoPrint instance will automatically register a device and several sensors to follow your printer status, printing and slicing progress.

***NOTE*** OctoPrint-MQTT works best with HomeAssistant if you leave the default "retain" option enabled. Remember to restart OctoPrint after configuring your MQTT broker settings or installing OctoPrint-HomeAssistant to properly register.

