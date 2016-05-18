#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Bugzilla to Elastic class helper
#
# Copyright (C) 2015 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#   Alvaro del Castillo San Felix <acs@bitergia.com>
#

from time import time
from dateutil import parser
import json
import logging
import requests
from urllib.parse import urlparse

from .enrich import Enrich

from .utils import get_time_diff_days

class BugzillaEnrich(Enrich):

    def __init__(self, bugzilla, sortinghat=True, db_projects_map = None):
        super().__init__(sortinghat, db_projects_map)
        self.perceval_backend = bugzilla
        self.elastic = None

    def set_elastic(self, elastic):
        self.elastic = elastic

    def get_field_date(self):
        return "delta_ts"

    def get_fields_uuid(self):
        return ["assigned_to_uuid", "reporter_uuid"]

    @classmethod
    def get_sh_identity(cls, user):
        """ Return a Sorting Hat identity using bugzilla user data """

        def fill_list_identity(identity, user_list_data):
            """ Fill identity with user data in first item in list """
            identity['username'] = user_list_data[0]['__text__']
            if '@' in identity['username']:
                identity['email'] = identity['username']
            if 'name' in user_list_data[0]:
                identity['name'] = user_list_data[0]['name']
            return identity

        identity = {}
        for field in ['name', 'email', 'username']:
            # Basic fields in Sorting Hat
            identity[field] = None
        if 'reporter' in user:
            identity = fill_list_identity(identity, user['reporter'])
        if 'assigned_to' in user:
            identity = fill_list_identity(identity, user['assigned_to'])
        if 'who' in user:
            identity = fill_list_identity(identity, user['who'])
        if 'Who' in user:
            identity['username'] = user['Who']
            if '@' in identity['username']:
                identity['email'] = identity['username']
        if 'qa_contact' in user:
            identity = fill_list_identity(identity, user['qa_contact'])
        if 'changed_by' in user:
            identity['name'] = user['changed_by']

        return identity

    def get_item_sh(self, item):
        """ Add sorting hat enrichment fields """
        eitem = {}  # Item enriched

        # Sorting Hat integration: reporter and assigned_to uuids
        if 'assigned_to' in item['data']:
            identity = BugzillaEnrich.get_sh_identity({'assigned_to':item["data"]['assigned_to']})
            eitem['assigned_to_uuid'] = self.get_uuid(identity, self.get_connector_name())
            eitem['assigned_to_name'] = identity['name']
            enrollments = self.get_enrollments(eitem['assigned_to_uuid'])
            if len(enrollments) > 0:
                eitem["assigned_to_org_name"] = enrollments[0].organization.name
            else:
                eitem["assigned_to_org_name"] = None

        if 'reporter' in item['data']:
            identity = BugzillaEnrich.get_sh_identity({'reporter':item["data"]['reporter']})
            eitem['reporter_uuid'] = self.get_uuid(identity, self.get_connector_name())
            eitem['reporter_name'] = identity['name']
            enrollments = self.get_enrollments(eitem['reporter_uuid'])
            if len(enrollments) > 0:
                eitem["reporter_org_name"] = enrollments[0].organization.name
            else:
                eitem["reporter_org_name"] = None
            if identity['email']:
                try:
                    eitem["reporter_domain"] = identity['email'].split("@")[1]
                except IndexError:
                    # logging.warning("Bad email format: %s" % (identity['email']))
                    eitem["reporter_domain"] = None
            else:
                eitem["reporter_domain"] = None

        # Unify fields name
        eitem["author_uuid"] = eitem["reporter_uuid"]
        eitem["author_name"] = eitem["reporter_name"]
        eitem["author_org_name"] = eitem["reporter_org_name"]
        eitem["author_domain"] = eitem["reporter_domain"]

        return eitem

    def get_item_project(self, item):
        """ Get project mapping enrichment field """
        ds_name = "its"  # data source name in projects map
        url = item['origin']
        # https://bugs.eclipse.org/bugs/buglist.cgi?product=Mylyn%20Tasks
        product = item['data']['product'][0]['__text__']
        repo = url+"/buglist.cgi?product="+product
        try:
            project = (self.prjs_map[ds_name][repo])
        except KeyError:
            # logging.warning("Project not found for repository %s" % (repo))
            project = None
        return {"project": project}

    def get_identities(self, item):
        ''' Return the identities from an item '''

        identities = []

        if 'activity' in item["data"]:
            for event in item["data"]['activity']:
                identities.append(self.get_sh_identity(event))
        if 'long_desc' in item["data"]:
            for comment in item["data"]['long_desc']:
                identities.append(self.get_sh_identity(comment))
        elif 'assigned_to' in item["data"]:
            identities.append(self.get_sh_identity({'assigned_to':
                                                    item["data"]['assigned_to']}))
        elif 'reporter' in item["data"]:
            identities.append(self.get_sh_identity({'reporter':
                                                    item["data"]['reporter']}))
        elif 'qa_contact' in item["data"]:
            identities.append(self.get_sh_identity({'qa_contact':
                                                    item['qa_contact']}))
        return identities

    def enrich_issue(self, item):

        def get_bugzilla_url(item):
            u = urlparse(item['origin'])
            return u.scheme+"//"+u.netloc

        if 'bug_id' not in item['data']:
            logging.warning("Dropped bug without bug_id %s" % (issue))
            return None

        eitem = {}

        # metadata fields to copy
        copy_fields = ["metadata__updated_on","metadata__timestamp","uuid","origin"]
        for f in copy_fields:
            if f in item:
                eitem[f] = item[f]
            else:
                eitem[f] = None

        # The real data
        issue = item['data']

        if "assigned_to" in issue:
            if "name" in issue["assigned_to"][0]:
                eitem["assigned_to"] = issue["assigned_to"][0]["name"]

        if "reporter" in issue:
            if "name" in issue["reporter"][0]:
                eitem["reporter"] = issue["reporter"][0]["name"]

        eitem["bug_id"] = issue['bug_id'][0]['__text__']
        eitem["status"]  = issue['bug_status'][0]['__text__']
        if "short_desc" in issue:
            if "__text__" in issue["short_desc"][0]:
                eitem["summary"]  = issue['short_desc'][0]['__text__']

        # Component and product
        eitem["component"] = issue['component'][0]['__text__']
        eitem["product"]  = issue['product'][0]['__text__']

        # Fix dates
        date_ts = parser.parse(issue['creation_ts'][0]['__text__'])
        eitem['creation_ts'] = date_ts.strftime('%Y-%m-%dT%H:%M:%S')
        date_ts = parser.parse(issue['delta_ts'][0]['__text__'])
        eitem['changeddate_date'] = date_ts.isoformat()
        eitem['delta_ts'] = date_ts.strftime('%Y-%m-%dT%H:%M:%S')

        # Add extra JSON fields used in Kibana (enriched fields)
        eitem['number_of_comments'] = 0
        eitem['time_to_last_update_days'] = None
        eitem['url'] = None

        if 'long_desc' in issue:
            eitem['number_of_comments'] = len(issue['long_desc'])
        eitem['url'] = get_bugzilla_url(item) + "show_bug.cgi?id=" + \
                        issue['bug_id'][0]['__text__']
        eitem['time_to_last_update_days'] = \
            get_time_diff_days(eitem['creation_ts'], eitem['delta_ts'])

        if self.sortinghat:
            eitem.update(self.get_item_sh(item))

        if self.prjs_map:
            eitem.update(self.get_item_project(item))

        return eitem


    def enrich_items(self, items):
#         if self.perceval_backend.detail == "list":
#             self.issues_list_to_es(items)
#         else:
#             self.issues_to_es(items)
        self.issues_to_es(items)

    def issues_list_to_es(self, items):

        elastic_type = "issues_list"

        max_items = self.elastic.max_items_bulk
        current = 0
        total = 0
        bulk_json = ""

        url = self.elastic.index_url+'/' + elastic_type + '/_bulk'

        logging.debug("Adding items to %s (in %i packs)" % (url, max_items))

        # In this client, we will publish all data in Elastic Search
        for issue in items:
            if current >= max_items:
                task_init = time()
                requests.put(url, data=bulk_json)
                bulk_json = ""
                total += current
                current = 0
                logging.debug("bulk packet sent (%.2f sec, %i total)"
                              % (time()-task_init, total))
            data_json = json.dumps(issue)
            bulk_json += '{"index" : {"_id" : "%s" } }\n' % (rich_item[self.get_field_unique_id()])
            bulk_json += data_json +"\n"  # Bulk document
            current += 1
        task_init = time()
        total += current
        requests.put(url, data=bulk_json)
        logging.debug("bulk packet sent (%.2f sec, %i total)"
                      % (time()-task_init, total))


    def issues_to_es(self, items):

        elastic_type = "issues"

        max_items = self.elastic.max_items_bulk
        current = 0
        bulk_json = ""

        url = self.elastic.index_url+'/' + elastic_type + '/_bulk'

        logging.debug("Adding items to %s (in %i packs)" % (url, max_items))

        for issue in items:
            if current >= max_items:
                requests.put(url, data=bulk_json)
                bulk_json = ""
                current = 0
            eitem = self.enrich_issue(issue)
            if not eitem:
                continue
            data_json = json.dumps(eitem)
            bulk_json += '{"index" : {"_id" : "%s" } }\n' % (eitem[self.get_field_unique_id()])
            bulk_json += data_json +"\n"  # Bulk document
            current += 1
        requests.put(url, data=bulk_json)

        logging.debug("Adding issues to ES Done")


    def get_elastic_mappings(self):
        ''' Specific mappings needed for ES '''

        mapping = '''
        {
            "properties": {
               "product": {
                  "type": "string",
                  "index":"not_analyzed"
               },
               "component": {
                  "type": "string",
                  "index":"not_analyzed"
               },
               "assigned_to": {
                  "type": "string",
                  "index":"not_analyzed"
               },
               "author_org_name": {
                 "type": "string",
                 "index":"not_analyzed"
               },
               "author_domain": {
                 "type": "string",
                 "index":"not_analyzed"
               },
               "author_name": {
                 "type": "string",
                 "index":"not_analyzed"
               },
               "origin": {
                 "type": "string",
                 "index":"not_analyzed"
               }
            }
        }
        '''

        return {"items":mapping}
