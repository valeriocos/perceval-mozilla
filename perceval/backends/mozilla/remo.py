# -*- coding: utf-8 -*-
#
# Copyright (C) 2016-2017 Bitergia
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
# Foundation, 51 Franklin Street, Fifth Floor, Boston, MA 02110-1335, USA.
#
# Authors:
#     Alvaro del Castillo <acs@bitergia.com>
#

import functools
import json
import logging

import requests

from grimoirelab.toolkit.datetime import str_to_datetime
from grimoirelab.toolkit.uris import urijoin

from perceval.backend import (Backend,
                              BackendCommand,
                              BackendCommandArgumentParser,
                              metadata)
from perceval.errors import CacheError


logger = logging.getLogger(__name__)

MOZILLA_REPS_URL = "https://reps.mozilla.org"
REMO_DEFAULT_OFFSET = 0


def remo_metadata(func):
    """ReMo metadata decorator.

    This decorator takes items overrides `metadata` decorator to add extra
    information related to Kitsune (offset of the item).
    """
    @functools.wraps(func)
    def decorator(self, *args, **kwargs):
        for item in func(self, *args, **kwargs):
            item['offset'] = item['data'].pop('offset')
            yield item
    return decorator


class ReMo(Backend):
    """ReMo backend for Perceval.

    This class retrieves the events from a ReMo URL. To initialize
    this class an URL may be provided. If not, https://reps.mozilla.org
    will be used. The origin of the data will be set to this URL.

    It uses v2 API to get events, people and activities data.

    :param url: ReMo URL
    :param tag: label used to mark the data
    :param cache: cache object to store raw data
    """
    version = '0.5.0'

    def __init__(self, url=None, tag=None, cache=None):
        if not url:
            url = MOZILLA_REPS_URL
        origin = url

        super().__init__(origin, tag=tag, cache=cache)
        self.url = url
        self.client = ReMoClient(url)
        self.__users = {}  # internal users cache

    @remo_metadata
    @metadata
    def fetch(self, offset=REMO_DEFAULT_OFFSET, category='events'):
        """Fetch items from the ReMo url.

        The method retrieves, from a ReMo URL, the set of items
        of the given `category`.

        :offset: obtain items after offset
        :category: category of items to retrieve
        :returns: a generator of items
        """
        supported_categories = ['activities', 'events', 'users']

        if category not in supported_categories:
            raise ValueError('ReMo perceval backend does not support ' + category)

        logger.info("Looking for events at url '%s' of %s category and %i offset",
                    self.url, category, offset)

        nitems = 0  # number of items processed
        titems = 0  # number of items from API data

        # Always get complete pages so the first item is always
        # the first one in the page
        page = int(offset / ReMoClient.ITEMS_PER_PAGE)
        page_offset = page * ReMoClient.ITEMS_PER_PAGE
        # drop items from page before the offset
        drop_items = offset - page_offset
        logger.debug("%i items dropped to get %i offset starting in page %i (%i page offset)",
                     drop_items, offset, page, page_offset)
        current_offset = offset

        self._purge_cache_queue()
        # Add to the cache the offset so it can be used to recover from cache
        self._push_cache_queue(offset)

        for raw_items in self.client.get_items(category, offset):
            self._push_cache_queue(raw_items)
            items_data = json.loads(raw_items)
            titems = items_data['count']
            logger.info("Pending items to retrieve: %i, %i current offset",
                        titems - current_offset, current_offset)
            items = items_data['results']
            for item in items:
                if drop_items > 0:
                    # Remove extra items due to page base retrieval
                    drop_items -= 1
                    continue
                raw_item_details = self.client.call(item['_url'])
                self._push_cache_queue(raw_item_details)
                item_details = json.loads(raw_item_details)
                item_details['offset'] = current_offset
                current_offset += 1
                yield item_details
                nitems += 1

                self._flush_cache_queue()

        logger.info("Total number of events: %i (%i total, %i offset)", nitems, titems, offset)

    @remo_metadata
    @metadata
    def fetch_from_cache(self):
        """Fetch the items from the cache.

        :returns: a generator of items

        :raises CacheError: raised when an error occurs accessing the
            cache
        """
        logger.info("Retrieving cached ReMo items: '%s'", self.url)

        if not self.cache:
            raise CacheError(cause="cache instance was not provided")

        cache_items = self.cache.retrieve()

        nitems = 0

        for item in cache_items:
            if type(item) is int:
                # offset from a new execution results in the cache
                offset = item
                item = next(cache_items)
            data = json.loads(item)
            # The raw_data is always a list of items or an item
            if 'count' in data:
                # It is a list
                continue
            else:
                data['offset'] = offset
                offset += 1
                yield data
                nitems += 1

        logger.info("Retrieval process completed: %s items retrieved from cache",
                    nitems)

    @classmethod
    def has_caching(cls):
        """Returns whether it supports caching items on the fetch process.

        :returns: this backend supports items cache
        """
        return True

    @classmethod
    def has_resuming(cls):
        """Returns whether it supports to resume the fetch process.

        :returns: this backend supports items resuming
        """
        return True

    @staticmethod
    def metadata_id(item):
        """Extracts the identifier from a ReMo item."""
        return str(item['remo_url'])

    @staticmethod
    def metadata_updated_on(item):
        """Extracts the update time from a ReMo item.

        The timestamp is extracted from 'end' field.
        This date is converted to a perceval format using a float value.

        :param item: item generated by the backend

        :returns: a UNIX timestamp
        """
        if 'end' in item:
            # events updated field
            updated = item['end']
        elif 'date_joined_program' in item:
            # users updated field that always appear
            updated = item['date_joined_program']
        elif 'report_date' in item:
            # activities updated field
            updated = item['report_date']
        else:
            raise ValueError("Can't find updated field for item " + item)

        return float(str_to_datetime(updated).timestamp())

    @staticmethod
    def metadata_category(item):
        """Extracts the category from a ReMo item.

        This backend generates items types 'event', 'activity'
        or 'user'. To guess the type of item, the code will look
        for unique fields.
        """
        if 'estimated_attendance' in item:
            category = 'event'
        elif 'activity' in item:
            category = 'activity'
        elif 'first_name' in item:
            category = 'user'
        else:
            raise TypeError("Could not define the category of item " + item)

        return category


class ReMoClient:
    """ReMo API client.

    This class implements a simple client to retrieve events from
    projects in a ReMo site.

    :param url: URL of ReMo (sample https://reps.mozilla.org)

    :raises HTTPError: when an error occurs doing the request
    """

    FIRST_PAGE = 1  # Initial page in ReMo API
    ITEMS_PER_PAGE = 20  # Items per page in ReMo API
    API_PATH = '/api/remo/v1'

    def __init__(self, url):
        self.url = url
        self.api_activities_url = urijoin(self.url, ReMoClient.API_PATH + '/activities/')
        self.api_activities_url += '/'  # API needs a final /
        self.api_events_url = urijoin(self.url, ReMoClient.API_PATH + '/events/')
        self.api_events_url += '/'  # API needs a final /
        self.api_users_url = urijoin(self.url, ReMoClient.API_PATH + '/users/')
        self.api_users_url += '/'  # API needs a final /

    def call(self, uri, params=None):
        """Run an API command.
        :param params: dict with the HTTP parameters needed to run
            the given command
        """
        logger.debug("ReMo client calls APIv2: %s params: %s",
                     uri, str(params))

        req = requests.get(uri, params=params)
        req.raise_for_status()

        return req.text

    def get_items(self, category='events', offset=REMO_DEFAULT_OFFSET):
        """Retrieve all items for category using pagination """

        more = True  # There are more items to be processed
        next_uri = None  # URI for the next items page query
        page = ReMoClient.FIRST_PAGE
        page += int(offset / ReMoClient.ITEMS_PER_PAGE)

        if category == 'events':
            api = self.api_events_url
        elif category == 'activities':
            api = self.api_activities_url
        elif category == 'users':
            api = self.api_users_url
        else:
            raise ValueError(category + ' not supported in ReMo')

        while more:
            params = {
                "page": page
            }

            raw_items = self.call(api, params)
            yield raw_items

            items_data = json.loads(raw_items)
            next_uri = items_data['next']

            if not next_uri:
                more = False
            else:
                # https://reps.mozilla.org/remo/api/remo/v1/events/?page=269
                page = next_uri.split("page=")[1]


class ReMoCommand(BackendCommand):
    """Class to run ReMo backend from the command line."""

    BACKEND = ReMo

    @staticmethod
    def setup_cmd_parser():
        """Returns the ReMo argument parser."""

        parser = BackendCommandArgumentParser(offset=True,
                                              cache=True)

        # ReMo options
        group = parser.parser.add_argument_group('ReMo arguments')
        group.add_argument('--category', default='events',
                           help="category could be events, activities or users")

        # Required arguments
        parser.parser.add_argument('url', nargs='?',
                                   default="https://reps.mozilla.org",
                                   help="ReMo URL (default: https://reps.mozilla.org)")

        return parser
