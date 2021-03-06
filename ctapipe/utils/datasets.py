# Licensed under a 3-clause BSD style license - see LICENSE.rst
import logging
import os
import re

from astropy.utils.decorators import deprecated
from pkg_resources import resource_listdir
from astropy.table import Table
from ..core import Provenance

logger = logging.getLogger(__name__)

try:
    import ctapipe_resources
except:
    raise RuntimeError("Please install the 'ctapipe-extra' package, "
                       "which contains the ctapipe_resources module "
                       "needed by ctapipe. (conda install ctapipe-extra)")


__all__ = ['get_dataset', 'find_in_path', 'find_all_matching_datasets']


def get_searchpath_dirs(searchpath=os.getenv("CTAPIPE_SVC_PATH")):
    """ returns a list of dirs in specified searchpath"""
    if searchpath == "" or searchpath is None:
        return []
    return os.path.expandvars(searchpath).split(':')

def find_all_matching_datasets(pattern,
                               searchpath=None,
                               regexp_group=None):
    """
    Returns a list of resource names (or substrings) matching the given 
    pattern, searching first in searchpath (a colon-separated list of 
    directories) and then in the ctapipe_resources module)
    
    Parameters
    ----------
    pattern: str
       regular expression to use for matching
    searchpath: str
       colon-seprated list of directories in which to search, defaulting to 
       CTAPIPE_SVC_PATH environment variable
    regexp_group: int
       if not None, return the regular expression group indicated (assuming 
       pattern has a group specifier in it)

    Returns
    -------
    list(str):
       resources names, use get_dataset() to retrieve the full filename
    """
    results = set()

    if searchpath is None:
        searchpath = os.getenv("CTAPIPE_SVC_PATH")

    # first check search path
    if searchpath is not None:
        for path in get_searchpath_dirs(searchpath):
            if os.path.exists(path):
                for filename in os.listdir(path):
                    match = re.match(pattern, filename)
                    if match:
                        if regexp_group is not None:
                            results.add(match.group(regexp_group))
                        else:
                            results.add(filename)

    # then check resources module
    for resource in resource_listdir('ctapipe_resources', ''):
        match = re.match(pattern, resource)
        if match:
            if regexp_group is not None:
                results.add(match.group(regexp_group))
            else:
                results.add(resource)

    return list(results)


def find_in_path(filename, searchpath):
    """
    Search in searchpath for filename, returning full path.
    
    Parameters
    ----------
    searchpath: str
        colon-separated list of directories (like PATH)
    filename:
        filename to find

    Returns
    -------
    full path to file if found, None otherwise

    """

    for dir in get_searchpath_dirs(searchpath):
        pathname = os.path.join(dir, filename)
        if os.path.exists(pathname):
            return pathname

    return None


def get_dataset(filename):
    """
    Returns the full file path to an auxiliary dataset needed by 
    ctapipe, given the dataset's full name (filename with no directory).
      
    This will first search for the file in directories listed in 
    tne environment variable CTAPIPE_SVC_PATH (if set), and if not found,  
    will look in the ctapipe_resources module 
    (installed with the ctapipe-extra package), which contains the defaults.
    
    Parameters
    ----------
    filename: str
        name of dataset to fetch

    Returns
    -------
    string with full path to the given dataset
    """
    searchpath = os.getenv("CTAPIPE_SVC_PATH")

    if searchpath:
        filepath = find_in_path(filename=filename, searchpath=searchpath)
        if filepath:
            return filepath

    logger.debug("Resource '{}' not found in CTAPIPE_SVC_PATH, looking in "
                 "ctapipe_resources...".format(filename))

    return ctapipe_resources.get(filename)


def get_table_dataset(table_name, role='resource', **kwargs):
    """
    get a tabular dataset as an `astropy.table.Table` object

    Parameters
    ----------
    table_name: str
        base name of table, without file extension
    role: str
        should be set to the CTA data hierarchy name when possible (e.g.
        dl1.sub.svc.arraylayout). This will be recorded in the provenance
        system.
    kwargs:
        extra arguments to pass to Table.read()

    Returns
    -------
    Table
    """

    # a mapping of types (keys) to any extra keyword args needed for
    # table.read()
    types_to_try = {
        '.fits.gz' : {},
        '.fits': {},
        '.ecsv': dict(format='ascii.ecsv'),
        '.ecsv.txt': dict(format='ascii.ecsv'),
    }

    for table_type in types_to_try:
        filename = table_name + table_type
        try:
            fullname = get_dataset(filename)
            if fullname:
                args = types_to_try[table_type]
                args.update(kwargs)
                table = Table.read(fullname, **args)
                Provenance().add_input_file(fullname, role)
                return table
        except FileNotFoundError:
            pass

    raise FileNotFoundError("couldn't locate table: {}[{}]".format(
        table_name, ', '.join(types_to_try)))


@deprecated("ctapipe-0.5",alternative='get_dataset()')
def get_path(filename):
    return get_dataset(filename)


if __name__ == '__main__':

    get_table_dataset("NectarCam.camgeom")