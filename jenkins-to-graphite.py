#!/usr/bin/python
#
# Send various statistics about jenkins to statsd
#
# Jeremy Katz <katzj@hubspot.com>
# Copyright 2012, HubSpot, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import base64
import logging
import optparse
import sys
import time
import urllib2
import librato
from datetime import datetime
import json


class JenkinsServer(object):
    def __init__(self, base_url, user, password):
        self.base_url = base_url
        self.user = user
        self.password = password

        self._opener = None

    @property
    def opener(self):
        """Creates a urllib2 opener with basic auth for talking to jenkins"""
        if self._opener is None:
            opener = urllib2.build_opener(urllib2.HTTPCookieProcessor())
            if self.user or self.password:
                opener.addheaders = [("Authorization", "Basic " + base64.encodestring("%s:%s" % (self.user, self.password)))]
            urllib2.install_opener(opener)
            self._opener = opener

        return self._opener

    def get_raw_data(self, url):
        """Get the data from jenkins at @url and return it as a dictionary"""

        try:
            f = self.opener.open("%s/%s" % (self.base_url, url))
            response = f.read()
            f.close()
            data = json.loads(response)
        except Exception, e:
            logging.warn("Unable to get jenkins response for url %s: %s" % (url, e))
            return {}

        return data

    def get_data(self, url):
        return self.get_raw_data("%s/api/json" % url)


def parse_args():
    parser = optparse.OptionParser()
    parser.add_option("", "--librato-user",
                      help="Librato username")
    parser.add_option("", "--librato-token",
                      help="Librato access token")
    parser.add_option("", "--jenkins-url",
                      help="Base url of your jenkins server (ex http://jenkins.example.com")
    parser.add_option("", "--jenkins-user",
                      help="User to authenticate with for jenkins")
    parser.add_option("", "--jenkins-password",
                      help="Password for authenticating with jenkins")

    parser.add_option("", "--view",
                      help="View to monitor for success/failure")
    parser.add_option("", "--prefix", default="jenkins",
                      help="Statsd metric prefix")
    parser.add_option("", "--label", action="append", dest="labels",
                      help="Fetch stats applicable to this node label. Can bee applied multiple times for monitoring more labels.")
    parser.add_option("", "--job", action="append", dest="individual_jobs",
                      help="Fetch stats applicable to this job. Can bee applied multiple times for monitoring more jobs.")
    parser.add_option("", "--monitor-nodes", action="store_true",
                      help="A flag to enable nodes monitoring (total, online, offline)")

    (opts, args) = parser.parse_args()

    if not opts.jenkins_url:
        print >> sys.stderr, "Need to specify jenkins url"
        sys.exit(1)

    return opts


def main():
    opts = parse_args()
    jenkins = JenkinsServer(opts.jenkins_url, opts.jenkins_user,
                            opts.jenkins_password)

    def prefix_name(name):
        return opts.prefix + '.' + name

    api = librato.connect(opts.librato_user, opts.librato_token, sanitizer=prefix_name)

    print("Loading computers...")
    executor_info = jenkins.get_data("computer")

    print("Loading queue...")
    queue_info = jenkins.get_data("queue")

    print("Loading timeline...")
    build_info_min = jenkins.get_raw_data("view/All/timeline/data?min=%d&max=%d" % ((time.time() - 60) * 1000, time.time() * 1000))
    build_info_hour = jenkins.get_raw_data("view/All/timeline/data?min=%d&max=%d" % ((time.time() - 3600) * 1000, time.time() * 1000))

    api.submit("queue.size", len(queue_info.get("items", [])), type="gauge")

    api.submit("builds.started_builds_last_minute", len(build_info_min.get("events", [])))
    api.submit("builds.started_builds_last_hour", len(build_info_hour.get("events", [])))

    api.submit("executors.total", executor_info.get("totalExecutors", 0))
    api.submit("executors.busy", executor_info.get("busyExecutors", 0))
    api.submit("executors.free",
               executor_info.get("totalExecutors", 0) -
               executor_info.get("busyExecutors", 0))

    if opts.monitor_nodes:
        nodes_total = executor_info.get("computer", [])
        nodes_offline = [j for j in nodes_total if j.get("offline")]
        api.submit("nodes.total", len(nodes_total))
        api.submit("nodes.offline", len(nodes_offline))
        api.submit("nodes.online", len(nodes_total) - len(nodes_offline))

    if opts.labels:
        for label in opts.labels:
            print("Loading information for label '%s'" % label)
            label_info = jenkins.get_data("label/%s" % label)
            label_node_names = [n['nodeName'] for n in label_info.get('nodes', [])]
            api.submit("labels.%s.jobs.tiedJobs" % label, len(label_info.get("tiedJobs", [])))
            api.submit("labels.%s.nodes.total" % label, len(label_node_names))
            api.submit("labels.%s.executors.total" % label, label_info.get("totalExecutors", 0))
            api.submit("labels.%s.executors.busy" % label, label_info.get("busyExecutors", 0))
            api.submit("labels.%s.executors.free" % label,
                       label_info.get("totalExecutors", 0) -
                       label_info.get("busyExecutors", 0))
            node_names_offline = [n['displayName'] for n in nodes_offline]
            label_offline_nodes = set(node_names_offline).intersection(set(label_node_names))
            api.submit("labels.%s.executors.offline" % label, len(label_offline_nodes))

    if opts.individual_jobs:
        for job in opts.individual_jobs:
            print("Loading information for job '%s'" % job)
            in_queue = [item for item in queue_info.get("items", []) if item.get("task", {}).get("name", None) == job]
            api.submit("jobs.%s.queue.size" % job, len(in_queue))
            if in_queue:
                longest_in_queue = min([datetime.fromtimestamp(item.get("inQueueSince", 1000 * int(time.time())) / 1000) for item in in_queue])
                delay = datetime.now() - longest_in_queue
                delay_seconds = delay.seconds + (delay.days * 86400)
            else:
                delay_seconds = 0
            api.submit("jobs.%s.queue.delay" % job, delay_seconds*1000)
            last_successful = jenkins.get_data("job/%s/lastSuccessfulBuild" % job)
            last_duration = last_successful.get('duration', None)
            api.submit("jobs.%s.duration" % job, last_duration)

    if opts.view:
        print("Loading information for view '%s'" % opts.view)
        builds_info = jenkins.get_data("/view/%s" % opts.view)
        jobs = builds_info.get("jobs", [])
        ok = [j for j in jobs if j.get("color", 0) == "blue"]
        fail = [j for j in jobs if j.get("color", 0) == "red"]
        warn = [j for j in jobs if j.get("color", 0) == "yellow"]
        
        api.submit("view.%s.total" % opts.view, len(jobs))
        api.submit("view.%s.ok" % opts.view, len(ok))
        api.submit("view.%s.fail" % opts.view, len(fail))
        api.submit("view.%s.warn" % opts.view, len(warn))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
