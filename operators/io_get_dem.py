# -*- coding:utf-8 -*-

import os
import time
import math
import io
import zipfile
import gzip
import logging

log = logging.getLogger(__name__)

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import bpy
import bmesh
from bpy.types import Operator
from bpy.props import (
    StringProperty, IntProperty, FloatProperty, BoolProperty,
    EnumProperty, FloatVectorProperty
)

from ..geoscene import GeoScene
from .utils import adjust3Dview, getBBOX, isTopView
from ..core.proj import SRS, reprojBbox

from ..core import settings
USER_AGENT = settings.user_agent

PKG, SUBPKG = __package__.split('.', maxsplit=1)

TIMEOUT = 120


_GEOTIFF_MAGIC = (b"II*\x00", b"MM\x00*")


def _safe_decode(b: bytes, limit: int = 400) -> str:
    try:
        return b[:limit].decode("utf-8", "replace")
    except Exception:
        return "<decode failed>"


def _peek_file(path: str, n: int = 512):
    if not os.path.exists(path):
        return -1, b""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(n)
    return size, head


def _is_tiff(head: bytes) -> bool:
    if not head:
        return False
    return any(head.startswith(m) for m in _GEOTIFF_MAGIC)


def _is_zip(head: bytes) -> bool:
    return head.startswith(b"PK\x03\x04")


def _is_gzip(head: bytes) -> bool:
    return head.startswith(b"\x1f\x8b")


def _approx_area_km2(bbox, epsg: int) -> float:
    try:
        dx = float(bbox.xmax - bbox.xmin)
        dy = float(bbox.ymax - bbox.ymin)
    except Exception:
        return 0.0

    if not (math.isfinite(dx) and math.isfinite(dy)):
        return 0.0

    dx = abs(dx)
    dy = abs(dy)

    if epsg == 4326:
        lat_mid = (bbox.ymin + bbox.ymax) * 0.5
        km_per_deg_lat = 111.0
        km_per_deg_lon = 111.0 * math.cos(math.radians(lat_mid))
        width_km = dx * km_per_deg_lon
        height_km = dy * km_per_deg_lat
        return max(0.0, width_km * height_km)

    return max(0.0, (dx / 1000.0) * (dy / 1000.0))


def _extract_first_tif_from_zip(zip_path: str, out_dir: str) -> str:
    with zipfile.ZipFile(zip_path, "r") as zf:
        tif_names = [n for n in zf.namelist() if n.lower().endswith((".tif", ".tiff"))]
        if not tif_names:
            raise RuntimeError("ZIP response contained no .tif/.tiff files")
        name = tif_names[0]
        out_path = os.path.join(out_dir, os.path.basename(name))
        with zf.open(name, "r") as src, open(out_path, "wb") as dst:
            dst.write(src.read())
        return out_path


def _ungzip_to_file(gz_path: str, out_path: str) -> str:
    with gzip.open(gz_path, "rb") as gz, open(out_path, "wb") as out:
        out.write(gz.read())
    return out_path


def _download_to_file(url: str, file_path: str):
    rq = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(rq, timeout=TIMEOUT) as response:
        status = getattr(response, "status", None)
        ctype = response.headers.get("Content-Type", "")
        clen = response.headers.get("Content-Length", "")
        data = response.read()
    with open(file_path, "wb") as out:
        out.write(data)
    return status, ctype, clen, len(data)


def _validate_and_normalize_download(file_path: str, content_type: str) -> str:
    size, head = _peek_file(file_path, 1024)

    print(f"[DEM] saved: {file_path}")
    print(f"[DEM] bytes on disk: {size}")
    print(f"[DEM] head(16): {head[:16]}")
    print(f"[DEM] head(ascii): {_safe_decode(head, 300)}")
    print(f"[DEM] content-type: {content_type}")

    if size <= 0:
        raise RuntimeError("Downloaded file is empty or missing")

    if _is_tiff(head):
        return file_path

    out_dir = os.path.dirname(file_path)

    if _is_zip(head) or ("zip" in (content_type or "").lower()):
        extracted = _extract_first_tif_from_zip(file_path, out_dir)
        size2, head2 = _peek_file(extracted, 32)
        print(f"[DEM] extracted tif: {extracted}")
        print(f"[DEM] extracted head(16): {head2[:16]}")
        if not _is_tiff(head2):
            raise RuntimeError("Extracted file is not a TIFF (unexpected ZIP payload)")
        return extracted

    if _is_gzip(head) or ("gzip" in (content_type or "").lower()):
        out_tif = os.path.splitext(file_path)[0] + ".tif"
        extracted = _ungzip_to_file(file_path, out_tif)
        size2, head2 = _peek_file(extracted, 32)
        print(f"[DEM] ungzipped tif: {extracted}")
        print(f"[DEM] ungzipped head(16): {head2[:16]}")
        if not _is_tiff(head2):
            raise RuntimeError("GZIP payload did not unpack to a TIFF")
        return extracted

    preview = _safe_decode(head, 400)
    raise RuntimeError(
        "Server did not return a GeoTIFF. "
        f"content-type={content_type!r}, head_preview={preview!r}"
    )


def _looks_like_geonorge_nhm_wms(url_template: str) -> bool:
    u = (url_template or "").lower()
    return (
        "wms.geonorge.no" in u
        and "wms.hoyde-dtm-nhm-25833" in u
        and "service=wms" in u
        and "request=getmap" in u
    )


def _build_geonorge_nhm_wcs_getcoverage_urls(bbox_25833, width: int = 1024, height: int = 1024):
    base = "https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833"

    minx, miny, maxx, maxy = bbox_25833.xmin, bbox_25833.ymin, bbox_25833.xmax, bbox_25833.ymax
    dx = float(maxx - minx)
    dy = float(maxy - miny)

    if not (math.isfinite(dx) and math.isfinite(dy)) or dx <= 0.0 or dy <= 0.0:
        raise ValueError(f"Invalid bbox for WCS: {(minx, miny, maxx, maxy)}")

    resx = dx / float(width)
    resy = dy / float(height)

    qs_common = (
        "SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage"
        "&CRS=EPSG:25833"
        f"&BBOX={minx},{miny},{maxx},{maxy}"
        "&FORMAT=GeoTIFF"
        f"&RESX={resx}&RESY={resy}"
    )

    url_a = f"{base}?{qs_common}&COVERAGE=dtm_25833"
    url_b = f"{base}?{qs_common}&CoverageID=dtm_25833"

    return [url_a, url_b]


def _safe_adjust_view(context, scn):
    try:
        context.view_layer.update()
    except Exception:
        pass

    try:
        bbox2 = getBBOX.fromScn(scn)
    except Exception:
        return

    try:
        dims = getattr(bbox2, "dimensions", None)
        if dims is None:
            return
        vals = []
        for d in dims:
            try:
                d = float(d)
            except Exception:
                continue
            if math.isfinite(d):
                vals.append(abs(d))
        if not vals:
            return
    except Exception:
        return

    try:
        adjust3Dview(context, bbox2, zoomToSelect=False)
    except Exception:
        pass


class IMPORTGIS_OT_dem_query(Operator):
    """Import elevation data from a web service"""

    bl_idname = "importgis.dem_query"
    bl_description = "Query for elevation data from a web service"
    bl_label = "Get elevation (DEM)"
    bl_options = {"UNDO"}

    def invoke(self, context, event):
        geoscn = GeoScene(context.scene)
        if not geoscn.isGeoref:
            self.report({"ERROR"}, "Scene is not georef")
            return {"CANCELLED"}
        if geoscn.isBroken:
            self.report({"ERROR"}, "Scene georef is broken, please fix it beforehand")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        prefs = context.preferences.addons[PKG].preferences
        layout = self.layout
        row = layout.row(align=True)
        row.prop(prefs, "demServer", text="Server")
        if "opentopography" in prefs.demServer:
            row = layout.row(align=True)
            row.prop(prefs, "opentopography_api_key", text="Api Key")

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        prefs = bpy.context.preferences.addons[PKG].preferences
        scn = context.scene
        geoscn = GeoScene(scn)
        _ = SRS(geoscn.crs)

        w = context.window
        w.cursor_set("WAIT")

        try:
            objs = bpy.context.selected_objects
            aObj = context.active_object

            if len(objs) == 1 and aObj and aObj.type == "MESH":
                onMesh = True
                bbox_scene = getBBOX.fromObj(aObj).toGeo(geoscn)
            elif isTopView(context):
                onMesh = False
                bbox_scene = getBBOX.fromTopView(context).toGeo(geoscn)
            else:
                self.report(
                    {"ERROR"},
                    "Define the query extent in top ortho view or by selecting a reference mesh",
                )
                return {"CANCELLED"}

            if bbox_scene.dimensions.x > 1000000 or bbox_scene.dimensions.y > 1000000:
                self.report({"ERROR"}, "Too large extent")
                return {"CANCELLED"}

            server_template = prefs.demServer or ""
            base_dir = os.path.dirname(bpy.data.filepath) if bpy.data.is_saved else bpy.app.tempdir
            ts = int(time.time())

            # --- Special handling: Geonorge NHM DTM WMS is NOT a DEM, it’s an image service.
            # Auto-upgrade to WCS GetCoverage GeoTIFF.
            if _looks_like_geonorge_nhm_wms(server_template):
                bbox_25833 = reprojBbox(geoscn.crs, 25833, bbox_scene)

                area_km2 = _approx_area_km2(bbox_25833, 25833)
                print(f"[DEM] approx request area (EPSG:25833): {area_km2:.3f} km^2")

                if area_km2 > 2_000.0:
                    self.report({"ERROR"}, "Requested extent too large for Geonorge WCS. Reduce extent.")
                    return {"CANCELLED"}

                urls = _build_geonorge_nhm_wcs_getcoverage_urls(bbox_25833, width=1024, height=1024)

                last_err = None
                for i, url in enumerate(urls):
                    filePath = os.path.join(base_dir, f"nhm_dtm_25833_{ts}_{i}.tif")
                    print(f"[DEM] url: {url}")
                    log.debug(url)
                    try:
                        status, ctype, clen, nbytes = _download_to_file(url, filePath)
                        print(f"[DEM] HTTP status={status} Content-Type={ctype} Content-Length={clen} bytes={nbytes}")
                        filePath = _validate_and_normalize_download(filePath, ctype)

                        rast_crs = "EPSG:25833"
                        if not onMesh:
                            bpy.ops.importgis.georaster(
                                "EXEC_DEFAULT",
                                filepath=filePath,
                                reprojection=True,
                                rastCRS=rast_crs,
                                importMode="DEM",
                                subdivision="subsurf",
                                demInterpolation=True,
                            )
                        else:
                            objectsLst = [str(i) for i, obj in enumerate(scn.collection.all_objects) if obj.name == context.active_object.name][0]
                            bpy.ops.importgis.georaster(
                                "EXEC_DEFAULT",
                                filepath=filePath,
                                reprojection=True,
                                rastCRS=rast_crs,
                                importMode="DEM",
                                subdivision="subsurf",
                                demInterpolation=True,
                                demOnMesh=True,
                                objectsLst=objectsLst,
                                clip=False,
                                fillNodata=False,
                            )

                        _safe_adjust_view(context, scn)
                        return {"FINISHED"}

                    except (HTTPError, URLError, TimeoutError, RuntimeError, IOError) as err:
                        last_err = err
                        log.error(f"[DEM] Geonorge WCS attempt failed: {err}", exc_info=True)
                        continue

                self.report({"ERROR"}, f"Geonorge WCS DEM download failed: {last_err}")
                return {"CANCELLED"}

            # --- Generic/OpenTopography-style path (expects W/E/S/N in degrees EPSG:4326)
            bbox_4326 = reprojBbox(geoscn.crs, 4326, bbox_scene)

            area_km2 = _approx_area_km2(bbox_4326, 4326)
            print(f"[DEM] approx request area (EPSG:4326): {area_km2:.0f} km^2")

            if area_km2 > 1_000_000:
                self.report({"ERROR"}, "Requested extent is extremely large (area > 1,000,000 km²). Reduce extent.")
                return {"CANCELLED"}

            if "SRTM" in server_template:
                if bbox_4326.ymin > 60:
                    self.report({"ERROR"}, "SRTM is not available beyond 60 degrees north")
                    return {"CANCELLED"}
                if bbox_4326.ymax < -56:
                    self.report({"ERROR"}, "SRTM is not available below 56 degrees south")
                    return {"CANCELLED"}

            if "opentopography" in server_template:
                if not getattr(prefs, "opentopography_api_key", ""):
                    self.report({"ERROR"}, "Please register to opentopography.org and request an API key")
                    return {"CANCELLED"}

            e = 0.002
            xmin, xmax = bbox_4326.xmin - e, bbox_4326.xmax + e
            ymin, ymax = bbox_4326.ymin - e, bbox_4326.ymax + e

            url = server_template.format(
                W=xmin, E=xmax, S=ymin, N=ymax, API_KEY=getattr(prefs, "opentopography_api_key", "")
            )
            log.debug(url)
            print(f"[DEM] url: {url}")

            filePath = os.path.join(base_dir, f"dem_{ts}.tif")

            try:
                status, ctype, clen, nbytes = _download_to_file(url, filePath)
                print(f"[DEM] HTTP status={status} Content-Type={ctype} Content-Length={clen} bytes={nbytes}")
            except HTTPError as err:
                body = b""
                try:
                    body = err.read() or b""
                except Exception:
                    body = b""
                log.error(
                    f"HTTPError url={url} code={getattr(err, 'code', None)} "
                    f"reason={getattr(err, 'reason', None)} body={_safe_decode(body, 400)}"
                )
                self.report({"ERROR"}, "DEM request failed (HTTPError). Check console/logs.")
                return {"CANCELLED"}
            except URLError as err:
                log.error(f"URLError url={url} reason={getattr(err, 'reason', None)}")
                self.report({"ERROR"}, "Cannot reach DEM web service. Check network / logs.")
                return {"CANCELLED"}
            except TimeoutError:
                log.error(f"Timeout url={url}")
                self.report({"ERROR"}, "DEM request timed out. Service may be overloaded. Retry later.")
                return {"CANCELLED"}
            except Exception as err:
                log.error(f"Download failed url={url} err={err}", exc_info=True)
                self.report({"ERROR"}, f"Download failed: {err}")
                return {"CANCELLED"}

            try:
                filePath = _validate_and_normalize_download(filePath, ctype)
            except Exception as err:
                log.error(f"Downloaded content invalid: {err}", exc_info=True)
                self.report({"ERROR"}, f"Downloaded content invalid: {err}")
                return {"CANCELLED"}

            if not onMesh:
                bpy.ops.importgis.georaster(
                    "EXEC_DEFAULT",
                    filepath=filePath,
                    reprojection=True,
                    rastCRS="EPSG:4326",
                    importMode="DEM",
                    subdivision="subsurf",
                    demInterpolation=True,
                )
            else:
                objectsLst = [str(i) for i, obj in enumerate(scn.collection.all_objects) if obj.name == context.active_object.name][0]
                bpy.ops.importgis.georaster(
                    "EXEC_DEFAULT",
                    filepath=filePath,
                    reprojection=True,
                    rastCRS="EPSG:4326",
                    importMode="DEM",
                    subdivision="subsurf",
                    demInterpolation=True,
                    demOnMesh=True,
                    objectsLst=objectsLst,
                    clip=False,
                    fillNodata=False,
                )

            _safe_adjust_view(context, scn)
            return {"FINISHED"}

        finally:
            try:
                w.cursor_set("DEFAULT")
            except Exception:
                pass


def register():
    try:
        bpy.utils.register_class(IMPORTGIS_OT_dem_query)
    except ValueError:
        log.warning("IMPORTGIS_OT_dem_query already registered, unregistering and retrying...")
        unregister()
        bpy.utils.register_class(IMPORTGIS_OT_dem_query)


def unregister():
    bpy.utils.unregister_class(IMPORTGIS_OT_dem_query)
