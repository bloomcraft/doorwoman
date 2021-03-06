#!/usr/bin/python
# pip install rpi.gpio lockfile python-systemd

import RPi.GPIO as GPIO
import sys,time
import signal
import subprocess
import json
import threading
import syslog
import atexit
import logging
from systemd.journal import JournalHandler
import os
from lockfile.pidlockfile import PIDLockFile
from lockfile import AlreadyLocked

conf_dir = "./conf/"

#####
# Setup
#####
def initialize():
    # PID lockfile
    pidlock=PIDLockFile('/var/run/doorwoman/doorwoman.pid', timeout=-1)
    deal_with_locks(pidlock)
    # Logging
    global logger
    logger = logging.getLogger("Doorwoman")
    logger.setLevel(logging.INFO)
    logger.addHandler(JournalHandler())
    # Config
    read_configs()
    GPIO.setmode(GPIO.BCM)
    setup_output_GPIOs()
    setup_readers()
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)
    signal.signal(signal.SIGHUP, rehash)
    signal.signal(signal.SIGUSR1, sigusr1)
    logger.info("%s access control is online" % zone)

def deal_with_locks(pidlock):
    try:
        pidlock.acquire()
    except AlreadyLocked:
        try:
            os.kill(pidlock.read_pid(), 0)
            logger.warn('Process already running, goodbye!')
            exit(1)
        except OSError:  #No process with locked PID
            pidlock.break_lock()
            pidlock.acquire()
            logger.warn("Breaking stale lock")

def read_configs():
    global zone, users, config
    jzone = load_json(conf_dir + "zone.json")
    users = load_json(conf_dir + "users.json")
    config = load_json(conf_dir + "config.json")
    zone = jzone["zone"]

def load_json(filename):
    file_handle = open(filename)
    config = json.load(file_handle)
    file_handle.close()
    return config

def setup_output_GPIOs():
    zone_by_pin[config[zone]["latch_gpio"]] = zone
    init_GPIO(config[zone])

def init_GPIO(zone):
    GPIO.setup(zone["latch_gpio"], GPIO.OUT)
    GPIO.setup(zone["green"], GPIO.OUT)
    GPIO.setup(zone["beep"], GPIO.OUT)
    lock(zone["latch_gpio"], zone["green"], zone["beep"])

#####
# Signals/Teardown
#####
def cleanup():
    message = ""
    if zone:
        message = "%s " % zone
    message += "access control is going offline"
    logger.info(message)
    GPIO.setwarnings(False)
    GPIO.cleanup()

def rehash(signal, b):
    global users
    logger.info("Reloading access list")
    users = load_json(conf_dir + "users.json")

def sigterm(signal, b):
    sys.exit(0) # calls cleanup() via atexit

def sigusr1(signal, b):
    logger.info("Deus ex machina opened %s" % zone)
    unlock_briefly(config[zone])

#####
# Door control
#####
def lock(gpio, green_gpio, beep_gpio):
    GPIO.output(gpio, active(gpio)^1)
    GPIO.output(green_gpio, active(gpio))
    GPIO.output(beep_gpio, active(gpio))

def unlock(gpio, green_gpio, beep_gpio):
    GPIO.output(gpio, active(gpio))
    GPIO.output(green_gpio, active(gpio)^1)
    GPIO.output(beep_gpio, active(gpio)^1)

def active(gpio):
    zone = zone_by_pin[gpio]
    return config[zone]["unlock_value"]

def unlock_briefly(zone):
    unlock(zone["latch_gpio"], zone["green"], zone["beep"])
    time.sleep(zone["open_delay"])
    lock(zone["latch_gpio"], zone["green"], zone["beep"])

#####
# Weigand Protocol
#####
def setup_readers():
    global zone_by_pin
    for name in iter(config):
        if name == "<zone>":
            continue
        if (type(config[name]) is dict and config[name].get("d0")
                                       and config[name].get("d1")):
            reader = config[name]
            reader["stream"] = ""
            reader["timer"] = None
            reader["name"] = name
            reader["unlocked"] = False
            zone_by_pin[reader["d0"]] = name
            zone_by_pin[reader["d1"]] = name
            GPIO.setup(reader["d0"], GPIO.IN)
            GPIO.setup(reader["d1"], GPIO.IN)
            GPIO.add_event_detect(reader["d0"], GPIO.FALLING,
                                  callback=data_pulse)
            GPIO.add_event_detect(reader["d1"], GPIO.FALLING,
                                  callback=data_pulse)

def data_pulse(channel):
    reader = config[zone_by_pin[channel]]
    if channel == reader["d0"]:
        reader["stream"] += "0"
    elif channel == reader["d1"]:
        reader["stream"] += "1"
    kick_timer(reader)

def kick_timer(reader):
    if reader["timer"] is None:
        reader["timer"] = threading.Timer(0.2, wiegand_stream_done,
                                          args=[reader])
        reader["timer"].start()

def wiegand_stream_done(reader):
    if reader["stream"] == "":
        return
    bitstring = reader["stream"]
    reader["stream"] = ""
    reader["timer"] = None
    validate_bits(bitstring)

def validate_bits(bstr):
    if len(bstr) != 26:
        logger.debug("Incorrect string length received: %i" % len(bstr))
        logger.debug(":%s:" % bstr)
        return False
    lparity = int(bstr[0])
    facility = int(bstr[1:9], 2)
    user_id = int(bstr[9:25], 2)
    rparity = int(bstr[25])
    logger.debug("%s is: %i %i %i %i" % (bstr, lparity, facility, user_id, rparity))

    calculated_lparity = 0
    calculated_rparity = 1
    for iter in range(0, 12):
        calculated_lparity ^= int(bstr[iter+1])
        calculated_rparity ^= int(bstr[iter+13])
    if (calculated_lparity != lparity or calculated_rparity != rparity):
        logger.debug("Parity error in received string!")
        return False

    card_id = "%08x" % int(bstr, 2)
    logger.debug("Successfully decoded %s facility=%i user=%i" %
          (card_id, facility, user_id))
    lookup_card(card_id, str(facility), str(user_id))

#####
# Users
#####
def lookup_card(card_id, facility, user_id):
    user = (users.get("%s,%s" % (facility, user_id)))
    if (user is None):
        reject_card(facility, user_id, "couldn't find user")
    elif (user.get(zone) and user[zone] == "authorized"):
        logger.info("%s has entered %s" % (user["name"], zone))
        unlock_briefly(config[zone])
    else:
        reject_card(facility, user_id, "user isn't authorized for this zone")

def reject_card(facility, user_id, reason):
    logger.warning("%s declined: (%s,%s): %s" % (zone, facility, user_id, reason))

# Globalize some variables for later
zone = None
users = None
config = None
logger = None
zone_by_pin = {}

if __name__ == "__main__":
    initialize()
    while True:
        time.sleep(1000)
