
import bpy
import math
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree
from bpy_extras.view3d_utils import region_2d_to_location_3d, region_2d_to_vector_3d
from bpy_extras.view3d_utils import region_2d_to_origin_3d

from ...core import BBOX

def isTopView(context):
    if context.area.type == 'VIEW_3D':
        reg3d = context.region_data
    else:
        return False
    return reg3d.view_perspective == 'ORTHO' and tuple(reg3d.view_matrix.to_euler()) == (0,0,0)

def mouseTo3d(context, x, y, plane_z=0.0):
    '''Convert event.mouse_region to world coordinates'''
    if context.area.type != 'VIEW_3D':
        raise Exception('Wrong context')

    coords = (x, y)
    reg = context.region
    reg3d = context.region_data

    # Stable path for top-ortho: intersect view ray with Z=plane_z
    if reg3d.view_perspective == 'ORTHO' and tuple(reg3d.view_matrix.to_euler()) == (0,0,0):
        origin = region_2d_to_origin_3d(reg, reg3d, coords)
        direction = region_2d_to_vector_3d(reg, reg3d, coords)
        if abs(direction.z) < 1e-12:
            return origin
        t = (plane_z - origin.z) / direction.z
        return origin + direction * t

    # Fallback for other views (kept as-is)
    vec = region_2d_to_vector_3d(reg, reg3d, coords)
    loc = region_2d_to_location_3d(reg, reg3d, coords, vec)
    return loc


class DropToGround():
    '''A class to perform raycasting accross z axis'''

    def __init__(self, scn, ground, method='OBJ'):
        self.method = method # 'BVH' or 'OBJ'
        self.scn = scn
        self.ground = ground
        self.bbox = getBBOX.fromObj(ground, applyTransform=True)
        self.mw = self.ground.matrix_world
        self.mwi = self.mw.inverted()
        if self.method == 'BVH':
            self.bvh = BVHTree.FromObject(self.ground, bpy.context.evaluated_depsgraph_get(), deform=True)

    def rayCast(self, x, y):
        #Hit vector
        offset = 100
        orgWldSpace = Vector((x, y, self.bbox.zmax + offset))
        orgObjSpace = self.mwi @ orgWldSpace
        direction = Vector((0,0,-1)) #down
        #build ray cast hit namespace object
        class RayCastHit(): pass
        rcHit = RayCastHit()
        #raycast
        if self.method == 'OBJ':
            rcHit.hit, rcHit.loc, rcHit.normal, rcHit.faceIdx = self.ground.ray_cast(orgObjSpace, direction)
        elif self.method == 'BVH':
            rcHit.loc, rcHit.normal, rcHit.faceIdx, rcHit.dst = self.bvh.ray_cast(orgObjSpace, direction)
            if not rcHit.loc:
                rcHit.hit = False
            else:
                rcHit.hit = True
        #adjust values
        if not rcHit.hit:
            #return same original 2d point with z=0
            rcHit.loc = Vector((orgWldSpace.x, orgWldSpace.y, 0)) #elseZero
        else:
            rcHit.hit = True

        rcHit.loc = self.mw @ rcHit.loc
        return rcHit

def placeObj(mesh, objName):
    '''Build and add a new object from a given mesh'''
    bpy.ops.object.select_all(action='DESELECT')
    #create an object with that mesh
    obj = bpy.data.objects.new(objName, mesh)
    # Link object to scene
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    #bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')
    return obj


import math

def adjust3Dview(context, bbox, zoomToSelect=True):
    dims = getattr(bbox, "dimensions", None)
    if dims is None:
        print("[BlenderGIS] adjust3Dview: bbox has no .dimensions:", bbox)
        return

    finite = []
    for d in dims:
        try:
            d = float(d)
        except Exception:
            continue
        if math.isfinite(d):
            finite.append(abs(d))

    if not finite:
        print("[BlenderGIS] adjust3Dview: non-finite bbox dimensions:", list(dims))
        print("[BlenderGIS] bbox:", getattr(bbox, "__dict__", bbox))
        return

    dst = round(max(finite))
    k = 5
    dst = dst * k

    areas = context.screen.areas
    for area in areas:
        if area.type == 'VIEW_3D':
            space = area.spaces.active
            if dst < 100:
                space.clip_start = 1
            elif dst < 1000:
                space.clip_start = 10
            else:
                space.clip_start = 100

            if space.clip_end < dst:
                if dst > 10000000:
                    dst = 10000000
                space.clip_end = dst

            if zoomToSelect:
                overrideContext = context.copy()
                overrideContext['area'] = area
                overrideContext['region'] = area.regions[-1]
                if bpy.app.version[0] > 3:
                    with context.temp_override(**overrideContext):
                        bpy.ops.view3d.view_selected()
                else:
                    bpy.ops.view3d.view_selected(overrideContext)


def showTextures(context):
    '''Force view mode with textures'''
    scn = context.scene
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            space = area.spaces.active
            if space.shading.type == 'SOLID':
                space.shading.color_type = 'TEXTURE'


def addTexture(mat, img, uvLay, name='texture'):
    '''Set a new image texture to a given material and following a given uv map'''
    engine = bpy.context.scene.render.engine
    mat.use_nodes = True
    node_tree = mat.node_tree
    node_tree.nodes.clear()
    # create uv map node
    uvMapNode = node_tree.nodes.new('ShaderNodeUVMap')
    uvMapNode.uv_map = uvLay.name
    uvMapNode.location = (-800, 200)
    # create image texture node
    textureNode = node_tree.nodes.new('ShaderNodeTexImage')
    textureNode.image = img
    textureNode.extension = 'CLIP'
    textureNode.show_texture = True
    textureNode.location = (-400, 200)
    # Create BSDF diffuse node
    diffuseNode = node_tree.nodes.new('ShaderNodeBsdfPrincipled')#ShaderNodeBsdfDiffuse
    diffuseNode.location = (0, 200)
    # Create output node
    outputNode = node_tree.nodes.new('ShaderNodeOutputMaterial')
    outputNode.location = (400, 200)
    # Connect the nodes
    node_tree.links.new(uvMapNode.outputs['UV'] , textureNode.inputs['Vector'])
    node_tree.links.new(textureNode.outputs['Color'] , diffuseNode.inputs['Base Color'])#diffuseNode.inputs['Color'])
    node_tree.links.new(diffuseNode.outputs['BSDF'] , outputNode.inputs['Surface'])


class getBBOX():

    '''Utilities to build BBOX object from various Blender context'''

    @staticmethod
    def fromObj(obj, applyTransform=True):
        if applyTransform:
            boundPts = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        else:
            boundPts = [Vector(corner) for corner in obj.bound_box]

        for p in boundPts:
            if any(not math.isfinite(float(v)) for v in p):
                raise ValueError(f"Non-finite bound_box for object: {obj.name}")

        xs = [pt.x for pt in boundPts]
        ys = [pt.y for pt in boundPts]
        zs = [pt.z for pt in boundPts]
        return BBOX(xmin=min(xs), ymin=min(ys), zmin=min(zs), xmax=max(xs), ymax=max(ys), zmax=max(zs))
    @classmethod
    def fromScn(cls, scn):
        objs = [obj for obj in scn.collection.all_objects if obj.empty_display_type != "IMAGE"]

        scnBbox = None
        for obj in objs:
            try:
                bbox = cls.fromObj(obj)
            except Exception as e:
                print("[BlenderGIS] Skipping object in bbox union:", obj.name, "|", e)
                continue
            scnBbox = bbox if scnBbox is None else (scnBbox + bbox)

        return scnBbox if scnBbox is not None else BBOX(0, 0, 0, 0, 0, 0)
    @staticmethod
    def fromBmesh(bm):
        '''Create a 3D bounding box from a bmesh object'''
        xmin = min([pt.co.x for pt in bm.verts])
        xmax = max([pt.co.x for pt in bm.verts])
        ymin = min([pt.co.y for pt in bm.verts])
        ymax = max([pt.co.y for pt in bm.verts])
        zmin = min([pt.co.z for pt in bm.verts])
        zmax = max([pt.co.z for pt in bm.verts])
        #
        return BBOX(xmin=xmin, ymin=ymin, zmin=zmin, xmax=xmax, ymax=ymax, zmax=zmax)

    @staticmethod
    def fromTopView(context):
        '''Create a 2D BBOX from Blender 3dview if the view is top left ortho else return None'''
        scn = context.scene
        area = context.area
        if area.type != 'VIEW_3D':
            return None
        reg = context.region
        reg3d = context.region_data
        if reg3d.view_perspective != 'ORTHO' or tuple(reg3d.view_matrix.to_euler()) != (0,0,0):
            print("View3d must be in top ortho")
            return None
        #
        loc = mouseTo3d(context, area.width, area.height)
        xmax, ymax = loc.x, loc.y
        #
        loc = mouseTo3d(context, 0, 0)
        xmin, ymin = loc.x, loc.y
        #
        return BBOX(xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax)
