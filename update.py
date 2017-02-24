#!/usr/bin/python

# With thanks for reassuring me google's permissions work the way I expect:
# https://www.twilio.com/blog/2017/02/an-easy-way-to-read-and-write-to-a-google-spreadsheet-in-python.html

import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from subprocess import call
import logging
from systemd.journal import JournalHandler

def get_records():
    scope = ['https://spreadsheets.google.com/feeds']
    creds = ServiceAccountCredentials.from_json_keyfile_name('client_secret.json', scope)
    client = gspread.authorize(creds)
    sheet = client.open("Keycards").sheet1
    return sheet.get_all_records(default_blank=None)

def format_fobs(fobs):
    return {i['ID'] : {"name": i['Name'], "maindoor": "authorized"} for i in fobs if not i['Disable?']}

def find_duplicates(rows):
    id_dict = {}
    for row in rows:
        id_dict.setdefault(row['ID'], []).append(row)
    duplicates = [x for n, x in enumerate(id_dict) if len(id_dict[x]) > 1]
    if duplicates:
        logger.warn("Warning! - Duplicate ID fields found:")
        for dup in duplicates:
            logger.warn(id_dict[dup])

if __name__ == "__main__":
    # Logging
    global logger
    logger = logging.getLogger("Doorwoman-updater")
    logger.setLevel(logging.INFO)
    logger.addHandler(JournalHandler())
    # logger.addHandler(logging.StreamHandler())

    users_file = open('conf/users.json', 'r') # This file MUST exist
    users_json = users_file.read()
    users = json.loads(users_json)

    records = get_records()
    records_by_id = format_fobs(records)

    find_duplicates(records)
    logger.info("Read %i ids from users.json, Downloaded %i (%i enabled) ids from Spreadsheet." %
        (len(users), len(records), len(records_by_id)))

    records_by_id_json = json.dumps(records_by_id, indent=4)
    if records_by_id_json == users_json:
        exit()
    elif len(users) and not len(records) or not len(records_by_id):
        logger.warning("ERROR! Refusing to overwrite with zero users")
    else:
        logger.info("Updating conf/users.json: %i new records" % (len(records_by_id) - len(users)))
        users_file.close()
        call('./datamounter.sh -w'.split())
        users_file.open('conf/users.json', 'r+')
        json.dump(records_by_id, users_file, indent=4)
        users_file.truncate()
        users_file.close()
        call("./datamounter.sh")
        call("sudo systemctl reload doorwoman".split())
