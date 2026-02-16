import logging
import sys
import site
import os

log = logging.getLogger(__name__)


def _ensure_user_site_on_path():
    """
    Sørg for at pip sitt user site-packages ligger på sys.path,
    slik at pakker installert med `--user` (som GDAL) blir synlige for Blender.
    """
    try:
        user_site = site.getusersitepackages()
    except Exception as e:
        log.debug("getusersitepackages() failed: %r", e)
        return

    if not os.path.isdir(user_site):
        log.debug("User site-packages does not exist: %s", user_site)
        return

    if user_site not in sys.path:
        sys.path.append(user_site)
        log.debug("Added user site-packages to sys.path: %s", user_site)


# Kjør denne før vi tester GDAL / andre deps
_ensure_user_site_on_path()


# ------------------------------------------------------------------------------
# GDAL
# ------------------------------------------------------------------------------

try:
    from osgeo import gdal  # type: ignore
except Exception:
    HAS_GDAL = False
    log.debug("GDAL Python binding unavailable")
else:
    HAS_GDAL = True
    try:
        ver = getattr(gdal, "__version__", "unknown")
    except Exception:
        ver = "unknown"
    log.debug("GDAL Python binding available (version: %s)", ver)


# ------------------------------------------------------------------------------
# PyProj
# ------------------------------------------------------------------------------

try:
    import pyproj  # type: ignore
except Exception:
    HAS_PYPROJ = False
    log.debug("PyProj unavailable")
else:
    HAS_PYPROJ = True
    try:
        ver = getattr(pyproj, "__version__", "unknown")
    except Exception:
        ver = "unknown"
    log.debug("PyProj available (version: %s)", ver)


# ------------------------------------------------------------------------------
# PIL / Pillow
# ------------------------------------------------------------------------------

#PIL/Pillow
try:
    from PIL import Image  # type: ignore
except Exception:
    HAS_PIL = False
    log.debug('Pillow unavailable')
else:
    HAS_PIL = True
    try:
        import PIL  # type: ignore
        ver = getattr(PIL, "__version__", "unknown")
    except Exception:
        ver = "unknown"
    log.debug('Pillow available (version: %s)', ver)

    # ------------------------------------------------------------------
    # Backwards compatibility for old BlenderGIS code:
    # New Pillow (>=10) fjernet Image.isImageType, men npimg.py bruker den.
    # Vi monkeypatcher den inn igjen.
    # ------------------------------------------------------------------
    if not hasattr(Image, "isImageType"):
        def _isImageType(obj):
            # I praksis: returner True hvis dette er et Pillow Image-objekt
            return isinstance(obj, Image.Image)

        Image.isImageType = staticmethod(_isImageType)
        log.debug("Patched PIL.Image.isImageType for compatibility")

# ------------------------------------------------------------------------------
# ImageIO FreeImage plugin
# ------------------------------------------------------------------------------

try:
    from .lib import imageio  # type: ignore

    # Forsøk å sikre at FreeImage-lib er lastet / lastes ned
    imageio.plugins._freeimage.get_freeimage_lib()
except Exception:
    log.error("Cannot install ImageIO's Freeimage plugin", exc_info=True)
    HAS_IMGIO = False
else:
    HAS_IMGIO = True
    log.debug("ImageIO Freeimage plugin available")
