#!/usr/bin/env python3
'''
    File name: play.py
    Author: Maxime Bergeron
    Date last modified: 12/04/2019
    Python Version: 3.5

    A python websocket server/client and IFTTT receiver to control various cheap IoT
    RGB BLE lightbulbs and HDMI-CEC-to-TV RPi3
'''
import os
import os.path
import re
import subprocess
import sys
import argparse
import sched
import time
import datetime
import socket
import threading
import configparser
import requests
import socketserver
import traceback
import json
import queue
import urllib.parse
import hashlib
import ssl
from devices.common import *
from devices import *
from dnn.dnn import run_tensorflow
from argparse import RawTextHelpFormatter, Namespace
from multiprocessing.pool import ThreadPool
from threading import Thread
from http.server import SimpleHTTPRequestHandler, BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from functools import partial
from __main__ import *


class HomeServer(object):
    """ Handles server-side request reception and handling """
    def __init__(self, lm):
        self.config = configparser.ConfigParser()
        self.config.read('play.ini')
        self.host = self.config['SERVER']['HOST']
        self.port = int(self.config['SERVER'].getint('PORT'))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sched_disconnect = sched.scheduler(time.time, time.sleep)
        self.scheduled_disconnect = None
        self.tcp_start_hour = datetime.datetime.strptime(self.config['SERVER']['TCP_START_HOUR'],'%H:%M').time()
        self.tcp_end_hour = datetime.datetime.strptime(self.config['SERVER']['TCP_END_HOUR'],'%H:%M').time()

    def listen(self):
        """ Starts the server """
        debug.write('Server started', 0)
        # Cleanup connection to allow new sock.accepts faster as sched is blocking
        self.disconnect_devices()
        try:
            self.sock.listen(5)
            while True:
                client, address = self.sock.accept()
                debug.write("Connected with {}:{}".format(address[0], address[1]), 0)
                client.settimeout(30)
                threading.Thread(target=self.listen_client, args=(client, address)).start()
        except (KeyboardInterrupt, SystemExit):
            self.remove_server()

    def listen_client(self, client, address):
        """ Listens for new requests and handle them properly """
        streamingdev = False
        streaminggrp = False
        streaming_id = None
        try:
            while True:
                msize = int(client.recv(4).decode('utf-8'))
                if self.scheduled_disconnect is not None:
                    self.sched_disconnect.cancel(self.scheduled_disconnect)
                    self.scheduled_disconnect = None
                #debug.write("Set message size {}".format(msize), 0)
                data = client.recv(msize)
                if data:
                    if data.decode('utf-8') == "getstate":
                        ls_status = {}
                        ls_status["state"] = lm.get_state()
                        ls_status["mode"] = lm.get_modes()
                        ls_status["type"] = lm.get_types()
                        ls_status["description"] = lm.get_descriptions(True)
                        ls_status["starttime"] = "{}".format(lm.starttime)
                        debug.write('Sending lightserver status', 0)
                        client.send(json.dumps(ls_status).encode('UTF-8'))
                        break
                    if data.decode('utf-8') == "setstate":
                        debug.write('Running a single device state change', 0)
                        iddata = int(client.recv(3).decode("UTF-8"))
                        valdata = int(client.recv(8).decode("UTF-8"))
                        skiptime = int(client.recv(1).decode("UTF-8"))
                        _col = ["-1"] * len(lm.devices)
                        _col[iddata] = str(valdata)
                        lm.set_colors(_col)
                        if skiptime == 1:
                            lm.set_skip_time_check()
                        lm.set_mode(False,False)
                        lm.run()
                        client.send("1".encode("UTF-8"))
                        break
                    if data.decode('utf-8') == "setmode":
                        debug.write('Running a single device mode change', 0)
                        iddata = int(client.recv(3).decode("UTF-8"))
                        cmode = int(client.recv(1).decode("UTF-8"))
                        if cmode == 1:
                            lm.set_mode_for_device(True, iddata)
                        else:
                            lm.set_mode_for_device(False, iddata)
                        debug.write('Device modes: {}'.format(lm.get_modes()), 0)
                        client.send("1".encode("UTF-8"))
                        break
                    if data.decode('utf-8') == "stream":
                        debug.write('Starting streaming mode', 0)
                        streamingdev = True
                        continue
                    if data.decode('utf-8') == "streamgroup":
                        debug.write('Starting group streaming mode', 0)
                        streaminggrp = True
                        continue
                    if data.decode('utf-8') == "nostream":
                        debug.write('Ending streaming mode', 0)
                        streamingdev = False
                        streaminggrp = False
                        streaming_id = None
                        break
                    if data.decode('utf-8')[:3] == "tcp":
                        debug.write('Getting TCP request: {}'.format(data.decode('utf-8')), 0)
                        if self.tcp_start_hour > datetime.datetime.now().time() or \
                           self.tcp_end_hour < datetime.datetime.now().time():
                           debug.write('TCP requests disabled until {}'.format(self.tcp_start_hour), 0)
                           break
                        if data.decode('utf-8')[3:] in self.config["TCP-PRESETS"]:
                            debug.write("Running TCP preset {}".format(data.decode('utf-8')[3:]), 0)
                            if self.config["TCP-PRESETS"].getboolean('AUTOMATIC_MODE'):
                                os.system("./playclient.py --auto-mode " + self.config["TCP-PRESETS"][data.decode('utf-8')[3:]])
                            else:
                                os.system("./playclient.py " + self.config["TCP-PRESETS"][data.decode('utf-8')[3:]])
                        else:
                            debug.write("TCP preset {} is not configured".format(data.decode('utf-8')[3:]), 1)
                        break

                    if data.decode('utf-8') == "sendloc":
                        locationData = json.loads(client.recv(1024).decode("UTF-8"))
                        debug.write('Recording a training location for room: {}'.format(locationData["room"]), 0)
                        with open(self.config['SERVER']['JOURNAL_DIR'] + "/dnn/train.log", "a") as jfile:
                            jfile.write("{},{},{},{},{},{},{}\n".format(locationData["room"], locationData["r1_mean"], \
                                locationData["r1_rssi"],locationData["r2_mean"],locationData["r2_rssi"],locationData["r3_mean"] \
                                ,locationData["r3_rssi"]))
                        break
                    if data.decode('utf-8') == "getloc":
                        ld = json.loads(client.recv(1024).decode("UTF-8"))
                        debug.write('[WIFI-RTT] Evaluating location from:', 0)
                        tf_str = '{},{},{},{},{},{}'.format(ld["r1_mean"], ld["r1_rssi"], ld["r2_mean"], ld["r2_rssi"], ld["r3_mean"], ld["r3_rssi"])
                        debug.write('[WIFI-RTT] {}'.format(tf_str), 0)
                        res = run_tensorflow(TfPredict=True, PredictList=tf_str)
                        debug.write("[WIFI-RTT] Device found to be in room: {}".format(res), 0)
                        client.send(res.encode("UTF-8"))
                        break
                    if streamingdev:
                        if streaming_id is None:
                            streaming_id = int(data.decode('utf-8'))
                            debug.write('Set streaming devid to {}' \
                                                  .format(streaming_id), 0)
                            continue
                        debug.write("Sending request to devid {} for color: {}" \
                                              .format(streaming_id, data.decode('utf-8')), 0)
                        lm.set_light_stream(streaming_id, data.decode('utf-8'), False)
                        continue
                    if streaminggrp:
                        if streaming_id is None:
                            streaming_id = data.decode('utf-8')
                            debug.write('Set streaming group to {}' \
                                                  .format(streaming_id), 0)
                            continue
                        debug.write("Sending request to group '{}' for color: {}" \
                                              .format(streaming_id, data.decode('utf-8')), 0)
                        lm.set_light_stream(streaming_id, data.decode('utf-8'), True)
                        continue
                    try:
                        args = self._sanitize(json.loads(data.decode('utf-8')))
                    except: #fallback - data is not UTF-8 formatted and/or JSON compatible ?
                        debug.write("Error - improperly formatted JSON. Got: {}".format(data.decode('utf-8')), 2)
                        break
                    debug.write('Change of lights requested with args: ' + str(args), 0)
                    self._validate_and_execute_req(args)
                    break

        except socket.timeout:
            pass

        except Exception as ex:
            debug.write('Unhandled exception of type {}: {}, {}' \
                                  .format(type(ex), ex, 
                                          ''.join(traceback.format_tb(ex.__traceback__))
                                         ), 2)

        finally:
            debug.write('Closing connection.', 0)
            lm.set_lock(0)
            lm.reinit()
            client.close()
            self.scheduled_disconnect = self.sched_disconnect.enter(60, 1, 
                                                                    self.disconnect_devices, ())
            self.sched_disconnect.run()

    def disconnect_devices(self):
        """ Disconnects all configured devices """
        self.scheduled_disconnect = None
        debug.write("Server unused. Disconnecting devices.", 0)
        for _dev in lm.devices:
            _dev.disconnect()

    def remove_server(self):
        """ Shuts down server and cleans resources """
        debug.write("Closing down server and lights.", 0)
        lm.set_skip_time_check()
        lm.set_colors([LIGHT_OFF] * len(lm.devices))
        lm.set_mode(False,True)
        #lm.run()
        self.sock.close()
        if not self.sched_disconnect.empty():
            self.sched_disconnect.cancel(self.scheduled_disconnect)
        self.disconnect_devices()

    def _validate_and_execute_req(self, args):
        debug.write("Validating arguments", 0)
        if args["reset_location_data"]:
            #TODO eventually add training data cleanup
            os.remove("./dnn/train.log")
            debug.write("Purged location and RTT data", 0)
        if args["hexvalues"] and (args["playbulb"] or args["milight"] or args["decora"]
                                  or args["meross"]):
            debug.write("Got color hexvalues for milights and/or playbulbs \
                                   and/or other devices in the same request, which is not \
                                   supported. Use '{} -h' for help. Quitting".format(sys.argv[0]),
                                 2)
            return     
        if len(args["hexvalues"]) != len(lm.devices) and not any([args["notime"], args["off"], args["on"], 
                                                                  args["playbulb"], args["milight"], 
                                                                  args["toggle"], args["decora"], 
                                                                  args["preset"], args["restart"],
                                                                  args["meross"]]):
            debug.write("Got {} color hexvalues, {} expected. Use '{} -h' for help. Quitting" \
                                  .format(len(args["hexvalues"]), len(lm.devices), sys.argv[0]), 2)
            return
        if args["priority"]:
            lm.priority = args["priority"]
        if args["hexvalues"]:
            debug.write("Received color hexvalues length {} for {} devices" \
                                  .format(len(args["hexvalues"]), len(lm.devices)), 0)
            lm.set_colors(args["hexvalues"])
        else:
            if args["set_mode_for_devid"] is not None:
                try:
                    debug.write("Received mode change request for devid {}".format(args["set_mode_for_devid"]), 0)
                    lm.set_mode_for_device(args["auto_mode"], args["set_mode_for_devid"])
                except KeyError:
                    debug.write("Devid {} does not exist".format(args["set_mode_for_devid"]), 1)
                return
            if args["playbulb"] is not None:
                debug.write("Received playbulb change request", 0)
                if not lm.set_typed_colors(args["playbulb"], Playbulb.Playbulb):
                    return
            if args["milight"] is not None:
                debug.write("Received milight change request", 0)
                if not lm.set_typed_colors(args["milight"], Milight.Milight):
                    return
            if args["decora"] is not None:
                debug.write("Received decora change request", 0)
                if not lm.set_typed_colors(args["decora"], DecoraSwitch.DecoraSwitch):
                    return
            if args["meross"] is not None:
                debug.write("Received meross change request", 0)
                if not lm.set_typed_colors(args["meross"], MerossSwitch.MerossSwitch):
                    return
            if args["preset"] is not None:
                debug.write("Received change to preset [{}] request".format(args["preset"]), 0)
                try:
                    lm.set_colors(self.config["PRESETS"][args["preset"]].split(','))
                    args["auto_mode"] = self.config["PRESETS"].getboolean("AUTOMATIC_MODE")
                except:
                    debug.write("Preset {} not found in play.ini. Quitting.".format(args["preset"]), 3)
                    return                       
            if args["off"]:
                debug.write("Received OFF change request", 0)
                lm.set_colors([LIGHT_OFF] * len(lm.devices))
            if args["on"]:
                debug.write("Received ON change request", 0)
                lm.set_colors([LIGHT_ON] * len(lm.devices))
            if args["restart"]:
                debug.write("Received RESTART change request", 0)
                if not lm.set_typed_colors(2, GenericOnOff.GenericOnOff):
                    return
            if args["toggle"]:
                debug.write("Received TOGGLE change request", 0)
                lm.set_colors(lm.get_toggle())
        if args["notime"] or args["off"]:
            lm.set_skip_time_check()
        if args["group"] is not None:
            lm.get_group(args["group"])
        debug.write("Arguments are OK", 0)
        lm.set_mode(args["auto_mode"], args["reset_mode"])
        lm.run(args["delay"])
        return

    def _sanitize(self, args):
        if "hexvalues" not in args:
            args["hexvalues"] = []
        if "off" not in args:
            args["off"] = False
        if "on" not in args:
            args["on"] = False
        if "restart" not in args:
            args["restart"] = False
        if "toggle" not in args:
            args["toggle"] = False
        if "playbulb" not in args:
            args["playbulb"] = None
        if "milight" not in args:
            args["milight"] = None
        if "decora" not in args:
            args["decora"] = None
        if "meross" not in args:
            args["meross"] = None
        if "notime" not in args:
            args["notime"] = False
        if "delay" not in args:
            args["delay"] = None
        if "priority" in args and args["priority"] is None:
            args["priority"] = 1
        if "priority" not in args:
            args["priority"] = 1
        if "preset" not in args:
            args["preset"] = None
        if "group" not in args:
            args["group"] = None
        if "manual_mode" not in args:
            args["manual_mode"] = False
        if "reset_mode" not in args:
            args["reset_mode"] = False
        if "set_mode_for_devid" not in args:
            args["set_mode_for_devid"] = None
        if "reset_location_data" not in args:
            args["reset_location_data"] = False
        if type(args["playbulb"]).__name__ == "str":
            debug.write('Converting values to lists for playbulb', 0)
            args["playbulb"] = args["playbulb"].replace("'", "").split(',')
        if type(args["milight"]).__name__ == "str":
            debug.write('Converting values to lists for milight', 0)
            args["milight"] = args["milight"].replace("'", "").split(',')
        if type(args["decora"]).__name__ == "str":
            debug.write('Converting values to lists for decora', 0)
            args["decora"] = args["decora"].replace("'", "").split(',')
        if type(args["meross"]).__name__ == "str":
            debug.write('Converting values to lists for meross', 0)
            args["meross"] = args["meross"].replace("'", "").split(',')
        return args


class IFTTTServer(BaseHTTPRequestHandler):
    def _set_response(self):
        self.send_response(200)
        self.send_header('Content-type', 'x-www-form-urlencoded')
        self.end_headers()

    def do_GET(self):
        self._set_response()

    def do_POST(self):
        config = configparser.ConfigParser()
        config.read('play.ini')
        """ Receives and handles POST request """
        SALT = config["IFTTT"]["SALT"]
        debug.write('[IFTTTServer] Getting request', 0)
        content_length = int(self.headers['Content-Length']) # <--- Gets the size of data
        postvars = urllib.parse.parse_qs(self.rfile.read(content_length), keep_blank_values=1)
        has_delayed_action = False
        self._set_response()
        try: 
            #TODO rewrite this more elegantly
            action = postvars[b'preaction'][0].decode('utf-8')
            post_action = postvars[b'postaction'][0].decode('utf-8')
            delay = int(postvars[b'delay'][0].decode('utf-8'))*60-5
            has_delayed_action = True
        except KeyError as ex:
            action = postvars[b'action'][0].decode('utf-8')
        _hash = postvars[b'hash'][0].decode('utf-8')

        if _hash == hashlib.sha512(bytes(SALT.encode('utf-8') + action.encode('utf-8'))).hexdigest():
            debug.write('IFTTTServer running action : {}'.format(action), 0)
            if action in config["IFTTT"]:
                debug.write('[IFTTTServer] Running action : {}'.format(config["IFTTT"][action]), 0)
                os.system("./playclient.py " + config["IFTTT"][action])
            else:
                #
                # Complex actions should be hardcoded here if needed
                #
                debug.write('[IFTTTServer] Unknown action : {}'.format(action), 1)
            time.sleep(5)
            if has_delayed_action:
                debug.write('IFTTTServer will run action {} in {} seconds'.format(post_action, delay+5), 0)
                if post_action in config["IFTTT"]:
                    os.system("./playclient.py --delay {} {}".format(delay, config["IFTTT"][post_action]))
                else:
                    #
                    # Complex delayed actions should be hardcoded here if needed
                    #
                    debug.write('[IFTTTServer] Unknown action : {}'.format(post_action), 1)
        else:
            debug.write('[IFTTTServer] Got unwanted request with action : {}'.format(action), 1)



class DFServer(BaseHTTPRequestHandler):
    def _set_response(self):
        self.send_response(200)
        self.send_header('Content-type', 'x-www-form-urlencoded')
        self.end_headers()

    def do_GET(self):
        self._set_response()

    def do_POST(self):
        config = configparser.ConfigParser()
        config.read('play.ini')
        """ Receives and handles POST request """
        debug.write('[DialogFlowServer] Getting request', 0)
        data_string = self.rfile.read(int(self.headers['Content-Length']))
        request = json.loads(data_string.decode('UTF-8'))
        self._set_response()
        action = request['queryResult']['parameters']['LightserverAction']
        groups = request['queryResult']['parameters']['LightserverGroups']
        if config['DIALOGFLOW'].getboolean('AUTOMATIC_MODE'):
            request = "./playclient.py --{} --auto-mode --notime --group {}".format(action, ' '.join(groups))
        else:
            request = "./playclient.py --{} --notime --group {}".format(action, ' '.join(groups))

        debug.write('[DialogFlowServer] Running detected request: {}'.format(request), 0)
        os.system(request)


class DeviceManager(object):
    """ Methods for instanciating and managing devices """
    def __init__(self, config=None):
        self.config = config
        self.devices = []
        i = 0
        while True:
            try:
                if self.config["DEVICE"+str(i)]["TYPE"] in getDevices():
                    _module = __import__("devices." + self.config["DEVICE"+str(i)]["TYPE"])
                    #TODO Needed twice ? looks unpythonic
                    _class = getattr(_module,self.config["DEVICE"+str(i)]["TYPE"])
                    _class = getattr(_class,self.config["DEVICE"+str(i)]["TYPE"])
                    self.devices.append(_class(i, self.config))
                else:
                    debug.write('Unsupported device type {}' \
                                          .format(self.config["DEVICE"+str(i)]["TYPE"]), 1)
            except KeyError:
                break
            i = i + 1
        self.skip_time = False
        self.serverwide_skip_time = False
        self.lastupdate = None
        self.get_event_time()
        self.queue = queue.Queue()
        self.colors = ["-1"] * len(self.devices)
        self.delays = [0] * len(self.devices)
        self.states = self.get_state()
        debug.write("Got initial device states {}".format(self.states), 0)
        self.set_lock(0)
        self.lockcount = 0
        self.priority = 0
        self.threaded = False
        self.light_threads = [None] * len(self.devices)
        self.light_pool = None

    def start_threaded(self):
        """ Enables multithreaded light change requests """
        self.threaded = True
        self.light_pool = ThreadPool(processes=4)

    def set_skip_time_check(self, serverwide=False):
        """ Enables skipping time check """
        if serverwide:
            debug.write("Skipping time check for all requests", 0)
            self.serverwide_skip_time = True
        else:
            debug.write("Skipping time check this time", 0)
            for _dev in self.devices:
                _dev.skip_time = True

    def set_colors(self, color):
        """ Setter function for color request. Required. """
        self.colors = color

    def set_mode(self, auto_mode, reset_mode):
        for _cnt, device in enumerate(self.devices):
            if self.colors[_cnt] != LIGHT_SKIP:
                self.devices[_cnt].request_auto_mode = auto_mode
                self.devices[_cnt].reset_mode = reset_mode

    def set_mode_for_device(self, auto_mode, devid):
        """ Used by the webserver to switch device modes one by one """
        self.devices[devid].auto_mode = auto_mode

    def get_group(self, group):
        """ Gets devices from a specific group for the light change """
        for _cnt, device in enumerate(self.devices):
            if group is not None and set(group).issubset(device.group):
                continue
            debug.write("Skipping device {} as it does not belong in the {} group(s)" \
                        .format(device.device, group), 0)
            self.colors[_cnt] = LIGHT_SKIP

    def get_toggle(self):
        """ Toggles the devices on/off """
        colors = [LIGHT_ON] * len(lm.devices)
        i = 0
        for color in self.get_state():
            if color != LIGHT_OFF:
                colors = [LIGHT_OFF] * len(lm.devices)
            i = i+1
        return colors

    def set_typed_colors(self, colorargs, atype):
        """ Gets devices of a specific  type for the light change """
        self.colors = ["-1"] * len(self.devices)
        cvals = self._get_type_index(atype)
        if len(colorargs) == 1 and cvals[0] > 1:
            # Allow a single value to be repeated to n devices
            debug.write("Expanding color {} to {} devices." \
                                  .format(len(colorargs), cvals[0]), 0)
            colorargs = [colorargs[0]] * cvals[0]
        if cvals[0] != len(colorargs):
            debug.write("Received color hexvalues length {} for {} devices. Quitting" \
                                  .format(len(colorargs), cvals[0]), 2)
            return False
        self.colors[cvals[1]:cvals[1]+cvals[0]] = colorargs

        return True

    def run(self, delay=None):
        """ Validates the request and runs the light change """
        if delay is not None:
            debug.write("Delaying request for {} seconds".format(delay), 0)
            time.sleep(delay)
        if self.check_event_time():
            self.queue.put(self.colors)
            #TODO Manage locking out when the run thread hangs
            debug.write("Locked status: {}".format(self.locked), 0)
            if not self.locked or self.lockcount == 2:
                self._set_lights()
            else:
                self.lockcount = self.lockcount + 1

    def get_descriptions(self, as_list = False):
        """ Getter for configured devices descriptions """
        desclist = []
        desctext = ""
        i = 1
        for obj in self.devices:
            #TODO make this dynamic
            if isinstance(obj, (Playbulb.Playbulb, Milight.Milight, DecoraSwitch.DecoraSwitch, 
                                GenericOnOff.GenericOnOff, MerossSwitch.MerossSwitch)):
                desctext += str(i) + " - " + obj.descriptions() + "\n"
                if as_list:
                    desclist.append(obj.descriptions())
            else:
                desctext += str(i) + " - " + "Unknown device type\n"
                if as_list:
                    desclist.append("Unknown device type")
            i += 1
        if as_list:
            return desclist
        return desctext

    def get_types(self):
        typelist = []
        for obj in self.devices:
            typelist.append(obj.__class__.__name__)
        return typelist

    def get_modes(self):
        modelist = []
        for obj in self.devices:
            modelist.append(obj.auto_mode)
        return modelist

    def get_event_time(self):
        if self.lastupdate != datetime.date.today():
            self.lastupdate = datetime.date.today()
            if str(self.config['SERVER']['EVENT_HOUR']) != "auto":
                self.lastupdate = datetime.date.today()
                self.starttime = datetime.datetime.strptime(self.config['SERVER']['EVENT_HOUR'],'%H:%M').time()
            else:
                self.lastupdate = datetime.date.today()
                self.starttime = self._update_sunset_time(self.config['SERVER']['EVENT_LOCALIZATION'])
                if not self.serverwide_skip_time:
                    debug.write("Event time set as sunset time: {}".format(self.starttime), 0)
        return self.starttime

    def check_event_time(self):
        self.get_event_time()
        if self.serverwide_skip_time:
            for _dev in self.devices:
                _dev.set_skip_time()
            return True
        if datetime.time(6, 00) < datetime.datetime.now().time() < self.starttime:
            for _dev in self.devices:
                if _dev.skip_time:
                    debug.write("Not all devices will be changed. Light changes begins at {}"
                                .format(self.starttime), 0)
                    return True
            debug.write("Too soon to change devices. Light changes begins at {}"
                        .format(self.starttime), 0)
            return False
        else:
            for _dev in self.devices:
                _dev.set_skip_time()
        return True

    def set_lock(self, is_locked):
        """ Locks the light change request """
        self.locked = is_locked

    def get_state(self, devid=None):
        """ Getter for configured devices actual colors """
        states = [None] * len(self.devices)
        for _cnt, dev in enumerate(self.devices):
            if devid is not None and devid != _cnt:
                continue
            states[_cnt] = dev.get_state()
        if devid is not None:
            return states[devid]
        return states

    def set_light_stream(self, devid, color, is_group):
        """ Simplified function for quick, streamed light change requests """
        if is_group:
            for device in self.devices:
                if device.group == devid:
                    cnt = 0
                    _color = device.convert(color)
                    while True:
                        if cnt == 4:
                            break
                        if device.run(_color, 3):
                            break
                        time.sleep(0.3)
                        cnt = cnt + 1
        else:
            cnt = 0
            _color = device.convert(color)
            while True:
                if cnt == 4:
                    break
                if self.devices[devid].run(_color, 3):
                    break
                time.sleep(0.3)
                cnt = cnt + 1
        self.reinit()

    def reinit(self):
        """ Resets the Success bool to False """
        i = 0
        while i < len(self.devices):
            self.devices[i].reinit()
            i += 1

    def _decode_colors(self, colors):
        self.delays = [0] * len(self.devices)
        for _cnt, _col in enumerate(colors):
            if re.match("[0-9a-fA-F]+d[0-9]+", _col) is not None:
                _vals = _col.split("d")
                colors[_cnt] = _vals[0]
                self.delays[_cnt] = int(_vals[1])
        return colors

    def _set_lights(self):
        debug.write("Running a change of lights (priority level: {})..." \
                              .format(self.priority), 0)
        try:
            self.lockcount = 0
            firstran = False
            try:
                while not self.queue.empty():
                    colors = None
                    if firstran:
                        debug.write("Getting remainder of queue", 0)
                        self.reinit()
                    colors = self._decode_colors(self.queue.get()) #TODO Check performance
                    if all(c == LIGHT_SKIP for c in colors):
                        debug.write("All device requests skipped", 0)
                        return                
                    debug.write("Changing colors to {} from state {}" \
                                .format(colors, self.states), 0)
                    self.set_lock(1)
                    i = 0
                    tries = 0
                    firstran = True

                    while i < len(self.devices):
                        if not self.devices[i].success:
                            _color = self.devices[i].convert(colors[i])

                            if _color != LIGHT_SKIP:
                                self.states[i] = self.get_state(i)
                                if _color != self.states[i]:
                                    debug.write(("DEVICE: {}, REQUESTED COLOR: {} "
                                                  "FROM STATE: {}, PRIORITY: {}, AUTO: {}")
                                                  .format(self.devices[i].device,
                                                          _color, self.states[i],
                                                          self.devices[i].priority,
                                                          self.devices[i].auto_mode),
                                                  0)
                            if self.threaded:
                                if not self.queue.empty():
                                    break

                                self.light_threads[i] = self.light_pool.apply_async(self._set_device,
                                                                                    args=(i, _color, 
                                                                                          self.priority,
                                                                                          self.delays[i]))
                            else:
                                self._set_device(i, _color, self.priority, self.delays[i])
                        i += 1

                        if i == len(self.devices):
                            if self.threaded:
                                debug.write("Awaiting results", 0)
                                for _cnt, _thread in enumerate(self.light_threads):
                                    if not self.queue.empty():
                                        continue
                                    if _thread is not None:
                                        try:
                                            if _thread.get(self.delays[_cnt] + 5) is not None:
                                                i = 0
                                        except:
                                            i = 0
                                tries = tries + 1
                                if tries == 5:
                                    break
                            else:
                                for _cnt, _dev in enumerate(self.devices):
                                    if not self.queue.empty():
                                        continue
                                    self.states[_cnt] = self.get_state(_cnt)
                                    if self.devices[_cnt].convert(colors[_cnt]) != self.states[_cnt]:
                                        i = 0
                                tries = tries + 1
                                if tries == 5:
                                    break

            except queue.Empty:
                debug.write("Nothing in queue", 0)
                pass

            finally:
                debug.write("Clearing up light change queues.", 0)
                if colors:
                    self.queue.task_done()
                self.states = self.get_state()

        except Exception as ex:
            debug.write('Unhandled exception of type {}: {}, {}'
                                  .format(type(ex), ex, 
                                          ''.join(traceback.format_tb(ex.__traceback__))), 2)

        finally:
            self.reinit()
            self.set_lock(0)

        debug.write("Change of lights completed.", 0)

    def _set_device(self, count, color, priority, delay):
        #TODO Find a way to make the delays non blocking
        if delay is not 0:
            #TODO return result of device.run to the server or rewrite this differently
            debug.write("Delaying for {} seconds request for device: {}"
                        .format(delay, self.devices[count].description), 0) 
            s = sched.scheduler(time.time, time.sleep)
            s.enter(delay, 1, self.devices[count].run, (color, priority,))
            s.run()
        else:
            return self.devices[count].run(color, priority)

    def _get_type_index(self, atype):
        # TODO This should not depend on an ordered set of devices
        i = 0
        count = 0
        firstindex = 0
        for obj in self.devices:
            if isinstance(obj, atype):
                if count == 0: 
                    firstindex = i
                count += 1
            i += 1
        if count == 0:
            raise Exception('Invalid bulb type given. Quitting')
        return [count, firstindex]

    def _update_sunset_time(self, localization):
        p1 = subprocess.Popen('./scripts/sunset.sh %s' % str(localization), stdout=subprocess.PIPE, \
                              shell=True)
        (output,_) = p1.communicate()
        p1.wait()
        try:
            _time = datetime.datetime.strptime(output.rstrip().decode('UTF-8'),'%H:%M').time()
        except ValueError:
            debug.write("Connection error to the sunset time server. Falling back to 18:00.", 1)             
            _time = datetime.datetime.strptime("18:00",'%H:%M').time()
        return _time


class runIFTTTServer(threading.Thread):
    def __init__(self, port):
        threading.Thread.__init__(self)
        self.port = port
        self.running = True

    def run(self):
        debug.write('[IFTTTServer] Getting lightserver POST requests on port {}' \
                    .format(self.port), 0)
        httpd = HTTPServer(('', self.port), IFTTTServer)
        try:
            while self.running:
                httpd.handle_request()
        finally:
            httpd.server_close()
            debug.write('[IFTTTServer] Stopped.', 0)
            return

    def stop(self):
        debug.write('[IFTTTServer] Stopping.', 0)
        self.running = False
        # Needs a last call to shut down properly
        _r = requests.get("http://localhost:{}/".format(self.port))


class runDFServer(threading.Thread):
    def __init__(self, config):
        threading.Thread.__init__(self)
        self.port = config['SERVER'].getint('VOICE_SERVER_PORT')
        self.key = config['DIALOGFLOW']['DIALOGFLOW_HTTPS_CERTS_KEY']
        self.cert = config['DIALOGFLOW']['DIALOGFLOW_HTTPS_CERTS_CERT']
        self.running = True

    def run(self):
        debug.write('[DialogFlowServer] Getting lightserver POST requests on port {}' \
                    .format(self.port), 0)
        httpd = HTTPServer(('', self.port), DFServer)
        httpd.socket = ssl.wrap_socket(httpd.socket, 
                keyfile=self.key, 
                certfile=self.cert, server_side=True)
        try:
            while self.running:
                httpd.handle_request()
        finally:
            httpd.server_close()
            debug.write('[DialogFlowServer] Stopped.', 0)
            return

    def stop(self):
        debug.write('[DialogFlowServer] Stopping.', 0)
        self.running = False
        # Needs a last call to shut down properly
        _r = requests.get("http://localhost:{}/".format(self.port))


class runDetectorServer(threading.Thread):
    def __init__ (self, config):
        threading.Thread.__init__(self)
        self.config = config
        self.stopevent = threading.Event()
        self.DEVICE_STATE_LEVEL = [0]*len(config['DETECTOR']['TRACKED_IPS'].split(","))
        self.DEVICE_STATE_MAX = self.config['DETECTOR'].getint('MAX_STATE_LEVEL')
        self.DEVICE_STATUS = [0]*len(self.config['DETECTOR']['TRACKED_IPS'].split(","))
        self.FIND3_SERVER= self.config['DETECTOR'].getboolean('FIND3_SERVER_ENABLE')
        self.DETECTOR_START_HOUR = datetime.datetime.strptime(self.config['DETECTOR']['START_HOUR'],'%H:%M').time()
        self.DETECTOR_END_HOUR = datetime.datetime.strptime(self.config['DETECTOR']['END_HOUR'],'%H:%M').time()
        self.status = 0
        self.delayed_start = 0

    def run(self):
        self.first_detect()
        while not self.stopevent.is_set():
            self.detect_devices()
            self.stopevent.wait(int(self.config['DETECTOR']['PING_FREQ_SEC']))
        return

    def stop(self):
        self.stopevent.set()

    def first_detect(self):
        debug.write("[Detector] Starting ping-based device detector", 0)

        if self.FIND3_SERVER:
            debug.write("[Detector] Starting FIND3 localization server", 0)
            TRACKED_FIND3_DEVS = self.config['DETECTOR']['FIND3_TRACKED_DEVICES'].split(",")
            TRACKED_FIND3_TIMES = [0]*len(TRACKED_FIND3_DEVS)
            TRACKED_FIND3_LOCAL = [""]*len(TRACKED_FIND3_DEVS)
            for _cnt, _dev in enumerate(TRACKED_FIND3_DEVS):
                # Get last update times
                if _dev != "_":
                    _r = requests.get("http://{}/api/v1/location/{}/{}".format(self.config['DETECTOR']['FIND3_SERVER_URL'],
                                                                               self.config['DETECTOR']['FIND3_FAMILY_NAME'],
                                                                               _dev))
                    TRACKED_FIND3_TIMES[_cnt] = _r.json()['sensors']['t']

        for _cnt, device in enumerate(self.config['DETECTOR']['TRACKED_IPS'].split(",")):
            if int(os.system("ping -c 1 -W 1 {} >/dev/null".format(device))) == 0:
                self.DEVICE_STATE_LEVEL[_cnt] = self.DEVICE_STATE_MAX
                self.DEVICE_STATUS[_cnt] = 1
            else:
                self.DEVICE_STATE_LEVEL[_cnt] = 0
                self.DEVICE_STATUS[_cnt] = 0
        debug.write("[Detector] Got initial states {} and status {}".format(self.DEVICE_STATE_LEVEL, self.status), 0)

        if self.DETECTOR_START_HOUR > datetime.datetime.now().time() or \
           self.DETECTOR_END_HOUR < datetime.datetime.now().time():
               debug.write("[Detector] Standby. Running between {} and {}".format(self.DETECTOR_START_HOUR, 
                                                                                  self.DETECTOR_END_HOUR), 0)

    def detect_devices(self):
        if self.DETECTOR_START_HOUR > datetime.datetime.now().time() or \
           self.DETECTOR_END_HOUR < datetime.datetime.now().time():
            time.sleep(30)
            return 
        EVENT_TIME = lm.get_event_time()
        for _cnt, device in enumerate(self.config['DETECTOR']['TRACKED_IPS'].split(",")):
            #TODO Maintain the two pings requirement for status change ?
            if int(os.system("ping -c 1 -W 1 {} >/dev/null".format(device))) == 0:
                if self.DEVICE_STATE_LEVEL[_cnt] == self.DEVICE_STATE_MAX and self.DEVICE_STATUS[_cnt] == 0:
                    debug.write("[Detector] DEVICE {} CONnected".format(device), 0)
                    self.DEVICE_STATUS[_cnt] = 1
                elif self.DEVICE_STATE_LEVEL[_cnt] != self.DEVICE_STATE_MAX:
                    self.DEVICE_STATE_LEVEL[_cnt] = self.DEVICE_STATE_LEVEL[_cnt] + 1
                if self.FIND3_SERVER and TRACKED_FIND3_DEVS[_cnt] != "_":
                    _r = requests.get("http://{}/api/v1/location/{}/{}".format(self.config['DETECTOR']['FIND3_SERVER_URL'],
                                                                               self.config['DETECTOR']['FIND3_FAMILY_NAME'],
                                                                               TRACKED_FIND3_DEVS[_cnt]))
                    if TRACKED_FIND3_TIMES[_cnt] != _r.json()['sensors']['t'] and \
                       TRACKED_FIND3_LOCAL[_cnt] != _r.json()['analysis']['guesses'][0]['location']:
                        if _r.json()['analysis']['guesses'][0]['location'] in self.config['FIND3-PRESETS']:
                            if self.config['FIND3-PRESETS'].getboolean('AUTOMATIC_MODE'):
                                os.system("./playclient.py --auto-mode " + self.config['FIND3-PRESETS'][_r.json()['analysis']['guesses'][0]['location']])
                            else:
                                os.system("./playclient.py " + self.config['FIND3-PRESETS'][_r.json()['analysis']['guesses'][0]['location']])
                            debug.write("[Detector-FIND3] Device {} found in '{}'. Running change of lights."
                                        .format(TRACKED_FIND3_DEVS[_cnt], 
                                                _r.json()['analysis']['guesses'][0]['location']), 0)

                        else:
                            debug.write("[Detector-FIND3] Device {} found in '{}' but preset is not self.configured."
                                        .format(TRACKED_FIND3_DEVS[_cnt], 
                                                _r.json()['analysis']['guesses'][0]['location']), 0)
                        if TRACKED_FIND3_LOCAL[_cnt]+"-off" in self.config['FIND3-PRESETS']:
                            if self.config['FIND3-PRESETS'].getboolean('AUTOMATIC_MODE'):
                                os.system("./playclient.py --auto-mode " + self.config['FIND3-PRESETS'][TRACKED_FIND3_LOCAL[_cnt]+"-off"])
                            else:
                                os.system("./playclient.py " + self.config['FIND3-PRESETS'][TRACKED_FIND3_LOCAL[_cnt]+"-off"])
                            debug.write("[Detector-FIND3] Device {} left '{}'. Running change of lights."
                                        .format(TRACKED_FIND3_DEVS[_cnt], 
                                                TRACKED_FIND3_LOCAL[_cnt]), 0)
                        TRACKED_FIND3_TIMES[_cnt] = _r.json()['sensors']['t']
                        TRACKED_FIND3_LOCAL[_cnt] = _r.json()['analysis']['guesses'][0]['location']
            else:
                if self.DEVICE_STATE_LEVEL[_cnt] == 0 and self.DEVICE_STATUS[_cnt] == 1:
                    debug.write("[Detector] DEVICE {} DISconnected".format(device), 0)
                    self.DEVICE_STATUS[_cnt] = 0
                elif self.DEVICE_STATE_LEVEL[_cnt] != 0:
                    # Decrease state level down to zero (OFF)
                    self.DEVICE_STATE_LEVEL[_cnt] = self.DEVICE_STATE_LEVEL[_cnt] - 1

        if self.status == 1 and all(s == 0 for s in self.DEVICE_STATE_LEVEL):
            debug.write("[Detector] STATE changed to {} and DELAYED_START {}, turned off" \
                                  .format(self.DEVICE_STATE_LEVEL, self.delayed_start), 0)
            os.system('./playclient.py --auto-mode --off --notime --priority 3')
            self.status = 0
            self.delayed_start = 0
        if datetime.datetime.now().time() == EVENT_TIME and self.delayed_start == 1:
            debug.write("[Detector] DELAYED STATE with actual state {}, turned on".format(self.DEVICE_STATE_LEVEL), 
                                                                                          0)
            os.system('./playclient.py --auto-mode --on --group passage')
            self.delayed_start = 0
            self.status = 1  
        if self.DEVICE_STATE_MAX in self.DEVICE_STATE_LEVEL and self.delayed_start == 0:
            if datetime.datetime.now().time() < EVENT_TIME:
                debug.write("[Detector] Scheduling state change, with actual state {}" \
                                      .format(self.DEVICE_STATE_LEVEL), 0)
                self.delayed_start = 1
                self.status = 0
        if self.DEVICE_STATE_MAX in self.DEVICE_STATE_LEVEL and self.status == 0 and datetime.datetime.now().time() \
           >= EVENT_TIME:
            debug.write("[Detector] STATE changed to {}, turned on".format(self.DEVICE_STATE_LEVEL), 0)
            os.system('./playclient.py --auto-mode --on --group passage')
            self.status = 1
            self.delayed_start = 0
        if all(s == 0 for s in self.DEVICE_STATE_LEVEL) and self.status == 0 and self.delayed_start == 1:
            debug.write("[Detector] Aborting light change, with actual state {}" \
                                      .format(self.DEVICE_STATE_LEVEL), 0)
            self.delayed_start = 0


class WebServerHandler(SimpleHTTPRequestHandler):
    def __init__(self, lmhost, lmport, *args, **kwargs):
        self.lmhost = lmhost
        self.lmport = lmport
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        return SimpleHTTPRequestHandler.translate_path(self, './web' + path)

    def _set_response(self):
        self.send_response(200)
        self.send_header('Content-type', 'x-www-form-urlencoded')
        self.end_headers()

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        postvars = urllib.parse.parse_qs(self.rfile.read(content_length), keep_blank_values=1)
        request = bool(postvars[b'request'][0].decode('utf-8'))
        reqtype = int(postvars[b'reqtype'][0].decode('utf-8'))
        self._set_response()
        response = BytesIO()
        if request:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((self.lmhost, self.lmport))
            if reqtype == 1:
                try:
                    s.sendall("0008".encode('utf-8'))
                    s.sendall("getstate".encode('utf-8'))
                    data = s.recv(1024)
                    if data:
                        response.write(data)
                finally:
                    s.close()
            if reqtype == 2:
                devid = str(postvars[b'devid'][0].decode('utf-8'))
                value = str(postvars[b'value'][0].decode('utf-8'))
                skiptime = postvars[b'skiptime'][0].decode('utf-8') in ['true', True]
                try:
                    s.sendall("0008".encode('utf-8'))
                    s.sendall("setstate".encode('utf-8'))
                    s.sendall(devid.zfill(3).encode('utf-8'))
                    s.sendall(value.zfill(8).encode('utf-8'))
                    if skiptime:
                        s.sendall("1".encode('utf-8'))
                    else:
                        s.sendall("0".encode('utf-8'))
                    data = s.recv(1)
                    if data:
                        response.write(data)
                finally:
                    s.close()
            if reqtype == 3:
                cmode = postvars[b'mode'][0].decode('utf-8') in ['true', True]
                devid = str(postvars[b'devid'][0].decode('utf-8'))
                try:
                    s.sendall("0007".encode('utf-8'))
                    s.sendall("setmode".encode('utf-8'))
                    s.sendall(devid.zfill(3).encode('utf-8'))
                    if cmode:
                        s.sendall("1".encode('utf-8'))
                    else:
                        s.sendall("0".encode('utf-8'))
                    data = s.recv(1)
                    if data:
                        response.write(data)
                finally:
                    s.close()

        else:
            response.write("No request".encode("UTF-8"))
        self.wfile.write(response.getvalue())


class runWebServer(threading.Thread):
    def __init__(self, port, config):
        threading.Thread.__init__(self)
        self.port = port
        self.config = config
        self.running = True

    def run(self):
        debug.write("[Webserver] Starting control webserver on port {}".format(self.port), 0)
        socketserver.TCPServer.allow_reuse_address = True
        _handler = partial(WebServerHandler, self.config['SERVER']['HOST'], int(self.config['SERVER'].getint('PORT')))
        httpd = socketserver.TCPServer(("", self.port), _handler)

        try:
            while self.running:
                httpd.handle_request()
        finally:
            httpd.server_close()
            debug.write("[Webserver] Stopped.", 0)
            return

    def stop(self):
        debug.write("[Webserver] Stopping.", 0)
        self.running = False
        # Needs a last call to shut down properly
        _r = requests.get("http://localhost:{}/".format(self.port))


def runServer():
    HomeServer(lm).listen()

""" Script executed directly """
if __name__ == "__main__":
    PLAYCONFIG = configparser.ConfigParser()
    PLAYCONFIG.read('play.ini')
    lm = DeviceManager(PLAYCONFIG)

    parser = argparse.ArgumentParser(description='BLE light bulbs manager script', epilog=lm.get_descriptions(),
                                     formatter_class=RawTextHelpFormatter)
    parser.add_argument('hexvalues', metavar='N', type=str, nargs="*",
                        help='color hex values for the lightbulbs (see list below)')
    parser.add_argument('--playbulb', metavar='P', type=str, nargs="*", help='Change playbulbs colors only')
    parser.add_argument('--milight', metavar='M', type=str, nargs="*", help='Change milights colors only')
    parser.add_argument('--decora', metavar='M', type=str, nargs="*", help='Change decora colors only')
    parser.add_argument('--meross', metavar='M', type=str, nargs="*", help='Change meross states only')
    parser.add_argument('--priority', metavar='prio', type=int, nargs="?", default=1,
                        help='Request priority from 1 to 3')
    parser.add_argument('--preset', metavar='preset', type=str, nargs="?", default=None,
                        help='Apply light actions from specified preset name defined in play.ini')
    parser.add_argument('--group', metavar='group', type=str, nargs="+", default=None,
                        help='Apply light actions on specified device group(s)')
    parser.add_argument('--notime', action='store_true', default=False,
                        help='Skip the time check and run the script anyways')
    parser.add_argument('--delay', metavar='delay', type=int, nargs="?", default=None,
                        help='Run the request after a given number of seconds')
    parser.add_argument('--on', action='store_true', default=False, help='Turn everything on')
    parser.add_argument('--off', action='store_true', default=False, help='Turn everything off')
    parser.add_argument('--restart', action='store_true', default=False, help='Restart generics')
    parser.add_argument('--toggle', action='store_true', default=False, help='Toggle all lights on/off')
    parser.add_argument('--server', action='store_true', default=False,
                        help='Start as a socket server daemon')
    parser.add_argument('--webserver', metavar='prio', type=int, nargs="?", default=0,
                        help='Starts a webserver at the given PORT')
    parser.add_argument('--voice', action='store_true', default=False,
                        help='Start a voice-assistant websocket receiver along with server')
    parser.add_argument('--detector', action='store_true', default=False,
                        help='Start a ping-based device detector (usually for mobiles)')
    parser.add_argument('--threaded', action='store_true', default=False,
                        help='Starts the server daemon with threaded light change requests')
    parser.add_argument('--stream-dev', metavar='str-dev', type=int, nargs="?", default=None,
                        help='Stream colors directly to device id')
    parser.add_argument('--stream-group', metavar='str-grp', type=str, nargs="?", default=None,
                        help='Stream colors directly to device group')
    parser.add_argument('--reset-mode', action='store_true', default=False,
                        help='Force light change (whatever the actual mode) and set back devices to AUTO mode')
    parser.add_argument('--reset-location-data', action='store_true', default=False,
                        help='Purge all RTT, locations and location training data (default: false)')
    parser.add_argument('--auto-mode', action='store_true', default=False,
                        help='(internal) Run requests for non-LIGHT_SKIP devices as AUTO mode (default: false)')
    parser.add_argument('--set-mode-for-devid', metavar='devid', type=int, nargs="?", default=None,
                        help='(internal) Force device# to change mode (as set by auto-mode)')

    args = parser.parse_args()

    if args.server and (args.playbulb or args.milight or args.decora or args.on
                        or args.off or args.toggle or args.stream_dev
                        or args.stream_group or args.preset or args.restart 
                        or args.meross):
        debug.write("You cannot start the daemon and send arguments at the same time. \
                              Quitting.", 2)
        sys.exit()

    voice_server = None
    if args.voice:
        voice_server = PLAYCONFIG['SERVER']['VOICE_SERVER_TYPE']
        if voice_server not in ['none', 'dialogflow', 'ifttt']:
            debug.write("Invalid voice assistant server type. Choose between none, dialogflow or ifttt. Quitting.", 2)
            sys.exit()
        if voice_server == 'none':
            voice_server = None

    if args.stream_dev and args.stream_group:
        debug.write("You cannot stream data to both devices and groups. Quitting.", 2)
        sys.exit()

    if args.reset_mode and args.auto_mode:
        DeviceManager.debugger("You should not set the mode to AUTO then reset it back to AUTO. Quitting.", 2)
        sys.exit()

    if args.server:
        if args.webserver is None:
            debug.write("You need to define a port for the webserver, using --webserver PORT. Quitting.", 2)
            sys.exit()
        if args.notime:
            lm.set_skip_time_check(True)
        if args.threaded:
            lm.start_threaded()
        if voice_server is not None:
            if voice_server == 'ifttt':
                ti = runIFTTTServer(PLAYCONFIG['SERVER'].getint('VOICE_SERVER_PORT'))
                ti.start()
            elif voice_server == 'dialogflow':
                ti = runDFServer(PLAYCONFIG)
                ti.start()
        if args.detector:
            td = runDetectorServer(PLAYCONFIG)
            td.start()
        if args.webserver != 0:
            tw = runWebServer(args.webserver,PLAYCONFIG)
            tw.start()
        runServer()
        if voice_server is not None:
            ti.stop()
            ti.join()
        if args.webserver != 0:
            tw.stop()
            tw.join()
        if args.detector:
            td.stop()
            td.join()

    elif args.stream_dev or args.stream_group:
        colorval = ""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((PLAYCONFIG['SERVER']['HOST'], int(PLAYCONFIG['SERVER'].getint('PORT'))))
        if args.stream_dev:
            s.sendall("0006".encode('utf-8'))
            s.sendall("stream".encode('utf-8'))
            s.sendall(('%04d' % args.stream_dev).encode('utf-8'))
            s.sendall(str(args.stream_dev).encode('utf-8'))
        else:
            s.sendall("0011".encode('utf-8'))
            s.sendall("streamgroup".encode('utf-8'))
            s.sendall(('%04d' % len(args.stream_group)).encode('utf-8'))
            s.sendall(args.stream_group.encode('utf-8'))
        while colorval != "quit":
            if args.stream_dev:
                colorval = input("Set device {} to colorvalue ('quit' to exit): " \
                                  .format(args.stream_dev))
            else:
                colorval = input("Set group '{}' to colorvalue ('quit' to exit): " \
                                  .format(args.stream_group))
            try:
                if colorval == "quit":
                    s.sendall("0008".encode('utf-8'))
                    s.sendall("nostream".encode('utf-8'))
                    break
                s.sendall(('%04d' % len(colorval)).encode('utf-8'))
                s.sendall(colorval.encode('utf-8'))
            except BrokenPipeError:
                if colorval != "quit":
                    s.close()
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.connect((PLAYCONFIG['SERVER']['HOST'], int(PLAYCONFIG['SERVER'].getint('PORT'))))
                    if args.stream_dev:
                        s.sendall("0006".encode('utf-8'))
                        s.sendall("stream".encode('utf-8'))
                        s.sendall(('%04d' % args.stream_dev).encode('utf-8'))
                        s.sendall(str(args.stream_dev).encode('utf-8'))
                    else:
                        s.sendall("0011".encode('utf-8'))
                        s.sendall("streamgroup".encode('utf-8'))
                        s.sendall(('%04d' % len(args.stream_group)).encode('utf-8'))
                        s.sendall(args.stream_group.encode('utf-8'))
                    s.sendall(('%04d' % len(colorval)).encode('utf-8'))
                    s.sendall(colorval.encode('utf-8'))
                    continue
        s.close()

    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((PLAYCONFIG['SERVER']['HOST'], int(PLAYCONFIG['SERVER'].getint('PORT'))))
        #TODO report connection errors or allow feedback response
        debug.write('Connecting with lightmanager daemon', 0)
        debug.write('Sending request: ' + json.dumps(vars(args)), 0)
        s.sendall("1024".encode('utf-8'))
        s.sendall(json.dumps(vars(args)).encode('utf-8'))
        s.close()

    sys.exit()
