#!/usr/bin/python
#
# vim: et ai sw=4

import RPi.GPIO as GPIO
import sys,time
import signal
import subprocess
import json
import smtplib
import threading
import syslog
import atexit
import traceback

debug_mode = False
conf_dir = "./conf/"

def initialize():
    sys.excepthook = log_uncaught_exceptions
    GPIO.setmode(GPIO.BCM)
    syslog.openlog("accesscontrol", syslog.LOG_PID, syslog.LOG_AUTH)
    debug("Initializing")
    read_configs()
    setup_output_GPIOs()
    setup_readers()
    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, sigterm)  # killall python
    signal.signal(signal.SIGHUP, rehash)    # killall -HUP python
    report("%s access control is online" % zone)

def report(subject, more=""):
    syslog.syslog(subject)
    if more:
        syslog.syslog(more)
    debug(subject)
    if config and config.get("emailserver"):
        # The trailing comma in args=() below is required to truncate args
        # TODO send body
        t = threading.Thread(target=send_email, args=(subject,))
        t.start()

def debug(message):
    if debug_mode:
        print message

def send_email(subject, body=""):
    try:
        emailfrom = config["emailfrom"]
        to = config["emailto"]
        smtpserver = smtplib.SMTP(config["emailserver"], config["emailport"])
        smtpserver.ehlo()
        header = "To: %s\nFrom: %s\nSubject: %s\n" % (to, emailfrom, subject)
        msg = "%s\n%s\n\n" % (header, body)
        smtpserver.sendmail(emailfrom, to, msg)
        smtpserver.close()
    except smtplib.SMTPException:
        # couldn't send.
        pass

def rehash(signal=None, b=None):
    global users
    report("Reloading access list")
    users = load_json(conf_dir + "users.json")

def sigterm(signal, b):
    sys.exit(0) # calls cleanup() via atexit

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
        debug("Incorrect string length received: %i" % len(bstr))
        debug(":%s:" % bstr)
        return False
    lparity = int(bstr[0])
    facility = int(bstr[1:9], 2)
    user_id = int(bstr[9:25], 2)
    rparity = int(bstr[25])
    debug("%s is: %i %i %i %i" % (bstr, lparity, facility, user_id, rparity))

    calculated_lparity = 0
    calculated_rparity = 1
    for iter in range(0, 12):
        calculated_lparity ^= int(bstr[iter+1])
        calculated_rparity ^= int(bstr[iter+13])
    if (calculated_lparity != lparity or calculated_rparity != rparity):
        debug("Parity error in received string!")
        return False

    card_id = "%08x" % int(bstr, 2)
    debug("Successfully decoded %s facility=%i user=%i" %
          (card_id, facility, user_id))
    lookup_card(card_id, str(facility), str(user_id))

def lookup_card(card_id, facility, user_id):
    user = (users.get("%s,%s" % (facility, user_id)) or
            users.get(card_id) or
            users.get(card_id.upper()) or
            users.get(user_id))
    if (user is None):
        return reject_card(card_id, facility, user_id, "couldn't find user")
    if (user.get(zone) and user[zone] == "authorized"):
        unlock_briefly(config[zone])
        report("%s has entered %s" % (user["name"], zone))
    else:
        reject_card(card_id, facility, user_id, "user isn't authorized for this zone")

def reject_card(card_id, facility, user_id, reason):
    report("%s declined: (card_id=%s, facilty=%s, user=%s): %s" %
          (zone, card_id, facility, user_id, reason))
    return False

def log_uncaught_exceptions(ex_cls, ex, tb):
    if ex_cls == KeyboardInterrupt:
        return
    report('Uncaught Exception {0}: {1}'.format(ex_cls, ex),
           ''.join(traceback.format_tb(tb)))
    sys.__excepthook__(ex_cls, ex, tb)
    cleanup()

def cleanup():
    message = ""
    if zone:
        message = "%s " % zone
    message += "access control is going offline"
    report(message)
    GPIO.setwarnings(False)
    GPIO.cleanup()

# Globalize some variables for later
zone = None
users = None
config = None
last_name = None
zone_by_pin = {}
repeat_read_count = 0
repeat_read_timeout = time.time()

initialize()
while True:
    # The main thread should open a command socket or something
    time.sleep(1000)
