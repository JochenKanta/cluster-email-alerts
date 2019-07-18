#!/usr/bin/env python
'''
This script sends out three types of configurable alerts based on matching
criteria as defined in the accompanying configuration file.. The alerts that can
be configured are:
    - Quota Capacity Exceeded (Soft Quota Alert)
    - Cluster Capacity Exceeded
    - Replication Relationship Errors
'''
# Copyright (c) 2013 Qumulo, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

# Import Python Libraries
import os
import sys
import datetime
import smtplib
import json
import argparse
from email.mime.text import MIMEText
from collections import namedtuple

# Import Qumulo REST Libraries
#pylint: disable=wrong-import-position
import qumulo.lib.auth
import qumulo.lib.opts
import qumulo.lib.request
import qumulo.rest

# Size Definitions in Kilobytes - base10
KILOBYTE = 1000
MEGABYTE = 1000 * KILOBYTE
GIGABYTE = 1000 * MEGABYTE
TERABYTE = 1000 * GIGABYTE

EmailSettings = namedtuple(
    'EmailSettings', ['sender', 'server', 'cluster_name'])

RestInfo = namedtuple('RestInfo', ['conninfo', 'creds'])

def load_config(config):
    '''
    Load the configuration and ensure that it's valid JSON.
    '''
    if os.path.exists(config):
        config_fh = open(config, 'r')
        try:
            data = json.load(config_fh)
            return data
        except ValueError as error:
            print 'Invalid JSON file: {}. Error: {}'.format(config, error)
        config_fh.close()
    else:
        sys.exit('File {} does not exist'.format(config))

def cluster_login(username, password, cluster, port):
    '''
    Generate the credentials object used to query Qumulo's API.
    '''
    # Open a connection to the REST server on the cluster.
    conninfo = qumulo.lib.request.Connection(cluster, int(port))

    # Get the bearer token by passing through 'conninfo'. We throw away
    # everything but the 'bearer_token'.
    results, _ = qumulo.rest.auth.login(conninfo, None, username, password)

    # Create a credentials object to use in the REST calls for alerts.
    creds = qumulo.lib.auth.Credentials.from_login_response(results)

    return RestInfo(conninfo, creds)

def send_mail(server, sender, recipients, subject, body):
    '''
    Send an email with the message generated by other functions.
    '''
    # Add a timestamp to the body.
    body += "<br><br>Alert sent on {}".format(
        datetime.datetime.now().strftime("%A, %d. %B %Y %I:%M%p"))

    # Compose the email to be sent based off received data.
    mmsg = MIMEText(body, 'html')
    mmsg['Subject'] = subject
    mmsg['From'] = sender
    mmsg['To'] = ", ".join(recipients)

    # Send the email to the server as the sender_address.
    session = smtplib.SMTP(server)
    session.sendmail(sender, recipients, mmsg.as_string())
    session.quit()

def humanize_bytes(num, suffix='B'):
    '''
    Convert bytes to a more human friendly size in base 10 to match the WebUI.
    '''
    for unit in ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z']:
        if abs(num) < 1000.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1000.0
    return "%.1f%s%s" % (num, 'Y', suffix)

def check_quotas_for_capacity(email_settings, rest_info, quota_rules):
    '''
    Check if any quotas have exeeded their configured thresholds. If so,
    trigger an email alert. Terminology:

        Usage = Percentage of quota used.
        Used = Capacity of quota used.
        Limit = Total capacity of quota.
    '''
    qr = qumulo.rest

    for quota in quota_rules:
        # Convert quota directory path to file system ID.
        quota_id = qr.fs.get_file_attr(
            rest_info.conninfo, rest_info.creds, quota['path'])[0]['id']

        # Use ID to get directory quota detail and status.
        quota_details = qr.quota.get_quota_with_status(
            rest_info.conninfo, rest_info.creds, quota_id)

        # Get quota used percentage, rounding happens in email.
        quota_used = quota_details[0]['capacity_usage']
        quota_limit = quota_details[0]['limit']
        if quota_limit == 0:
            sys.exit('Quota "{}" has a limit of 0.'.format(quota['name']))
        pct_used = (float(quota_used) / float(quota_limit)) * 100

        # Check used percent versus configured limits and record if exceeded.
        send_alert = False
        threshold_exceeded = 0.0
        for threshold in quota['thresholds']:
            if pct_used > threshold:
                threshold_exceeded = threshold
                send_alert = True

        # After looping through all thresholds, email if exceeded.
        if send_alert:
            quota_send_alert(
                email_settings,
                rest_info,
                quota,
                quota_details,
                threshold_exceeded)

def quota_send_alert(
        email_settings, rest_info, quota_rule, quota_details, threshold):
    '''
    Generate the Subject and Body for the soft quota alert. The terminology is
    the same as in the WebUI:

        Usage = Percentage of quota used.
        Used = Capacity of quota used.
        Limit = Total capacity of quota.

    Note that the body of the email is HTML and will accept typical tags such
    as <br> and <strong>. Two <br> are needed to add 1 line of space.
    '''
    quota_used = quota_details[0]['capacity_usage']
    quota_limit = quota_details[0]['limit']
    quota_path = quota_rule['path']
    quota_pct_used = round((float(quota_used) / float(quota_limit)) * 100, 2)

    # Generate humanized capacity numbers to send.
    q_used = humanize_bytes(int(quota_used))
    q_limit = humanize_bytes(int(quota_limit))

    # Compose the email to send.
    subject = "{}: Soft quota alert on path {}".format(
        email_settings.cluster_name, quota_path)
    body = \
'''The quota '{0[name]} on directory path {0[path]} has exceeded it's usage \
threshold of {threshold}%.
Current usage is {used} out of {limit}. ({full}% full)'''.format(
    quota_rule,
    threshold=threshold,
    used=q_used,
    limit=q_limit,
    full=quota_pct_used)

    if quota_rule['include_capacity']:
        # Cluster Stats
        cluster_stats = qumulo.rest.fs.read_fs_stats(
            rest_info.conninfo, rest_info.creds)
        cluster_capacity = int(cluster_stats[0]['total_size_bytes'])
        body += "\nCluster total capacity: {}".format(
            humanize_bytes(cluster_capacity))
    if quota_rule['custom_msg']:
        body += "\n{}".format(quota_rule['custom_msg'])

    body = body.replace('\n', '<br><br>')

    # Send the actual email.
    send_mail(
        email_settings.server,
        email_settings.sender,
        quota_rule['mail_to'],
        subject,
        body)

def cluster_capacity_check(email_settings, rest_info, capacity_alerts):
    '''
    Check if the capacity of the cluster has exceeded a threshold. If so,
    trigger an email alert.
    '''
    cluster_stats = qumulo.rest.fs.read_fs_stats(
        rest_info.conninfo, rest_info.creds)
    cluster_capacity = int(cluster_stats[0]['total_size_bytes'])
    cluster_used = int(cluster_stats[0]['total_size_bytes']) - \
                    int(cluster_stats[0]['free_size_bytes'])
    cluster_pct_used = (float(cluster_used) / float(cluster_capacity)) * 100

    for capacity_alert in capacity_alerts:
        for threshold in capacity_alert['thresholds']:
            if cluster_pct_used > threshold:
                threshold_exceeded = threshold
                send_alert = True

        if send_alert:
            cluster_capacity_send_alert(
                email_settings,
                cluster_capacity,
                cluster_used,
                round(cluster_pct_used, 2),
                threshold_exceeded,
                capacity_alert['custom_msg'],
                capacity_alert['mail_to'])

def cluster_capacity_send_alert(
        email_settings,
        capacity,
        used,
        percent,
        threshold,
        custom_msg,
        recipients):
    '''
    Send an alert because the cluster's capacity has exceeded the threshold.
    '''
    # Generate humanized capacity numbers to send.
    c_capacity = humanize_bytes(int(capacity))
    c_used = humanize_bytes(int(used))

    # Compose the email to send.
    subject = "{}: Cluster capacity alert. Usage has exceeded {}".format(
        email_settings.cluster_name, threshold)

    body = \
'''The cluster '{}' has exceeded its usage threshold of {}%.
Current usage is {} out of {} ({}% full).'''.format(
    email_settings.cluster_name, threshold, c_used, c_capacity, percent)

    if custom_msg:
        body += "\n{}".format(custom_msg)

    body = body.replace('\n', '<br><br>')

    # Send the actual email.
    send_mail(
        email_settings.server,
        email_settings.sender,
        recipients,
        subject,
        body)

def replication_check_status(email_settings, rest_info, replication_rules):
    '''
    Check all source & target replication relationships for errors. Trigger an
    alert if any show errors.
    '''
    qr = qumulo.rest
    src_relationships = qr.replication.list_source_relationship_statuses(
        rest_info.conninfo, rest_info.creds)[0]
    err_relationships = \
        [r for r in src_relationships if r['error_from_last_job']]

    tgt_relationships = qr.replication.list_target_relationship_statuses(
        rest_info.conninfo, rest_info.creds)[0]
    err_relationships += \
        [r for r in tgt_relationships if r['error_from_last_job']]

    if err_relationships:
        replication_send_alert(
            email_settings, err_relationships, replication_rules)

def replication_send_alert(email_settings, relationships, replication_rules):
    '''
    Send an alert because a relationship has an error.
    '''
    subject = "{}: Relationship error alert.".format(
        email_settings.cluster_name)
    newline = "<br><br>"

    body = ""
    body += "The following replication relationships have reported an error:"
    body += newline
    for r in relationships:
        tmp_body = \
'''Source cluster name: {0[source_cluster_name]}
Source replication root path: {0[source_root_path]}
Target cluster name: {0[target_cluster_name]}
Target replication root path: {0[target_root_path]}
Recovery point: {0[recovery_point]}
Error from last replication job: {0[error_from_last_job]}'''.format(r)

        tmp_body = tmp_body.replace('\n', newline)
        body += tmp_body

    for rule in replication_rules:
        msg_body = body + rule['custom_msg']
        send_mail(
            email_settings.server,
            email_settings.sender,
            rule['mail_to'],
            subject,
            msg_body)

def main():
    '''
    Main function where the logic and config happens.
    '''
    parser = argparse.ArgumentParser(
        description='This script will generate email alerts when run based \
                    on the configuration passed through in --config. This \
                    script requires the Qumulo API Tools which can be \
                    downloaded from the cluster itself.')
    parser.add_argument('-c', '--config', required=True,
                        help='Configuration file to be used.')
    options = parser.parse_args()
    config = options.config

    # Load the config.json file into an object.
    loaded_config = load_config(config)

    # Email Details
    email_settings = EmailSettings(
        loaded_config['email_settings']['sender_address'],
        loaded_config['email_settings']['server_address'],
        loaded_config['cluster_settings']['cluster_name'])

    # Cluster Config Details
    username = loaded_config['cluster_settings']['username']
    password = loaded_config['cluster_settings']['password']
    cluster_addr = loaded_config['cluster_settings']['cluster_address']
    port = loaded_config['cluster_settings']['rest_port']

    # Generate Connection Info & Credentials
    rest_info = cluster_login(username, password, cluster_addr, port)

    cluster_capacity_check(
        email_settings, rest_info, loaded_config['capacity_rules'])
    check_quotas_for_capacity(
        email_settings, rest_info, loaded_config['quota_rules'])
    replication_check_status(
        email_settings, rest_info, loaded_config['replication_rules'])

    return 0

if __name__ == '__main__':
    sys.exit(main())
