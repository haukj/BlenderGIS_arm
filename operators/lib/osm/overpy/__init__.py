from collections import OrderedDict
from decimal import Decimal
import re
import sys
import os
import time
import gzip
import io

from . import exception
from .__about__ import (
    __author__, __copyright__, __email__, __license__, __summary__, __title__,
    __uri__, __version__
)

import xml.etree.ElementTree as ET
import json

PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3

if PY2:
    from StringIO import StringIO
    from urllib2 import urlopen
    from urllib2 import HTTPError, URLError
elif PY3:
    from io import StringIO
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError

TIMEOUT = 120

DEFAULT_OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
)

def is_valid_type(element, cls):
    """
    Test if an element is of a given type.

    :param Element() element: The element instance to test
    :param Element cls: The element class to test
    :return: False or True
    :rtype: Boolean
    """
    return isinstance(element, cls) and element.id is not None

class Overpass(object):

    default_read_chunk_size = 4096

    def __init__(
        self,
        overpass_server="http://overpass-api.de/api/interpreter",
        read_chunk_size=None,
        referer=None,
        user_agent=None,
        fallback_servers=None,
        max_tries=3,
        timeout=TIMEOUT,
    ):
        self.referer = referer

        if not user_agent:
            try:
                user_agent = "{} / {} ({})".format(__title__, __version__, __uri__)
            except Exception:
                user_agent = "BlenderGIS Overpass Client"
        self.user_agent = user_agent

        self.timeout = timeout
        self.max_tries = max(1, int(max_tries))

        def _norm(u):
            u = (u or "").strip()
            if u.startswith("http://overpass-api.de/"):
                u = "https://" + u[len("http://"):]
            return u

        base = _norm(overpass_server)
        if not base:
            base = DEFAULT_OVERPASS_SERVERS[0]

        servers = [base]
        if fallback_servers:
            servers.extend([_norm(u) for u in fallback_servers if _norm(u)])
        else:
            servers.extend(list(DEFAULT_OVERPASS_SERVERS))

        dedup = []
        seen = set()
        for u in servers:
            if u and u not in seen:
                seen.add(u)
                dedup.append(u)

        self._servers = dedup
        self.url = self._servers[0]

        self._regex_extract_error_msg = re.compile(rb"<p>(?P<msg><strong\s.*?)</p>")
        self._regex_remove_tag = re.compile(b"<[^>]*?>")

        if read_chunk_size is None:
            read_chunk_size = self.default_read_chunk_size
        self.read_chunk_size = read_chunk_size

    def _read_all(self, f):
        chunks = []
        while True:
            data = f.read(self.read_chunk_size)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)

    def _get_header(self, f, name):
        if PY2:
            try:
                return f.info().getheader(name.lower())
            except Exception:
                return None
        try:
            v = f.getheader(name)
            if v:
                return v
        except Exception:
            pass
        try:
            return f.headers.get(name)
        except Exception:
            return None

    def query(self, query):
        if not isinstance(query, bytes):
            query = query.encode("utf-8")

        last_code = None
        last_exc = None

        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/octet-stream",
        }

        backoff_base = 1.25

        for server in self._servers:
            self.url = server

            for attempt in range(self.max_tries):
                req = Request(server, data=query)

                if self.referer:
                    req.add_header("Referer", self.referer)
                if self.user_agent:
                    req.add_header("User-Agent", self.user_agent)
                for k, v in headers.items():
                    req.add_header(k, v)

                try:
                    f = urlopen(req, timeout=self.timeout)
                except HTTPError as e:
                    f = e
                except URLError as e:
                    last_exc = e
                    time.sleep(backoff_base * (attempt + 1))
                    continue

                try:
                    response = self._read_all(f)
                finally:
                    try:
                        f.close()
                    except Exception:
                        pass

                code = getattr(f, "code", None)
                last_code = code

                content_encoding = (self._get_header(f, "Content-Encoding") or "").lower()
                if "gzip" in content_encoding:
                    try:
                        response = gzip.GzipFile(fileobj=io.BytesIO(response)).read()
                    except Exception:
                        pass

                if code == 200:
                    content_type = self._get_header(f, "Content-Type") or ""
                    ct = content_type.split(";")[0].strip().lower()

                    if ct == "application/json":
                        return self.parse_json(response)

                    if ct in ("application/osm3s+xml", "application/xml", "text/xml"):
                        return self.parse_xml(response)

                    head = response.lstrip()[:1]
                    if head == b"{":
                        return self.parse_json(response)
                    if head == b"<":
                        return self.parse_xml(response)

                    raise exception.OverpassUnknownContentType(content_type)

                if code == 400:
                    msgs = []
                    for msg in self._regex_extract_error_msg.finditer(response):
                        tmp = self._regex_remove_tag.sub(b"", msg.group("msg"))
                        try:
                            tmp = tmp.decode("utf-8")
                        except UnicodeDecodeError:
                            tmp = repr(tmp)
                        msgs.append(tmp)

                    raise exception.OverpassBadRequest(query, msgs=msgs)

                if code == 429:
                    time.sleep(max(2.0, backoff_base * (attempt + 1)))
                    if attempt + 1 >= self.max_tries:
                        raise exception.OverpassTooManyRequests
                    continue

                if code == 504:
                    time.sleep(backoff_base * (attempt + 1))
                    if attempt + 1 >= self.max_tries:
                        raise exception.OverpassGatewayTimeout
                    continue

                if code in (403, 502, 503):
                    time.sleep(backoff_base * (attempt + 1))
                    break

                raise exception.OverpassUnknownHTTPStatusCode(code)

        if last_exc is not None:
            raise last_exc
        raise exception.OverpassUnknownHTTPStatusCode(last_code if last_code is not None else 0)

class Result(object):

    """
    Class to handle the result.
    """

    def __init__(self, elements=None, api=None):
        """

        :param List elements:
        :param api:
        :type api: overpy.Overpass
        """
        if elements is None:
            elements = []
        self._nodes = OrderedDict((element.id, element) for element in elements if is_valid_type(element, Node))
        self._ways = OrderedDict((element.id, element) for element in elements if is_valid_type(element, Way))
        self._relations = OrderedDict((element.id, element)
                                      for element in elements if is_valid_type(element, Relation))
        self._class_collection_map = {Node: self._nodes, Way: self._ways, Relation: self._relations}
        self.api = api
        self._bounds = {}

    def expand(self, other):
        """
        Add all elements from an other result to the list of elements of this result object.

        It is used by the auto resolve feature.

        :param other: Expand the result with the elements from this result.
        :type other: overpy.Result
        :raises ValueError: If provided parameter is not instance of :class:`overpy.Result`
        """
        if not isinstance(other, Result):
            raise ValueError("Provided argument has to be instance of overpy:Result()")

        other_collection_map = {Node: other.nodes, Way: other.ways, Relation: other.relations}
        for element_type, own_collection in self._class_collection_map.items():
            for element in other_collection_map[element_type]:
                if is_valid_type(element, element_type) and element.id not in own_collection:
                    own_collection[element.id] = element

    def append(self, element):
        """
        Append a new element to the result.

        :param element: The element to append
        :type element: overpy.Element
        """
        if is_valid_type(element, Element):
            self._class_collection_map[element.__class__].setdefault(element.id, element)

    def get_elements(self, filter_cls, elem_id=None):
        """
        Get a list of elements from the result and filter the element type by a class.

        :param filter_cls:
        :param elem_id: ID of the object
        :type elem_id: Integer
        :return: List of available elements
        :rtype: List
        """
        result = []
        if elem_id is not None:
            try:
                result = [self._class_collection_map[filter_cls][elem_id]]
            except KeyError:
                result = []
        else:
            for e in self._class_collection_map[filter_cls].values():
                result.append(e)
        return result

    def get_ids(self, filter_cls):
        """

        :param filter_cls:
        :return:
        """
        return list(self._class_collection_map[filter_cls].keys())

    def get_node_ids(self):
        return self.get_ids(filter_cls=Node)

    def get_way_ids(self):
        return self.get_ids(filter_cls=Way)

    def get_relation_ids(self):
        return self.get_ids(filter_cls=Relation)

    @classmethod
    def from_json(cls, data, api=None):
        """
        Create a new instance and load data from json object.

        :param data: JSON data returned by the Overpass API
        :type data: Dict
        :param api:
        :type api: overpy.Overpass
        :return: New instance of Result object
        :rtype: overpy.Result
        """
        result = cls(api=api)
        for elem_cls in [Node, Way, Relation]:
            for element in data.get("elements", []):
                e_type = element.get("type")
                if hasattr(e_type, "lower") and e_type.lower() == elem_cls._type_value:
                    result.append(elem_cls.from_json(element, result=result))

        return result

    @classmethod
    def from_xml(cls, data, api=None, iterparse=False):
        """
        Create a new instance and load data from xml object.

        :param data: Root element
        :type data: xml.etree.ElementTree.Element
        :param api:
        :type api: Overpass
        :return: New instance of Result object
        :rtype: Result
        """
        result = cls(api=api)

        try:
            isFile = os.path.exists(data)
        except:
            isFile = False

        if not iterparse:
            #Method 1 : full parsing at once
            if isFile:
                with open(data, 'r', encoding='utf-8') as f:
                    data = f.read() #all file in memory
            root = ET.fromstring(data)
            for elem_cls in [Node, Way, Relation]:
                for child in root:
                    if child.tag.lower() == elem_cls._type_value:
                        result.append(elem_cls.from_xml(child, result=result))
        else:
            #Method 2 : iter parsing (memory friendly)
            #WARNING Issue #198
            if not isFile:
                data = StringIO(data)
            root = ET.iterparse(data, events=("start", "end"))
            elem_clss = {'node':Node, 'way':Way, 'relation':Relation}
            for event, child in root:
                if event == 'start':
                    if child.tag.lower() == 'bounds':
                        result._bounds = {k:float(v) for k, v in child.attrib.items()}
                    if child.tag.lower() in elem_clss:
                        elem_cls = elem_clss[child.tag.lower()]
                        result.append(elem_cls.from_xml(child, result=result))
                elif event == 'end':
                    child.clear()

        return result

    def get_node(self, node_id, resolve_missing=False):
        """
        Get a node by its ID.

        :param node_id: The node ID
        :type node_id: Integer
        :param resolve_missing: Query the Overpass API if the node is missing in the result set.
        :return: The node
        :rtype: overpy.Node
        :raises overpy.exception.DataIncomplete: At least one referenced node is not available in the result cache.
        :raises overpy.exception.DataIncomplete: If resolve_missing is True and at least one node can't be resolved.
        """
        nodes = self.get_nodes(node_id=node_id)
        if len(nodes) == 0:
            if not resolve_missing:
                raise exception.DataIncomplete("Resolve missing nodes is disabled")

            query = ("\n"
                     "[out:json];\n"
                     "node({node_id});\n"
                     "out body;\n"
                     )
            query = query.format(
                node_id=node_id
            )
            tmp_result = self.api.query(query)
            self.expand(tmp_result)

            nodes = self.get_nodes(node_id=node_id)

        if len(nodes) == 0:
            raise exception.DataIncomplete("Unable to resolve all nodes")

        return nodes[0]

    def get_nodes(self, node_id=None, **kwargs):
        """
        Alias for get_elements() but filter the result by Node()

        :param node_id: The Id of the node
        :type node_id: Integer
        :return: List of elements
        """
        return self.get_elements(Node, elem_id=node_id, **kwargs)

    def get_relation(self, rel_id, resolve_missing=False):
        """
        Get a relation by its ID.

        :param rel_id: The relation ID
        :type rel_id: Integer
        :param resolve_missing: Query the Overpass API if the relation is missing in the result set.
        :return: The relation
        :rtype: overpy.Relation
        :raises overpy.exception.DataIncomplete: The requested relation is not available in the result cache.
        :raises overpy.exception.DataIncomplete: If resolve_missing is True and the relation can't be resolved.
        """
        relations = self.get_relations(rel_id=rel_id)
        if len(relations) == 0:
            if resolve_missing is False:
                raise exception.DataIncomplete("Resolve missing relations is disabled")

            query = ("\n"
                     "[out:json];\n"
                     "relation({relation_id});\n"
                     "out body;\n"
                     )
            query = query.format(
                relation_id=rel_id
            )
            tmp_result = self.api.query(query)
            self.expand(tmp_result)

            relations = self.get_relations(rel_id=rel_id)

        if len(relations) == 0:
            raise exception.DataIncomplete("Unable to resolve requested reference")

        return relations[0]

    def get_relations(self, rel_id=None, **kwargs):
        """
        Alias for get_elements() but filter the result by Relation

        :param rel_id: Id of the relation
        :type rel_id: Integer
        :return: List of elements
        """
        return self.get_elements(Relation, elem_id=rel_id, **kwargs)

    def get_way(self, way_id, resolve_missing=False):
        """
        Get a way by its ID.

        :param way_id: The way ID
        :type way_id: Integer
        :param resolve_missing: Query the Overpass API if the way is missing in the result set.
        :return: The way
        :rtype: overpy.Way
        :raises overpy.exception.DataIncomplete: The requested way is not available in the result cache.
        :raises overpy.exception.DataIncomplete: If resolve_missing is True and the way can't be resolved.
        """
        ways = self.get_ways(way_id=way_id)
        if len(ways) == 0:
            if resolve_missing is False:
                raise exception.DataIncomplete("Resolve missing way is disabled")

            query = ("\n"
                     "[out:json];\n"
                     "way({way_id});\n"
                     "out body;\n"
                     )
            query = query.format(
                way_id=way_id
            )
            tmp_result = self.api.query(query)
            self.expand(tmp_result)

            ways = self.get_ways(way_id=way_id)

        if len(ways) == 0:
            raise exception.DataIncomplete("Unable to resolve requested way")

        return ways[0]

    def get_ways(self, way_id=None, **kwargs):
        """
        Alias for get_elements() but filter the result by Way

        :param way_id: The Id of the way
        :type way_id: Integer
        :return: List of elements
        """
        return self.get_elements(Way, elem_id=way_id, **kwargs)

    def get_bounds(self):
        if not self._bounds:
            lons, lats = zip(*[(e.lon, e.lat) for e in self._nodes.values()])
            self._bounds['minlon'] = float(min(lons))
            self._bounds['maxlon'] = float(max(lons))
            self._bounds['minlat'] = float(min(lats))
            self._bounds['maxlat'] = float(max(lats))
        return self._bounds

    node_ids = property(get_node_ids)
    nodes = property(get_nodes)
    relation_ids = property(get_relation_ids)
    relations = property(get_relations)
    way_ids = property(get_way_ids)
    ways = property(get_ways)
    bounds = property(get_bounds)

class Element(object):

    """
    Base element
    """

    def __init__(self, attributes=None, result=None, tags=None):
        """
        :param attributes: Additional attributes
        :type attributes: Dict
        :param result: The result object this element belongs to
        :param tags: List of tags
        :type tags: Dict
        """

        self._result = result
        self.attributes = attributes
        self.id = None
        self.tags = tags


class Node(Element):

    """
    Class to represent an element of type node
    """

    _type_value = "node"

    def __init__(self, node_id=None, lat=None, lon=None, **kwargs):
        """
        :param lat: Latitude
        :type lat: Decimal or Float
        :param lon: Longitude
        :type long: Decimal or Float
        :param node_id: Id of the node element
        :type node_id: Integer
        :param kwargs: Additional arguments are passed directly to the parent class
        """

        Element.__init__(self, **kwargs)
        self.id = node_id
        self.lat = lat
        self.lon = lon

    def __repr__(self):
        return "<overpy.Node id={} lat={} lon={}>".format(self.id, self.lat, self.lon)

    @classmethod
    def from_json(cls, data, result=None):
        """
        Create new Node element from JSON data

        :param data: Element data from JSON
        :type data: Dict
        :param result: The result this element belongs to
        :type result: overpy.Result
        :return: New instance of Node
        :rtype: overpy.Node
        :raises overpy.exception.ElementDataWrongType: If type value of the passed JSON data does not match.
        """
        if data.get("type") != cls._type_value:
            raise exception.ElementDataWrongType(
                type_expected=cls._type_value,
                type_provided=data.get("type")
            )

        tags = data.get("tags", {})

        node_id = data.get("id")
        lat = data.get("lat")
        lon = data.get("lon")

        attributes = {}
        ignore = ["type", "id", "lat", "lon", "tags"]
        for n, v in data.items():
            if n in ignore:
                continue
            attributes[n] = v

        return cls(node_id=node_id, lat=lat, lon=lon, tags=tags, attributes=attributes, result=result)

    @classmethod
    def from_xml(cls, child, result=None):
        """
        Create new way element from XML data

        :param child: XML node to be parsed
        :type child: xml.etree.ElementTree.Element
        :param result: The result this node belongs to
        :type result: overpy.Result
        :return: New Way oject
        :rtype: overpy.Node
        :raises overpy.exception.ElementDataWrongType: If name of the xml child node doesn't match
        :raises ValueError: If a tag doesn't have a name
        """
        if child.tag.lower() != cls._type_value:
            raise exception.ElementDataWrongType(
                type_expected=cls._type_value,
                type_provided=child.tag.lower()
            )

        tags = {}

        for sub_child in child:
            if sub_child.tag.lower() == "tag":
                name = sub_child.attrib.get("k")
                if name is None:
                    raise ValueError("Tag without name/key.")
                value = sub_child.attrib.get("v")
                tags[name] = value

        node_id = child.attrib.get("id")
        if node_id is not None:
            node_id = int(node_id)
        lat = child.attrib.get("lat")
        if lat is not None:
            lat = Decimal(lat)
        lon = child.attrib.get("lon")
        if lon is not None:
            lon = Decimal(lon)

        attributes = {}
        ignore = ["id", "lat", "lon"]
        for n, v in child.attrib.items():
            if n in ignore:
                continue
            attributes[n] = v

        return cls(node_id=node_id, lat=lat, lon=lon, tags=tags, attributes=attributes, result=result)


class Way(Element):

    """
    Class to represent an element of type way
    """

    _type_value = "way"

    def __init__(self, way_id=None, node_ids=None, **kwargs):
        """
        :param node_ids: List of node IDs
        :type node_ids: List or Tuple
        :param way_id: Id of the way element
        :type way_id: Integer
        :param kwargs: Additional arguments are passed directly to the parent class

        """

        Element.__init__(self, **kwargs)
        #: The id of the way
        self.id = way_id

        #: List of Ids of the associated nodes
        self._node_ids = node_ids

    def __repr__(self):
        return "<overpy.Way id={} nodes={}>".format(self.id, self._node_ids)

    @property
    def nodes(self):
        """
        List of nodes associated with the way.
        """
        return self.get_nodes()

    def get_nodes(self, resolve_missing=False):
        """
        Get the nodes defining the geometry of the way

        :param resolve_missing: Try to resolve missing nodes.
        :type resolve_missing: Boolean
        :return: List of nodes
        :rtype: List of overpy.Node
        :raises overpy.exception.DataIncomplete: At least one referenced node is not available in the result cache.
        :raises overpy.exception.DataIncomplete: If resolve_missing is True and at least one node can't be resolved.
        """
        result = []
        resolved = False

        for node_id in self._node_ids:
            try:
                node = self._result.get_node(node_id)
            except exception.DataIncomplete:
                node = None

            if node is not None:
                result.append(node)
                continue

            if not resolve_missing:
                raise exception.DataIncomplete("Resolve missing nodes is disabled")

            # We tried to resolve the data but some nodes are still missing
            if resolved:
                raise exception.DataIncomplete("Unable to resolve all nodes")

            query = ("\n"
                     "[out:json];\n"
                     "way({way_id});\n"
                     "node(w);\n"
                     "out body;\n"
                     )
            query = query.format(
                way_id=self.id
            )
            tmp_result = self._result.api.query(query)
            self._result.expand(tmp_result)
            resolved = True

            try:
                node = self._result.get_node(node_id)
            except exception.DataIncomplete:
                node = None

            if node is None:
                raise exception.DataIncomplete("Unable to resolve all nodes")

            result.append(node)

        return result

    @classmethod
    def from_json(cls, data, result=None):
        """
        Create new Way element from JSON data

        :param data: Element data from JSON
        :type data: Dict
        :param result: The result this element belongs to
        :type result: overpy.Result
        :return: New instance of Way
        :rtype: overpy.Way
        :raises overpy.exception.ElementDataWrongType: If type value of the passed JSON data does not match.
        """
        if data.get("type") != cls._type_value:
            raise exception.ElementDataWrongType(
                type_expected=cls._type_value,
                type_provided=data.get("type")
            )

        tags = data.get("tags", {})

        way_id = data.get("id")
        node_ids = data.get("nodes")

        attributes = {}
        ignore = ["id", "nodes", "tags", "type"]
        for n, v in data.items():
            if n in ignore:
                continue
            attributes[n] = v

        return cls(way_id=way_id, attributes=attributes, node_ids=node_ids, tags=tags, result=result)

    @classmethod
    def from_xml(cls, child, result=None):
        """
        Create new way element from XML data

        :param child: XML node to be parsed
        :type child: xml.etree.ElementTree.Element
        :param result: The result this node belongs to
        :type result: overpy.Result
        :return: New Way oject
        :rtype: overpy.Way
        :raises overpy.exception.ElementDataWrongType: If name of the xml child node doesn't match
        :raises ValueError: If the ref attribute of the xml node is not provided
        :raises ValueError: If a tag doesn't have a name
        """
        if child.tag.lower() != cls._type_value:
            raise exception.ElementDataWrongType(
                type_expected=cls._type_value,
                type_provided=child.tag.lower()
            )

        tags = {}
        node_ids = []

        for sub_child in child:
            if sub_child.tag.lower() == "tag":
                name = sub_child.attrib.get("k")
                if name is None:
                    raise ValueError("Tag without name/key.")
                value = sub_child.attrib.get("v")
                tags[name] = value
            if sub_child.tag.lower() == "nd":
                ref_id = sub_child.attrib.get("ref")
                if ref_id is None:
                    raise ValueError("Unable to find required ref value.")
                ref_id = int(ref_id)
                node_ids.append(ref_id)

        way_id = child.attrib.get("id")
        if way_id is not None:
            way_id = int(way_id)

        attributes = {}
        ignore = ["id"]
        for n, v in child.attrib.items():
            if n in ignore:
                continue
            attributes[n] = v

        return cls(way_id=way_id, attributes=attributes, node_ids=node_ids, tags=tags, result=result)


class Relation(Element):

    """
    Class to represent an element of type relation
    """

    _type_value = "relation"

    def __init__(self, rel_id=None, members=None, **kwargs):
        """
        :param members:
        :param rel_id: Id of the relation element
        :type rel_id: Integer
        :param kwargs:
        :return:
        """

        Element.__init__(self, **kwargs)
        self.id = rel_id
        self.members = members

    def __repr__(self):
        return "<overpy.Relation id={}>".format(self.id)

    @classmethod
    def from_json(cls, data, result=None):
        """
        Create new Relation element from JSON data

        :param data: Element data from JSON
        :type data: Dict
        :param result: The result this element belongs to
        :type result: overpy.Result
        :return: New instance of Relation
        :rtype: overpy.Relation
        :raises overpy.exception.ElementDataWrongType: If type value of the passed JSON data does not match.
        """
        if data.get("type") != cls._type_value:
            raise exception.ElementDataWrongType(
                type_expected=cls._type_value,
                type_provided=data.get("type")
            )

        tags = data.get("tags", {})

        rel_id = data.get("id")

        members = []

        supported_members = [RelationNode, RelationWay, RelationRelation]
        for member in data.get("members", []):
            type_value = member.get("type")
            for member_cls in supported_members:
                if member_cls._type_value == type_value:
                    members.append(
                        member_cls.from_json(
                            member,
                            result=result
                        )
                    )

        attributes = {}
        ignore = ["id", "members", "tags", "type"]
        for n, v in data.items():
            if n in ignore:
                continue
            attributes[n] = v

        return cls(rel_id=rel_id, attributes=attributes, members=members, tags=tags, result=result)

    @classmethod
    def from_xml(cls, child, result=None):
        """
        Create new way element from XML data

        :param child: XML node to be parsed
        :type child: xml.etree.ElementTree.Element
        :param result: The result this node belongs to
        :type result: overpy.Result
        :return: New Way oject
        :rtype: overpy.Relation
        :raises overpy.exception.ElementDataWrongType: If name of the xml child node doesn't match
        :raises ValueError: If a tag doesn't have a name
        """
        if child.tag.lower() != cls._type_value:
            raise exception.ElementDataWrongType(
                type_expected=cls._type_value,
                type_provided=child.tag.lower()
            )

        tags = {}
        members = []

        supported_members = [RelationNode, RelationWay, RelationRelation]
        for sub_child in child:
            if sub_child.tag.lower() == "tag":
                name = sub_child.attrib.get("k")
                if name is None:
                    raise ValueError("Tag without name/key.")
                value = sub_child.attrib.get("v")
                tags[name] = value
            if sub_child.tag.lower() == "member":
                type_value = sub_child.attrib.get("type")
                for member_cls in supported_members:
                    if member_cls._type_value == type_value:
                        members.append(
                            member_cls.from_xml(
                                sub_child,
                                result=result
                            )
                        )

        rel_id = child.attrib.get("id")
        if rel_id is not None:
            rel_id = int(rel_id)

        attributes = {}
        ignore = ["id"]
        for n, v in child.attrib.items():
            if n in ignore:
                continue
            attributes[n] = v

        return cls(rel_id=rel_id, attributes=attributes, members=members, tags=tags, result=result)


class RelationMember(object):

    """
    Base class to represent a member of a relation.
    """

    def __init__(self, ref=None, role=None, result=None):
        """
        :param ref: Reference Id
        :type ref: Integer
        :param role: The role of the relation member
        :type role: String
        :param result:
        """
        self.ref = ref
        self._result = result
        self.role = role

    @classmethod
    def from_json(cls, data, result=None):
        """
        Create new RelationMember element from JSON data

        :param child: Element data from JSON
        :type child: Dict
        :param result: The result this element belongs to
        :type result: overpy.Result
        :return: New instance of RelationMember
        :rtype: overpy.RelationMember
        :raises overpy.exception.ElementDataWrongType: If type value of the passed JSON data does not match.
        """
        if data.get("type") != cls._type_value:
            raise exception.ElementDataWrongType(
                type_expected=cls._type_value,
                type_provided=data.get("type")
            )

        ref = data.get("ref")
        role = data.get("role")
        return cls(ref=ref, role=role, result=result)

    @classmethod
    def from_xml(cls, child, result=None):
        """
        Create new RelationMember from XML data

        :param child: XML node to be parsed
        :type child: xml.etree.ElementTree.Element
        :param result: The result this element belongs to
        :type result: overpy.Result
        :return: New relation member oject
        :rtype: overpy.RelationMember
        :raises overpy.exception.ElementDataWrongType: If name of the xml child node doesn't match
        """
        if child.attrib.get("type") != cls._type_value:
            raise exception.ElementDataWrongType(
                type_expected=cls._type_value,
                type_provided=child.tag.lower()
            )

        ref = child.attrib.get("ref")
        if ref is not None:
            ref = int(ref)
        role = child.attrib.get("role")
        return cls(ref=ref, role=role, result=result)


class RelationNode(RelationMember):
    _type_value = "node"

    def resolve(self, resolve_missing=False):
        return self._result.get_node(self.ref, resolve_missing=resolve_missing)

    def __repr__(self):
        return "<overpy.RelationNode ref={} role={}>".format(self.ref, self.role)


class RelationWay(RelationMember):
    _type_value = "way"

    def resolve(self, resolve_missing=False):
        return self._result.get_way(self.ref, resolve_missing=resolve_missing)

    def __repr__(self):
        return "<overpy.RelationWay ref={} role={}>".format(self.ref, self.role)


class RelationRelation(RelationMember):
    _type_value = "relation"

    def resolve(self, resolve_missing=False):
        return self._result.get_relation(self.ref, resolve_missing=resolve_missing)

    def __repr__(self):
        return "<overpy.RelationRelation ref={} role={}>".format(self.ref, self.role)
