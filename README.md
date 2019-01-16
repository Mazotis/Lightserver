# Lightserver
A python websocket server/client to control various cheap IoT RGB BLE lightbulbs and HDMI-CEC-to-TV RPi3

# Usage

*** On a RPi3 or a linux-based bluetooth-enabled processor board ***
1) Setup python3 + required pip imports.
2) Configure your playbulb/milight bulbs in the play.ini file.
3) Run ./play.py --server (or execute as systemd startup script).
4) To use HDMI-CEC, connect HDMI cable to a free TV port.

*** On a client device (tested on an AsusWRT router) ***
1) Setup python3 + required pip imports (opkg)
2) Run server.py using the init.d script. This will receive web POST requests (using POST variables action and hash - a SHA512 hashed SALT+action string).
3) Run detector.py using the init.d script. This will query active WIFI devices (cellphones, tablets...using the MAC addresses) on the network and open/close lights accordingly.
4) You can also trigger light changes/HDMI-CEC requests by runing ./playclient.py OPTIONS

*** PLAY.INI tweakables ***
[DEVICE#]
TYPE = Playbulb
ADDRESS = (The bluetooth MAC address for the bulb)
DESCRIPTION = (A description string)
GROUP = (A group string to link devices within a same room)
SUBGROUP = (Another string to subgroup devices within a same room)
DEFAULT_INTENSITY = (The default ON intensity, AARRGGBB format, recommended: 05000000)

[DEVICE#]
TYPE = Milight
ADDRESS = (The bluetooth MAC address for the bulb)
ID1 = (The first ID value for the bulb. Use a BLE sniffing app to find this)
ID2 = (The second ID value for the bulb. Use a BLE sniffing app to find this)
DESCRIPTION = (A description string)
GROUP = (A group string to link devices within a same room)
SUBGROUP = (Another string to subgroup devices within a same room)

[DEVICE#]
TYPE = Decora
NAME = (The name of the device as set in the MyLeviton app)
EMAIL = (Your email address as set in the MyLeviton app)
PASSWORD = (Your password as set in the MyLeviton app)
DESCRIPTION = (A description string)
GROUP = (A group string to link devices within a same room)
SUBGROUP = (Another string to subgroup devices within a same room)
DEFAULT_INTENSITY = (The default ON intensity, from 0 to 100)