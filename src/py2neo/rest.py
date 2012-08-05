#!/usr/bin/env python

# Copyright 2011 Nigel Small
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""REST client based on httplib for use with Neo4j REST interface.
"""

try:
    import simplejson as json
except ImportError:
    import json
import httplib
import logging
import socket
import threading
import time
from urlparse import urlsplit


__author__    = "Nigel Small <py2neo@nigelsmall.org>"
__copyright__ = "Copyright 2011 Nigel Small"
__license__   = "Apache License, Version 2.0"


AUTO_REDIRECTS = [301, 302, 303, 307, 308]

logger = logging.getLogger(__name__)

_thread_local = threading.local()


def local_client():
    if not hasattr(_thread_local, "client"):
        _thread_local.client = Client()
    return _thread_local.client


class BadRequest(ValueError):

    def __init__(self, data):
        ValueError.__init__(self)
        self.data = data

    def __str__(self):
        return repr(self.data)


class ResourceNotFound(LookupError):

    def __init__(self, uri):
        LookupError.__init__(self)
        self.uri = uri

    def __str__(self):
        return repr(self.uri)


class ResourceConflict(EnvironmentError):

    def __init__(self, uri):
        EnvironmentError.__init__(self)
        self.uri = uri

    def __str__(self):
        return repr(self.uri)


class SocketError(IOError):

    def __init__(self, uri):
        IOError.__init__(self)
        self.uri = uri

    def __str__(self):
        return repr(self.uri)


class PropertyCache(object):

    def __init__(self, properties=None, max_age=None):
        self._properties = {}
        self.max_age = max_age
        self._last_updated_time = None
        if properties:
            self.update(properties)

    def __nonzero__(self):
        return bool(self._properties)

    def __len__(self):
        return len(self._properties)

    def __getitem__(self, item):
        return self._properties[item]

    def __setitem__(self, item, value):
        self._properties[item] = value

    def __delitem__(self, item):
        del self._properties[item]

    def __iter__(self):
        return self._properties.__iter__()

    def __contains__(self, item):
        return item in self._properties

    @property
    def expired(self):
        if self._last_updated_time and self.max_age:
            return time.time() - self._last_updated_time > self.max_age
        else:
            return None

    @property
    def needs_update(self):
        return not self._properties or self.expired

    def clear(self):
        self.update(None)

    def update(self, properties):
        self._properties.clear()
        if properties:
            self._properties.update(properties)
        self._last_updated_time = time.time()

    def get(self, key, default=None):
        return self._properties.get(key, default)

    def get_all(self):
        return self._properties


class URI(object):

    def __init__(self, uri, marker):
        bits = str(uri).rpartition(marker)
        self.base = bits[0]
        self.reference = "".join(bits[1:])

    def __repr__(self):
        return self.base + self.reference

    def __eq__(self, other):
        return str(self) == str(other)

    def __ne__(self, other):
        return str(self) != str(other)


class Request(object):

    def __init__(self, graph_db, method, uri, body=None):
        self.graph_db = graph_db
        self.method = method
        self.uri = uri
        self.body = body

    def description(self, id_):
        return {
            "id": id_,
            "method": self.method,
            "to": self.uri,
            "body": self.body,
        }

class Response(object):

    def __init__(self, graph_db, status, uri, location=None, body=None):
        self.graph_db = graph_db
        self.status = status
        self.uri = str(uri)
        self.location = location
        self.body = body


class Client(object):

    def __init__(self):
        self.http = {}
        self.https = {}
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Stream": "true",
        }

    def _connection(self, scheme, netloc, reconnect=False):
        if scheme == "http":
            return self._http_connection(netloc, reconnect)
        elif scheme == "https":
            return self._https_connection(netloc, reconnect)
        else:
            raise ValueError("Unsupported URI scheme: " + scheme)

    def _http_connection(self, netloc, reconnect=False):
        if netloc not in self.http or reconnect:
            self.http[netloc] = httplib.HTTPConnection(netloc)
        return self.http[netloc]

    def _https_connection(self, netloc, reconnect=False):
        if netloc not in self.https or reconnect:
            self.https[netloc] = httplib.HTTPSConnection(netloc)
        return self.https[netloc]

    def _send_request(self, method, uri, data=None):
        reconnect = False
        uri_values = urlsplit(str(uri))
        scheme, netloc = uri_values[0:2]
        for tries in range(1, 4):
            http = self._connection(scheme, netloc, reconnect)
            if uri_values[3]:
                path = uri_values[2] + "?" + uri_values[3]
            else:
                path = uri_values[2]
            if data is not None:
                data = json.dumps(data)
            logger.info("{0} {1}".format(method, path))
            try:
                http.request(method, path, data, self.headers)
                return http.getresponse()
            except httplib.HTTPException as err:
                if tries < 3:
                    reconnect = True
                else:
                    raise err

    def send(self, request, *args, **kwargs):
        rs = self._send_request(request.method, request.uri, request.body)
        if rs.status in AUTO_REDIRECTS:
            # automatic redirection - discard data and call recursively
            rs.read()
            request.uri = rs.getheader("Location")
            return self.send(request, *args, **kwargs)
        else:
            # direct response
            rs_body = rs.read()
            try:
                rs_body = json.loads(rs_body)
            except ValueError:
                rs_body = None
            return Response(request.graph_db, rs.status, request.uri, rs.getheader("Location", None), rs_body)


class Resource(object):
    """Web service resource class, designed to work with a well-behaved REST
    web service.

    :param uri:              the URI identifying this resource
    :param reference_marker:
    :param metadata:         previously obtained resource metadata
    """

    def __init__(self, uri, reference_marker, metadata=None):
        self._uri = URI(uri, reference_marker)
        self._last_location = None
        self._last_headers = None
        self._metadata = PropertyCache(metadata)

    def __repr__(self):
        """Return a valid Python representation of this object.
        """
        return "{0}('{1}')".format(self.__class__.__name__, repr(self._uri))

    def __eq__(self, other):
        """Determine equality of two objects based on URI.
        """
        return self._uri == other._uri

    def __ne__(self, other):
        """Determine inequality of two objects based on URI.
        """
        return self._uri != other._uri

    def _client(self):
        """Fetch the HTTP client for use by this resource.
        Uses the client belonging to the local thread.
        """
        global _thread_local
        if not hasattr(_thread_local, "client"):
            _thread_local.client = Client()
        return _thread_local.client

    def _send(self, request):
        """Issue an HTTP request.

        :param request: a rest.Request object
        :return: object created from returned content (200), C{Location} header value (201) or C{None} (204)
        :raise BadRequest: when supplied data is not appropriate (400)
        :raise ResourceNotFound: when URI is not found (404)
        :raise ResourceConflict: when a conflict occurs (409)
        :raise SystemError: when a server error occurs (500)
        :raise SocketError: when a connection fails or cannot be established
        """
        try:
            response = self._client().send(request)
            if response.status == 200:
                return response
            elif response.status == 201:
                return response
            elif response.status == 204:
                return None
            elif response.status == 400:
                raise BadRequest(response.body)
            elif response.status == 404:
                raise ResourceNotFound(request.uri)
            elif response.status == 409:
                raise ResourceConflict(request.uri)
            elif response.status // 100 == 5:
                raise SystemError(response.body)
        except socket.error as err:
            raise SocketError(err)

    def _lookup(self, key):
        """Look up a value in the resource metadata by key; will lazily load
        metadata if required.
        
        :param key: the key to look up
        """
        if self._metadata.needs_update:
            rs = self._send(Request(None, "GET", self._uri))
            self._metadata.update(rs.body)
        if key in self._metadata:
            return self._metadata[key]
        else:
            raise KeyError(key)

