#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
:py:mod:`detrender.py` - De-trending models
-------------------------------------------

This module contains the generic models used to de-trend light curves for the various
supported missions. Most of the functionality is implemented in :py:class:`Detrender`, and
specific de-trending methods are implemented as subclasses.

'''

from __future__ import division, print_function, absolute_import, unicode_literals
from . import missions
from .basecamp import Basecamp
from . import pld
from .config import EVEREST_DAT
from .utils import InitLog, Formatter, AP_SATURATED_PIXEL, AP_COLLAPSED_PIXEL
from .math import Chunks, RMS, CDPP6, SavGol, Interpolate
from .fits import MakeFITS
from .gp import GetCovariance, GetKernelParams
from .dvs import DVS1, DVS2
import os, sys
import numpy as np
import george
from scipy.optimize import fmin_powell
import matplotlib.pyplot as pl
from matplotlib.ticker import MaxNLocator
from matplotlib.backends.backend_pdf import PdfPages
import traceback
import logging
log = logging.getLogger(__name__)

__all__ = ['Detrender', 'rPLD', 'nPLD', 'nPLD2']

class Detrender(Basecamp):
  '''
  A generic *PLD* model with scalar matrix *L2* regularization. Includes functionality
  for loading pixel-level light curves, identifying outliers, generating the data
  covariance matrix, computing the regularized pixel model, and plotting the results.
  Specific models are implemented as subclasses.
  
  **General:**
  
  :param ID: The target star ID (*EPIC*, *KIC*, or *TIC* number, for instance)
  :param str cadence: The cadence of the observations. Default :py:obj:`lc`
  :param bool clobber: Overwrite existing :py:obj:`everest` models? Default :py:obj:`False`
  :param bool clobber_tpf: Download and overwrite the saved raw TPF data? Default :py:obj:`False`
  :param bool debug: De-trend in debug mode? If :py:obj:`True`, prints all output to screen and \
                     enters :py:obj:`pdb` post-mortem mode for debugging when an error is raised.
                     Default :py:obj:`False`
  :param bool make_fits: Generate a *FITS* file at the end of rthe run? Default :py:obj:`True`
  :param str mission: The name of the mission. Default `k2`
  
  **Detrender:**
  
  :param str aperture_name: The name of the aperture to use. These are defined in the datasets and are \
                            mission specific. Defaults to the mission default
  :param int bpad: When light curve breakpoints are set, the light curve chunks must be stitched together \
                   at the end. To prevent kinks and/or discontinuities, the chunks are made to overlap by \
                   :py:obj:`bpad` cadences on either end. The chunks are then mended and the overlap is \
                   discarded. Default 100
  :param breakpoints: Add light curve breakpoints when de-trending? If :py:obj:`True`, splits the \
                      light curve into chunks and de-trends each one separately, then stitches them \
                      back and the end. This is useful for missions like *K2*, where the light curve noise \
                      properties are very different at the beginning and end of each campaign. The cadences \
                      at which breakpoints are inserted are specified in the :py:func:`Breakpoints` function \
                      of each mission. Alternatively, the user may specify a list of cadences at which to \
                      break up the light curve. Default :py:obj:`True`
  :param int cdivs: The number of light curve subdivisions when cross-validating. During each iteration, \
                    one of these subdivisions will be masked and used as the validation set. Default 3
  :param int giter: The number of iterations when optimizing the GP. During each iteration, the minimizer \
                    is initialized with a perturbed guess; after :py:obj:`giter` iterations, the step with \
                    the highest likelihood is kept. Default 3
  :param float gp_factor: When computing the initial kernel parameters, the red noise amplitude is set to \
                          the standard deviation of the data times this factor. Larger values generally \
                          help with convergence, particularly for very variable stars. Default 100
  :param array_like kernel_params: The initial value of the :py:obj:`Matern-3/2` kernel parameters \
                                   (white noise amplitude in flux units, red noise amplitude in flux units, \
                                   and timescale in days). Default :py:obj:`None` (determined from the data)
  :param array_like lambda_arr: The array of :math:`\Lambda` values to iterate over during the \
                                cross-validation step. :math:`\Lambda` is the regularization parameter,
                                or the standard deviation of \
                                the Gaussian prior on the weights for each order of PLD. \
                                Default ``10 ** np.arange(0,18,0.5)``
  :param float leps: The fractional tolerance when optimizing :math:`\Lambda`. The chosen value of \
                     :math:`\Lambda` will be within this amount of the minimum of the CDPP curve. \
                     Default 0.05
  :param int max_pixels: The maximum number of pixels. Very large apertures are likely to cause memory \
                         errors, particularly for high order PLD. If the chosen aperture exceeds this many \
                         pixels, a different aperture is chosen from the dataset. If no apertures with fewer \
                         than this many pixels are available, an error is thrown. Default 75
  :param bool optimize_gp: Perform the GP optimization steps? Default :py:obj:`True`
  :param float osigma: The outlier standard deviation threshold. Default 5
  :param int oiter: The maximum number of steps taken during iterative sigma clipping. Default 10
  :param int pld_order: The pixel level decorrelation order. Default `3`. Higher orders may cause memory errors
  :param bool recursive: Calculate the fractional pixel flux recursively? If :py:obj:`False`, \
                         always computes the fractional pixel flux as :math:`f_{ij}/\sum_i{f_{ij}}`. \
                         If :py:obj:`True`, uses the current de-trended flux as the divisor in an attempt \
                         to minimize the amount of instrumental information that is divided out in this \
                         step: :math:`f_{ij}/(\sum_i{f_{ij}} - M)`. Default :py:obj:`True`
  :param str saturated_aperture_name: If the target is found to be saturated, de-trending is performed \
                                      on this aperture instead. Defaults to the mission default
  :param float saturation_tolerance: The tolerance when determining whether or not to collapse a column \
                                     in the aperture. The column collapsing is implemented in the individual \
                                     mission modules. Default -0.1, i.e., if a target is 10% shy of the \
                                     nominal saturation level, it is considered to be saturated.
                                     
  '''

  def __init__(self, ID, **kwargs):
    '''
    
    '''
        
    # Initialize logging
    self.ID = ID
    self.cadence = kwargs.get('cadence', 'lc').lower()
    if self.cadence not in ['lc', 'sc']:
      raise ValueError("Invalid cadence selected.")
    self.recursive = kwargs.get('recursive', True)
    self.make_fits = kwargs.get('make_fits', True)
    self.mission = kwargs.get('mission', 'k2')
    self.clobber = kwargs.get('clobber', False)
    self.debug = kwargs.get('debug', False)
    self.is_parent = kwargs.get('is_parent', False)
    if not self.is_parent:
      screen_level = kwargs.get('screen_level', logging.CRITICAL)
      log_level = kwargs.get('log_level', logging.DEBUG)
      InitLog(self.logfile, log_level, screen_level, self.debug)
      log.info("Initializing %s model for %d." % (self.name, self.ID))
    
    # If this is a short cadence light curve, get the
    # GP params from the long cadence model. It would
    # take way too long and too much memory to optimize
    # the GP based on the short cadence light curve
    if self.cadence == 'sc':
      log.info("Loading long cadence model...")
      kwargs.pop('cadence', None)
      kwargs.pop('clobber', None)
      lc = self.__class__(ID, is_parent = True, **kwargs)
      kwargs.update({'kernel_params': kwargs.get('kernel_params', lc.kernel_params),
                     'optimize_gp': False})
      del lc
    
    # Read general model kwargs
    self.lambda_arr = kwargs.get('lambda_arr', 10 ** np.arange(0,18,0.5))
    if self.lambda_arr[0] != 0:
      self.lambda_arr = np.append(0, self.lambda_arr)
    self.leps = kwargs.get('leps', 0.05)
    self.osigma = kwargs.get('osigma', 5)
    self.oiter = kwargs.get('oiter', 10)
    self.cdivs = kwargs.get('cdivs', 3)
    self.giter = kwargs.get('giter', 3)
    self.optimize_gp = kwargs.get('optimize_gp', True)
    self.kernel_params = kwargs.get('kernel_params', None)    
    self.clobber_tpf = kwargs.get('clobber_tpf', False)
    self.bpad = kwargs.get('bpad', 100)
    self.aperture_name = kwargs.get('aperture', None)
    self.saturated_aperture_name = kwargs.get('saturated_aperture', None)
    self.max_pixels = kwargs.get('max_pixels', 75)
    self.saturation_tolerance = kwargs.get('saturation_tolerance', -0.1)
    self.gp_factor = kwargs.get('gp_factor', 100.)
    
    # Handle breakpointing. The breakpoint is the *last* index of each 
    # light curve chunk.
    bkpts = kwargs.get('breakpoints', True)
    if bkpts is True:
      self.breakpoints = np.append(self._mission.Breakpoints(self.ID, cadence = self.cadence), [999999])
    elif hasattr(bkpts, '__len__'):
      self.breakpoints = np.append(bkpts, [999999])
    else:
      self.breakpoints = np.array([999999])
    nseg = len(self.breakpoints)

    # Get the pld order
    pld_order = kwargs.get('pld_order', 3)
    assert (pld_order > 0), "Invalid value for the de-trending order."
    self.pld_order = pld_order

    # Initialize model params 
    self.lam_idx = -1
    self.lam = [[1e5] + [None for i in range(self.pld_order - 1)] for b in range(nseg)]
    self.X1N = None
    self.cdpp6_arr = np.array([np.nan for b in range(nseg)])
    self.cdppr_arr = np.array([np.nan for b in range(nseg)])
    self.cdppv_arr = np.array([np.nan for b in range(nseg)])
    self.cdpp6 = np.nan
    self.cdppr = np.nan
    self.cdppv = np.nan
    self.gppp = np.nan
    self.neighbors = []
    self.loaded = False
    self._weights = None
    
    # Initialize plotting
    self.dvs1 = DVS1(len(self.breakpoints), pld_order = self.pld_order)
    self.dvs2 = DVS2(len(self.breakpoints))
  
  @property
  def name(self):
    '''
    Returns the name of the current :py:class:`Detrender` subclass.
    
    '''
    
    if self.cadence == 'lc':
      return self.__class__.__name__
    else:
      return '%s.sc' % self.__class__.__name__
      
  @name.setter
  def name(self, value):
    '''
    
    '''
    
    raise NotImplementedError("Can't set this property.") 
  
  def cv_precompute(self, mask, b):
    '''
    Pre-compute the matrices :py:obj:`A` and :py:obj:`B` (cross-validation step only)
    for chunk :py:obj:`b`.
    
    '''
    
    # Get current chunk and mask outliers
    m1 = self.get_masked_chunk(b)
    flux = self.fraw[m1]
    K = GetCovariance(self.kernel_params, self.time[m1], self.fraw_err[m1])
    med = np.nanmedian(flux)
    
    # Now mask the validation set
    M = lambda x, axis = 0: np.delete(x, mask, axis = axis)
    m2 = M(m1)
    mK = M(M(K, axis = 0), axis = 1)
    f = M(flux) - med
    
    # Pre-compute the matrices
    A = [None for i in range(self.pld_order)]
    B = [None for i in range(self.pld_order)] 
    for n in range(self.pld_order):
      # Only compute up to the current PLD order
      if self.lam_idx >= n:
        X2 = self.X(n,m2)
        X1 = self.X(n,m1)
        A[n] = np.dot(X2, X2.T)
        B[n] = np.dot(X1, X2.T)
        del X1, X2
    
    return A, B, mK, f
    
  def cv_compute(self, b, A, B, mK, f):
    '''
    Compute the model (cross-validation step only) for chunk :py:obj:`b`.
    
    '''

    A = np.sum([l * a for l, a in zip(self.lam[b], A) if l is not None], axis = 0)
    B = np.sum([l * b for l, b in zip(self.lam[b], B) if l is not None], axis = 0)
    W = np.linalg.solve(mK + A, f)
    model = np.dot(B, W)
    model -= np.nanmedian(model)
    
    return model
  
  def get_outliers(self):
    '''
    Performs iterative sigma clipping to get outliers.
    
    '''
            
    log.info("Clipping outliers...")
    log.info('Iter %d/%d: %d outliers' % (0, self.oiter, len(self.outmask)))
    M = lambda x: np.delete(x, np.concatenate([self.nanmask, self.badmask]), axis = 0)
    t = M(self.time)
    outmask = [np.array([-1]), np.array(self.outmask)]
    
    # Loop as long as the last two outlier arrays aren't equal
    while not np.array_equal(outmask[-2], outmask[-1]):

      # Check if we've done this too many times
      if len(outmask) - 1 > self.oiter:
        log.error('Maximum number of iterations in ``get_outliers()`` exceeded. Skipping...')
        break
    
      # Check if we're going in circles
      if np.any([np.array_equal(outmask[-1], i) for i in outmask[:-1]]):
        log.error('Function ``get_outliers()`` is going in circles. Skipping...')
        break
      
      # Compute the model to get the flux
      self.compute()
    
      # Get the outliers
      f = SavGol(M(self.flux))
      med = np.nanmedian(f)
      MAD = 1.4826 * np.nanmedian(np.abs(f - med))
      inds = np.where((f > med + self.osigma * MAD) | (f < med - self.osigma * MAD))[0]
      
      # Project onto unmasked time array
      inds = np.array([np.argmax(self.time == t[i]) for i in inds])
      self.outmask = np.array(inds, dtype = int)
      
      # Add them to the running list
      outmask.append(np.array(inds))
      
      # Log
      log.info('Iter %d/%d: %d outliers' % (len(outmask) - 2, self.oiter, len(self.outmask)))

  def optimize_lambda(self, validation):
    '''
    Returns the index of :py:attr:`self.lambda_arr` that minimizes the validation scatter
    in the segment with minimum at the lowest value of :py:obj:`lambda`, with
    fractional tolerance :py:attr:`self.leps`.
    
    :param numpy.ndarray validation: The scatter in the validation set as a function of :py:obj:`lambda`
    
    '''
    
    maxm = 0
    minr = len(validation)
    for n in range(validation.shape[1]):
      m = np.nanargmin(validation[:,n])
      if m > maxm:
        maxm = m
      r = np.where((validation[:,n] - validation[m,n]) / 
                    validation[m,n] <= self.leps)[0][-1]
      if r < minr:
        minr = r
    return min(maxm, minr)

  def cross_validate(self, ax, info = ''):
    '''
    Cross-validate to find the optimal value of :py:obj:`lambda`.
    
    :param ax: The current :py:obj:`matplotlib.pyplot` axis instance to plot the \
               cross-validation results.
    :param str info: The label to show in the bottom right-hand corner of the plot. Default `''`
    
    '''
    
    # Loop over all chunks
    ax = np.atleast_1d(ax)
    for b, brkpt in enumerate(self.breakpoints):
    
      log.info("Cross-validating chunk %d/%d..." % (b + 1, len(self.breakpoints)))      
      med_training = np.zeros_like(self.lambda_arr)
      med_validation = np.zeros_like(self.lambda_arr)
        
      # Mask for current chunk 
      m = self.get_masked_chunk(b)
      
      # Check that we have enough data
      if len(m) < 3 * self.cdivs:
        self.cdppv_arr[b] = np.nan
        self.lam[b][self.lam_idx] = -np.inf
        log.info("Insufficient data to run cross-validation on this chunk.")
        continue
        
      # Mask transits and outliers
      time = self.time[m]
      flux = self.fraw[m]
      ferr = self.fraw_err[m]
      med = np.nanmedian(flux)
    
      # The precision in the validation set
      validation = [[] for k, _ in enumerate(self.lambda_arr)]
    
      # The precision in the training set
      training = [[] for k, _ in enumerate(self.lambda_arr)]
    
      # Setup the GP
      _, amp, tau = self.kernel_params
      gp = george.GP(amp ** 2 * george.kernels.Matern32Kernel(tau ** 2))
      gp.compute(time, ferr)
    
      # The masks
      masks = list(Chunks(np.arange(0, len(time)), len(time) // self.cdivs))
    
      # Loop over the different masks
      for i, mask in enumerate(masks):
      
        log.info("Section %d/%d..." % (i + 1, len(masks)))
      
        # Pre-compute (training set)
        pre_t = self.cv_precompute([], b)

        # Pre-compute (validation set)
        pre_v = self.cv_precompute(mask, b)
    
        # Iterate over lambda
        for k, lam in enumerate(self.lambda_arr):
      
          # Update the lambda matrix
          self.lam[b][self.lam_idx] = lam
      
          # Training set. Note that we're computing the MAD, not the
          # standard deviation, as this handles extremely variable
          # stars much better!
          model = self.cv_compute(b, *pre_t)
          gpm, _ = gp.predict(flux - model - med, time[mask])
          fdet = (flux - model)[mask] - gpm
          scatter = 1.e6 * (1.4826 * np.nanmedian(np.abs(fdet / med - 
                                                  np.nanmedian(fdet / med))) /
                                                  np.sqrt(len(mask)))
          training[k].append(scatter)
      
          # Validation set
          model = self.cv_compute(b, *pre_v)
          gpm, _ = gp.predict(flux - model - med, time[mask])
          fdet = (flux - model)[mask] - gpm
          scatter = 1.e6 * (1.4826 * np.nanmedian(np.abs(fdet / med - 
                                                  np.nanmedian(fdet / med))) /
                                                  np.sqrt(len(mask)))
          validation[k].append(scatter)
      
      # Finalize
      training = np.array(training)
      validation = np.array(validation)
      for k, _ in enumerate(self.lambda_arr):

        # Take the mean
        med_validation[k] = np.nanmean(validation[k])
        med_training[k] = np.nanmean(training[k])
            
      # Compute best model
      i = self.optimize_lambda(validation)
      v_best = med_validation[i]
      t_best = med_training[i]
      self.cdppv_arr[b] = v_best / t_best
      self.lam[b][self.lam_idx] = self.lambda_arr[i]
      log.info("Found optimum solution at log(lambda) = %.1f." % np.log10(self.lam[b][self.lam_idx]))
      
      # Plotting: There's not enough space in the DVS to show the cross-val results
      # for more than two light curve segments.
      if len(self.breakpoints) <= 2:
      
        # Plotting hack: first x tick will be -infty
        lambda_arr = np.array(self.lambda_arr)
        lambda_arr[0] = 10 ** (np.log10(lambda_arr[1]) - 3)
    
        # Plot cross-val
        for n in range(len(masks)):
          ax[b].plot(np.log10(lambda_arr), validation[:,n], 'r-', alpha = 0.3)
        ax[b].plot(np.log10(lambda_arr), med_training, 'b-', lw = 1., alpha = 1)
        ax[b].plot(np.log10(lambda_arr), med_validation, 'r-', lw = 1., alpha = 1)            
        ax[b].axvline(np.log10(self.lam[b][self.lam_idx]), color = 'k', ls = '--', lw = 0.75, alpha = 0.75)
        ax[b].axhline(v_best, color = 'k', ls = '--', lw = 0.75, alpha = 0.75)
        ax[b].set_ylabel(r'Scatter (ppm)', fontsize = 5)
        hi = np.max(validation[0])
        lo = np.min(training)
        rng = (hi - lo)
        ax[b].set_ylim(lo - 0.15 * rng, hi + 0.15 * rng)
        if rng > 2:
          ax[b].get_yaxis().set_major_formatter(Formatter.CDPP)
          ax[b].get_yaxis().set_major_locator(MaxNLocator(integer = True))
        elif rng > 0.2:
          ax[b].get_yaxis().set_major_formatter(Formatter.CDPP1F)
        else:
          ax[b].get_yaxis().set_major_formatter(Formatter.CDPP2F)
        
        # Fix the x ticks
        xticks = [np.log10(lambda_arr[0])] + list(np.linspace(np.log10(lambda_arr[1]), np.log10(lambda_arr[-1]), 6))
        ax[b].set_xticks(xticks)
        ax[b].set_xticklabels(['' for x in xticks])
        pad = 0.01 * (np.log10(lambda_arr[-1]) - np.log10(lambda_arr[0]))
        ax[b].set_xlim(np.log10(lambda_arr[0]) - pad, np.log10(lambda_arr[-1]) + pad)
        ax[b].annotate('%s.%d' % (info, b), xy = (0.02, 0.025), xycoords = 'axes fraction', 
                       ha = 'left', va = 'bottom', fontsize = 8, alpha = 0.5, 
                       fontweight = 'bold')
    
    # Finally, compute the model
    self.compute()
    
    # Tidy up
    if len(ax) == 2:
      ax[0].xaxis.set_ticks_position('top')
    for axis in ax[1:]:
      axis.spines['top'].set_visible(False)
      axis.xaxis.set_ticks_position('bottom')
    
    if len(self.breakpoints) <= 2:

      # A hack to mark the first xtick as -infty
      labels = ['%.1f' % x for x in xticks]
      labels[0] = r'$-\infty$'
      ax[-1].set_xticklabels(labels) 
      ax[-1].set_xlabel(r'Log $\Lambda$', fontsize = 5)
    
    else:
        
      # We're just going to plot lambda as a function of chunk number (DEBUG)
      bs = np.arange(len(self.breakpoints))
      ax[0].plot(bs + 1, [np.log10(self.lam[b][self.lam_idx]) for b in bs], 'r.')
      ax[0].plot(bs + 1, [np.log10(self.lam[b][self.lam_idx]) for b in bs], 'r-', alpha = 0.25)
      ax[0].set_ylabel(r'$\log\Lambda$', fontsize = 5)
      ax[0].margins(0.1, 0.1)
      ax[0].set_xticklabels([])
      
      # Now plot the CDPP and approximate validation CDPP
      cdpp_arr = self.get_cdpp_arr()
      cdppv_arr = self.cdppv_arr * cdpp_arr
      ax[1].plot(bs + 1, cdpp_arr, 'b.')
      ax[1].plot(bs + 1, cdpp_arr, 'b-', alpha = 0.25)
      ax[1].plot(bs + 1, cdppv_arr, 'r.')
      ax[1].plot(bs + 1, cdppv_arr, 'r-', alpha = 0.25)
      ax[1].margins(0.1, 0.1)
      ax[1].set_ylabel(r'CDPP', fontsize = 5)
      ax[1].set_xlabel(r'Chunk', fontsize = 5)
      
  def finalize(self):
    '''
    This method is called at the end of the de-trending, prior to plotting the final results.
    Subclass it to add custom functionality to individual models.
    
    '''
    
    pass
  
  def get_ylim(self):
    '''
    Computes the ideal y-axis limits for the light curve plot. Attempts to set
    the limits equal to those of the raw light curve, but if more than 1% of the
    flux lies either above or below these limits, auto-expands to include those
    points. At the end, adds 5% padding to both the top and the bottom.
    
    '''
    
    bn = np.array(list(set(np.concatenate([self.badmask, self.nanmask]))), dtype = int)
    fraw = np.delete(self.fraw, bn)
    lo, hi = fraw[np.argsort(fraw)][[3,-3]]
    flux = np.delete(self.flux, bn)
    fsort = flux[np.argsort(flux)]
    if fsort[int(0.01 * len(fsort))] < lo:
      lo = fsort[int(0.01 * len(fsort))]
    if fsort[int(0.99 * len(fsort))] > hi:
      hi = fsort[int(0.99 * len(fsort))]
    pad = (hi - lo) * 0.05
    ylim = (lo - pad, hi + pad)
    return ylim
    
  def plot_lc(self, ax, info_left = '', info_right = '', color = 'b'):
    '''
    Plots the current light curve. This is called at several stages to plot the
    de-trending progress as a function of the different *PLD* orders.
    
    :param ax: The current :py:obj:`matplotlib.pyplot` axis instance
    :param str info_left: Information to display at the left of the plot. Default `''`
    :param str info_right: Information to display at the right of the plot. Default `''`
    :param str color: The color of the data points. Default `'b'`
    
    '''

    # Plot
    if self.cadence == 'lc':
      ax.plot(self.apply_mask(self.time), self.apply_mask(self.flux), ls = 'none', marker = '.', color = color, markersize = 2, alpha = 0.5)
    else:
      ax.plot(self.apply_mask(self.time), self.apply_mask(self.flux), ls = 'none', marker = '.', color = color, markersize = 2, alpha = 0.03, zorder = -1)
      ax.set_rasterization_zorder(0)
    ylim = self.get_ylim()
    
    # Plot the outliers
    bnmask = np.array(list(set(np.concatenate([self.badmask, self.nanmask]))), dtype = int)
    O1 = lambda x: x[self.outmask]
    O2 = lambda x: x[bnmask]
    if self.cadence == 'lc':
      ax.plot(O1(self.time), O1(self.flux), ls = 'none', color = "#777777", marker = '.', markersize = 2, alpha = 0.5)
      ax.plot(O2(self.time), O2(self.flux), 'r.', markersize = 2, alpha = 0.25)
    else:
      ax.plot(O1(self.time), O1(self.flux), ls = 'none', color = "#777777", marker = '.', markersize = 2, alpha = 0.25, zorder = -1)
      ax.plot(O2(self.time), O2(self.flux), 'r.', markersize = 2, alpha = 0.125, zorder = -1)
    for i in np.where(self.flux < ylim[0])[0]:
      if i in bnmask:
        color = "#ffcccc"
      elif i in self.outmask:
        color = "#cccccc"
      else:
        color = "#ccccff"
      ax.annotate('', xy=(self.time[i], ylim[0]), xycoords = 'data',
                  xytext = (0, 15), textcoords = 'offset points',
                  arrowprops=dict(arrowstyle = "-|>", color = color))
    for i in np.where(self.flux > ylim[1])[0]:
      if i in bnmask:
        color = "#ffcccc"
      elif i in self.outmask:
        color = "#cccccc"
      else:
        color = "#ccccff"
      ax.annotate('', xy=(self.time[i], ylim[1]), xycoords = 'data',
                  xytext = (0, -15), textcoords = 'offset points',
                  arrowprops=dict(arrowstyle = "-|>", color = color))
    
    # Plot the breakpoints
    for brkpt in self.breakpoints[:-1]:
      if len(self.breakpoints) <= 5:
        ax.axvline(self.time[brkpt], color = 'r', ls = '--', alpha = 0.5)
      else:
        ax.axvline(self.time[brkpt], color = 'r', ls = '-', alpha = 0.025)
        
    # Appearance
    if len(self.cdpp6_arr) == 2:
      ax.annotate('%.2f ppm' % self.cdpp6_arr[0], xy = (0.02, 0.975), xycoords = 'axes fraction', 
                  ha = 'left', va = 'top', fontsize = 12)
      ax.annotate('%.2f ppm' % self.cdpp6_arr[1], xy = (0.98, 0.975), xycoords = 'axes fraction', 
                  ha = 'right', va = 'top', fontsize = 12)
    else:
      ax.annotate('%.2f ppm' % self.cdpp6, xy = (0.02, 0.975), xycoords = 'axes fraction', 
                  ha = 'left', va = 'top', fontsize = 12)
    ax.annotate(info_right, xy = (0.98, 0.025), xycoords = 'axes fraction', 
                ha = 'right', va = 'bottom', fontsize = 10, alpha = 0.5, 
                fontweight = 'bold')            
    ax.annotate(info_left, xy = (0.02, 0.025), xycoords = 'axes fraction', 
                ha = 'left', va = 'bottom', fontsize = 8)     
    ax.set_xlabel(r'Time (%s)' % self._mission.TIMEUNITS, fontsize = 5)
    ax.margins(0.01, 0.1)
    ax.set_ylim(*ylim)
    ax.get_yaxis().set_major_formatter(Formatter.Flux)
  
  def plot_final(self, ax):
    '''
    Plots the final de-trended light curve.
    
    '''
 
    # Plot the light curve
    bnmask = np.array(list(set(np.concatenate([self.badmask, self.nanmask]))), dtype = int)
    M = lambda x: np.delete(x, bnmask)
    if self.cadence == 'lc':
      ax.plot(M(self.time), M(self.flux), ls = 'none', marker = '.', color = 'k', markersize = 2, alpha = 0.3)
    else:
      ax.plot(M(self.time), M(self.flux), ls = 'none', marker = '.', color = 'k', markersize = 2, alpha = 0.03, zorder = -1)
      ax.set_rasterization_zorder(0)

    # Plot the GP (long cadence only)
    if self.cadence == 'lc':
      _, amp, tau = self.kernel_params
      gp = george.GP(amp ** 2 * george.kernels.Matern32Kernel(tau ** 2))
      gp.compute(self.apply_mask(self.time), self.apply_mask(self.fraw_err))
      med = np.nanmedian(self.apply_mask(self.flux))
      y, _ = gp.predict(self.apply_mask(self.flux) - med, self.time)
      y += med
      ax.plot(M(self.time), M(y), 'r-', lw = 0.5, alpha = 0.5)
      
      # Compute the CDPP of the GP-detrended flux
      self.gppp = CDPP6(self.apply_mask(self.flux - y + med), cadence = self.cadence)
    
    else:
      
      # We're not going to calculate this
      self.gppp = 0.
      
    # Appearance
    ax.annotate('Final', xy = (0.98, 0.025), xycoords = 'axes fraction', 
                ha = 'right', va = 'bottom', fontsize = 10, alpha = 0.5, 
                fontweight = 'bold') 
    ax.margins(0.01, 0.1)          
    
    # Get y lims that bound 99% of the flux
    flux = np.delete(self.flux, bnmask)
    N = int(0.995 * len(flux))
    hi, lo = flux[np.argsort(flux)][[N,-N]]
    fsort = flux[np.argsort(flux)]
    pad = (hi - lo) * 0.1
    ylim = (lo - pad, hi + pad)
    ax.set_ylim(ylim)   
    ax.get_yaxis().set_major_formatter(Formatter.Flux)

  def plot_info(self, dvs):
    '''
    Plots miscellaneous de-trending information on the data validation summary figure.
    
    :param dvs: A :py:class:`dvs.DVS1` or :py:class:`dvs.DVS2` figure instance
    
    '''
    
    axl, axc, axr = dvs.title()
    axc.annotate("%s %d" % (self._mission.IDSTRING, self.ID),
                 xy = (0.5, 0.5), xycoords = 'axes fraction', 
                 ha = 'center', va = 'center', fontsize = 18)
    
    axc.annotate(r"%.2f ppm $\rightarrow$ %.2f ppm" % (self.cdppr, self.cdpp6),
                 xy = (0.5, 0.2), xycoords = 'axes fraction',
                 ha = 'center', va = 'center', fontsize = 8, color = 'k',
                 fontstyle = 'italic')
    
    axl.annotate("%s %s%02d: %s" % (self.mission.upper(), 
                 self._mission.SEASONCHAR, self.season, self.name),
                 xy = (0.5, 0.5), xycoords = 'axes fraction', 
                 ha = 'center', va = 'center', fontsize = 12,
                 color = 'k')
    
    axl.annotate(self.aperture_name if len(self.neighbors) == 0 else "%s, %d neighbors" % (self.aperture_name, len(self.neighbors)),
                 xy = (0.5, 0.2), xycoords = 'axes fraction',
                 ha = 'center', va = 'center', fontsize = 8, color = 'k',
                 fontstyle = 'italic')
    
    axr.annotate("%s %.3f" % (self._mission.MAGSTRING, self.mag),
                 xy = (0.5, 0.5), xycoords = 'axes fraction', 
                 ha = 'center', va = 'center', fontsize = 12,
                 color = 'k')
    
    axr.annotate(r"GP %.3f ppm" % (self.gppp),
                 xy = (0.5, 0.2), xycoords = 'axes fraction',
                 ha = 'center', va = 'center', fontsize = 8, color = 'k',
                 fontstyle = 'italic')
      
  def plot_page2(self):
    '''
    Plots the second page of the data validation summary.
    
    '''
    
    # Plot the raw light curve
    ax1 = self.dvs2.lc1()    
    if self.cadence == 'lc':
      ax1.plot(self.time, self.fraw, ls = 'none', marker = '.', color = 'k', markersize = 2, alpha = 0.5)
    else:
      ax1.plot(self.time, self.fraw, ls = 'none', marker = '.', color = 'k', markersize = 2, alpha = 0.05, zorder = -1)
      ax1.set_rasterization_zorder(0)
    ax1.annotate('Raw', xy = (0.98, 0.025), xycoords = 'axes fraction', 
                ha = 'right', va = 'bottom', fontsize = 10, alpha = 0.5, 
                fontweight = 'bold') 
    ax1.margins(0.01, 0.1)  
    bnmask = np.array(list(set(np.concatenate([self.badmask, self.nanmask]))), dtype = int)
    flux = np.delete(self.flux, bnmask)
    N = int(0.995 * len(flux))
    hi, lo = flux[np.argsort(flux)][[N,-N]]
    fsort = flux[np.argsort(flux)]
    pad = (hi - lo) * 0.1
    ylim = (lo - pad, hi + pad)   
    ax1.set_ylim(ylim)   
    ax1.get_yaxis().set_major_formatter(Formatter.Flux) 

    # Plot the de-trended light curve
    ax2 = self.dvs2.lc2()
    bnmask = np.array(list(set(np.concatenate([self.badmask, self.nanmask]))), dtype = int)
    M = lambda x: np.delete(x, bnmask)
    if self.cadence == 'lc':
      ax2.plot(M(self.time), M(self.flux), ls = 'none', marker = '.', color = 'k', markersize = 2, alpha = 0.3)
    else:
      ax2.plot(M(self.time), M(self.flux), ls = 'none', marker = '.', color = 'k', markersize = 2, alpha = 0.03, zorder = -1)
      ax2.set_rasterization_zorder(0)
    ax2.annotate('LC', xy = (0.98, 0.025), xycoords = 'axes fraction', 
                ha = 'right', va = 'bottom', fontsize = 10, alpha = 0.5, 
                fontweight = 'bold') 
    ax2.margins(0.01, 0.1)          
    ax2.set_ylim(ylim)   
    ax2.get_yaxis().set_major_formatter(Formatter.Flux) 
    
    # Plot the PLD weights
    if len(self.breakpoints) <= 2:
      self.plot_weights(*self.dvs2.weights_grid())
    
  def load_tpf(self):
    '''
    Loads the target pixel file.
    
    '''
    
    if not self.loaded:
      data = self._mission.GetData(self.ID, season = self.season, 
                  cadence = self.cadence, clobber = self.clobber_tpf, 
                  aperture_name = self.aperture_name, 
                  saturated_aperture_name = self.saturated_aperture_name, 
                  max_pixels = self.max_pixels,
                  saturation_tolerance = self.saturation_tolerance)
      self.cadn = data.cadn
      self.time = data.time
      self.model = np.zeros_like(self.time)
      self.fpix = data.fpix
      self.fraw = np.sum(self.fpix, axis = 1)
      self.fpix_err = data.fpix_err
      self.fraw_err = np.sqrt(np.sum(self.fpix_err ** 2, axis = 1))
      self.nanmask = data.nanmask
      self.badmask = data.badmask
      self.transitmask = np.array([], dtype = int)
      self.outmask = np.array([], dtype = int)
      self.aperture = data.aperture
      self.aperture_name = data.aperture_name
      self.apertures = data.apertures
      self.quality = data.quality
      self.Xpos = data.Xpos
      self.Ypos = data.Ypos
      self.mag = data.mag
      self.pixel_images = data.pixel_images
      self.nearby = data.nearby
      self.hires = data.hires
      self.saturated = data.saturated
      self.meta = data.meta
      self.bkg = data.bkg
      
      # Update the last breakpoint to the correct value
      self.breakpoints[-1] = len(self.time) - 1
      self.loaded = True
  
  def load_model(self, name = None):
    '''
    Loads a saved version of the model.
    
    '''
    
    if self.clobber:
      return False
    
    if name is None:
      name = self.name    
    file = os.path.join(self.dir, '%s.npz' % name)
    if os.path.exists(file):
      if not self.is_parent: 
        log.info("Loading '%s.npz'..." % name)
      try:
        data = np.load(file)
        for key in data.keys():
          try:
            setattr(self, key, data[key][()])
          except NotImplementedError:
            pass
        pl.close()
        return True
      except:
        log.warn("Error loading '%s.npz'." % name)
        exctype, value, tb = sys.exc_info()
        for line in traceback.format_exception_only(exctype, value):
          l = line.replace('\n', '')
          log.warn(l)
        os.rename(file, file + '.bad')
    
    if self.is_parent:
      raise Exception('Unable to load `%s` model for target %d.' % (self.name, self.ID))
    
    return False

  def save_model(self):
    '''
    Saves all of the de-trending information to disk in an `npz` file
    and saves the DVS as a `pdf`.
    
    '''
    
    # Save the data
    log.info("Saving data to '%s.npz'..." % self.name)
    d = dict(self.__dict__)
    d.pop('_weights', None)
    d.pop('_A', None)
    d.pop('_B', None)
    d.pop('_f', None)
    d.pop('_mK', None)
    d.pop('K', None)
    d.pop('dvs1', None)
    d.pop('dvs2', None)
    d.pop('clobber', None)
    d.pop('clobber_tpf', None)
    d.pop('_mission', None)
    d.pop('debug', None)
    np.savez(os.path.join(self.dir, self.name + '.npz'), **d)
    
    # Save the DVS
    pdf = PdfPages(os.path.join(self.dir, self.name + '.pdf'))
    pdf.savefig(self.dvs1.fig)
    pl.close(self.dvs1.fig)
    pdf.savefig(self.dvs2.fig)
    pl.close(self.dvs2.fig)
    d = pdf.infodict()
    d['Title'] = 'EVEREST: %s de-trending of %s %d' % (self.name, self._mission.IDSTRING, self.ID)
    d['Author'] = 'Rodrigo Luger'
    pdf.close()
    
  def exception_handler(self, pdb):
    '''
    A custom exception handler.
    
    :param pdb: If :py:obj:`True`, enters PDB post-mortem mode for debugging.
    
    '''
    
    # Grab the exception
    exctype, value, tb = sys.exc_info()
    
    # Log the error and create a .err file
    errfile = os.path.join(self.dir, self.name + '.err')
    with open(errfile, 'w') as f:
      for line in traceback.format_exception_only(exctype, value):
        l = line.replace('\n', '')
        log.error(l)
        print(l, file = f)
      for line in traceback.format_tb(tb):
        l = line.replace('\n', '')
        log.error(l)
        print(l, file = f)
    
    # Re-raise?
    if pdb:
      raise
  
  def update_gp(self):
    '''
    Calls :py:func:`gp.GetKernelParams` to optimize the GP and obtain the
    covariance matrix for the regression.
    
    '''
    
    self.kernel_params = GetKernelParams(self.time, self.flux, self.fraw_err, 
                                         mask = self.mask, guess = self.kernel_params, 
                                         giter = self.giter)
  
  def init_kernel(self):
    '''
    Initializes the covariance matrix with a guess at the GP kernel parameters.
    
    '''
    
    if self.kernel_params is None:
      X = self.apply_mask(self.fpix / self.flux.reshape(-1, 1))
      y = self.apply_mask(self.flux) - np.dot(X, np.linalg.solve(np.dot(X.T, X), np.dot(X.T, self.apply_mask(self.flux))))      
      white = np.nanmedian([np.nanstd(c) for c in Chunks(y, 13)])
      amp = self.gp_factor * np.nanstd(y)
      tau = 30.0
      self.kernel_params = [white, amp, tau]
  
  def run(self):
    '''
    Runs the de-trending step.
    
    '''
    
    try:
          
      # Load raw data
      log.info("Loading target data...")
      self.load_tpf()
      self.plot_aperture([self.dvs1.top_right() for i in range(4)])  
      self.plot_aperture([self.dvs2.top_right() for i in range(4)]) 
      self.init_kernel()
      M = self.apply_mask(np.arange(len(self.time)))
      self.cdppr_arr = self.get_cdpp_arr()
      self.cdpp6_arr = np.array(self.cdppr_arr)
      self.cdppv_arr = np.array(self.cdppr_arr)
      self.cdppr = self.get_cdpp()
      self.cdpp6 = self.cdppr
      self.cdppv = self.cdppr

      log.info("%s (Raw): CDPP6 = %s" % (self.name, self.cdpps))
      self.plot_lc(self.dvs1.left(), info_right = 'Raw', color = 'k')
      
      # Loop
      for n in range(self.pld_order):
        self.lam_idx += 1
        self.get_outliers()
        if n > 0 and self.optimize_gp:
          self.update_gp()
        self.cross_validate(self.dvs1.right(), info = 'CV%d' % n)
        self.cdpp6_arr = self.get_cdpp_arr()
        self.cdppv_arr *= self.cdpp6_arr
        self.cdpp6 = self.get_cdpp()
        self.cdppv = np.nanmean(self.cdppv_arr)
        log.info("%s (%d/%d): CDPP = %s" % (self.name, n + 1, self.pld_order, self.cdpps))
        self.plot_lc(self.dvs1.left(), info_right= 'LC%d' % (n + 1), info_left = '%d outliers' % len(self.outmask))
        
      # Save
      self.finalize()
      self.plot_final(self.dvs1.top_left())
      self.plot_page2()
      self.plot_info(self.dvs1)
      self.plot_info(self.dvs2)
      self.save_model()
      
      if self.make_fits:
        MakeFITS(self)
        
    except:
    
      self.exception_handler(self.debug)

class rPLD(pld.rPLD, Detrender):
  '''
  A wrapper around the standard PLD model.
  
  '''
        
  def __init__(self, *args, **kwargs):
    '''
    
    '''
    
    # Initialize
    super(rPLD, self).__init__(*args, **kwargs)
    
    # Check for saved model
    if self.load_model():
      return
    
    # Setup
    self._setup(**kwargs)
    
    # Run
    self.run()

class nPLD(pld.nPLD, Detrender):
  '''
  A wrapper around the "neighboring stars" *PLD* model. This model uses the 
  *PLD* vectors of neighboring stars to help in the de-trending and can lead 
  to increased performance over the regular :py:class:`rPLD` model, 
  particularly for dimmer stars.
    
  '''
        
  def __init__(self, *args, **kwargs):
    '''
    
    '''
    
    # Initialize
    super(nPLD, self).__init__(*args, **kwargs)
    
    # Check for saved model
    if self.load_model():
      return
    
    # Setup
    self._setup(**kwargs)

    # Run
    self.run()

class nPLD2(pld.nPLD, Detrender):
  '''
  A wrapper around the "neighboring stars" *PLD* model. This model uses the 
  *PLD* vectors of neighboring stars to help in the de-trending and can lead 
  to increased performance over the regular :py:class:`rPLD` model, 
  particularly for dimmer stars.
    
  '''
        
  def __init__(self, *args, **kwargs):
    '''
    
    '''
    
    # Initialize
    super(nPLD2, self).__init__(*args, **kwargs)
    
    # Check for saved model
    if self.load_model():
      return
    
    # Setup
    self._setup(**kwargs)

    # Run
    self.run()