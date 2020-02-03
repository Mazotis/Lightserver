#!/usr/bin/env python3
'''
    File name: updater.py
    Author: Maxime Bergeron
    Date last modified: 07/01/2020
    Python Version: 3.5

    The updater module for the homeserver
'''

import subprocess
from core.common import *
from threading import Thread, Event
from urllib.error import HTTPError
from urllib.request import urlopen


class updater(Thread):
    def __init__(self, dm):
        Thread.__init__(self)
        self.stopevent = Event()
        self.dm = dm
        self.init_from_config()

    def run(self):
        self.check_for_update()
        last_update = datetime.datetime.now().date()

        while not self.stopevent.is_set():
            actual_time = datetime.datetime.now().hour
            actual_date = datetime.datetime.now().date()

            if self.UPDATER_HOUR >= actual_time and \
                    actual_date != last_update:
                self.check_for_update()
                last_update = actual_date
            self.stopevent.wait(300)
        debug.write("Stopped.", 0, "UPDATER")
        return

    def check_for_update(self):
        debug.write("Checking for updates (actual version: {})...".format(
            VERSION), 0, "UPDATER")
        try:
            NEW_VERSION = urlopen(
                "https://raw.githubusercontent.com/Mazotis/Homeserver/master/VERSION").read().decode("UTF-8")
        except HTTPError:
            debug.write("Could not check version with the main Github server.", 1, "UPDATER")
            NEW_VERSION = VERSION
        if VERSION != NEW_VERSION:
            debug.write("A new version ({}) is available".format(
                NEW_VERSION), 0, "UPDATER")
            if self.AUTOMATIC_UPDATE:
                self.run_update()
        else:
            debug.write("Homeserver is up to date.", 0, "UPDATER")

    def run_update(self):
        debug.write("Fetching new version from git", 0, "UPDATER")
        try:
            _update = subprocess.Popen(
                "cd {}/.. && git fetch --all".format(CORE_DIR), shell=True, stdout=subprocess.PIPE)
            _update.wait()
            _update = subprocess.Popen(
                "cd {}/.. && git reset --hard origin/master".format(CORE_DIR), shell=True, stdout=subprocess.PIPE)
            _update.wait()
        except subprocess.CalledProcessError:
            debug.write(
                "Could not run updater. Make sure you have git-core installed.", 1, "UPDATER")
            return
        debug.write("Restarting the Homeserver main script", 0, "UPDATER")

        self.dm.shutdown_modules()
        python = sys.executable
        os.execl(python, python, *sys.argv)

    def stop(self):
        debug.write("Stopping.", 0, "UPDATER")
        self.stopevent.set()

    def init_from_config(self):
        self.config = getConfigHandler().set_section("UPDATER")
        self.UPDATER_HOUR = self.config.get_value('UPDATER_HOUR', int)
        self.AUTOMATIC_UPDATE = self.config.get_value('AUTOMATIC_UPDATE', bool)