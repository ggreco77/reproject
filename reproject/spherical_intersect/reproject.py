# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import numpy as np

from astropy.io import fits
from astropy.wcs import WCS
from astropy import log as logger

from ..wcs_utils import wcs_to_celestial_frame, convert_world_coordinates

from ._overlap import _compute_overlap

import signal

__all__ = ['reproject_celestial']

# Function to disable ctrl+c in the worker processes.
def _init_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)

def reproject_celestial(array, wcs_in, wcs_out, shape_out, parallel=True, _method = "c"):
    """
    Reproject celestial slices from an n-d array from one WCS to another using
    flux-conserving spherical polygon intersection.

    Parameters
    ----------
    array : `~numpy.ndarray`
        The array to reproject
    wcs_in : `~astropy.wcs.WCS`
        The input WCS
    wcs_out : `~astropy.wcs.WCS`
        The output WCS
    shape_out : tuple
        The shape of the output array
    parallel : bool or int
        Flag for parallel implementation. If ``True``, a parallel implementation
        is chosen, the number of processes selected automatically to be equal to
        the number of logical CPUs detected on the machine. If ``False``, a
        serial implementation is chosen. If the flag is a positive integer ``n``
        greater than one, a parallel implementation using ``n`` processes is chosen.

    Returns
    -------
    array_new : `~numpy.ndarray`
        The reprojected array
    footprint : `~numpy.ndarray`
        Footprint of the input array in the output array. Values of 0 indicate
        no coverage or valid values in the input image, while values of 1
        indicate valid values. Intermediate values indicate partial coverage.
    """

    # Check the parallel flag.
    if type(parallel) != bool and type(parallel) != int:
        raise TypeError("The 'parallel' flag must be a boolean or integral value")

    if type(parallel) == int:
        # parallel is a number of processes.
        if parallel <= 0:
            raise ValueError("The number of processors to use must be strictly positive")
        nproc = parallel
    else:
        # parallel is a boolean flag. nproc = None here means automatically selected
        # number of processes.
        nproc = None if parallel else 1

    # Convert input array to float values. If this comes from a FITS, it might have
    # float32 as value type and that can break things in cythin.
    array = array.astype(float)

    # TODO: make this work for n-dimensional arrays
    if wcs_in.naxis != 2:
        raise NotImplementedError("Only 2-dimensional arrays can be reprojected at this time")

    # TODO: at the moment, we compute the coordinates of all of the corners,
    # but we might want to do it in steps for large images.

    # Start off by finding the world position of all the corners of the input
    # image in world coordinates

    ny_in, nx_in = array.shape

    x = np.arange(nx_in + 1.) - 0.5
    y = np.arange(ny_in + 1.) - 0.5

    xp_in, yp_in = np.meshgrid(x, y)

    xw_in, yw_in = wcs_in.wcs_pix2world(xp_in, yp_in, 0)

    # Now compute the world positions of all the corners in the output header

    ny_out, nx_out = shape_out

    x = np.arange(nx_out + 1.) - 0.5
    y = np.arange(ny_out + 1.) - 0.5

    xp_out, yp_out = np.meshgrid(x, y)

    xw_out, yw_out = wcs_out.wcs_pix2world(xp_out, yp_out, 0)

    # Convert the input world coordinates to the frame of the output world
    # coordinates.

    xw_in, yw_in = convert_world_coordinates(xw_in, yw_in, wcs_in, wcs_out)

    # Finally, compute the pixel positions in the *output* image of the pixels
    # from the *input* image.

    xp_inout, yp_inout = wcs_out.wcs_world2pix(xw_in, yw_in, 0)

    if _method == "legacy":
        # Create output image

        array_new = np.zeros(shape_out)
        weights = np.zeros(shape_out)

        for i in range(nx_in):
            for j in range(ny_in):

                # For every input pixel we find the position in the output image in
                # pixel coordinates, then use the full range of overlapping output
                # pixels with the exact overlap function.

                xmin = int(min(xp_inout[j, i], xp_inout[j, i+1], xp_inout[j+1, i+1], xp_inout[j+1, i]) + 0.5)
                xmax = int(max(xp_inout[j, i], xp_inout[j, i+1], xp_inout[j+1, i+1], xp_inout[j+1, i]) + 0.5)
                ymin = int(min(yp_inout[j, i], yp_inout[j, i+1], yp_inout[j+1, i+1], yp_inout[j+1, i]) + 0.5)
                ymax = int(max(yp_inout[j, i], yp_inout[j, i+1], yp_inout[j+1, i+1], yp_inout[j+1, i]) + 0.5)

                ilon = [[xw_in[j, i], xw_in[j, i+1], xw_in[j+1, i+1], xw_in[j+1, i]][::-1]]
                ilat = [[yw_in[j, i], yw_in[j, i+1], yw_in[j+1, i+1], yw_in[j+1, i]][::-1]]
                ilon = np.radians(np.array(ilon))
                ilat = np.radians(np.array(ilat))

                xmin = max(0, xmin)
                xmax = min(nx_out-1, xmax)
                ymin = max(0, ymin)
                ymax = min(ny_out-1, ymax)

                for ii in range(xmin, xmax+1):
                    for jj in range(ymin, ymax+1):

                        olon = [[xw_out[jj, ii], xw_out[jj, ii+1], xw_out[jj+1, ii+1], xw_out[jj+1, ii]][::-1]]
                        olat = [[yw_out[jj, ii], yw_out[jj, ii+1], yw_out[jj+1, ii+1], yw_out[jj+1, ii]][::-1]]
                        olon = np.radians(np.array(olon))
                        olat = np.radians(np.array(olat))

                        # Figure out the fraction of the input pixel that makes it
                        # to the output pixel at this position.

                        overlap, _ = _compute_overlap(ilon, ilat, olon, olat)
                        original, _ = _compute_overlap(olon, olat, olon, olat)
                        array_new[jj, ii] += array[j, i] * overlap / original
                        weights[jj, ii] += overlap / original

        array_new /= weights

        return array_new, weights

    # Put together the parameters common both to the serial and parallel implementations. The aca
    # function is needed to enforce that the array will be contiguous when passed to the low-level
    # raw C function, otherwise Cython might complain.
    from numpy import ascontiguousarray as aca
    from ._overlap import _reproject_slice_cython
    common_func_par = [0,ny_in,nx_out,ny_out,aca(xp_inout),aca(yp_inout),aca(xw_in),aca(yw_in),aca(xw_out),aca(yw_out),aca(array),shape_out]

    # Abstract the serial implementation in a separate function so we can reuse it.
    def serial_impl():
        array_new, weights = _reproject_slice_cython(0,nx_in,*common_func_par);

        array_new /= weights

        return array_new, weights

    if _method == "c" and nproc == 1:
        return serial_impl()

    # Abstract the parallel implementation as well.
    def parallel_impl(nproc):
        from multiprocessing import Pool, cpu_count
        # If needed, establish the number of processors to use.
        if nproc is None:
                nproc = cpu_count()

        # Create the pool.
        pool = None
        try:
            # Prime each process in the pool with a small function that disables
            # the ctrl+c signal in the child process.
            pool = Pool(nproc,_init_worker)

            # Accumulator for the results from the parallel processes.
            results = []

            for i in range(nproc):
                start = int(nx_in) // nproc * i
                end = int(nx_in) if i == nproc - 1 else int(nx_in) // nproc * (i + 1)
                results.append(pool.apply_async(_reproject_slice_cython,[start,end] + common_func_par))

            array_new = sum([_.get()[0] for _ in results])
            weights = sum([_.get()[1] for _ in results])

        except KeyboardInterrupt:
            # If we hit ctrl+c while running things in parallel, we want to terminate
            # everything and erase the pool before re-raising. Note that since we inited the pool
            # with the _init_worker function, we disabled catching ctrl+c from the subprocesses. ctrl+c
            # can be handled only by the main process.
            if not pool is None:
                pool.terminate()
                pool.join()
                pool = None
            raise

        finally:
            if not pool is None:
                # Clean up the pool, if still alive.
                pool.close()
                pool.join()

        return array_new / weights, weights

    if _method == "c" and (nproc is None or nproc > 1):
        try:
            return parallel_impl(nproc)
        except KeyboardInterrupt:
            # If we stopped the parallel implementation with ctrl+c, we don't really want to run
            # the serial one.
            raise
        except Exception as e:
            logger.warn("The parallel implementation failed, the reported error message is: '{0}'".format(repr(e,)))
            logger.warn("Running the serial implementation instead")
            return serial_impl()

    raise ValueError('unrecognized method "{0}"'.format(_method,))
