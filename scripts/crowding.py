#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
crowding.py
-----------

'''

from __future__ import division, print_function, absolute_import, unicode_literals
from everest.missions.k2 import GetData, Season, TargetDirectory
import os, sys, glob
import k2plr
from k2plr.config import KPLR_ROOT
import numpy as np
import matplotlib.pyplot as pl
from scipy.interpolate import RectBivariateSpline
from scipy.optimize import fmin_powell
try:
  import pyfits
except ImportError:
  try:
    import astropy.io.fits as pyfits
  except ImportError:
    raise Exception('Please install the `pyfits` package.')

class CrowdingTarget(object):
    '''
    
    '''
    
    def __init__(self, ID, maxsrc = 3, prfdir = '/Users/rodrigo/src/KeplerPRF',
                 xtol = 1.e-4, ftol = 1.e-4):
        '''
        
        '''
        
        # Target ID (EPIC number)
        self.ID = ID
        
        # Maximum number of sources to fit
        self.nsrc = maxsrc

        # Kepler PRF directory
        self.prfdir = prfdir
        
        # Powell minimization params
        self.xtol = xtol
        self.ftol = ftol
        
        # Some geometrical stuff...     
        self.wx = 1.0
        self.wy = 1.0
        self.a = 0.0
        
        # Get the data
        self.GetK2Data()
        _, self.ny, self.nx = self.fpix.shape

        # Generate the PRF and clip sources outside the aperture
        self.generatePRF()
        self.clipNearby()
        
        # Initialize arrays
        self.index = 100
        self.bic = []
        self.chisq = []
        self.guesses = []
        self.answers = []
        
    def GetK2Data(self):
        '''
  
        '''

        GetData(self.ID, download_only = True)
        data = np.load(os.path.join(TargetDirectory(self.ID, Season(self.ID)), 'data.npz'))
  
        # Load the raw data
        fpix = data['fpix']
        time = data['time']
        qual = data['qual']
        flux = np.nansum(fpix, axis = (1,2))
    
        # Remove bad timestamps
        badmask = list(np.where(np.isnan(time) | np.isnan(flux) | (flux == 0))[0])
        for b in [1,2,3,4,5,6,7,8,9,11,12,13,14,16,17]:
            badmask += list(np.where(qual & 2 ** (b - 1))[0])
        badmask = np.array(list(set(badmask)))
        fpix = np.delete(fpix, badmask, axis = 0)
        flux = np.delete(flux, badmask)
  
        # Compute the standard deviation of the flux
        # TODO: We need to think about this more carefully, since
        # there's a TON of instrumental noise mixed in here. We
        # want just the photometric noise.
        ferr = np.nanstd(flux)
    
        # Get the nearby targets
        nearby_dict = data['nearby']
        nearby = []
        for source in nearby_dict:
          source['flux'] = (10 ** (17. - source['mag']))
          nearby.append(type('Source', (object,), source))
    
        # Get the kepler magnitude
        mag = data['fitsheader'][()][0]['KEPMAG'][1]
        
        # Save
        self.fpix = fpix
        self.ferr = ferr
        self.nearby = nearby
        self.mag = mag

    def generatePRF(self):
        '''
        Create PRF for location of target on detector
        
        '''
        
        # read PRF header data
        client = k2plr.API()
        star = client.k2_star(self.ID)
        tpf = star.get_target_pixel_files(fetch = True)[0]
        ftpf = os.path.join(KPLR_ROOT, 'data', 'k2', 'target_pixel_files', '%d' % self.ID, tpf._filename)
        tdim5 = pyfits.getheader(ftpf,1)['TDIM5']
        xdim = int(tdim5.strip().strip('(').strip(')').split(',')[0])
        ydim = int(tdim5.strip().strip('(').strip(')').split(',')[1])
        module = pyfits.getheader(ftpf,0)['MODULE']
        output = pyfits.getheader(ftpf,0)['OUTPUT']
        column = pyfits.getheader(ftpf,2)['CRVAL1P']
        row = pyfits.getheader(ftpf,2)['CRVAL2P']
        with pyfits.open(ftpf) as f:
            xpos = f[1].data['pos_corr1']
            ypos = f[1].data['pos_corr2']
        
        # Get PRF file name
        if int(module) < 10:
            prefix = 'kplr0'
        else:
            prefix = 'kplr'
        prfglob = os.path.join(self.prfdir, prefix + str(module) + '.' + str(output) + '*' + '_prf.fits')
        prffile = glob.glob(prfglob)[0]

        # create PRF matrix
        prfn = [0,0,0,0,0]
        crpix1p = np.zeros((5),dtype='float32')
        crpix2p = np.zeros((5),dtype='float32')
        crval1p = np.zeros((5),dtype='float32')
        crval2p = np.zeros((5),dtype='float32')
        cdelt1p = np.zeros((5),dtype='float32')
        cdelt2p = np.zeros((5),dtype='float32')
        
        # Read in the PRF
        for i in range(5):
          with pyfits.open(prffile, mode = 'readonly') as prf:
              prfn[i] = prf[i+1].data
              crpix1p[i] = prf[i+1].header['CRPIX1P']
              crpix2p[i] = prf[i+1].header['CRPIX2P']
              crval1p[i] = prf[i+1].header['CRVAL1P']
              crval2p[i] = prf[i+1].header['CRVAL2P']
              cdelt1p[i] = prf[i+1].header['CDELT1P']
              cdelt2p[i] = prf[i+1].header['CDELT2P']
        prfn = np.array(prfn)
        
        # PRF dimensions
        PRFx = np.arange(0.5,np.shape(prfn[0])[1]+0.5)
        PRFy = np.arange(0.5,np.shape(prfn[0])[0]+0.5)
        self.PRFx = (PRFx - np.size(PRFx) / 2) * cdelt1p[0]
        self.PRFy = (PRFy - np.size(PRFy) / 2) * cdelt2p[0]
        
        # Combine the PRFs
        prf = np.zeros(np.shape(prfn[0]), dtype='float32')
        prfWeight = np.zeros((5), dtype='float32')
        for i in range(5):
            prfWeight[i] = np.sqrt((column - crval1p[i])**2 + (row - crval2p[i])**2)
            if prfWeight[i] == 0.0:
                prfWeight[i] = 1.0e-6
            prf = prf + prfn[i] / prfWeight[i]
        self.prf = prf / np.nansum(prf) / cdelt1p[0] / cdelt2p[0]
        self.DATx = np.arange(column,column+xdim)
        self.DATy = np.arange(row,row+ydim)
        
        # Generate interpolant
        self.splineInterpolation = RectBivariateSpline(self.PRFx, self.PRFy, self.prf)
    
    def findSolution(self, index = 100):
        '''
        Minimize residuals to find array with best parameters
        
        '''
        
        # This is the index of the timeseries we're going to fit
        self.index = index
        
        # Initialize the BIC and the CHISQ. The first term
        # corresponds to zero fit parameters (i.e., model = 0.)
        X = np.nansum((self.fpix[self.index] / self.ferr) ** 2)
        self.bic = [X]
        self.chisq = [X]
        self.guesses = [None]
        self.answers = [None]

        # return the BIC and guess array for the best fit
        for i in range(1, self.nsrc + 1):
            
            # Generate our guess parameter array
            f = [source.flux for source in self.nearby[:i]]
            x = [source.x for source in self.nearby[:i]]
            y = [source.y for source in self.nearby[:i]]
            guess = np.concatenate([f, x, y])

            # calculate best parameters for PRF
            answer, chisq, _, iter, funcalls, warn = fmin_powell(self.PRF, guess, xtol = self.xtol, ftol = self.ftol, 
                                                                 disp = False, full_output = True)

            # Update chisq and bic
            self.chisq.append(chisq)
            self.bic.append(chisq + len(answer) * np.log(len(self.fpix)))
            
            # Save the guess fit and the answer fit
            self.guesses.append(self.Fit(guess))
            self.answers.append(self.Fit(answer))
            
    def Fit(self, fxy):
        '''
        
        '''
        
        i = len(fxy) // 3
        f = fxy[:i]
        x = fxy[i:2 * i]
        y = fxy[2 * i:]
        fit = self.PRF2DET(f, x, y)
        
        return type('Fit', (object,), {'f': f, 'x': x, 'y': y, 'fit': fit})
     
    def PRF(self, fxy):
        '''
        Returns the residuals of the PRF fit for ``nsrc`` sources.
        
        '''
        
        # calculate PRF model binned to the detector pixel size
        fit = self.Fit(fxy)
        x = fit.x
        y = fit.y
        f = fit.f
        PRFfit = fit.fit

        # calculate the sum squared difference between data and model
        PRFres = np.nansum(((self.fpix[self.index] - PRFfit) / self.ferr) ** 2)

        # keep the fit centered
        if max(np.abs(np.mean(self.DATx) - x[0]), np.abs(np.mean(self.DATy) - y[0])) > 10.0:
            PRFres = 1.0e300

        # Reject negative fluxes
        for elem in f:
            if elem < 0:
                PRFres = 1.0e300

        return PRFres

    def PRF2DET(self, flux, OBJx, OBJy):
        '''
        
        '''
        
        # Constants
        cosa = np.cos(np.radians(self.a))
        sina = np.sin(np.radians(self.a))
        
        # Loop over each of the sources
        PRFfit = np.zeros((np.size(self.DATy), np.size(self.DATx)))
        for i in range(len(flux)):
            FRCx,INTx = np.modf(OBJx[i])
            FRCy,INTy = np.modf(OBJy[i])
            if FRCx > 0.5:
                FRCx -= 1.0
                INTx += 1.0
            if FRCy > 0.5:
                FRCy -= 1.0
                INTy += 1.0
            FRCx = -FRCx
            FRCy = -FRCy

            # Construct model PRF in detector coordinates
            for j, y in enumerate(self.DATy):
                for k, x in enumerate(self.DATx):
                    xx = x - INTx + FRCx
                    yy = y - INTy + FRCy
                    dx = xx * cosa - yy * sina
                    dy = xx * sina + yy * cosa
                    PRFfit[j,k] += PRFfit[j,k] + self.splineInterpolation(dy * self.wy, dx * self.wx) * flux[i]

        return PRFfit
        
    def clipNearby(self):
        '''
        
        '''
        
        self.nearby = np.array([source for source in self.nearby if 
                               (source.x >= self.DATx[0]) and 
                               (source.x <= self.DATx[-1]) and 
                               (source.y >= self.DATy[0]) and 
                               (source.y <= self.DATy[-1])])
        self.nearby = self.nearby[np.argsort([source.mag for source in self.nearby])]
        self.nearby = self.nearby[:self.nsrc]
        self.nsrc = len(self.nearby)
    
    def plot(self):
        '''
        
        '''
        
        rdbu = pl.get_cmap('RdBu_r')
        
        fig, ax = pl.subplots(2, self.nsrc, figsize = (12, 8))
        vmax = np.max([np.nanmax(self.fpix[self.index])] + [np.nanmax(a.fit) for a in self.answers[1:]])
        vmin = np.min([np.nanmin(self.fpix[self.index])] + [np.nanmin(a.fit) for a in self.answers[1:]])
        vmax = max(vmax, -vmin)
        vmin = -vmax
        
        ax[0,0].set_ylabel('Fits', fontsize = 18)
        ax[1,0].set_ylabel('Residuals', fontsize = 18)
        
        for n in range(self.nsrc):
            
            ax[0,n].set_title('%d sources' % (n + 1), fontsize = 18)
            
            # Show the fit
            ax[0,n].imshow(self.answers[n + 1].fit, interpolation = 'nearest', vmin = vmin, vmax = vmax, cmap = rdbu)
          
            # Show the residuals
            ax[1,n].imshow(self.fpix[self.index] - self.answers[n + 1].fit, interpolation = 'nearest', vmin = vmin, vmax = vmax, cmap = rdbu)
            
            # Calculate the fractional error in the fit
            err = np.sqrt(np.nansum((self.fpix[self.index] - self.answers[n + 1].fit) ** 2) / np.nansum(self.fpix[self.index] ** 2))
            
            # Display the catalog positions
            for i in range(n + 1):
                ax[0,n].plot(self.nearby[i].x - self.nearby[i].x0, 
                             self.nearby[i].y - self.nearby[i].y0, 
                             'ko', markeredgecolor = 'none', alpha = 0.5,
                             markersize = 10)
            
            # Display the solution positions
            ax[0,n].plot(self.answers[n + 1].x - self.nearby[n].x0, 
                         self.answers[n + 1].y - self.nearby[n].y0, 
                         'ro', markeredgecolor = 'none',
                         markersize = 3)
            
            # Display fit info
            ax[1,n].annotate(r'$\mathrm{BIC} = %.3f$' % self.bic[n + 1],
                             xy = (0.025, 0.95), xycoords = 'axes fraction',
                             ha = 'left', va = 'top', color = 'k', fontsize = 12)
            ax[1,n].annotate(r'$\mathrm{ERROR} = %.1f$' % (100 * err) + r'$\%$',
                             xy = (0.975, 0.05), xycoords = 'axes fraction',
                             ha = 'right', va = 'bottom', color = 'k', fontsize = 12)                 
            
            # Set limits
            ax[0,n].set_xlim(-0.5, self.nx - 0.5)
            ax[0,n].set_ylim(self.ny - 0.5, -0.5)
            ax[1,n].set_xlim(-0.5, self.nx - 0.5)
            ax[1,n].set_ylim(self.ny - 0.5, -0.5)

        pl.show()
    
c = CrowdingTarget(215796924)
c.findSolution()
c.plot()