# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
Utilities for reading or working with Camera geometry files
"""
import logging
from collections import defaultdict

import numpy as np
from astropy import units as u
from astropy.coordinates import Angle
from astropy.table import Table
from astropy.utils import lazyproperty
from scipy.spatial import cKDTree as KDTree

from ctapipe.utils import get_dataset, find_all_matching_datasets
from ctapipe.utils.linalg import rotation_matrix_2d

__all__ = ['CameraGeometry',
           'get_camera_types',
           'print_camera_types']

logger = logging.getLogger(__name__)

# dictionary to convert number of pixels to camera + the focal length of the
# telescope into a camera type for use in `CameraGeometry.guess()`
#     Key = (num_pix, focal_length_in_meters)
#     Value = (type, subtype, pixtype, pixrotation, camrotation)
_CAMERA_GEOMETRY_TABLE = {
    (2048, 2.3): ('SST', 'GATE', 'rectangular', 0 * u.degree, 0 * u.degree),
    (2048, 2.2): ('SST', 'GATE', 'rectangular', 0 * u.degree, 0 * u.degree),
    (2048, 36.0): ('LST', 'HESS-II', 'hexagonal', 0 * u.degree,
                   0 * u.degree),
    (960, None): ('MST', 'HESS-I', 'hexagonal', 0 * u.degree,
                   0 * u.degree),
    (1855, 16.0): ('MST', 'NectarCam', 'hexagonal',
                   0 * u.degree, -100.893 * u.degree),
    (1855, 28.0): ('LST', 'LSTCam', 'hexagonal',
                   0. * u.degree, -100.893 * u.degree),
    (1296, None): ('SST', 'DigiCam', 'hexagonal', 30 * u.degree, 0 * u.degree),
    (1764, None): ('MST', 'FlashCam', 'hexagonal', 30 * u.degree, 0 * u.degree),
    (2368, None): ('SST', 'ASTRICam', 'rectangular', 0 * u.degree,
                   0 * u.degree),
    (11328, None): ('SCT', 'SCTCam', 'rectangular', 0 * u.degree, 0 * u.degree),
}


class CameraGeometry:
    """`CameraGeometry` is a class that stores information about a
    Cherenkov Camera that us useful for imaging algorithms and
    displays. It contains lists of pixel positions, areas, pixel
    shapes, as well as a neighbor (adjacency) list and matrix for each pixel. 
    In general the neighbor_matrix attribute should be used in any algorithm 
    needing pixel neighbors, since it is much faster. See for example 
    `ctapipe.image.tailcuts_clean` 

    The class is intended to be generic, and work with any Cherenkov
    Camera geometry, including those that have square vs hexagonal
    pixels, gaps between pixels, etc.
    
    You can construct a CameraGeometry either by specifying all data, 
    or using the `CameraGeometry.guess()` constructor, which takes metadata 
    like the pixel positions and telescope focal length to look up the rest 
    of the data. Note that this function is memoized, so calling it multiple 
    times with the same inputs will give back the same object (for speed).
    
    """

    _geometry_cache = {}  # dictionary CameraGeometry instances for speed

    def __init__(self, cam_id, pix_id, pix_x, pix_y, pix_area, pix_type,
                 pix_rotation=0 * u.degree, cam_rotation=0 * u.degree,
                 neighbors=None, apply_derotation=True):
        """
        Parameters
        ----------
        self: type
            description
        cam_id: camera id name or number
            camera identification string
        pix_id: array(int)
            pixels id numbers
        pix_x: array with units
            position of each pixel (x-coordinate)
        pix_y: array with units
            position of each pixel (y-coordinate)
        pix_area: array(float)
            surface area of each pixe
        neighbors: list(arrays)
            adjacency list for each pixel
        pix_type: string
            either 'rectangular' or 'hexagonal'
        pix_rotation: value convertable to an `astropy.coordinates.Angle`
            rotation angle with unit (e.g. 12 * u.deg), or "12d"
        cam_rotation: overall camera rotation with units

        """
        self.cam_id = cam_id
        self.pix_id = pix_id
        self.pix_x = pix_x
        self.pix_y = pix_y
        self.pix_area = pix_area
        self.pix_type = pix_type
        self.pix_rotation = Angle(pix_rotation)
        self.cam_rotation = Angle(cam_rotation)
        self._precalculated_neighbors = neighbors

        if apply_derotation:
            # todo: this should probably not be done, but need to fix
            # GeometryConverter and reco algorithms if we change it.
            if len(pix_x.shape) == 1:
                self.rotate(cam_rotation)


    def __eq__(self, other):
        return ( (self.cam_id == other.cam_id)
                 and (self.pix_x == other.pix_x).all()
                 and (self.pix_y == other.pix_y).all()
                 and (self.pix_type == other.pix_type)
                 and (self.pix_rotation == other.pix_rotation)
                 and (self.pix_type == other.pix_type)
                )

    @classmethod
    @u.quantity_input
    def guess(cls, pix_x: u.m, pix_y: u.m, optical_foclen: u.m):
        """ 
        Construct a `CameraGeometry` by guessing the appropriate quantities
        from a list of pixel positions and the focal length. 
        """
        # only construct a new one if it has never been constructed before,
        # to speed up access. Otherwise return the already constructed instance
        # the identifier uses the values of pix_x (which are converted to a
        # string to make them hashable) and the optical_foclen. So far,
        # that is enough to uniquely identify a geometry.
        identifier = (pix_x.value.tostring(), optical_foclen)
        if identifier in CameraGeometry._geometry_cache:
            return CameraGeometry._geometry_cache[identifier]

        # now try to determine the camera type using the map defined at the
        # top of this file.
        dist = _get_min_pixel_seperation(pix_x, pix_y)

        tel_type, cam_id, pix_type, pix_rotation, cam_rotation = \
            _guess_camera_type(len(pix_x), optical_foclen)

        if pix_type.startswith('hex'):
            rad = dist / np.sqrt(3)  # radius to vertex of hexagon
            area = rad ** 2 * (3 * np.sqrt(3) / 2.0)  # area of hexagon
        elif pix_type.startswith('rect'):
            area = dist ** 2
        else:
            raise KeyError("unsupported pixel type")

        instance = cls(
            cam_id=cam_id,
            pix_id=np.arange(len(pix_x)),
            pix_x=pix_x,
            pix_y=pix_y,
            pix_area=np.ones(pix_x.shape) * area,
            neighbors=None,
            pix_type=pix_type,
            pix_rotation=Angle(pix_rotation),
            cam_rotation=Angle(cam_rotation),
        )

        CameraGeometry._geometry_cache[identifier] = instance
        return instance

    @classmethod
    def get_known_camera_names(cls, array_id='CTA'):
        """
        Returns a list of camera_ids that are registered in 
        `ctapipe_resources`. These are all the camera-ids that can be 
        instantiated by the `from_name` method
     
        Parameters
        ----------
        array_id: str 
            which array to search (default CTA)

        Returns
        -------
        list(str)
        """

        pattern = "(.*)\.camgeom\.fits(\.gz)?"
        return find_all_matching_datasets(pattern, regexp_group=1)


    @classmethod
    def from_name(cls, camera_id='NectarCam', version=None):
        """
        Construct a CameraGeometry using the name of the camera and array.
        
        This expects that there is a resource in the `ctapipe_resources` module
        called "[array]-[camera].camgeom.fits.gz" or "[array]-[camera]-[
        version].camgeom.fits.gz"
        
        Parameters
        ----------
        camera_id: str
           name of camera (e.g. 'NectarCam', 'LSTCam', 'GCT', 'SST-1M')
        array_id: str
           array identifier (e.g. 'CTA', 'HESS')
        version:
           camera version id (currently unused)

        Returns
        -------
        new CameraGeometry
        """

        if version is None:
            verstr = ''
        else:
            verstr = "-{:03d}".format(version)

        filename = get_dataset("{camera_id}{verstr}.camgeom.fits.gz"
                               .format(camera_id=camera_id, verstr=verstr))
        return CameraGeometry.from_table(filename)


    def to_table(self):
        """ convert this to an `astropy.table.Table` """
        # currently the neighbor list is not supported, since
        # var-length arrays are not supported by astropy.table.Table
        return Table([self.pix_id, self.pix_x, self.pix_y, self.pix_area],
                     names=['pix_id', 'pix_x', 'pix_y', 'pix_area'],
                     meta=dict(PIX_TYPE=self.pix_type,
                               TAB_TYPE='ctapipe.instrument.CameraGeometry',
                               TAB_VER='1.0',
                               CAM_ID=self.cam_id,
                               PIX_ROT=self.pix_rotation.deg,
                               CAM_ROT=self.cam_rotation.deg,
                ))


    @classmethod
    def from_table(cls, url_or_table, **kwargs):
        """
        Load a CameraGeometry from an `astropy.table.Table` instance or a 
        file that is readable by `astropy.table.Table.read()`
         
        Parameters
        ----------
        url_or_table: string or astropy.table.Table
            either input filename/url or a Table instance
        
        format: str
            astropy.table format string (e.g. 'ascii.ecsv') in case the 
            format cannot be determined from the file extension
            
        kwargs: extra keyword arguments
            extra arguments passed to `astropy.table.read()`, depending on 
            file type (e.g. format, hdu, path)


        """

        tab = url_or_table
        if not isinstance(url_or_table, Table):
            tab = Table.read(url_or_table, **kwargs)

        return cls(
            cam_id=tab.meta.get('CAM_ID', 'Unknown'),
            pix_id=tab['pix_id'],
            pix_x=tab['pix_x'].quantity,
            pix_y=tab['pix_y'].quantity,
            pix_area=tab['pix_area'].quantity,
            pix_type=tab.meta['PIX_TYPE'],
            pix_rotation=Angle(tab.meta['PIX_ROT'] * u.deg),
            cam_rotation=Angle(tab.meta['CAM_ROT'] * u.deg),
        )

    def __str__(self):
        tab = self.to_table()
        return "CameraGeometry(cam_id='{cam_id}', pix_type='{pix_type}', " \
               "npix={npix})".format(cam_id=self.cam_id,
                                     pix_type=self.pix_type,
                                     npix=len(self.pix_id))

    @lazyproperty
    def neighbors(self):
        """" only calculate neighbors when needed or if not already 
        calculated"""

        # return pre-calculated ones (e.g. those that were passed in during
        # the object construction) if they exist
        if self._precalculated_neighbors is not None:
            return self._precalculated_neighbors

        # otherwise compute the neighbors from the pixel list
        dist = _get_min_pixel_seperation(self.pix_x, self.pix_y)

        neighbors = _find_neighbor_pixels(
            self.pix_x.value,
            self.pix_y.value,
            rad=1.4 * dist.value
        )

        return neighbors

    @lazyproperty
    def neighbor_matrix(self):
        return _neighbor_list_to_matrix(self.neighbors)

    def rotate(self, angle):
        """rotate the camera coordinates about the center of the camera by
        specified angle. Modifies the CameraGeometry in-place (so
        after this is called, the pix_x and pix_y arrays are
        rotated.

        Notes
        -----

        This is intended only to correct simulated data that are
        rotated by a fixed angle.  For the more general case of
        correction for camera pointing errors (rotations,
        translations, skews, etc), you should use a true coordinate
        transformation defined in `ctapipe.coordinates`.

        Parameters
        ----------

        angle: value convertable to an `astropy.coordinates.Angle`
            rotation angle with unit (e.g. 12 * u.deg), or "12d"

        """
        rotmat = rotation_matrix_2d(angle)
        rotated = np.dot(rotmat.T, [self.pix_x.value, self.pix_y.value])
        self.pix_x = rotated[0] * self.pix_x.unit
        self.pix_y = rotated[1] * self.pix_x.unit
        self.pix_rotation -= angle
        self.cam_rotation -= angle

    @classmethod
    def make_rectangular(cls, npix_x=40, npix_y=40, range_x=(-0.5, 0.5),
                         range_y=(-0.5, 0.5)):
        """Generate a simple camera with 2D rectangular geometry.

        Used for testing.

        Parameters
        ----------
        npix_x : int
            number of pixels in X-dimension
        npix_y : int
            number of pixels in Y-dimension
        range_x : (float,float)
            min and max of x pixel coordinates in meters
        range_y : (float,float)
            min and max of y pixel coordinates in meters

        Returns
        -------
        CameraGeometry object

        """
        bx = np.linspace(range_x[0], range_x[1], npix_x)
        by = np.linspace(range_y[0], range_y[1], npix_y)
        xx, yy = np.meshgrid(bx, by)
        xx = xx.ravel() * u.m
        yy = yy.ravel() * u.m

        ids = np.arange(npix_x * npix_y)
        rr = np.ones_like(xx).value * (xx[1] - xx[0]) / 2.0

        return cls(cam_id=-1,
                   pix_id=ids,
                   pix_x=xx * u.m,
                   pix_y=yy * u.m,
                   pix_area=(2 * rr) ** 2,
                   neighbors=None,
                   pix_type='rectangular')


# ======================================================================
# utility functions:
# ======================================================================

def _get_min_pixel_seperation(pix_x, pix_y):
    """
    Obtain the minimum seperation between two pixels on the camera

    Parameters
    ----------
    pix_x : array_like
        x position of each pixel
    pix_y : array_like
        y position of each pixels

    Returns
    -------
    pixsep : astropy.units.Unit

    """
    #    dx = pix_x[1] - pix_x[0]    <=== Not adjacent for DC-SSTs!!
    #    dy = pix_y[1] - pix_y[0]

    dx = pix_x - pix_x[0]
    dy = pix_y - pix_y[0]
    pixsep = np.min(np.sqrt(dx ** 2 + dy ** 2)[1:])
    return pixsep


def _find_neighbor_pixels(pix_x, pix_y, rad):
    """use a KD-Tree to quickly find nearest neighbors of the pixels in a
    camera. This function can be used to find the neighbor pixels if
    they are not already present in a camera geometry file.

    Parameters
    ----------
    pix_x : array_like
        x position of each pixel
    pix_y : array_like
        y position of each pixels
    rad : float
        radius to consider neighbor it should be slightly larger
        than the pixel diameter.

    Returns
    -------
    array of neighbor indices in a list for each pixel

    """

    points = np.array([pix_x, pix_y]).T
    indices = np.arange(len(pix_x))
    kdtree = KDTree(points)
    neighbors = [kdtree.query_ball_point(p, r=rad) for p in points]
    for nn, ii in zip(neighbors, indices):
        nn.remove(ii)  # get rid of the pixel itself
    return neighbors


def _guess_camera_type(npix, optical_foclen):
    global _CAMERA_GEOMETRY_TABLE

    try:
        return _CAMERA_GEOMETRY_TABLE[(npix, None)]
    except KeyError:
        return _CAMERA_GEOMETRY_TABLE.get((npix, round(optical_foclen.value, 1)),
                                          ('unknown', 'unknown', 'hexagonal',
                                  0 * u.degree, 0 * u.degree))



def _neighbor_list_to_matrix(neighbors):
    """ 
    convert a neighbor adjacency list (list of list of neighbors) to a 2D 
    numpy array, which is much faster (and can simply be multiplied)
    """

    npix = len(neighbors)
    neigh2d = np.zeros(shape=(npix, npix), dtype=np.bool)

    for ipix, neighbors in enumerate(neighbors):
        for jn, neighbor in enumerate(neighbors):
            neigh2d[ipix, neighbor] = True

    return neigh2d


def get_camera_types(inst):
    """ return dict of camera names mapped to a list of tel_ids
     that use that camera
     
     Parameters
     ----------
     inst: instument Container
     
     """

    cam_types = defaultdict(list)

    for telid in inst.pixel_pos:
        x, y = inst.pixel_pos[telid]
        f = inst.optical_foclen[telid]
        geom = CameraGeometry.guess(x, y, f)

        cam_types[geom.cam_id].append(telid)

    return cam_types


def print_camera_types(inst, printer=print):
    """
    Print out a friendly table of which camera types are registered in the 
    inst dictionary (from a hessio file), along with their starting and 
    stopping tel_ids.
    
    Parameters
    ----------
    inst: ctapipe.io.containers.InstrumentContainer
        input container
    printer: func
        function to call to output the text (default is the standard python 
        print command, but you can give for example logger.info to have it 
        write to a logger) 
    """
    camtypes = get_camera_types(inst)

    printer("              CAMERA  Num IDmin  IDmax")
    printer("=====================================")
    for cam, tels in camtypes.items():
        printer("{:>20s} {:4d} {:4d} ..{:4d}".format(cam, len(tels), min(tels),
                                                     max(tels)))
