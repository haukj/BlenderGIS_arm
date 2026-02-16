# -*- coding:utf-8 -*-

import json
import logging
import urllib.parse
import urllib.request

import bpy
import bmesh
from bpy.types import Operator
from bpy.props import BoolProperty, EnumProperty

from .. import settings
from ..geoscene import GeoScene
from ..core.proj import reprojBbox, reprojPts
from .utils import adjust3Dview, getBBOX, isTopView

log = logging.getLogger(__name__)

NVDB_API_ENDPOINTS = [
	('https://nvdbapiles-v3.atlas.vegvesen.no', 'NVDB API LES v3 (prod)', 'Statens vegvesen NVDB lese-API'),
	('https://nvdbapiles-v3.test.atlas.vegvesen.no', 'NVDB API LES v3 (test)', 'Statens vegvesen NVDB test-API'),
]


def _extract_linestrings(payload):
	'''
	Extract line coordinates from common NVDB response formats.
	Returns list[list[(lon, lat)]].
	'''
	lines = []

	def parse_wkt_linestring(wkt):
		wkt = (wkt or '').strip()
		if not wkt:
			return None
		upper = wkt.upper()
		if not upper.startswith('LINESTRING'):
			return None
		try:
			coords_text = wkt[wkt.index('(') + 1:wkt.rindex(')')]
		except ValueError:
			return None
		pts = []
		for pair in coords_text.split(','):
			parts = [p for p in pair.strip().split(' ') if p]
			if len(parts) < 2:
				continue
			try:
				x = float(parts[0])
				y = float(parts[1])
			except ValueError:
				continue
			pts.append((x, y))
		return pts if len(pts) >= 2 else None

	def parse_geojson_geom(geom):
		if not isinstance(geom, dict):
			return None
		gtype = (geom.get('type') or '').upper()
		coords = geom.get('coordinates')
		if gtype == 'LINESTRING' and isinstance(coords, list):
			pts = []
			for c in coords:
				if isinstance(c, (list, tuple)) and len(c) >= 2:
					pts.append((float(c[0]), float(c[1])))
			return pts if len(pts) >= 2 else None
		if gtype == 'MULTILINESTRING' and isinstance(coords, list):
			parsed = []
			for line in coords:
				if not isinstance(line, list):
					continue
				pts = []
				for c in line:
					if isinstance(c, (list, tuple)) and len(c) >= 2:
						pts.append((float(c[0]), float(c[1])))
				if len(pts) >= 2:
					parsed.append(pts)
			return parsed if parsed else None
		return None

	if isinstance(payload, dict) and payload.get('type') == 'FeatureCollection':
		features = payload.get('features') or []
		for feat in features:
			geom = (feat or {}).get('geometry')
			parsed = parse_geojson_geom(geom)
			if isinstance(parsed, list) and parsed and isinstance(parsed[0], tuple):
				lines.append(parsed)
			elif isinstance(parsed, list):
				lines.extend(parsed)
		return lines

	candidates = []
	if isinstance(payload, dict):
		for k in ('objekter', 'vegnett', 'segmenter'):
			v = payload.get(k)
			if isinstance(v, list):
				candidates.extend(v)

	for item in candidates:
		if not isinstance(item, dict):
			continue
		geo = item.get('geometri') or {}
		parsed = parse_geojson_geom(geo)
		if parsed is None:
			parsed = parse_geojson_geom(item.get('geometry') or {})
		if parsed is None:
			parsed = parse_wkt_linestring(geo.get('wkt') if isinstance(geo, dict) else None)
		if parsed is None:
			continue
		if isinstance(parsed, list) and parsed and isinstance(parsed[0], tuple):
			lines.append(parsed)
		elif isinstance(parsed, list):
			lines.extend(parsed)

	return lines


class IMPORTGIS_OT_nvdb_query(Operator):
	"""Import road centerlines from Statens vegvesen NVDB API"""

	bl_idname = "importgis.nvdb_query"
	bl_description = 'Query NVDB road network covering current top view or selected mesh extent'
	bl_label = "Get NVDB road network"
	bl_options = {"UNDO"}

	endpoint: EnumProperty(
		name='NVDB endpoint',
		description='Velg Statens vegvesen NVDB API-endepunkt',
		items=NVDB_API_ENDPOINTS,
		default=NVDB_API_ENDPOINTS[0][0],
	)

	only_drivable: BoolProperty(
		name='Kun kjørende veger',
		description='Filtrer på trafikantgruppe K (kjørende)',
		default=True,
	)

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def _get_query_bbox_lonlat(self, context, geoscn):
		objs = context.selected_objects
		aObj = context.active_object
		if len(objs) == 1 and aObj and aObj.type == 'MESH':
			bbox = getBBOX.fromObj(aObj).toGeo(geoscn)
		elif isTopView(context):
			bbox = getBBOX.fromTopView(context).toGeo(geoscn)
		else:
			return None
		return reprojBbox(geoscn.crs, 4326, bbox)

	def execute(self, context):
		scn = context.scene
		geoscn = GeoScene(scn)

		if not geoscn.isGeoref:
			self.report({'ERROR'}, 'Scene is not georeferenced')
			return {'CANCELLED'}
		if geoscn.isBroken:
			self.report({'ERROR'}, 'Scene georef is broken, please fix it beforehand')
			return {'CANCELLED'}

		bbox = self._get_query_bbox_lonlat(context, geoscn)
		if bbox is None:
			self.report({'ERROR'}, 'Define extent with top orthographic view or one selected mesh object')
			return {'CANCELLED'}

		params = {
			'kartutsnitt': f'{bbox.xmin},{bbox.ymin},{bbox.xmax},{bbox.ymax}',
			'srid': 4326,
			'detaljniva': 'VT,VTKB',
		}
		if self.only_drivable:
			params['trafikantgruppe'] = 'K'

		url = self.endpoint.rstrip('/') + '/vegnett/veglenkesekvenser/segmentert?' + urllib.parse.urlencode(params)
		headers = {
			'Accept': 'application/json',
			'User-Agent': settings.user_agent,
			'Referer': 'https://www.vegvesen.no/',
		}

		log.info('Requesting NVDB: %s', url)
		request = urllib.request.Request(url=url, headers=headers)
		try:
			with urllib.request.urlopen(request, timeout=45) as resp:
				payload = json.loads(resp.read().decode('utf-8'))
		except Exception:
			log.error('NVDB query failed', exc_info=True)
			self.report({'ERROR'}, 'NVDB query failed. Check network and logs for details.')
			return {'CANCELLED'}

		lines_lonlat = _extract_linestrings(payload)
		if not lines_lonlat:
			self.report({'WARNING'}, 'No NVDB road segments found in requested area')
			return {'CANCELLED'}

		dx, dy = geoscn.getOriginPrj()
		obj_count = 0
		for i, line in enumerate(lines_lonlat, 1):
			try:
				pts = reprojPts(4326, geoscn.crs, line)
			except Exception:
				continue
			if len(pts) < 2:
				continue

			bm = bmesh.new()
			verts = [bm.verts.new((x - dx, y - dy, 0.0)) for x, y in pts]
			bm.verts.ensure_lookup_table()
			for vi in range(len(verts) - 1):
				try:
					bm.edges.new((verts[vi], verts[vi + 1]))
				except ValueError:
					pass

			me = bpy.data.meshes.new(f'NVDB_road_{i:04d}')
			bm.to_mesh(me)
			bm.free()
			obj = bpy.data.objects.new(me.name, me)
			scn.collection.objects.link(obj)
			obj_count += 1

		if obj_count == 0:
			self.report({'WARNING'}, 'NVDB response parsed, but no valid geometry could be built')
			return {'CANCELLED'}

		bbox_all = getBBOX.fromScn(scn)
		adjust3Dview(context, bbox_all, zoomToSelect=False)
		self.report({'INFO'}, f'Imported {obj_count} NVDB road segment objects')
		return {'FINISHED'}


classes = [
	IMPORTGIS_OT_nvdb_query,
]


def register():
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError:
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)


def unregister():
	for cls in classes:
		bpy.utils.unregister_class(cls)
