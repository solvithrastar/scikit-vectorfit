import numpy as np
import os
from numpy import matlib, squeeze
import pdb

# imports for type hinting
from typing import Any, Tuple, TYPE_CHECKING, List

from sympy import to_cnf

if TYPE_CHECKING:
    from .network import Network

from functools import wraps

try:
    from . import (
        plotting,
    )  # will perform the correct setup for matplotlib before it is called below
    import matplotlib.pyplot as mplt
    from matplotlib.ticker import EngFormatter
except ImportError:
    mplt = None

import logging
import warnings
from timeit import default_timer as timer
import quadprog


def check_plotting(func):
    """
    This decorator checks if matplotlib.pyplot is available under the name mplt.
    If not, raise an RuntimeError.

    Raises
    ------
    RuntimeError
        When trying to run the decorated function without matplotlib
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        if mplt is None:
            raise RuntimeError("Plotting is not available")
        func(*args, **kwargs)

    return wrapper


class VectorFitting:
    """
    This class provides a Python implementation of the Vector Fitting algorithm and various functions for the fit
    analysis, passivity evaluation and enforcement, and export of SPICE equivalent circuits.

    Parameters
    ----------
    network : :class:`skrf.network.Network`
            Network instance of the :math:`N`-port holding the frequency responses to be fitted, for example a
            scattering, impedance or admittance matrix.

    Examples
    --------
    Load the `Network`, create a `VectorFitting` instance, perform the fit with a given number of real and
    complex-conjugate starting poles:

    >>> nw_3port = skrf.Network('my3port.s3p')
    >>> vf = skrf.VectorFitting(nw_3port)
    >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)

    Notes
    -----
    The fitting code is based on the original algorithm [#Gustavsen_vectfit]_ and on two improvements for relaxed pole
    relocation [#Gustavsen_relaxed]_ and efficient (fast) solving [#Deschrijver_fast]_. See also the Vector Fitting
    website [#vectfit_website]_ for further information and download of the papers listed below. A Matlab implementation
    is also available there for reference.

    References
    ----------
    .. [#Gustavsen_vectfit] B. Gustavsen, A. Semlyen, "Rational Approximation of Frequency Domain Responses by Vector
        Fitting", IEEE Transactions on Power Delivery, vol. 14, no. 3, pp. 1052-1061, July 1999,
        DOI: https://doi.org/10.1109/61.772353

    .. [#Gustavsen_relaxed] B. Gustavsen, "Improving the Pole Relocating Properties of Vector Fitting", IEEE
        Transactions on Power Delivery, vol. 21, no. 3, pp. 1587-1592, July 2006,
        DOI: https://doi.org/10.1109/TPWRD.2005.860281

    .. [#Deschrijver_fast] D. Deschrijver, M. Mrozowski, T. Dhaene, D. De Zutter, "Marcomodeling of Multiport Systems
        Using a Fast Implementation of the Vector Fitting Method", IEEE Microwave and Wireless Components Letters,
        vol. 18, no. 6, pp. 383-385, June 2008, DOI: https://doi.org/10.1109/LMWC.2008.922585

    .. [#vectfit_website] Vector Fitting website: https://www.sintef.no/projectweb/vectorfitting/
    """

    def __init__(self, network: "Network"):
        self.network = network
        """ Instance variable holding the Network to be fitted. This is the Network passed during initialization, 
        which may be changed or set to *None*. """

        self.poles = None
        """ Instance variable holding the list of fitted poles. Will be initialized by :func:`vector_fit`. """

        self.residues = None
        """ Instance variable holding the list of fitted residues. Will be initialized by :func:`vector_fit`. """

        self.proportional_coeff = None
        """ Instance variable holding the list of fitted proportional coefficients. Will be initialized by 
        :func:`vector_fit`. """

        self.constant_coeff = None
        """ Instance variable holding the list of fitted constants. Will be initialized by :func:`vector_fit`. """

        self.max_iterations = 100
        """ Instance variable specifying the maximum number of iterations for the fitting process and for the passivity 
        enforcement. To be changed by the user before calling :func:`vector_fit` and/or :func:`passivity_enforce`. """

        self.max_tol = 1e-6
        """ Instance variable specifying the convergence criterion in terms of relative tolerance. To be changed by the
         user before calling :func:`vector_fit`. """

        self.wall_clock_time = 0
        """ Instance variable holding the wall-clock time (in seconds) consumed by the most recent fitting process with 
        :func:`vector_fit`. Subsequent calls of :func:`vector_fit` will overwrite this value. """

        self.d_res_history = []
        self.delta_max_history = []
        self.history_max_sigma = []
        self.history_cond_A = []

    # legacy getter and setter methods to support deprecated 'zeros' attribute (now correctly called 'residues')
    @property
    def zeros(self):
        """
        **Deprecated**; Please use :attr:`residues` instead.
        """
        warnings.warn(
            "Attribute `zeros` is deprecated and will be removed in a future version. Please use the new "
            "attribute `residues` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.residues

    @zeros.setter
    def zeros(self, value):
        warnings.warn(
            "Attribute `zeros` is deprecated and will be removed in a future version. Please use the new "
            "attribute `residues` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.residues = value

    @property
    def all_poles(self):
        all_poles = []
        if self.poles is not None:
            for pole in self.poles:
                if np.imag(pole) == 0:
                    all_poles.append(pole)
                else:
                    all_poles.append(pole)
                    all_poles.append(np.conj(pole))
        return np.asarray(all_poles, dtype=complex)

    def vector_fit(
        self,
        n_poles_real: int = 2,
        n_poles_cmplx: int = 2,
        init_pole_spacing: str = "lin",
        parameter_type: str = "s",
        fit_constant: bool = True,
        fit_proportional: bool = False,
    ) -> None:
        """
        Main work routine performing the vector fit. The results will be stored in the class variables
        :attr:`poles`, :attr:`residues`, :attr:`proportional_coeff` and :attr:`constant_coeff`.

        Parameters
        ----------
        n_poles_real : int, optional
            Number of initial real poles. See notes.

        n_poles_cmplx : int, optional
            Number of initial complex conjugate poles. See notes.

        init_pole_spacing : str, optional
            Type of initial pole spacing across the frequency interval of the S-matrix. Either linear (lin) or
            logarithmic (log).

        parameter_type : str, optional
            Representation type of the frequency responses to be fitted. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`). As scikit-rf can currently
            only read S parameters from a Touchstone file, the fit should also be performed on the original S
            parameters. Otherwise, scikit-rf will convert the responses from S to Z or Y, which might work for the fit
            but can cause other issues.

        fit_constant : bool, optional
            Include a constant term **d** in the fit.

        fit_proportional : bool, optional
            Include a proportional term **e** in the fit.

        Returns
        -------
        None
            No return value.

        Notes
        -----
        The required number of real or complex conjugate starting poles depends on the behaviour of the frequency
        responses. To fit a smooth response such as a low-pass characteristic, 1-3 real poles and no complex conjugate
        poles is usually sufficient. If resonances or other types of peaks are present in some or all of the responses,
        a similar number of complex conjugate poles is required. Be careful not to use too many poles, as excessive
        poles will not only increase the computation workload during the fitting and the subsequent use of the model,
        but they can also introduce unwanted resonances at frequencies well outside the fit interval.
        """

        timer_start = timer()

        # create initial poles and space them across the frequencies in the provided Touchstone file
        # use normalized frequencies during the iterations (seems to be more stable during least-squares fit)
        norm = np.average(self.network.f)
        freqs_norm = np.array(self.network.f) / norm

        fmin = np.amin(freqs_norm)
        fmax = np.amax(freqs_norm)
        if init_pole_spacing == "log" and fmin == 0:
            pole_freqs_real = np.geomspace(fmin + 0.1, fmax, n_poles_real)
            pole_freqs_cmplx = np.geomspace(fmin + 0.1, fmax, n_poles_cmplx)
        elif init_pole_spacing == "log":
            pole_freqs_real = np.geomspace(fmin, fmax, n_poles_real)
            pole_freqs_cmplx = np.geomspace(fmin, fmax, n_poles_cmplx)
        elif init_pole_spacing == "lin" and fmin == 0:
            pole_freqs_real = np.linspace(fmin + 0.1, fmax, n_poles_real)
            pole_freqs_cmplx = np.linspace(fmin + 0.1, fmax, n_poles_cmplx)
        elif init_pole_spacing == "lin":
            pole_freqs_real = np.linspace(fmin, fmax, n_poles_real)
            pole_freqs_cmplx = np.linspace(fmin, fmax, n_poles_cmplx)
        else:
            warnings.warn(
                "Invalid choice of initial pole spacing; proceeding with linear spacing.",
                UserWarning,
                stacklevel=2,
            )
            pole_freqs_real = np.linspace(fmin, fmax, n_poles_real)
            pole_freqs_cmplx = np.linspace(fmin, fmax, n_poles_cmplx)

        # init poles array of correct length
        poles = np.zeros(n_poles_real + n_poles_cmplx, dtype=complex)

        # add real poles
        for i, f in enumerate(pole_freqs_real):
            omega = 2 * np.pi * f
            poles[i] = -1 * omega

        # add complex-conjugate poles (store only positive imaginary parts)
        i_offset = len(pole_freqs_real)
        for i, f in enumerate(pole_freqs_cmplx):
            omega = 2 * np.pi * f
            poles[i_offset + i] = (-0.01 + 1j) * omega

        # save initial poles (un-normalize first)
        initial_poles = poles * norm
        max_singular = 1

        # logging.info("### Starting pole relocation process.\n")

        n_responses = self.network.nports**2
        n_freqs = len(freqs_norm)
        n_samples = n_responses * n_freqs

        # select network representation type
        if parameter_type.lower() == "s":
            nw_responses = self.network.s
        elif parameter_type.lower() == "z":
            nw_responses = self.network.z
        elif parameter_type.lower() == "y":
            nw_responses = self.network.y
        else:
            warnings.warn(
                "Invalid choice of matrix parameter type (S, Z, or Y); proceeding with scattering " "representation.",
                UserWarning,
                stacklevel=2,
            )
            nw_responses = self.network.s

        # stack frequency responses as a single vector
        # stacking order (row-major):
        # s11, s12, s13, ..., s21, s22, s23, ...
        freq_responses = []
        for i in range(self.network.nports):
            for j in range(self.network.nports):
                freq_responses.append(nw_responses[:, i, j])
        freq_responses = np.array(freq_responses)

        # responses will be weighted according to their norm;
        # alternative: equal weights with weight_response = 1.0
        # or anti-proportional weights with
        weights_responses = 1 / np.linalg.norm(freq_responses, axis=1)
        # weights_responses = np.linalg.norm(freq_responses, axis=1)
        # weights_responses = np.ones(self.network.nports ** 2)

        # weight of extra equation to avoid trivial solution
        weight_extra = np.linalg.norm(weights_responses[:, None] * freq_responses) / n_samples

        # weights w are applied directly to the samples, which get squared during least-squares fitting; hence sqrt(w)
        weights_responses = np.sqrt(weights_responses)
        weight_extra = np.sqrt(weight_extra)

        # ITERATIVE FITTING OF POLES to the provided frequency responses
        # initial set of poles will be replaced with new poles after every iteration
        iterations = self.max_iterations
        self.d_res_history = []
        self.delta_max_history = []
        self.history_cond_A = []
        converged = False

        omega = 2 * np.pi * freqs_norm
        s = 1j * omega

        while iterations > 0:
            # logging.info("Iteration {}".format(self.max_iterations - iterations + 1))

            # count number of rows and columns in final coefficient matrix to solve for (c_res, d_res)
            # (ratio #real/#complex poles might change during iterations)

            # We need two columns for complex poles and one column for real poles in A matrix.
            # poles.imag != 0 is True(1) for complex poles, False (0) for real poles.
            # Adding one to each element gives 2 columns for complex and 1 column for real poles.
            n_cols_unused = np.sum((poles.imag != 0) + 1)

            n_cols_used = n_cols_unused
            n_cols_used += 1
            idx_constant = []
            idx_proportional = []
            if fit_constant:
                idx_constant = [n_cols_unused]
                n_cols_unused += 1
            if fit_proportional:
                idx_proportional = [n_cols_unused]
                n_cols_unused += 1

            real_mask = poles.imag == 0
            # list of indices in 'poles' with real values
            idx_poles_real = np.nonzero(real_mask)[0]
            # list of indices in 'poles' with complex values
            idx_poles_complex = np.nonzero(~real_mask)[0]

            # positions (columns) of coefficients for real and complex-conjugate terms in the rows of A determine the
            # respective positions of the calculated residues in the results vector.
            # to have them ordered properly for the subsequent assembly of the test matrix H for eigenvalue extraction,
            # place real poles first, then complex-conjugate poles with their respective real and imaginary parts:
            # [r1', r2', ..., (r3', r3''), (r4', r4''), ...]
            n_real = len(idx_poles_real)
            n_cmplx = len(idx_poles_complex)
            idx_res_real = np.arange(n_real)
            idx_res_complex_re = n_real + 2 * np.arange(n_cmplx)
            idx_res_complex_im = idx_res_complex_re + 1

            # complex coefficient matrix of shape [N_responses, N_freqs, n_cols_unused + n_cols_used]
            # layout of each row:
            # [pole1, pole2, ..., (constant), (proportional), pole1, pole2, ..., constant]
            A = np.empty((n_responses, n_freqs, n_cols_unused + n_cols_used), dtype=complex)

            # calculate coefficients for real and complex residues in the solution vector
            #
            # real pole-residue term (r = r', p = p'):
            # fractional term is r' / (s - p')
            # coefficient for r' is 1 / (s - p')
            coeff_real = 1 / (s[:, None] - poles[None, idx_poles_real])

            # complex-conjugate pole-residue pair (r = r' + j r'', p = p' + j p''):
            # fractional term is r / (s - p) + conj(r) / (s - conj(p))
            #                   = [1 / (s - p) + 1 / (s - conj(p))] * r' + [1j / (s - p) - 1j / (s - conj(p))] * r''
            # coefficient for r' is 1 / (s - p) + 1 / (s - conj(p))
            # coefficient for r'' is 1j / (s - p) - 1j / (s - conj(p))
            coeff_complex_re = 1 / (s[:, None] - poles[None, idx_poles_complex]) + 1 / (
                s[:, None] - np.conj(poles[None, idx_poles_complex])
            )
            coeff_complex_im = 1j / (s[:, None] - poles[None, idx_poles_complex]) - 1j / (
                s[:, None] - np.conj(poles[None, idx_poles_complex])
            )

            # part 1: first sum of rational functions (variable c)
            A[:, :, idx_res_real] = coeff_real
            A[:, :, idx_res_complex_re] = coeff_complex_re
            A[:, :, idx_res_complex_im] = coeff_complex_im

            # part 2: constant (variable d) and proportional term (variable e)
            A[:, :, idx_constant] = 1
            A[:, :, idx_proportional] = s[:, None]

            # part 3: second sum of rational functions multiplied with frequency response (variable c_res)
            A[:, :, n_cols_unused + idx_res_real] = -1 * freq_responses[:, :, None] * coeff_real
            A[:, :, n_cols_unused + idx_res_complex_re] = -1 * freq_responses[:, :, None] * coeff_complex_re
            A[:, :, n_cols_unused + idx_res_complex_im] = -1 * freq_responses[:, :, None] * coeff_complex_im

            # part 4: constant (variable d_res)
            A[:, :, -1] = -1 * freq_responses

            # QR decomposition
            # R = np.linalg.qr(np.hstack((A.real, A.imag)), 'r')

            # direct QR of stacked matrices for linalg.qr() only works with numpy>=1.22.0
            # workaround for old numpy:
            R = np.empty((n_responses, n_cols_unused + n_cols_used, n_cols_unused + n_cols_used))
            A_ri = np.hstack((A.real, A.imag))
            for i in range(n_responses):
                R[i] = np.linalg.qr(A_ri[i], mode="r")

            # only R22 is required to solve for c_res and d_res
            R22 = R[:, n_cols_unused:, n_cols_unused:]

            # weighting
            R22 = weights_responses[:, None, None] * R22

            # assemble compressed coefficient matrix A_fast by row-stacking individual upper triangular matrices R22
            A_fast = np.empty((n_responses * n_cols_used + 1, n_cols_used))
            A_fast[:-1, :] = R22.reshape((n_responses * n_cols_used, n_cols_used))

            # extra equation to avoid trivial solution
            A_fast[-1, idx_res_real] = np.sum(coeff_real.real, axis=0)
            A_fast[-1, idx_res_complex_re] = np.sum(coeff_complex_re.real, axis=0)
            A_fast[-1, idx_res_complex_im] = np.sum(coeff_complex_im.real, axis=0)
            A_fast[-1, -1] = n_freqs

            # weighting
            A_fast[-1, :] = weight_extra * A_fast[-1, :]

            # right hand side vector (weighted)
            b = np.zeros(n_responses * n_cols_used + 1)
            b[-1] = weight_extra * n_samples

            cond_A = np.linalg.cond(A_fast)
            # logging.info("Condition number of coeff. matrix A = {}".format(int(cond_A)))
            self.history_cond_A.append(cond_A)

            # solve least squares for real parts
            x, residuals, rank, singular_vals = np.linalg.lstsq(A_fast, b, rcond=None)

            # assemble individual result vectors from single LS result x
            c_res = x[:-1]
            d_res = x[-1]

            # check if d_res is suited for zeros calculation
            tol_res = 1e-8
            if np.abs(d_res) < tol_res:
                # d_res is too small, discard solution and proceed the |d_res| = tol_res
                d_res = tol_res * (d_res / np.abs(d_res))
                warnings.warn(
                    "Replacing d_res solution as it was too small. This is not a good sign and probably "
                    "means that more starting poles are required",
                    RuntimeWarning,
                    stacklevel=2,
                )

            self.d_res_history.append(d_res)
            # logging.info("d_res = {}".format(d_res))

            # build test matrix H, which will hold the new poles as eigenvalues
            H = np.zeros((len(c_res), len(c_res)))

            poles_real = poles[np.nonzero(real_mask)]
            poles_cplx = poles[np.nonzero(~real_mask)]

            H[idx_res_real, idx_res_real] = poles_real.real
            H[idx_res_real] -= c_res / d_res

            H[idx_res_complex_re, idx_res_complex_re] = poles_cplx.real
            H[idx_res_complex_re, idx_res_complex_im] = poles_cplx.imag
            H[idx_res_complex_im, idx_res_complex_re] = -1 * poles_cplx.imag
            H[idx_res_complex_im, idx_res_complex_im] = poles_cplx.real
            H[idx_res_complex_re] -= 2 * c_res / d_res

            poles_new = np.linalg.eigvals(H)

            # replace poles for next iteration
            # complex poles need to come in complex conjugate pairs; append only the positive part
            poles = poles_new[np.nonzero(poles_new.imag >= 0)]

            # flip real part of unstable poles (real part needs to be negative for stability)
            poles.real = -1 * np.abs(poles.real)

            # calculate relative changes in the singular values; stop iteration loop once poles have converged
            new_max_singular = np.amax(singular_vals)
            delta_max = np.abs(1 - new_max_singular / max_singular)
            self.delta_max_history.append(delta_max)
            # logging.info("Max. relative change in residues = {}\n".format(delta_max))
            max_singular = new_max_singular

            stop = False
            if delta_max < self.max_tol:
                if converged:
                    # is really converged, finish
                    logging.info(
                        "Pole relocation process converged after {} iterations.".format(
                            self.max_iterations - iterations + 1
                        )
                    )
                    stop = True
                else:
                    # might be converged, but do one last run to be sure
                    converged = True
            else:
                if converged:
                    # is not really converged, continue
                    converged = False

            iterations -= 1

            if iterations == 0:
                max_cond = np.amax(self.history_cond_A)
                if max_cond > 1e10:
                    msg_illcond = (
                        "Hint: the linear system was ill-conditioned (max. condition number = {}). "
                        "This often means that more poles are required.".format(max_cond)
                    )
                else:
                    msg_illcond = ""
                if converged and stop is False:
                    warnings.warn(
                        "Vector Fitting: The pole relocation process barely converged to tolerance. "
                        "It took the max. number of iterations (N_max = {}). "
                        "The results might not have converged properly. ".format(self.max_iterations) + msg_illcond,
                        RuntimeWarning,
                        stacklevel=2,
                    )
                else:
                    warnings.warn(
                        "Vector Fitting: The pole relocation process stopped after reaching the "
                        "maximum number of iterations (N_max = {}). "
                        "The results did not converge properly. ".format(self.max_iterations) + msg_illcond,
                        RuntimeWarning,
                        stacklevel=2,
                    )

            if stop:
                iterations = 0

        # ITERATIONS DONE
        # logging.info("Initial poles before relocation:")
        # logging.info(initial_poles)

        # logging.info("Final poles:")
        # logging.info(poles * norm)

        # logging.info("\n### Starting residues calculation process.\n")

        # finally, solve for the residues with the previously calculated poles

        # We need two columns for complex poles and one column for real poles in A matrix.
        # poles.imag != 0 is True(1) for complex poles, False (0) for real poles.
        # Adding one to each element gives 2 columns for complex and 1 column for real poles.
        n_cols = np.sum((poles.imag != 0) + 1)

        idx_constant = []
        idx_proportional = []
        if fit_constant:
            idx_constant = [n_cols]
            n_cols += 1
        if fit_proportional:
            idx_proportional = [n_cols]
            n_cols += 1

        # list of indices in 'poles' with real and with complex values
        real_mask = poles.imag == 0
        idx_poles_real = np.nonzero(real_mask)[0]
        idx_poles_complex = np.nonzero(~real_mask)[0]

        # find and save indices of real and complex poles in the poles list
        i = 0
        idx_res_real = []
        idx_res_complex_re = []
        idx_res_complex_im = []
        for pole in poles:
            if pole.imag == 0:
                idx_res_real.append(i)
                i += 1
            else:
                idx_res_complex_re.append(i)
                idx_res_complex_im.append(i + 1)
                i += 2

        # complex coefficient matrix of shape [N_freqs, n_cols]
        # layout of each row:
        # [pole1, pole2, ..., (constant), (proportional)]
        A = np.empty((n_freqs, n_cols), dtype=complex)

        # calculate coefficients for real and complex residues in the solution vector
        #
        # real pole-residue term (r = r', p = p'):
        # fractional term is r' / (s - p')
        # coefficient for r' is 1 / (s - p')
        coeff_real = 1 / (s[:, None] - poles[None, idx_poles_real])

        # complex-conjugate pole-residue pair (r = r' + j r'', p = p' + j p''):
        # fractional term is r / (s - p) + conj(r) / (s - conj(p))
        #                   = [1 / (s - p) + 1 / (s - conj(p))] * r' + [1j / (s - p) - 1j / (s - conj(p))] * r''
        # coefficient for r' is 1 / (s - p) + 1 / (s - conj(p))
        # coefficient for r'' is 1j / (s - p) - 1j / (s - conj(p))
        coeff_complex_re = 1 / (s[:, None] - poles[None, idx_poles_complex]) + 1 / (
            s[:, None] - np.conj(poles[None, idx_poles_complex])
        )
        coeff_complex_im = 1j / (s[:, None] - poles[None, idx_poles_complex]) - 1j / (
            s[:, None] - np.conj(poles[None, idx_poles_complex])
        )

        # part 1: first sum of rational functions (variable c)
        A[:, idx_res_real] = coeff_real
        A[:, idx_res_complex_re] = coeff_complex_re
        A[:, idx_res_complex_im] = coeff_complex_im

        # part 2: constant (variable d) and proportional term (variable e)
        A[:, idx_constant] = 1
        A[:, idx_proportional] = s[:, None]

        # logging.info(
        #    "Condition number of coefficient matrix = {}".format(int(np.linalg.cond(A)))
        # )

        # solve least squares and obtain results as stack of real part vector and imaginary part vector
        x, residuals, rank, singular_vals = np.linalg.lstsq(
            np.vstack((A.real, A.imag)),
            np.hstack((freq_responses.real, freq_responses.imag)).transpose(),
            rcond=None,
        )

        # align poles and residues arrays to get matching pole-residue pairs
        poles = np.concatenate((poles[idx_poles_real], poles[idx_poles_complex]))
        residues = np.concatenate(
            (x[idx_res_real], x[idx_res_complex_re] + 1j * x[idx_res_complex_im]),
            axis=0,
        ).transpose()

        if fit_constant:
            constant_coeff = x[idx_constant][0]
        else:
            constant_coeff = np.zeros(n_responses)

        if fit_proportional:
            proportional_coeff = x[idx_proportional][0]
        else:
            proportional_coeff = np.zeros(n_responses)

        # save poles, residues, d, e in actual frequencies (un-normalized)
        self.poles = poles * norm
        self.residues = np.array(residues) * norm
        self.constant_coeff = np.array(constant_coeff)
        self.proportional_coeff = np.array(proportional_coeff) / norm

        timer_stop = timer()
        self.wall_clock_time = timer_stop - timer_start

        logging.info("\n### Vector fitting finished in {} seconds.\n".format(self.wall_clock_time))

        # raise a warning if the fitted Network is passive but the fit is not (only without proportional_coeff):
        if self.network.is_passive() and not fit_proportional:
            if not self.is_passive():
                warnings.warn(
                    "The fitted network is passive, but the vector fit is not passive. Consider running "
                    "`passivity_enforce()` to enforce passivity before using this model.",
                    UserWarning,
                    stacklevel=2,
                )

    def get_rms_error(self, i=-1, j=-1, parameter_type: str = "s"):
        r"""
        Returns the root-mean-square (rms) error magnitude of the fit, i.e.
        :math:`\sqrt{ \mathrm{mean}(|S - S_\mathrm{fit} |^2) }`,
        either for an individual response :math:`S_{i+1,j+1}` or for larger slices of the network.

        Parameters
        ----------
        i : int, optional
            Row indices of the responses to be evaluated. Either a single row selected by an integer
            :math:`i \in [0, N_\mathrm{ports}-1]`, or multiple rows selected by a list of integers, or all rows
            selected by :math:`i = -1` (*default*).

        j : int, optional
            Column indices of the responses to be evaluated. Either a single column selected by an integer
            :math:`j \in [0, N_\mathrm{ports}-1]`, or multiple columns selected by a list of integers, or all columns
            selected by :math:`j = -1` (*default*).

        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`).

        Returns
        -------
        rms_error : ndarray
            The rms error magnitude between the vector fitted model and the original network data.

        Raises
        ------
        ValueError
            If the specified parameter representation type is not :attr:`s`, :attr:`z`, nor :attr:`y`.
        """

        if i == -1:
            list_i = range(self.network.nports)
        elif isinstance(i, int):
            list_i = [i]
        else:
            list_i = i

        if j == -1:
            list_j = range(self.network.nports)
        elif isinstance(j, int):
            list_j = [j]
        else:
            list_j = j

        if parameter_type.lower() == "s":
            nw_responses = self.network.s
        elif parameter_type.lower() == "z":
            nw_responses = self.network.z
        elif parameter_type.lower() == "y":
            nw_responses = self.network.y
        else:
            raise ValueError("Invalid parameter type `{}`. Valid options: `s`, `z`, or `y`".format(parameter_type))

        error_mean_squared = 0
        for i in list_i:
            for j in list_j:
                nw_ij = nw_responses[:, i, j]
                fit_ij = self.get_model_response(i, j, self.network.f)
                error_mean_squared += np.mean(np.square(np.abs(nw_ij - fit_ij)))

        return np.sqrt(error_mean_squared)

    def _get_ABCDE(
        self,
        for_passivity_enforcing=False,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Private method.
        Returns the real-valued system matrices of the state-space representation of the current rational model, as
        defined in [#]_.

        Returns
        -------
        A : ndarray
            State-space matrix A holding the poles on the diagonal as real values with imaginary parts on the sub-
            diagonal
        B : ndarray
            State-space matrix B holding coefficients (1, 2, or 0), depending on the respective type of pole in A
        C : ndarray
            State-space matrix C holding the residues
        D : ndarray
            State-space matrix D holding the constants
        E : ndarray
            State-space matrix E holding the proportional coefficients (usually 0 in case of fitted S-parameters)

        Raises
        ------
        ValueError
            If the model parameters have not been initialized (by running :func:`vector_fit()` or :func:`read_npz()`).

        References
        ----------
        .. [#] B. Gustavsen and A. Semlyen, "Fast Passivity Assessment for S-Parameter Rational Models Via a Half-Size
            Test Matrix," in IEEE Transactions on Microwave Theory and Techniques, vol. 56, no. 12, pp. 2701-2708,
            Dec. 2008, DOI: 10.1109/TMTT.2008.2007319.
        """

        # initial checks
        if self.poles is None:
            raise ValueError("self.poles = None; nothing to do. You need to run vector_fit() first.")
        if self.residues is None:
            raise ValueError("self.residues = None; nothing to do. You need to run vector_fit() first.")
        if self.proportional_coeff is None:
            raise ValueError("self.proportional_coeff = None; nothing to do. You need to run vector_fit() first.")
        if self.constant_coeff is None:
            raise ValueError("self.constant_coeff = None; nothing to do. You need to run vector_fit() first.")

        # assemble real-valued state-space matrices A, B, C, D, E from fitted complex-valued pole-residue model

        # determine size of the matrix system
        n_ports = int(np.sqrt(len(self.constant_coeff)))
        n_poles_real = 0
        n_poles_cplx = 0
        for pole in self.poles:
            if np.imag(pole) == 0.0:
                n_poles_real += 1
            else:
                n_poles_cplx += 1
        n_matrix = (n_poles_real + 2 * n_poles_cplx) * n_ports

        # state-space matrix A holds the poles on the diagonal as real values with imaginary parts on the sub-diagonal
        # state-space matrix B holds coefficients (1, 2, or 0), depending on the respective type of pole in A
        # assemble A = [[poles_real,   0,                  0],
        #               [0,            real(poles_cplx),   imag(poles_cplx],
        #               [0,            -imag(poles_cplx),  real(poles_cplx]]
        if for_passivity_enforcing:
            B = np.ones(shape=(n_matrix, n_ports))
            A = np.identity(n_matrix, dtype=complex)
            C = np.zeros(shape=(n_ports, n_matrix), dtype=complex)
            D = np.zeros(shape=(n_ports, n_ports))
            E = np.zeros(shape=(n_ports, n_ports))
            i_A = 0
            i_C = 0
            for _i, pole in enumerate(self.poles):
                if np.imag(pole) == 0.0:
                    A[i_A, i_A] = pole
                    C[0, i_C] = self.residues[0, _i]
                    i_A += 1
                    i_C += 1
                else:
                    A[i_A, i_A] = np.real(pole) + 1j * np.imag(pole)
                    A[i_A + 1, i_A + 1] = np.real(pole) - 1j * np.imag(pole)
                    C[0, i_C] = np.real(self.residues[0, _i]) + 1j * np.imag(self.residues[0, _i])
                    C[0, i_C + 1] = np.real(self.residues[0, _i]) - 1j * np.imag(self.residues[0, _i])
                    i_A += 2
                    i_C += 2
            D[0, 0] = self.constant_coeff[0]
            return A, B, C, D, E
        A = np.identity(n_matrix)
        B = np.zeros(shape=(n_matrix, n_ports))
        i_A = 0  # index on diagonal of A
        for j in range(n_ports):
            for pole in self.poles:
                if np.imag(pole) == 0.0:
                    # adding a real pole
                    A[i_A, i_A] = np.real(pole)
                    B[i_A, j] = 1
                    i_A += 1
                else:
                    # adding a complex-conjugate pole
                    A[i_A, i_A] = np.real(pole)
                    A[i_A, i_A + 1] = np.imag(pole)
                    A[i_A + 1, i_A] = -1 * np.imag(pole)
                    A[i_A + 1, i_A + 1] = np.real(pole)
                    B[i_A, j] = 2
                    i_A += 2

        # state-space matrix C holds the residues
        # assemble C = [[R1.11, R1.12, R1.13, ...], [R2.11, R2.12, R2.13, ...], ...]
        C = np.zeros(shape=(n_ports, n_matrix))
        for i in range(n_ports):
            for j in range(n_ports):
                # i: row index
                # j: column index
                i_response = i * n_ports + j

                j_residues = 0
                for zero in self.residues[i_response]:
                    if np.imag(zero) == 0.0:
                        C[i, j * (n_poles_real + 2 * n_poles_cplx) + j_residues] = np.real(zero)
                        j_residues += 1
                    else:
                        C[i, j * (n_poles_real + 2 * n_poles_cplx) + j_residues] = np.real(zero)
                        C[i, j * (n_poles_real + 2 * n_poles_cplx) + j_residues + 1] = np.imag(zero)
                        j_residues += 2

        # state-space matrix D holds the constants
        # assemble D = [[d11, d12, ...], [d21, d22, ...], ...]
        D = np.zeros(shape=(n_ports, n_ports))
        for i in range(n_ports):
            for j in range(n_ports):
                # i: row index
                # j: column index
                i_response = i * n_ports + j
                D[i, j] = self.constant_coeff[i_response]

        # state-space matrix E holds the proportional coefficients (usually 0 in case of fitted S-parameters)
        # assemble E = [[e11, e12, ...], [e21, e22, ...], ...]
        E = np.zeros(shape=(n_ports, n_ports))
        for i in range(n_ports):
            for j in range(n_ports):
                # i: row index
                # j: column index
                i_response = i * n_ports + j
                E[i, j] = self.proportional_coeff[i_response]

        return A, B, C, D, E

    @staticmethod
    def _get_s_from_ABCDE(
        freqs: np.ndarray,
        A: np.ndarray,
        B: np.ndarray,
        C: np.ndarray,
        D: np.ndarray,
        E: np.ndarray,
    ) -> np.ndarray:
        """
        Private method.
        Returns the S-matrix of the vector fitted model calculated from the real-valued system matrices of the state-
        space representation, as provided by `_get_ABCDE()`.
        Also works for the Y-matrix so no worries

        Parameters
        ----------
        freqs : ndarray
            Frequencies (in Hz) at which to calculate the S-matrices.
        A : ndarray
        B : ndarray
        C : ndarray
        D : ndarray
        E : ndarray

        Returns
        -------
        ndarray
            Complex-valued S-matrices (fxNxN) calculated at frequencies `freqs`.
        """

        dim_A = np.shape(A)[0]
        stsp_poles = np.linalg.inv(2j * np.pi * freqs[:, None, None] * np.identity(dim_A)[None, :, :] - A[None, :, :])
        stsp_S = np.matmul(np.matmul(C, stsp_poles), B)
        stsp_S += D + 2j * np.pi * freqs[:, None, None] * E
        return stsp_S

    def passivity_test(self, parameter_type: str = "s") -> np.ndarray:
        """
        Evaluates the passivity of reciprocal vector fitted models by means of a half-size test matrix [#]_. Any
        existing frequency bands of passivity violations will be returned as a sorted list.

        Parameters
        ----------
        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`). Currently, only scattering
            parameters are supported for passivity evaluation.

        Raises
        ------
        NotImplementedError
            If the function is called for `parameter_type` different than `S` (scattering).

        ValueError
            If the function is used with a model containing nonzero proportional coefficients.

        Returns
        -------
        violation_bands : ndarray
            NumPy array with frequency bands of passivity violation:
            `[[f_start_1, f_stop_1], [f_start_2, f_stop_2], ...]`.

        See Also
        --------
        is_passive : Query the model passivity as a boolean value.
        passivity_enforce : Enforces the passivity of the vector fitted model, if required.

        Examples
        --------
        Load and fit the `Network`, then evaluate the model passivity:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> violations = vf.passivity_test()

        References
        ----------
        .. [#] B. Gustavsen and A. Semlyen, "Fast Passivity Assessment for S-Parameter Rational Models Via a Half-Size
            Test Matrix," in IEEE Transactions on Microwave Theory and Techniques, vol. 56, no. 12, pp. 2701-2708,
            Dec. 2008, DOI: 10.1109/TMTT.2008.2007319.
        """
        if parameter_type.lower() == "y":
            violation_bands = self._passivity_test_y()
            return violation_bands
        if parameter_type.lower() == "r":  # parameter is the reflection coefficient
            violation_bands = self._passivity_test_r()
            return violation_bands

        if parameter_type.lower() != "s":
            raise NotImplementedError("Passivity testing is currently only supported for scattering (S) parameters.")
        if parameter_type.lower() == "s" and len(np.flatnonzero(self.proportional_coeff)) > 0:
            raise ValueError(
                "Passivity testing of scattering parameters with nonzero proportional coefficients does "
                "not make any sense; you need to run vector_fit() with option `fit_proportional=False` "
                "first."
            )

        # # the network needs to be reciprocal for this passivity test method to work: S = transpose(S)
        # if not np.allclose(self.residues, np.transpose(self.residues)) or \
        #         not np.allclose(self.constant_coeff, np.transpose(self.constant_coeff)) or \
        #         not np.allclose(self.proportional_coeff, np.transpose(self.proportional_coeff)):
        #     logging.error('Passivity testing with unsymmetrical model parameters is not supported. '
        #                   'The model needs to be reciprocal.')
        #     return

        # get state-space matrices
        A, B, C, D, E = self._get_ABCDE()
        n_ports = np.shape(D)[0]

        # build half-size test matrix P from state-space matrices A, B, C, D
        inv_neg = np.linalg.inv(D - np.identity(n_ports))
        inv_pos = np.linalg.inv(D + np.identity(n_ports))
        prod_neg = np.matmul(np.matmul(B, inv_neg), C)
        prod_pos = np.matmul(np.matmul(B, inv_pos), C)
        P = np.matmul(A - prod_neg, A - prod_pos)

        # extract eigenvalues of P
        P_eigs = np.linalg.eigvals(P)

        # purely imaginary square roots of eigenvalues identify frequencies (2*pi*f) of borders of passivity violations
        freqs_violation = []
        for sqrt_eigenval in np.sqrt(P_eigs):
            if np.real(sqrt_eigenval) == 0.0:
                freqs_violation.append(np.imag(sqrt_eigenval) / 2 / np.pi)

        # include dc (0) unless it's already included
        if len(np.nonzero(np.array(freqs_violation) == 0.0)[0]) == 0:
            freqs_violation.append(0.0)

        # sort the output from lower to higher frequencies
        freqs_violation = np.sort(freqs_violation)

        # identify frequency bands of passivity violations

        # sweep the bands between crossover frequencies and identify bands of passivity violations
        violation_bands = []
        for i, freq in enumerate(freqs_violation):
            if i == len(freqs_violation) - 1:
                # last band stops always at infinity
                f_start = freq
                f_stop = np.inf
                f_center = 1.1 * f_start  # 1.1 is chosen arbitrarily to have any frequency for evaluation
            else:
                # intermediate band between this frequency and the previous one
                f_start = freq
                f_stop = freqs_violation[i + 1]
                f_center = 0.5 * (f_start + f_stop)

            # calculate singular values at the center frequency between crossover frequencies to identify violations
            s_center = self._get_s_from_ABCDE(np.array([f_center]), A, B, C, D, E)
            sigma = np.linalg.svd(s_center[0], compute_uv=False)
            passive = True
            for singval in sigma:
                if singval > 1:
                    # passivity violation in this band
                    passive = False
            if not passive:
                # add this band to the list of passivity violations
                if violation_bands is None:
                    violation_bands = [[f_start, f_stop]]
                else:
                    violation_bands.append([f_start, f_stop])

        return np.array(violation_bands)

    def _passivity_test_y(self) -> np.ndarray:
        """
        Checks to see if the pole-residue model is passive (Does not add energy into the system).
                            N       Rm
                    Y(s) = SUM  (---------) + D
                            m=1    (s-am)
        The system Y(s) is passive if these conditions apply:

        - eig [RE(Y)] > 0 for all frequencies
        - D needs to be positive definite

        :return: An array with the frequency bands which violate the conditions
        :rtype: np.ndarray
        """

        wintervals = []
        # Get State-Space model
        A, B, C, D, _ = self._get_ABCDE(for_passivity_enforcing=False)
        Acmplx, Bcmplx, Ccmplx, Dcmplx = (
            A,
            B,
            C,
            D,
        )  # Need to understand a bit better what's going on here.
        # Not sure about the real vs positive aspects of everything there
        # The Matlab code converts to real only at some point. I need to look into that
        if np.sum(np.linalg.eigvals(D) == 0) > 0:
            Ahat = np.linalg.inv(A)
            Bhat = -Ahat @ B
            Chat = C @ Ahat
            Dhat = D - C @ Ahat @ B
            A, B, C, D = Ahat, Bhat, Chat, Dhat
        # D_inv = np.linalg.inv(D)
        S1 = A @ (B @ np.linalg.inv(D) @ C - A)
        # bdc_comp = np.matmul(A, np.matmul(B, np.matmul(np.linalg.inv(D), C) - A))
        # S1 = A @ bdc_a

        wS1 = np.emath.sqrt(np.linalg.eigvals(S1))
        if np.any(np.linalg.eig(Dcmplx) == 0):
            wS1 = 1 / wS1
        ind = np.where(np.imag(wS1) == 0, True, False)
        wS1 = wS1[ind].real
        sing_w = np.sort(wS1)
        if len(sing_w) == 0:
            return np.array(wintervals)

        A, B, C, D, _ = self._get_ABCDE(for_passivity_enforcing=True)
        # Now we create a list of frequencies at midpoint of all the bands
        mid_w = np.zeros(len(sing_w) + 1)
        viol = np.zeros_like(mid_w)
        mid_w[0] = sing_w[0] / 2.0
        mid_w[-1] = 2 * sing_w[-1]
        for k in range(len(sing_w) - 1):
            mid_w[k + 1] = (sing_w[k] + sing_w[k + 1]) / 2.0

        # Checking passivity at all midpoints
        # C_comp = np.zeros_like(C, dtype=complex)
        # B_comp = np.ones_like(B)
        # i = 0
        # z = 0
        # for pole in self.poles:
        #     if np.imag(pole) == 0.0:
        #         C_comp[i, z] = C[i, z]
        #         z += 1
        #     else:
        #         C_comp[i, z] = C[i, z] + 1j * C[i, z + 1]
        #         C_comp[i, z + 1] = C[i, z] - 1j * C[i, z + 1]
        #         z += 2
        for k in range(len(mid_w)):
            sk = 1j * mid_w[k]
            # C = C * (1.0 / (sk - self.poles))
            G = np.real((C * (1.0 / (sk - self.all_poles))) @ B + D)  # E is always zero in our situation
            EE = np.linalg.eigvals(G)
            if np.any(EE < 0):
                viol[k] = 1
            else:
                viol[k] = 0
        # Establishing intervals for passivity violations:

        intervals = np.zeros((len(np.nonzero(viol)[0]), 2))
        count = 0
        for k in range(len(mid_w)):
            if viol[k] == 1:
                if k == 0:
                    intervals[count, :] = np.array([0, sing_w[0]]).T
                elif k == len(mid_w) - 1:
                    intervals[count, :] = np.array([sing_w[k - 1], 1e16])
                    # intervals[count, :] = np.concatenate(
                    #     intervals, np.array([sing_w[k - 1], 1e16]).T
                    # )
                else:
                    intervals[count, :] = np.array([sing_w[k - 1], sing_w[k]]).T
                count += 1

        if len(intervals) == 0:
            return np.array(wintervals)
        killindex = []
        for k in range(1, len(intervals)):
            if intervals[k - 1, 1] == intervals[k, 1]:  # An overlap exists

                intervals[k - 1, 1] = intervals[k, 1]  # Extending interval
                intervals[k, :] = intervals[k - 1, :]
                killindex.append(k - 1)

        if len(killindex) != 0:
            intervals = np.delete(intervals, killindex, axis=0)
        wintervals = intervals
        return wintervals

    def _passivity_test_r(self) -> np.ndarray:
        """
        Look for frequency bands which violate the passivity condition.

        The conditions for R is that abs(R) < 1.0 for all frequencies
        and that abs(D) < 1

        :return: an array of the violating frequency bands
        :rtype: np.ndarray
        """
        # Ok, few different ways to do this.
        # We can just sample through the frequency space and get a 01 array

        wintervals = []
        freq = self.network.f
        freqtest = np.linspace(0.1, np.max(freq) * 1.6, 99999)

        freqtest = np.insert(freqtest, 0, 0.0)
        A, B, C, D, E = self._get_ABCDE()

        S = np.abs(self._get_s_from_ABCDE(freqs=freqtest, A=A, B=B, C=C, D=D, E=E))
        non_passive = np.where(S >= 1.0)[0]
        if len(non_passive) == 0:
            return np.array(wintervals)

        # Create the correct intervals where R is not passive
        # So I need to go through 'non_passive' and see when it contains
        # jumps in indices. That is where we jump from non-passive to passive
        # and that is where we define the end points of the intervals.
        interval_indices = self._find_limits_of_violation(violations=non_passive)
        freq_intervals = np.zeros((interval_indices.shape[0], 2))

        for _i, interval in enumerate(interval_indices):
            min_freq = 2 * np.pi * freqtest[interval[0]]
            max_freq = 2 * np.pi * freqtest[interval[1]]
            if min_freq == max_freq:
                # Made something temporary to have something to handle this case for now at least.
                # Same thing as in the passivity_test_y function
                if max_freq == 2 * np.pi * np.max(freqtest):
                    max_freq = 1e16

                elif max_freq == 2 * np.pi * np.min(freqtest):
                    max_freq = freqtest[interval[1] + 1]
                else:
                    min_freq *= 7 / 8
                    max_freq *= 9 / 8
            else:
                if max_freq == 2 * np.pi * np.max(freqtest):
                    max_freq = 1e16
            freq_intervals[_i, :] = np.array([min_freq, max_freq])
        return freq_intervals

    def _find_limits_of_violation(self, violations: np.ndarray) -> List[Tuple]:
        """
        Input an array of indices where violations take place. This one finds the
        min max index of each violation interval and returns an array of min max tuples

        :param violations: Array of indices of violations
        :type violations: np.ndarray
        :return: Array of min max indices of each violation interval
        :rtype: List[Tuple]
        """

        intervals = []
        i = 0
        while i < len(violations):
            min_index = violations[i]
            while i < len(violations) - 1 and violations[i] + 1 == violations[i + 1]:
                i += 1
            max_index = violations[i]
            intervals.append((min_index, max_index))
            i += 1

        return np.array(intervals)

    def is_passive(self, parameter_type: str = "s") -> bool:
        """
        Returns the passivity status of the model as a boolean value.

        Parameters
        ----------
        parameter_type : str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`). Currently, only scattering
            parameters are supported for passivity evaluation.

        Returns
        -------
        passivity : bool
            :attr:`True` if model is passive, else :attr:`False`.

        See Also
        --------
        passivity_test : Verbose passivity evaluation routine.
        passivity_enforce : Enforces the passivity of the vector fitted model, if required.

        Examples
        --------
        Load and fit the `Network`, then check whether or not the model is passive:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.is_passive() # returns True or False
        """

        viol_bands = self.passivity_test(parameter_type)
        if len(viol_bands) != 0:
            return False
        else:
            if parameter_type in ["r", "y"]:
                _, _, _, D, _ = self._get_ABCDE()
                if parameter_type == "r":
                    if np.all(np.abs(D) < 1.0):
                        return True
                    else:
                        return False
                else:
                    if np.all(D > 0.0):
                        return True
                    else:
                        return False
            else:
                return True

        # if len(viol_bands) == 0:
        #     return True
        # else:
        #     return False

    def passivity_enforce(self, n_samples: int = 200, f_max: float = None, parameter_type: str = "s") -> None:
        """
        Enforces the passivity of the vector fitted model, if required. This is an implementation of the method
        presented in [#]_. Passivity is achieved by updating the residues and the constants.

        Parameters
        ----------
        n_samples : int, optional
            Number of linearly spaced frequency samples at which passivity will be evaluated and enforced.
            (Default: 100)

        f_max : float or None, optional
            Highest frequency of interest for the passivity enforcement (in Hz, not rad/s). This limit usually
            equals the highest sample frequency of the fitted Network. If None, the highest frequency in
            :attr:`self.network` is used, which must not be None is this case. If `f_max` is not None, it overrides the
            highest frequency in :attr:`self.network`.

        parameter_type : str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`). Currently, only scattering
            parameters are supported for passivity evaluation.

        Returns
        -------
        None

        Raises
        ------
        NotImplementedError
            If the function is called for `parameter_type` different than `S` (scattering).

        ValueError
            If the function is used with a model containing nonzero proportional coefficients. Or if both `f_max` and
            :attr:`self.network` are None.

        See Also
        --------
        is_passive : Returns the passivity status of the model as a boolean value.
        passivity_test : Verbose passivity evaluation routine.
        plot_passivation : Convergence plot for passivity enforcement iterations.

        Examples
        --------
        Load and fit the `Network`, then enforce the passivity of the model:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.passivity_enforce()  # won't do anything if model is already passive

        References
        ----------
        .. [#] T. Dhaene, D. Deschrijver and N. Stevens, "Efficient Algorithm for Passivity Enforcement of S-Parameter-
            Based Macromodels," in IEEE Transactions on Microwave Theory and Techniques, vol. 57, no. 2, pp. 415-420,
            Feb. 2009, DOI: 10.1109/TMTT.2008.2011201.
        """
        if parameter_type.lower() in ["y", "r"]:
            self.passivity_enforce_y_or_r(parameter_type=parameter_type.lower())
            return
        if parameter_type.lower() != "s":
            raise NotImplementedError("Passivity testing is currently only supported for scattering (S) parameters.")
        if parameter_type.lower() == "s" and len(np.flatnonzero(self.proportional_coeff)) > 0:
            raise ValueError(
                "Passivity testing of scattering parameters with nonzero proportional coefficients does "
                "not make any sense; you need to run vector_fit() with option `fit_proportional=False` "
                "first."
            )

        # always run passivity test first; this will write 'self.violation_bands'
        if self.is_passive(parameter_type=parameter_type):
            # model is already passive; do nothing and return
            logging.info("Passivity enforcement: The model is already passive. Nothing to do.")
            return

        # find the highest relevant frequency; either
        # 1) the highest frequency of passivity violation (f_viol_max)
        # or
        # 2) the highest fitting frequency (f_samples_max)
        violation_bands = self.passivity_test()

        f_viol_max = violation_bands[-1, 1]

        if f_max is None:
            if self.network is None:
                raise RuntimeError(
                    "Both `self.network` and parameter `f_max` are None. One of them is required to "
                    "specify the frequency band of interest for the passivity enforcement."
                )
            else:
                f_samples_max = self.network.f[-1]
        else:
            f_samples_max = f_max

        # deal with unbounded violation interval (f_viol_max == np.inf)
        if np.isinf(f_viol_max):
            f_viol_max = 1.5 * violation_bands[-1, 0]
            warnings.warn(
                "Passivity enforcement: The passivity violations of this model are unbounded. Passivity "
                "enforcement might still work, but consider re-fitting with a lower number of poles and/or "
                "without the constants (`fit_constant=False`) if the results are not satisfactory.",
                UserWarning,
                stacklevel=2,
            )

        # the frequency band for the passivity evaluation is from dc to 20% above the highest relevant frequency
        if f_viol_max < f_samples_max:
            f_eval_max = 1.2 * f_samples_max
        else:
            f_eval_max = 1.2 * f_viol_max
        freqs_eval = np.linspace(0, f_eval_max, n_samples)

        A, B, C, D, E = self._get_ABCDE()
        dim_A = np.shape(A)[0]
        C_t = C

        # only include constant if it has been fitted (not zero)
        if len(np.nonzero(D)[0]) == 0:
            D_t = None
        else:
            D_t = D

        if self.network is not None:
            # find highest singular value among all frequencies and responses to use as target for the perturbation
            # singular value decomposition
            sigma = np.linalg.svd(self.network.s, compute_uv=False)
            delta = np.amax(sigma)
            if delta > 0.999:
                delta = 0.999
        else:
            delta = 0.999  # predefined tolerance parameter (users should not need to change this)

        # calculate coefficient matrix
        A_freq = np.linalg.inv(2j * np.pi * freqs_eval[:, None, None] * np.identity(dim_A)[None, :, :] - A[None, :, :])

        # construct coefficient matrix with an extra column for the constants (if present)
        if D_t is not None:
            coeffs = np.empty((len(freqs_eval), np.shape(B)[0] + 1, np.shape(B)[1]), dtype=complex)
            coeffs[:, :-1, :] = np.matmul(A_freq, B[None, :, :])
            coeffs[:, -1, :] = 1
        else:
            coeffs = np.matmul(A_freq, B[None, :, :])

        # iterative compensation of passivity violations
        t = 0
        self.history_max_sigma = []
        while t < self.max_iterations:
            logging.info("Passivity enforcement; Iteration {}".format(t + 1))

            # calculate S-matrix at this frequency (shape fxNxN)
            if D_t is not None:
                s_eval = self._get_s_from_ABCDE(freqs_eval, A, B, C_t, D_t, E)
            else:
                s_eval = self._get_s_from_ABCDE(freqs_eval, A, B, C_t, D, E)

            # singular value decomposition
            u, sigma, vh = np.linalg.svd(s_eval, full_matrices=False)

            # keep track of the greatest singular value in every iteration step
            sigma_max = np.amax(sigma)

            # find and perturb singular values that cause passivity violations
            idx_viol = np.nonzero(sigma > delta)
            sigma_viol = np.zeros_like(sigma)
            sigma_viol[idx_viol] = sigma[idx_viol] - delta

            # construct a stack of diagonal matrices with the perturbed singular values on the diagonal
            sigma_viol_diag = np.zeros_like(u, dtype=float)
            idx_diag = np.arange(np.shape(sigma)[1])
            sigma_viol_diag[:, idx_diag, idx_diag] = sigma_viol

            # calculate violation S-responses
            s_viol = np.matmul(np.matmul(u, sigma_viol_diag), vh)

            # fit perturbed residues C_t for each response S_{i,j}
            for i in range(np.shape(s_viol)[1]):
                for j in range(np.shape(s_viol)[2]):
                    # mind the transpose of the system to compensate for the exchanged order of matrix multiplication:
                    # wanting to solve S = C_t * coeffs
                    # but actually solving S = coeffs * C_t
                    # S = C_t * coeffs <==> transpose(S) = transpose(coeffs) * transpose(C_t)

                    # solve least squares (real-valued)
                    x, residuals, rank, singular_vals = np.linalg.lstsq(
                        np.vstack((np.real(coeffs[:, :, i]), np.imag(coeffs[:, :, i]))),
                        np.hstack((np.real(s_viol[:, j, i]), np.imag(s_viol[:, j, i]))),
                        rcond=None,
                    )

                    # perturb residues by subtracting respective row and column in C_t
                    # one half of the solution will always be 0 due to construction of A and B
                    # also perturb constants (if present)
                    if D_t is not None:
                        C_t[j, :] = C_t[j, :] - x[:-1]
                        D_t[j, i] = D_t[j, i] - x[-1]
                    else:
                        C_t[j, :] = C_t[j, :] - x

            t += 1
            self.history_max_sigma.append(sigma_max)

            # stop iterations when model is passive
            if sigma_max < 1.0:
                break

        # PASSIVATION PROCESS DONE; model is either passive or max. number of iterations have been exceeded
        if t == self.max_iterations:
            warnings.warn(
                "Passivity enforcement: Aborting after the max. number of iterations has been exceeded.",
                RuntimeWarning,
                stacklevel=2,
            )

        # save/update model parameters (perturbed residues)
        self.history_max_sigma = np.array(self.history_max_sigma)

        n_ports = np.shape(D)[0]
        for i in range(n_ports):
            k = 0  # column index in C_t
            for j in range(n_ports):
                i_response = i * n_ports + j
                z = 0  # column index self.residues
                for pole in self.poles:
                    if np.imag(pole) == 0.0:
                        # real pole --> real residue
                        self.residues[i_response, z] = C_t[i, k]
                        k += 1
                    else:
                        # complex-conjugate pole --> complex-conjugate residue
                        self.residues[i_response, z] = C_t[i, k] + 1j * C_t[i, k + 1]
                        k += 2
                    z += 1
                if D_t is not None:
                    self.constant_coeff[i_response] = D_t[i, j]

        # run final passivity test to make sure passivation was successful
        violation_bands = self.passivity_test()
        if len(violation_bands) > 0:
            warnings.warn(
                "Passivity enforcement was not successful.\nModel is still non-passive in these frequency "
                "bands: {}.\nTry running this routine again with a larger number of samples (parameter "
                "`n_samples`).".format(violation_bands),
                RuntimeWarning,
                stacklevel=2,
            )

    def passivity_enforce_y_or_r(self, parameter_type="y"):
        """
        Use the Fast residue perturbation technique to perturbe the eigenvalues
        of D and the residues to find a passive model while minimizing the difference
        between the initial pole-residue model and the final passive one.
        """
        # always run passivity test first; this will write 'self.violation_bands'
        if self.is_passive(parameter_type=parameter_type):
            # model is already passive; do nothing and return
            logging.info("Passivity enforcement: The model is already passive. Nothing to do.")
            return

        # find the highest relevant frequency; either
        # 1) the highest frequency of passivity violation (f_viol_max)
        # or
        # 2) the highest fitting frequency (f_samples_max)
        A0, B0, C0, D0, E0 = self._get_ABCDE(for_passivity_enforcing=True)
        A1, B1, C1, D1, E1 = (
            np.copy(A0),
            np.copy(B0),
            np.copy(C0),
            np.copy(D0),
            np.copy(E0),
        )
        Nc = len(D0)

        # We perturb the residue matrix, and D matrix to reach passivity
        # E is also forced to be positive definite, but we have no E so that's irrelevant
        # We want the changes to Y to be close to zero, including our perturbations of
        # R and D, which enforce the eigenvalues of Y, and D to be > 0

        TOLGD = 1e-6
        # Outer loop
        iter_out = 0
        niter_out = 60
        niter_in = 2
        break_outer = False
        s = 1j * 2 * np.pi * self.network.f
        while iter_out <= niter_out:
            logging.info(f"Passivity enforcing Iteration: {iter_out}")
            if break_outer:
                break
            s3 = []
            for iter_in in range(niter_in):
                s2 = []
                if iter_in == 0:
                    violation_bands = self.passivity_test(parameter_type=parameter_type)
                    if parameter_type == "r":
                        if len(violation_bands) == 0 and np.all(np.abs(D1) < 1):
                            break_outer = True
                            break
                    else:
                        if len(violation_bands) == 0 and np.all(np.linalg.eigvals(D1) >= 0):
                            break_outer = True
                            break

                    # Now we need to find the minima within each interval
                    # So we need to find the lowest eigenvalue within each violating interval
                    # and bring it up above zero.
                    s_viol, g_pass, ss = self.violextrema(violation_bands, parameter_type=parameter_type)
                    s2 = np.sort(s_viol)
                    if parameter_type == "r":
                        if len(s2) == 0 and np.abs(D1) < 1.0:
                            break
                    else:
                        if len(s2) == 0 and np.all(np.linalg.eigvals(D1) > 0):
                            break
                if parameter_type == "r":
                    C1, D1 = self.FRPR(A0, B0, C0, D0, s, s2, s3)
                else:
                    C1, D1 = self.FRPY(A0, B0, C0, D0, s, s2, s3, parameter_type=parameter_type)
                k = 0
                z = 0
                flag = False
                for pole in self.all_poles:
                    if flag:
                        flag = False
                        continue
                    if np.imag(pole) == 0.0:
                        self.residues[0, z] = C1[0, k]
                        k += 1
                    else:
                        self.residues[0, z] = np.real(C1[0, k]) + 1j * np.imag(C1[0, k])
                        k += 2
                        flag = True
                    z += 1
                # self.residues = C1.copy().astype(complex)
                self.constant_coeff = D1.copy()
                if iter_in != niter_in - 1:
                    wintervals = self.passivity_test(parameter_type=parameter_type)
                    s_viol, g_pass, ss = self.violextrema(wintervals, parameter_type=parameter_type)
                    olds3 = s3
                    if len(s3) == 0:
                        if len(s_viol) == 0 or s_viol in s2:
                            s3 = s2.copy()
                        else:
                            s3 = np.concatenate((s2, s_viol))
                    else:
                        s3 = np.concatenate((s3, s2, s_viol))

                if iter_in == niter_in - 1:
                    s3, s2 = [], []
                    C0, D0 = C1.copy(), D1.copy()

            iter_out += 1

    def FRPY(self, A, B, C, D, s, s2, s3, parameter_type="y") -> Tuple[np.ndarray, np.ndarray]:
        """
        Function which modifies the elements in the C and D to enforce passivity
        of Y-parameter model at frequency samples in s2 and s3, such that the perturbation
        of the model is minimized at samples in s.

        :return: Updated C and D matrices
        :rtype: Tuple[np.ndarray, np.ndarray]
        """

        Cnew, Dnew = C.copy(), D.copy()
        N = len(self.all_poles)

        d = np.linalg.eigvals(D)
        if parameter_type.lower() == "r":
            violation = np.abs(d) > 1.0
        else:
            violation = d < 0
        if violation:
            Dflag = True
            eigD, VD = np.linalg.eig(D)
            invVD = np.linalg.inv(VD)
        else:
            Dflag = False

        TOL = 1e-6
        Ns = len(s)
        Ns2 = len(s2)
        Nc = len(D)  # This is 1 in all my use cases
        Nc2 = Nc * Nc
        I = np.identity(Nc)
        Mmat = np.zeros(N + Dflag, dtype=complex)

        cindex = np.zeros(N)
        for m in range(N):
            if np.imag(self.all_poles[m]) != 0:
                if m == 0:
                    cindex[m] = 1
                else:
                    if cindex[m - 1] == 0 or cindex[m - 1] == 2:
                        cindex[m] = 1
                        cindex[m + 1] = 2
                    else:
                        cindex[m] = 2

        bigA = np.zeros((Ns, (N + Dflag)), dtype=complex)
        bigV = np.zeros((1, N))
        biginvV = np.zeros((1, N))
        bigD = np.zeros((1, N))
        for m in range(N):
            R = C[:, m].copy()
            if cindex[m] == 0:
                R = R
            elif cindex[m] == 1:
                R = np.real(R)
            else:
                R = np.imag(R)
            if len(R) == 1:
                D_val, V = R, 1
            else:
                D_val, V = np.linalg.eig(R)
            bigV[0, m] = V
            if V == 1:
                biginvV[0, m] = 1 / V
            else:
                biginvV[0, m] = np.linalg.inv(V)

            bigD[:, m] = D_val

        for k in range(Ns):
            sk = s[k]
            tell = 0
            offs = 0
            Yfit = self.fitcalcPRE(sk, C, D)

            weight = 1 / np.abs(Yfit[0])

            for m in range(N):
                V = np.squeeze(bigV[:, m])
                if V == 1:
                    invV = 1
                else:
                    invV = np.linalg.inv(V)
                if cindex[m] == 0:
                    dum = 1 / (sk - self.all_poles[m])
                elif cindex[m] == 1:
                    dum = 1 / (sk - self.all_poles[m]) + 1 / (sk - np.conj(self.all_poles[m]))
                else:
                    dum = 1j / (sk - np.conj(self.all_poles[m])) - 1j / (sk - self.all_poles[m])

                if V == 1:
                    gamm = V
                else:
                    gamm = V @ invV
                Mmat[offs] = gamm * weight * dum
                offs += 1

            if Dflag:
                if VD == 1:
                    gamm = VD
                else:
                    gamm = VD @ invV
                gamm = VD @ invVD
                Mmat[offs] = gamm * weight
            bigA[k, :] = Mmat

        # Now we introduce samples outside LS region: One sample per pole (s4)
        s4 = []
        # s4 = np.zeros(len(self.all_poles), dtype=complex)
        tell = 0
        for m in range(len(self.all_poles)):
            if cindex[m] == 0:
                if (np.abs(self.all_poles[m]) > s[Ns - 1] / 1j) or (np.abs(self.all_poles[m]) < s[0] / 1j):
                    s4.append(1j * np.abs(self.all_poles[m]))
                    tell += 1
            elif cindex[m] == 1:
                if (
                    np.abs(np.imag(self.all_poles[m]) > s[Ns - 1] / 1j)
                    or np.abs(np.imag(self.all_poles[m])) < s[0] / 1j
                ):
                    s4.append(1j * np.abs(np.imag(self.all_poles[m])))
                    tell += 1
        Ns4 = len(s4)

        bigA2 = np.empty((Ns4, (N + Dflag)), dtype=complex)
        weightfactor = 1e-3  # Weightfactor for out of band frequencies
        for k in range(Ns4):
            sk = s4[k]
            tell = 0
            offs = 0
            Yfit = self.fitcalcPRE(sk, C, D)
            weight = 1 / np.abs(Yfit[0])
            weight = weight * weightfactor

            for m in range(N):
                V = np.squeeze(bigV[:, m])
                if V == 1:
                    invV = 1
                else:
                    invV = np.linalg.inv(V)
                if cindex[m] == 0:
                    dum = gamm / (sk - self.all_poles[m])
                elif cindex[m] == 1:
                    dum = gamm * (1 / (sk - self.all_poles[m]) + 1 / (sk - np.conj(self.all_poles[m])))
                else:
                    dum = gamm * (1j / (sk - np.conj(self.all_poles[m])) - 1j / (sk - self.all_poles[m]))
                if V == 1:
                    gamm = V
                else:
                    gamm = V @ invV
                Mmat[m] = gamm * weight * dum
                offs += 1
            if Dflag:
                if VD == 1:
                    gamm = VD
                else:
                    gamm = VD @ invVD
                Mmat[offs] = gamm * weight
            bigA2[k, :] = Mmat
        bigA = np.vstack((bigA, bigA2))

        bigA = np.vstack((np.real(bigA), np.imag(bigA)))
        Acol = len(bigA[0, :])
        Escale = np.zeros(Acol)
        for col in range(Acol):
            Escale[col] = np.linalg.norm(bigA[:, col], ord=2)
            bigA[:, col] = bigA[:, col] / Escale[col]
        H = bigA.T @ bigA

        Mmat2 = np.zeros((N + Dflag), dtype=complex)
        viol_G = []
        viol_D = []
        # Loop for constraint problem, type 1 (violating eigenvalues in s2)
        for k in range(Ns2):
            sk = s2[k]
            Y = D + np.sum(np.squeeze(C[0]) / (sk - self.all_poles))
            if parameter_type.lower() == "r":
                Z = np.abs(Y)
                violation = Z > 1.0
            else:
                Z, eigvec = np.linalg.eig(np.real(Y))
                violation = np.min(np.real(Z)) < 0

            if violation:  # Any violations
                offs = 0
                for m in range(N):
                    VV = bigV[:, m]
                    invVV = biginvV[:, m]
                    if VV == 1:
                        gamm = VV
                    else:
                        gamm = VV @ invVV
                    if cindex[m] == 0:
                        Mmat2[offs] = gamm / (sk - self.all_poles[m])
                    elif cindex[m] == 1:
                        Mmat2[offs] = gamm * (1 / (sk - self.all_poles[m]) + 1 / (sk - np.conj(self.all_poles[m])))
                    else:
                        Mmat2[offs] = gamm * (1j / (sk - np.conj(self.all_poles[m])) - 1j / (sk - self.all_poles[m]))
                    offs += 1
                if Dflag:
                    if VD == 1:
                        gamm = VD
                    else:
                        gamm = VD @ invVD
                    Mmat2[offs] = gamm
                if V == 1:
                    V1 = 1
                else:
                    V1 = V[:, 0]
                qij = V1**2
                Q = qij
                if Q == 1:
                    BB = Q * Mmat2
                else:
                    BB = Q @ Mmat2
                if parameter_type.lower() == "r":
                    delz = np.abs(Z)
                    violation = delz > 1
                else:
                    delz = np.real(Z)
                    violation = delz < 0
                    # We need to be a bit different with bigC due to D
                if violation:
                    if parameter_type.lower() == "r":
                        try:
                            bigB = np.vstack((bigB, -BB))
                        except:
                            bigB = -BB.copy()
                        try:
                            bigC = np.vstack((bigC, 1 - delz - TOL))
                        except:
                            bigC = 1 - delz.copy() - TOL
                    else:
                        try:
                            bigB = np.vstack((bigB, BB))
                        except:
                            bigB = BB.copy()
                        try:
                            bigC = np.vstack((bigC, -TOL + delz))
                        except:
                            bigC = -TOL + delz.copy()
                    viol_G.append(delz)

        # Loop for constraint problem (Type 2): all eigenvalues in s3
        Ns3 = len(s3)
        for k in range(Ns3):
            sk = s3[k]
            Y = D + np.sum(np.squeeze(C[0]) / (sk - self.all_poles))
            if parameter_type.lower() == "r":
                Z = np.abs(Y)
            else:
                Z, eigvec = np.linalg.eig(np.real(Y))

            tell = 0
            offs = 0

            for m in range(N):
                VV = bigV[:, m]
                invVV = biginvV[:, m]
                if VV == 1:
                    gamm = VV
                else:
                    gamm = VV @ invVV
                if cindex[m] == 0:
                    Mmat2[offs] = gamm / (sk - self.all_poles[m])
                elif cindex[m] == 1:
                    Mmat2[offs] = gamm * (1 / (sk - self.all_poles[m]) + 1 / (sk - np.conj(self.all_poles[m])))
                else:
                    Mmat2[offs] = gamm * (1j / (sk - np.conj(self.all_poles[m])) - 1j / (sk - self.all_poles[m]))
                offs += 1

                tell = 0
            if Dflag:
                if VD == 1:
                    gamm = VD
                else:
                    gamm = VD[:, 0] @ invVD[0, :]
                Mmat2[offs] = gamm
            if V == 1:
                V1 = 1
            else:
                V1 = V[:, 0]
            qij = V1**2
            Q = qij
            if Q == 1:
                BB = Q * Mmat2
            else:
                BB = Q @ Mmat2

            if parameter_type.lower() == "r":
                delz = np.abs(Z)
                violation = delz > 1
            else:
                delz = np.real(Z)
                violation = delz < 0
                # We need to be a bit different with bigC due to D
            if violation:
                if parameter_type.lower() == "r":
                    try:
                        bigB = np.vstack((bigB, -BB))
                    except:
                        bigB = -BB.copy()
                    try:
                        bigC = np.vstack((bigC, 1 - delz - TOL))
                    except:
                        bigC = 1 - delz.copy() - TOL
                else:
                    try:
                        bigB = np.vstack((bigB, BB))
                    except:
                        bigB = BB.copy()
                    try:
                        bigC = np.vstack((bigC, -TOL + delz))
                    except:
                        bigC = -TOL + delz.copy()
                viol_G.append(delz)
            # delz = np.real(Z)
            # if delz < 0:
            #     try:
            #         bigB = np.vstack((bigB, BB))
            #     except:
            #         bigB = BB.copy()
            #     try:
            #         bigC = np.vstack((bigC, -TOL + delz))
            #     except:
            #         bigC = -TOL + delz.copy()
            #     viol_G.append(delz)
        if parameter_type == "r":
            try:
                bigB = np.abs(bigB)
            except:
                pass
            # bigB = np.sqrt(1 - np.square(np.real(bigB)) + np.square(np.imag(bigB)))
        if Dflag:
            if parameter_type.lower() == "r":
                violation = np.abs(eigD) > 1
            else:
                violation = eigD < 0
            if violation:
                dum = np.zeros((N + Dflag))
                dum[N] = 1
                if parameter_type.lower() == "r":
                    try:
                        bigB = np.vstack((bigB, -dum))
                    except:
                        bigB = -dum.copy()
                    try:
                        bigC = np.vstack((bigC, 1 - np.abs(eigD) - TOL))
                    except:
                        bigC = 1 - np.abs(eigD) - TOL
                else:
                    try:
                        bigB = np.vstack((bigB, dum))
                    except:
                        bigB = dum.copy()
                    try:
                        bigC = np.vstack((bigC, -TOL + eigD))
                    except:
                        bigC = -TOL + eigD.copy()
                viol_G.append(eigD)
                viol_D.append(eigD)

        if len(bigB) == 0:
            return Cnew, Dnew
        ff = np.zeros(len(H))
        bigB = np.reshape(bigB, (len(bigC), len(H)))
        if len(bigC.shape) > 1:
            if bigC.shape == (1, 1):
                bigC = bigC[0]
            else:
                bigC = np.squeeze(bigC)

        if parameter_type != "r":
            bigB = np.real(bigB)
        # if parameter_type == "r":
        #     bigB = -np.abs(bigB)
        #     # bigB = np.sqrt(1 - np.square(np.real(bigB)) + np.square(np.imag(bigB)))
        # else:
        #     bigB = np.real(bigB)

        for col in range(len(H)):
            if len(bigB) > 0:
                bigB[:, col] = bigB[:, col] / Escale[col]

        dx, f, xu, iterations, lagrangian, iact = quadprog.solve_qp(H, ff, bigB.T, -bigC)
        dx = dx / Escale

        Cnew = C.copy()
        Dnew = D.copy()
        bigV = bigV[0]
        biginvV = biginvV[0]
        for m in range(N):
            if cindex[m] == 0:
                if isinstance(dx[m], float):
                    Diff1 = dx[m]
                    Cnew[:, m] = Cnew[:, m] + bigV[m] * Diff1 * biginvV[m]
                else:
                    Diff1 = np.diag(np.array(dx[m]))
                    Cnew[:, m] = Cnew[:, m] + bigV[:, m] @ Diff1 @ biginvV[:, m]
            elif cindex[m] == 1:
                GAMM1 = bigV[m]
                GAMM2 = bigV[m + 1]
                invGAMM1 = biginvV[m]
                invGAMM2 = biginvV[(m + 1)]

                R1 = np.real(C[:, m])
                R2 = np.imag(C[:, m])
                if isinstance(dx[m], float):
                    Diff1 = dx[m]
                    Diff2 = dx[m + 1]
                    R1new = R1 + GAMM1 * Diff1 * invGAMM1
                    R2new = R2 + GAMM2 * Diff2 * invGAMM2
                else:
                    Diff1 = np.diag(np.array(dx[m]))
                    Diff2 = np.diag(np.array(dx[m + 1]))
                    R1new = R1 + GAMM1 @ Diff1 @ invGAMM1
                    R2new = R2 + GAMM2 @ Diff2 @ invGAMM2
                Cnew[:, m] = R1new + 1j * R2new
                Cnew[:, m + 1] = R1new - 1j * R2new
        if Dflag:
            if isinstance(dx[N], float):
                DD = dx[N]
                Dnew = Dnew + VD * DD * invVD
            else:
                DD = np.diag(dx[N])
                Dnew = Dnew + VD @ DD @ invVD

            Dnew = (Dnew + Dnew.T) / 2
        for m in range(N):
            Cnew[:, m] = (Cnew[:, m] + Cnew[:, m].T) / 2

        return Cnew, Dnew

    def FRPR(self, A, B, C, D, s, s2, s3) -> Tuple[np.ndarray, np.ndarray]:
        """
        Function which modifies the elements in the C and D to enforce passivity
        of Y-parameter model at frequency samples in s2 and s3, such that the perturbation
        of the model is minimized at samples in s.

        :return: Updated C and D matrices
        :rtype: Tuple[np.ndarray, np.ndarray]
        """

        Cnew, Dnew = C.copy(), D.copy()
        N = len(self.all_poles)

        d = np.linalg.eigvals(D)
        violation = np.abs(d) > 1.0
        if violation:
            Dflag = True
            eigD, VD = np.linalg.eig(D)
            invVD = np.linalg.inv(VD)
        else:
            Dflag = False

        TOL = 1e-6
        Ns = len(s)
        Ns2 = len(s2)
        Nc = len(D)  # This is 1 in all my use cases
        Nc2 = Nc * Nc
        I = np.identity(Nc)
        Mmat = np.zeros(N + Dflag, dtype=complex)

        cindex = np.zeros(N)
        for m in range(N):
            if np.imag(self.all_poles[m]) != 0:
                if m == 0:
                    cindex[m] = 1
                else:
                    if cindex[m - 1] == 0 or cindex[m - 1] == 2:
                        cindex[m] = 1
                        cindex[m + 1] = 2
                    else:
                        cindex[m] = 2

        bigA = np.zeros((Ns, (N + Dflag)), dtype=complex)
        bigV = np.zeros((1, N))
        biginvV = np.zeros((1, N))
        bigD = np.zeros((1, N))
        for m in range(N):
            R = C[:, m].copy()
            if cindex[m] == 0:
                R = R
            elif cindex[m] == 1:
                R = np.real(R)
            else:
                R = np.imag(R)
            if len(R) == 1:
                D_val, V = R, 1
            else:
                D_val, V = np.linalg.eig(R)
            bigV[0, m] = V
            if V == 1:
                biginvV[0, m] = 1 / V
            else:
                biginvV[0, m] = np.linalg.inv(V)

            bigD[:, m] = D_val

        for k in range(Ns):
            sk = s[k]
            tell = 0
            offs = 0
            Yfit = self.fitcalcPRE(sk, C, D)

            weight = 1 / np.abs(Yfit[0])

            for m in range(N):
                V = np.squeeze(bigV[:, m])
                if V == 1:
                    invV = 1
                else:
                    invV = np.linalg.inv(V)
                if cindex[m] == 0:
                    dum = 1 / (sk - self.all_poles[m])
                elif cindex[m] == 1:
                    dum = 1 / (sk - self.all_poles[m]) + 1 / (sk - np.conj(self.all_poles[m]))
                else:
                    dum = 1j / (sk - np.conj(self.all_poles[m])) - 1j / (sk - self.all_poles[m])

                if V == 1:
                    gamm = V
                else:
                    gamm = V @ invV
                Mmat[offs] = gamm * weight * dum
                offs += 1

            if Dflag:  # I probably need to add to this one, no don't think so
                if VD == 1:
                    gamm = VD
                else:
                    gamm = VD @ invV
                gamm = VD @ invVD
                Mmat[offs] = gamm * weight
            bigA[k, :] = Mmat

        # Now we introduce samples outside LS region: One sample per pole (s4)
        s4 = []
        # s4 = np.zeros(len(self.all_poles), dtype=complex)
        tell = 0
        for m in range(len(self.all_poles)):
            if cindex[m] == 0:
                if (np.abs(self.all_poles[m]) > s[Ns - 1] / 1j) or (np.abs(self.all_poles[m]) < s[0] / 1j):
                    s4.append(1j * np.abs(self.all_poles[m]))
                    tell += 1
            elif cindex[m] == 1:
                if (
                    np.abs(np.imag(self.all_poles[m]) > s[Ns - 1] / 1j)
                    or np.abs(np.imag(self.all_poles[m])) < s[0] / 1j
                ):
                    s4.append(1j * np.abs(np.imag(self.all_poles[m])))
                    tell += 1
        Ns4 = len(s4)

        bigA2 = np.empty((Ns4, (N + Dflag)), dtype=complex)
        weightfactor = 1e-3  # Weightfactor for out of band frequencies
        for k in range(Ns4):
            sk = s4[k]
            tell = 0
            offs = 0
            Yfit = self.fitcalcPRE(sk, C, D)
            weight = 1 / np.abs(Yfit[0])
            weight = weight * weightfactor

            for m in range(N):
                V = np.squeeze(bigV[:, m])
                if V == 1:
                    invV = 1
                else:
                    invV = np.linalg.inv(V)
                if cindex[m] == 0:
                    dum = gamm / (sk - self.all_poles[m])
                elif cindex[m] == 1:
                    dum = gamm * (1 / (sk - self.all_poles[m]) + 1 / (sk - np.conj(self.all_poles[m])))
                else:
                    dum = gamm * (1j / (sk - np.conj(self.all_poles[m])) - 1j / (sk - self.all_poles[m]))
                if V == 1:
                    gamm = V
                else:
                    gamm = V @ invV
                Mmat[m] = gamm * weight * dum
                offs += 1
            if Dflag:  # I might need to add to this one here, nope I don't think so
                if VD == 1:
                    gamm = VD
                else:
                    gamm = VD @ invVD
                Mmat[offs] = gamm * weight
            bigA2[k, :] = Mmat
        bigA = np.vstack((bigA, bigA2))

        bigA = np.vstack((np.real(bigA), np.imag(bigA)))  # Is this something I need to think about?
        Acol = len(bigA[0, :])
        Escale = np.zeros(Acol)
        for col in range(Acol):
            Escale[col] = np.linalg.norm(bigA[:, col], ord=2)
            bigA[:, col] = bigA[:, col] / Escale[col]
        H = bigA.T @ bigA

        Mmat2 = np.zeros((N + Dflag), dtype=complex)
        viol_G = []
        viol_D = []
        # Loop for constraint problem, type 1 (violating eigenvalues in s2)
        for k in range(Ns2):
            sk = s2[k]
            Y = D + np.sum(np.squeeze(C[0]) / (sk - self.all_poles))
            Z = np.abs(Y)
            violation = Z > 1.0

            if violation:  # Any violations
                offs = 0
                for m in range(N):
                    VV = bigV[:, m]
                    invVV = biginvV[:, m]
                    if VV == 1:
                        gamm = VV
                    else:
                        gamm = VV @ invVV
                    if cindex[m] == 0:
                        Mmat2[offs] = gamm / (sk - self.all_poles[m])
                    elif cindex[m] == 1:
                        Mmat2[offs] = gamm * (1 / (sk - self.all_poles[m]) + 1 / (sk - np.conj(self.all_poles[m])))
                    else:
                        Mmat2[offs] = gamm * (1j / (sk - np.conj(self.all_poles[m])) - 1j / (sk - self.all_poles[m]))
                    offs += 1
                if Dflag:
                    if VD == 1:
                        gamm = VD
                    else:
                        gamm = VD @ invVD
                    Mmat2[offs] = gamm
                if V == 1:
                    V1 = 1
                else:
                    V1 = V[:, 0]
                qij = V1**2
                Q = qij
                if Q == 1:
                    BB = Q * Mmat2
                else:
                    BB = Q @ Mmat2
                delz = Z
                violation = np.abs(delz) > 1
                # else:
                #     delz = np.real(Z)
                #     violation = delz < 0
                # We need to be a bit different with bigC due to D
                if violation:
                    # We approximate abs(Y + dY) < 1 with four conditions
                    # 1. Re(Y) + Re(dY) + Im(Y) + Im(dY) < 1
                    try:
                        bigB = np.vstack(
                            (bigB, np.real(BB) + np.imag(BB))
                        )  # I'm putting -BB here, need to keep in mind
                    except:
                        bigB = np.real(BB) + np.imag(BB)
                    try:
                        bigC = np.vstack((bigC, 1 - np.real(delz) - np.imag(delz) - TOL))  # Make-ar thetta sense?
                    except:
                        bigC = 1 - np.real(delz) - np.imag(delz) - TOL
                    # 2. -Re(Y) + Re(dY) - Im(Y) + Im(dY) < 1
                    try:
                        bigB = np.vstack(
                            (bigB, np.real(BB) + np.imag(BB))
                        )  # I'm putting -BB here, need to keep in mind
                    except:
                        bigB = np.real(BB) + np.imag(BB)
                    try:
                        bigC = np.vstack((bigC, 1 + np.real(delz) + np.imag(delz) - TOL))  # Make-ar thetta sense?
                    except:
                        bigC = 1 + np.real(delz) + np.imag(delz) - TOL
                    # 3. -Re(Y) + Re(dY) + Im(Y) + Im(dY) < 1
                    try:
                        bigB = np.vstack(
                            (bigB, np.real(BB) + np.imag(BB))
                        )  # I'm putting -BB here, need to keep in mind
                    except:
                        bigB = np.real(BB) + np.imag(BB)
                    try:
                        bigC = np.vstack((bigC, 1 + np.real(delz) - np.imag(delz) - TOL))  # Make-ar thetta sense?
                    except:
                        bigC = 1 - np.real(delz) + np.imag(delz) - TOL
                    # 4. Re(Y) + Re(dY) - Im(Y) + Im(dY) < 1
                    try:
                        bigB = np.vstack(
                            (bigB, np.real(BB) + np.imag(BB))
                        )  # I'm putting -BB here, need to keep in mind
                    except:
                        bigB = np.real(BB) + np.imag(BB)
                    try:
                        bigC = np.vstack((bigC, 1 - np.real(delz) + np.imag(delz) - TOL))  # Make-ar thetta sense?
                    except:
                        bigC = 1 - np.real(delz) + np.imag(delz) - TOL
                    # else:
                    #     try:
                    #         bigB = np.vstack((bigB, BB))
                    #     except:
                    #         bigB = BB.copy()
                    #     try:
                    #         bigC = np.vstack((bigC, -TOL + delz))
                    #     except:
                    #         bigC = -TOL + delz.copy()
                    viol_G.append(delz)

        # Loop for constraint problem (Type 2): all eigenvalues in s3
        Ns3 = len(s3)
        for k in range(Ns3):
            sk = s3[k]
            Y = D + np.sum(np.squeeze(C[0]) / (sk - self.all_poles))
            # if parameter_type.lower() == "r":
            Z = np.abs(Y)
            # else:
            #     Z, eigvec = np.linalg.eig(np.real(Y))

            tell = 0
            offs = 0

            for m in range(N):
                VV = bigV[:, m]
                invVV = biginvV[:, m]
                if VV == 1:
                    gamm = VV
                else:
                    gamm = VV @ invVV
                if cindex[m] == 0:
                    Mmat2[offs] = gamm / (sk - self.all_poles[m])
                elif cindex[m] == 1:
                    Mmat2[offs] = gamm * (1 / (sk - self.all_poles[m]) + 1 / (sk - np.conj(self.all_poles[m])))
                else:
                    Mmat2[offs] = gamm * (1j / (sk - np.conj(self.all_poles[m])) - 1j / (sk - self.all_poles[m]))
                offs += 1

                tell = 0
            if Dflag:
                if VD == 1:
                    gamm = VD
                else:
                    gamm = VD[:, 0] @ invVD[0, :]
                Mmat2[offs] = gamm
            if V == 1:
                V1 = 1
            else:
                V1 = V[:, 0]
            qij = V1**2
            Q = qij
            if Q == 1:
                BB = Q * Mmat2
            else:
                BB = Q @ Mmat2

            # if parameter_type.lower() == "r":
            delz = np.abs(Z)
            violation = delz > 1
            # else:
            #     delz = np.real(Z)
            #     violation = delz < 0
            # We need to be a bit different with bigC due to D
            if violation:
                try:
                    bigB = np.vstack((bigB, np.real(BB) + np.imag(BB)))  # I'm putting -BB here, need to keep in mind
                except:
                    bigB = np.real(BB) + np.imag(BB)
                try:
                    bigC = np.vstack((bigC, 1 - np.real(delz) - np.imag(delz) - TOL))  # Make-ar thetta sense?
                except:
                    bigC = 1 - np.real(delz) - np.imag(delz) - TOL
                # 2. -Re(Y) + Re(dY) - Im(Y) + Im(dY) < 1
                try:
                    bigB = np.vstack((bigB, np.real(BB) + np.imag(BB)))  # I'm putting -BB here, need to keep in mind
                except:
                    bigB = np.real(BB) + np.imag(BB)
                try:
                    bigC = np.vstack((bigC, 1 + np.real(delz) + np.imag(delz) - TOL))  # Make-ar thetta sense?
                except:
                    bigC = 1 + np.real(delz) + np.imag(delz)
                # 3. -Re(Y) + Re(dY) + Im(Y) + Im(dY) < 1
                try:
                    bigB = np.vstack((bigB, np.real(BB) + np.imag(BB)))  # I'm putting -BB here, need to keep in mind
                except:
                    bigB = np.real(BB) + np.imag(BB)
                try:
                    bigC = np.vstack((bigC, 1 + np.real(delz) - np.imag(delz) - TOL))  # Make-ar thetta sense?
                except:
                    bigC = 1 - np.real(delz) + np.imag(delz) - TOL
                # 4. Re(Y) + Re(dY) - Im(Y) + Im(dY) < 1
                try:
                    bigB = np.vstack((bigB, np.real(BB) + np.imag(BB)))  # I'm putting -BB here, need to keep in mind
                except:
                    bigB = np.real(BB) + np.imag(BB)
                try:
                    bigC = np.vstack((bigC, 1 - np.real(delz) + np.imag(delz) - TOL))  # Make-ar thetta sense?
                except:
                    bigC = 1 - np.real(delz) + np.imag(delz) - TOL
                # try:
                #     bigB = np.vstack((bigB, -BB))
                # except:
                #     bigB = -BB.copy()
                # try:
                #     bigC = np.vstack((bigC, 1 - delz - TOL))
                # except:
                #     bigC = 1 - delz.copy() - TOL
                # else:
                #     try:
                #         bigB = np.vstack((bigB, BB))
                #     except:
                #         bigB = BB.copy()
                #     try:
                #         bigC = np.vstack((bigC, -TOL + delz))
                #     except:
                #         bigC = -TOL + delz.copy()
                viol_G.append(delz)
            # delz = np.real(Z)
            # if delz < 0:
            #     try:
            #         bigB = np.vstack((bigB, BB))
            #     except:
            #         bigB = BB.copy()
            #     try:
            #         bigC = np.vstack((bigC, -TOL + delz))
            #     except:
            #         bigC = -TOL + delz.copy()
            #     viol_G.append(delz)
        # if parameter_type == "r":
        # try:
        #     bigB = np.abs(bigB)
        # except:
        #     pass
        # bigB = np.sqrt(1 - np.square(np.real(bigB)) + np.square(np.imag(bigB)))
        if Dflag:  # This is the only place where I need to add the extra D condition
            # if parameter_type.lower() == "r":
            violation = np.abs(eigD) > 1
            # else:
            #     violation = eigD < 0
            if violation:
                dum = np.zeros((N + Dflag))
                dum[N] = 1
                # if parameter_type.lower() == "r":

                # First condition: D < 1 - tol
                try:
                    bigB = np.vstack((bigB, dum))
                except:
                    bigB = dum.copy()
                try:
                    bigC = np.vstack((bigC, 1 - eigD - TOL))
                except:
                    bigC = 1 - eigD - TOL

                # 2nd condition: D > -1 + tol
                try:
                    bigB = np.vstack((bigB, -dum))
                except:
                    bigB = -dum.copy()
                try:
                    bigC = np.vstack((bigC, 1 + eigD - TOL))
                except:
                    bigC = 1 + eigD - TOL

                # else:
                #     try:
                #         bigB = np.vstack((bigB, dum))
                #     except:
                #         bigB = dum.copy()
                #     try:
                #         bigC = np.vstack((bigC, -TOL + eigD))
                #     except:
                #         bigC = -TOL + eigD.copy()
                viol_G.append(eigD)
                viol_D.append(eigD)

        if len(bigB) == 0:
            return Cnew, Dnew
        ff = np.zeros(len(H))
        bigB = np.reshape(bigB, (len(bigC), len(H)))
        if len(bigC.shape) > 1:
            if bigC.shape == (1, 1):
                bigC = bigC[0]
            else:
                bigC = np.squeeze(bigC)

        # I have to take a look there what to do regarding the commented block below

        # if parameter_type != "r":
        # bigB = np.real(bigB)
        # if parameter_type == "r":
        #     bigB = -np.abs(bigB)
        #     # bigB = np.sqrt(1 - np.square(np.real(bigB)) + np.square(np.imag(bigB)))
        # else:
        #     bigB = np.real(bigB)

        for col in range(len(H)):
            if len(bigB) > 0:
                bigB[:, col] = bigB[:, col] / Escale[col]
        dx, f, xu, iterations, lagrangian, iact = quadprog.solve_qp(H, ff, -bigB.T, -bigC)
        dx = dx / Escale

        Cnew = C.copy()
        Dnew = D.copy()
        bigV = bigV[0]
        biginvV = biginvV[0]
        for m in range(N):
            if cindex[m] == 0:
                if isinstance(dx[m], float):
                    Diff1 = dx[m]
                    Cnew[:, m] = Cnew[:, m] + bigV[m] * Diff1 * biginvV[m]
                else:
                    Diff1 = np.diag(np.array(dx[m]))
                    Cnew[:, m] = Cnew[:, m] + bigV[:, m] @ Diff1 @ biginvV[:, m]
            elif cindex[m] == 1:
                GAMM1 = bigV[m]
                GAMM2 = bigV[m + 1]
                invGAMM1 = biginvV[m]
                invGAMM2 = biginvV[(m + 1)]

                R1 = np.real(C[:, m])
                R2 = np.imag(C[:, m])
                if isinstance(dx[m], float):
                    Diff1 = dx[m]
                    Diff2 = dx[m + 1]
                    R1new = R1 + GAMM1 * Diff1 * invGAMM1
                    R2new = R2 + GAMM2 * Diff2 * invGAMM2
                else:
                    Diff1 = np.diag(np.array(dx[m]))
                    Diff2 = np.diag(np.array(dx[m + 1]))
                    R1new = R1 + GAMM1 @ Diff1 @ invGAMM1
                    R2new = R2 + GAMM2 @ Diff2 @ invGAMM2
                Cnew[:, m] = R1new + 1j * R2new
                Cnew[:, m + 1] = R1new - 1j * R2new
        if Dflag:
            if isinstance(dx[N], float):
                DD = dx[N]
                Dnew = Dnew + VD * DD * invVD
            else:
                DD = np.diag(dx[N])
                Dnew = Dnew + VD @ DD @ invVD

            Dnew = (Dnew + Dnew.T) / 2
        for m in range(N):
            Cnew[:, m] = (Cnew[:, m] + Cnew[:, m].T) / 2
        return Cnew, Dnew

    def fitcalcPRE(self, sk, C, D):
        N = len(self.poles)
        Y = D + np.sum(C / (sk - self.all_poles))
        return Y

    def violextrema(self, violation_bands, parameter_type="y"):
        """
        Find the lowest eigenvalue within each violation band.
        """
        s_pass = []
        g_pass = []
        A, B, C, D, _ = self._get_ABCDE(for_passivity_enforcing=True)
        sss = []
        Nc = len(D)
        g_pass = 1e16
        smin = 0
        for m in range(len(violation_bands)):
            Nint = 21  # number of internal frequency samples resolving each interval
            w1 = violation_bands[m, 0]
            if violation_bands[m, 1] == 1e16:
                w2 = 2 * np.pi * 1e16  # expressing endless angular frequency (feels pointless)
            else:
                w2 = violation_bands[m, 1]

            s_pass1 = 1j * np.linspace(w1, w2, Nint)
            if w1 == 0:
                s_pass2 = 1j * np.logspace(-8, np.log10(w2), Nint)
            else:
                s_pass2 = 1j * np.logspace(np.log10(w1), np.log10(w2), Nint)
            s_pass = np.sort_complex(np.concatenate((s_pass1, s_pass2), axis=0))
            Nint *= 2
            EE = np.zeros((1, Nint))
            for k in range(len(s_pass)):
                Y = (C * (1.0 / (s_pass[k] - self.all_poles))) @ B + D
                if parameter_type.lower() == "r":
                    G = np.abs(Y)
                else:
                    G = np.real(Y)
                EV, T0 = np.linalg.eig(G)
                if k == 0:
                    old_T0 = np.zeros_like(T0)
                old_T0 = T0
                EE[:, k] = np.diag(EV)
            # Identifying violations, picking minima for s2
            s_pass_ind = np.zeros(shape=(len(s_pass)))
            # if parameter_type.lower() == "r":
            #     if np.max(EE[0]) > 1.0:
            #         s_pass_ind_2 = np.where(EE[0] == np.max(EE[0]))[0]
            #     else:
            #         s_pass_ind_2 = np.zeros(shape=(len(s_pass)))
            #     if isinstance(s_pass_ind_2, (np.ndarray, list)):
            #         # if len(s_pass_ind_2) > 1:
            #         s_pass_ind_2 = s_pass_ind_2[0]
            # else:
            for row in range(Nc):
                if parameter_type == "r":
                    if EE[row, 0] > 1:
                        s_pass_ind[0] = 1
                else:
                    if EE[row, 0] < 0:
                        s_pass_ind[0] = 1
            for k in range(1, len(s_pass) - 1):
                for row in range(Nc):
                    if parameter_type == "r":
                        if EE[row, k] > 1:
                            if EE[row, k] > EE[row, k - 1] and EE[row, k] > EE[row, k + 1]:
                                s_pass_ind[k] = 1
                    else:
                        if EE[row, k] < 0:  # Violation
                            if EE[row, k] < EE[row, k - 1] and EE[row, k] < EE[row, k + 1]:
                                s_pass_ind[k] = 1
            # if parameter_type == "r":
            #     flotti = np.where(EE == np.max(EE))[0]
            #     s_pass_ind[flotti] = 1
            # breakpoint()
            for s_p in s_pass[np.where(s_pass_ind == 1)[0]]:
                sss.append(s_p)
            # for s_p in s_pass[np.where(s_pass_ind == 1)[0]]:
            #     s.append(s_p)
            # sss.append(s_pass[s_pass_ind_2])
            if parameter_type == "r":
                dum = np.max(EE[0], axis=0)
                g_pass_2, ind = np.max(dum), np.where(dum == np.max(dum))[0][0]
                smin2 = s_pass[ind]  # Largest violation in interval
                g_pass_list = [g_pass, g_pass_2]
                g_pass = max(g_pass, g_pass_2)
                ind = g_pass_list.index(g_pass)
                dums = [smin, smin2]
                smin = dums[ind]
                g_pass = max(g_pass, np.max(np.max(EE)))
            else:
                dum = np.min(EE[0], axis=0)
                g_pass_2, ind = np.min(dum), np.where(dum == np.min(dum))[0][0]
                smin2 = s_pass[ind]  # Largest violation in interval
                g_pass_list = [g_pass, g_pass_2]
                g_pass = min(g_pass, g_pass_2)
                ind = g_pass_list.index(g_pass)
                dums = [smin, smin2]
                smin = dums[ind]
                g_pass = min(g_pass, np.min(np.min(EE)))
        s_pass = np.array(sss, dtype=complex)
        # if s_pass.shape == (1, 1):
        #     s_pass = s_pass[0]

        return s_pass, g_pass, smin

    def write_npz(self, path: str) -> None:
        """
        Writes the model parameters in :attr:`poles`, :attr:`residues`,
        :attr:`proportional_coeff` and :attr:`constant_coeff` to a labeled NumPy .npz file.

        Parameters
        ----------
        path : str
            Target path without filename for the export. The filename will be added automatically based on the network
            name in :attr:`network`

        Returns
        -------
        None

        See Also
        --------
        read_npz : Reads all model parameters from a .npz file

        Examples
        --------
        Load and fit the `Network`, then export the model parameters to a .npz file:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.write_npz('./data/')

        The filename depends on the network name stored in `nw_3port.name` and will have the prefix `coefficients_`, for
        example `coefficients_my3port.npz`. The coefficients can then be read using NumPy's load() function:

        >>> coeffs = numpy.load('./data/coefficients_my3port.npz')
        >>> poles = coeffs['poles']
        >>> residues = coeffs['residues']
        >>> prop_coeffs = coeffs['proportionals']
        >>> constants = coeffs['constants']

        Alternatively, the coefficients can be read directly into a new instance of `VectorFitting`, see
        :func:`read_npz`.
        """

        if self.poles is None:
            warnings.warn(
                "Nothing to export; Poles have not been fitted.",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        if self.residues is None:
            warnings.warn(
                "Nothing to export; Residues have not been fitted.",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        if self.proportional_coeff is None:
            warnings.warn(
                "Nothing to export; Proportional coefficients have not been fitted.",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        if self.constant_coeff is None:
            warnings.warn(
                "Nothing to export; Constants have not been fitted.",
                RuntimeWarning,
                stacklevel=2,
            )
            return

        filename = self.network.name

        logging.info("Exporting results as compressed NumPy array to {}".format(path))
        np.savez_compressed(
            os.path.join(path, "coefficients_{}".format(filename)),
            poles=self.poles,
            residues=self.residues,
            proportionals=self.proportional_coeff,
            constants=self.constant_coeff,
        )

    def read_npz(self, file: str) -> None:
        """
        Reads all model parameters :attr:`poles`, :attr:`residues`, :attr:`proportional_coeff` and
        :attr:`constant_coeff` from a labeled NumPy .npz file.

        Parameters
        ----------
        file : str
            NumPy .npz file containing the parameters. See notes.

        Returns
        -------
        None

        Raises
        ------
        ValueError
            If the shapes of the coefficient arrays in the provided file are not compatible.

        Notes
        -----
        The .npz file needs to include the model parameters as individual NumPy arrays (ndarray) labeled '*poles*',
        '*residues*', '*proportionals*' and '*constants*'. The shapes of those arrays need to match the network
        properties in :class:`network` (correct number of ports). Preferably, the .npz file was created by
        :func:`write_npz`.

        See Also
        --------
        write_npz : Writes all model parameters to a .npz file

        Examples
        --------
        Create an empty `VectorFitting` instance (with or without the fitted `Network`) and load the model parameters:

        >>> vf = skrf.VectorFitting(None)
        >>> vf.read_npz('./data/coefficients_my3port.npz')

        This can be useful to analyze or process a previous vector fit instead of fitting it again, which sometimes
        takes a long time. For example, the model passivity can be evaluated and enforced:

        >>> vf.passivity_enforce()
        """

        with np.load(file) as data:
            poles = data["poles"]

            # legacy support for exported residues
            if "zeros" in data:
                # old .npz file from deprecated write_npz() with residues called 'zeros'
                residues = data["zeros"]
            else:
                # new .npz file from current write_npz()
                residues = data["residues"]

            proportional_coeff = data["proportionals"]
            constant_coeff = data["constants"]

            n_ports = int(np.sqrt(len(constant_coeff)))
            n_resp = n_ports**2
            if np.shape(residues)[0] == np.shape(proportional_coeff)[0] == np.shape(constant_coeff)[0] == n_resp:
                self.poles = poles
                self.residues = residues
                self.proportional_coeff = proportional_coeff
                self.constant_coeff = constant_coeff
            else:
                raise ValueError(
                    "The shapes of the provided parameters are not compatible. The coefficient file needs "
                    "to contain NumPy arrays labled `poles`, `residues`, `proportionals`, and "
                    "`constants`. Their shapes must match the number of network ports and the number of "
                    "frequencies."
                )

    def get_model_response(self, i: int, j: int, freqs: Any = None) -> np.ndarray:
        """
        Returns one of the frequency responses :math:`H_{i+1,j+1}` of the fitted model :math:`H`.

        Parameters
        ----------
        i : int
            Row index of the response in the response matrix.

        j : int
            Column index of the response in the response matrix.

        freqs : list of float or ndarray or None, optional
            List of frequencies for the response plot. If None, the sample frequencies of the fitted network in
            :attr:`network` are used.

        Returns
        -------
        response : ndarray
            Model response :math:`H_{i+1,j+1}` at the frequencies specified in `freqs` (complex-valued Numpy array).

        Examples
        --------
        Get fitted S11 at 101 frequencies from 0 Hz to 10 GHz:

        >>> import skrf
        >>> vf = skrf.VectorFitting(skrf.data.ring_slot)
        >>> vf.vector_fit(3, 0)
        >>> s11_fit = vf.get_model_response(0, 0, numpy.linspace(0, 10e9, 101))
        """

        if self.poles is None:
            warnings.warn(
                "Returning a zero-vector; Poles have not been fitted.",
                RuntimeWarning,
                stacklevel=2,
            )
            return np.zeros_like(freqs)
        if self.residues is None:
            warnings.warn(
                "Returning a zero-vector; Residues have not been fitted.",
                RuntimeWarning,
                stacklevel=2,
            )
            return np.zeros_like(freqs)
        if self.proportional_coeff is None:
            warnings.warn(
                "Returning a zero-vector; Proportional coefficients have not been fitted.",
                RuntimeWarning,
                stacklevel=2,
            )
            return np.zeros_like(freqs)
        if self.constant_coeff is None:
            warnings.warn(
                "Returning a zero-vector; Constants have not been fitted.",
                RuntimeWarning,
                stacklevel=2,
            )
            return np.zeros_like(freqs)
        if freqs is None:
            freqs = np.linspace(np.amin(self.network.f), np.amax(self.network.f), 1000)

        s = 2j * np.pi * np.array(freqs)
        n_ports = int(np.sqrt(len(self.constant_coeff)))
        i_response = i * n_ports + j
        residues = self.residues[i_response]

        resp = self.proportional_coeff[i_response] * s + self.constant_coeff[i_response]
        for i, pole in enumerate(self.poles):
            if np.imag(pole) == 0.0:
                # real pole
                resp += residues[i] / (s - pole)
            else:
                # complex conjugate pole
                resp += residues[i] / (s - pole) + np.conjugate(residues[i]) / (s - np.conjugate(pole))
        return resp

    @check_plotting
    def plot(
        self,
        component: str,
        i: int = -1,
        j: int = -1,
        freqs: Any = None,
        parameter: str = "s",
        ax: mplt.Axes = None,
    ) -> mplt.Axes:
        """
        Plots the specified component of the parameter :math:`H_{i+1,j+1}` in the fit, where :math:`H` is
        either the scattering (:math:`S`), the impedance (:math:`Z`), or the admittance (:math:`H`) response specified
        in `parameter`.

        Parameters
        ----------
        component : str
            The component to be plotted. Must be one of the following items:
            ['db', 'mag', 'deg', 'deg_unwrap', 're', 'im'].
            `db` for magnitude in decibels,
            `mag` for magnitude in linear scale,
            `deg` for phase in degrees (wrapped),
            `deg_unwrap` for phase in degrees (unwrapped/continuous),
            `re` for real part in linear scale,
            `im` for imaginary part in linear scale.

        i : int, optional
            Row index of the response. `-1` to plot all rows.

        j : int, optional
            Column index of the response. `-1` to plot all columns.

        freqs : list of float or ndarray or None, optional
            List of frequencies for the response plot. If None, the sample frequencies of the fitted network in
            :attr:`network` are used. This only works if :attr:`network` is not `None`.

        parameter : str, optional
            The network representation to be used. This is only relevant for the plot of the original sampled response
            in :attr:`network` that is used for comparison with the fit. Must be one of the following items unless
            :attr:`network` is `None`: ['s', 'z', 'y'] for *scattering* (default), *impedance*, or *admittance*.

        ax : :class:`matplotlib.Axes` object or None
            matplotlib axes to draw on. If None, the current axes is fetched with :func:`gca()`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Raises
        ------
        ValueError
            If the `freqs` parameter is not specified while the Network in :attr:`network` is `None`.
            Also if `component` and/or `parameter` are not valid.
        """

        components = ["db", "mag", "deg", "deg_unwrap", "re", "im"]
        if component.lower() in components:
            if ax is None:
                ax = mplt.gca()

            if self.residues is None or self.poles is None:
                raise RuntimeError("Poles and/or residues have not been fitted. Cannot plot the model response.")

            n_ports = int(np.sqrt(np.shape(self.residues)[0]))

            if i == -1:
                list_i = range(n_ports)
            elif isinstance(i, int):
                list_i = [i]
            else:
                list_i = i

            if j == -1:
                list_j = range(n_ports)
            elif isinstance(j, int):
                list_j = [j]
            else:
                list_j = j

            if self.network is not None:
                # plot the original network response at each sample frequency (scatter plot)
                if parameter.lower() == "s":
                    responses = self.network.s
                elif parameter.lower() == "z":
                    responses = self.network.z
                elif parameter.lower() == "y":
                    responses = self.network.y
                else:
                    raise ValueError(
                        "The network parameter type is not valid, must be `s`, `z`, or `y`, "
                        "got `{}`.".format(parameter)
                    )

                i_samples = 0
                for i in list_i:
                    for j in list_j:
                        if i_samples == 0:
                            label = "Samples"
                        else:
                            label = "_nolegend_"
                        i_samples += 1

                        y_vals = None
                        if component.lower() == "db":
                            y_vals = 20 * np.log10(np.abs(responses[:, i, j]))
                        elif component.lower() == "mag":
                            y_vals = np.abs(responses[:, i, j])
                        elif component.lower() == "deg":
                            y_vals = np.rad2deg(np.angle(responses[:, i, j]))
                        elif component.lower() == "deg_unwrap":
                            y_vals = np.rad2deg(np.unwrap(np.angle(responses[:, i, j])))
                        elif component.lower() == "re":
                            y_vals = np.real(responses[:, i, j])
                        elif component.lower() == "im":
                            y_vals = np.imag(responses[:, i, j])

                        ax.scatter(self.network.f, y_vals, color="r", label=label)

                if freqs is None:
                    # get frequency array from the network
                    freqs = self.network.f

            if freqs is None:
                raise ValueError(
                    "Neither `freqs` nor `self.network` is specified. Cannot plot model response without any "
                    "frequency information."
                )

            # plot the fitted responses
            y_label = ""
            i_fit = 0
            for i in list_i:
                for j in list_j:
                    if i_fit == 0:
                        label = "Fit"
                    else:
                        label = "_nolegend_"
                    i_fit += 1

                    y_model = self.get_model_response(i, j, freqs)
                    y_vals = None
                    if component.lower() == "db":
                        y_vals = 20 * np.log10(np.abs(y_model))
                        y_label = "Magnitude (dB)"
                    elif component.lower() == "mag":
                        y_vals = np.abs(y_model)
                        y_label = "Magnitude"
                    elif component.lower() == "deg":
                        y_vals = np.rad2deg(np.angle(y_model))
                        y_label = "Phase (Degrees)"
                    elif component.lower() == "deg_unwrap":
                        y_vals = np.rad2deg(np.unwrap(np.angle(y_model)))
                        y_label = "Phase (Degrees)"
                    elif component.lower() == "re":
                        y_vals = np.real(y_model)
                        y_label = "Real Part"
                    elif component.lower() == "im":
                        y_vals = np.imag(y_model)
                        y_label = "Imaginary Part"

                    ax.plot(freqs, y_vals, color="k", label=label)

            ax.set_xlabel("Frequency (Hz)")
            ax.set_ylabel(y_label)
            ax.legend(loc="best")

            if i_fit == 1:
                ax.set_title("Response i={}, j={}".format(i, j))

            return ax
        else:
            raise ValueError(
                'The specified component ("{}") is not valid. Must be in {}.'.format(component, components)
            )

    def plot_s_db(self, *args, **kwargs) -> mplt.Axes:
        """
        Plots the magnitude in dB of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('db', *args, **kwargs)``.
        """

        return self.plot("db", *args, **kwargs)

    def plot_s_mag(self, *args, **kwargs) -> mplt.Axes:
        """
        Plots the magnitude in linear scale of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('mag', *args, **kwargs)``.
        """

        return self.plot("mag", *args, **kwargs)

    def plot_s_deg(self, *args, **kwargs) -> mplt.Axes:
        """
        Plots the phase in degrees of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('deg', *args, **kwargs)``.
        """

        return self.plot("deg", *args, **kwargs)

    def plot_s_deg_unwrap(self, *args, **kwargs) -> mplt.Axes:
        """
        Plots the unwrapped phase in degrees of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('deg_unwrap', *args, **kwargs)``.
        """

        return self.plot("deg_unwrap", *args, **kwargs)

    def plot_s_re(self, *args, **kwargs) -> mplt.Axes:
        """
        Plots the real part of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('re', *args, **kwargs)``.
        """

        return self.plot("re", *args, **kwargs)

    def plot_s_im(self, *args, **kwargs) -> mplt.Axes:
        """
        Plots the imaginary part of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('im', *args, **kwargs)``.
        """

        return self.plot("im", *args, **kwargs)

    @check_plotting
    def plot_s_singular(self, freqs: Any = None, ax: mplt.Axes = None) -> mplt.Axes:
        """
        Plots the singular values of the vector fitted S-matrix in linear scale.

        Parameters
        ----------
        freqs : list of float or ndarray or None, optional
            List of frequencies for the response plot. If None, the sample frequencies of the fitted network in
            :attr:`network` are used. This only works if :attr:`network` is not `None`.

        ax : :class:`matplotlib.Axes` object or None
            matplotlib axes to draw on. If None, the current axes is fetched with :func:`gca()`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Raises
        ------
        ValueError
            If the `freqs` parameter is not specified while the Network in :attr:`network` is `None`.
        """

        if freqs is None:
            if self.network is None:
                raise ValueError(
                    "Neither `freqs` nor `self.network` is specified. Cannot plot model response without any "
                    "frequency information."
                )
            else:
                freqs = self.network.f

        if ax is None:
            ax = mplt.gca()

        # get system matrices of state-space representation
        A, B, C, D, E = self._get_ABCDE()

        n_ports = np.shape(D)[0]
        singvals = np.zeros((n_ports, len(freqs)))

        # calculate and save singular values for each frequency
        u, sigma, vh = np.linalg.svd(self._get_s_from_ABCDE(freqs, A, B, C, D, E))

        # plot the frequency response of each singular value
        for n in range(n_ports):
            ax.plot(freqs, sigma[:, n], label=r"$\sigma_{}$".format(n + 1))
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("Magnitude")
        ax.legend(loc="best")
        return ax

    @check_plotting
    def plot_convergence(self, ax: mplt.Axes = None) -> mplt.Axes:
        """
        Plots the history of the model residue parameter **d_res** during the iterative pole relocation process of the
        vector fitting, which should eventually converge to a fixed value. Additionally, the relative change of the
        maximum singular value of the coefficient matrix **A** are plotted, which serve as a convergence indicator.

        Parameters
        ----------
        ax : :class:`matplotlib.Axes` object or None
            matplotlib axes to draw on. If None, the current axes is fetched with :func:`gca()`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.
        """

        if ax is None:
            ax = mplt.gca()

        ax.semilogy(
            np.arange(len(self.delta_max_history)) + 1,
            self.delta_max_history,
            color="darkblue",
        )
        ax.set_xlabel("Iteration step")
        ax.set_ylabel("Max. relative change", color="darkblue")
        ax2 = ax.twinx()
        ax2.plot(
            np.arange(len(self.d_res_history)) + 1,
            self.d_res_history,
            color="orangered",
        )
        ax2.set_ylabel("Residue", color="orangered")
        return ax

    @check_plotting
    def plot_passivation(self, ax: mplt.Axes = None) -> mplt.Axes:
        """
        Plots the history of the greatest singular value during the iterative passivity enforcement process, which
        should eventually converge to a value slightly lower than 1.0 or stop after reaching the maximum number of
        iterations specified in the class variable :attr:`max_iterations`.

        Parameters
        ----------
        ax : :class:`matplotlib.Axes` object or None
            matplotlib axes to draw on. If None, the current axes is fetched with :func:`gca()`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.
        """

        if ax is None:
            ax = mplt.gca()

        ax.plot(np.arange(len(self.history_max_sigma)) + 1, self.history_max_sigma)
        ax.set_xlabel("Iteration step")
        ax.set_ylabel("Max. singular value")
        return ax

    def write_spice_subcircuit_s(self, file: str) -> None:
        """
        Creates an equivalent N-port SPICE subcircuit based on its vector fitted S parameter responses.

        Parameters
        ----------
        file : str
            Path and filename including file extension (usually .sp) for the SPICE subcircuit file.

        Returns
        -------
        None

        Notes
        -----
        In the SPICE subcircuit, all ports will share a common reference node (global SPICE ground on node 0). The
        equivalent circuit uses linear dependent current sources on all ports, which are controlled by the currents
        through equivalent admittances modelling the parameters from a vector fit. This approach is based on [#]_.

        Examples
        --------
        Load and fit the `Network`, then export the equivalent SPICE subcircuit:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.write_spice_subcircuit_s('/my3port_model.sp')

        References
        ----------
        .. [#] G. Antonini, "SPICE Equivalent Circuits of Frequency-Domain Responses", IEEE Transactions on
            Electromagnetic Compatibility, vol. 45, no. 3, pp. 502-512, August 2003,
            DOI: https://doi.org/10.1109/TEMC.2003.815528
        """

        # list of subcircuits for the equivalent admittances
        subcircuits = []

        # provides a unique SPICE subcircuit identifier (X1, X2, X3, ...)
        def get_new_subckt_identifier():
            subcircuits.append("X{}".format(len(subcircuits) + 1))
            return subcircuits[-1]

        # use engineering notation for the numbers in the SPICE file (1000 --> 1k)
        formatter = EngFormatter(sep="", places=3, usetex=False)
        # replace "micron" sign by "u" and "mega" sign by "meg"
        letters_dict = formatter.ENG_PREFIXES
        letters_dict.update({-6: "u", 6: "meg"})
        formatter.ENG_PREFIXES = letters_dict

        with open(file, "w") as f:
            # write title line
            f.write("* EQUIVALENT CIRCUIT FOR VECTOR FITTED S-MATRIX\n")
            f.write("* Created using scikit-rf vectorFitting.py\n")
            f.write("*\n")

            # define the complete equivalent circuit as a subcircuit with one input node per port
            # those port nodes are labeled p1, p2, p3, ...
            # all ports share a common node for ground reference (node 0)
            str_input_nodes = ""
            for n in range(self.network.nports):
                str_input_nodes += "p{} ".format(n + 1)

            f.write(".SUBCKT s_equivalent {}\n".format(str_input_nodes))

            for n in range(self.network.nports):
                f.write("*\n")
                f.write("* port {}\n".format(n + 1))
                # add port reference impedance z0 (has to be resistive, no imaginary part)
                f.write("R{} a{} 0 {}\n".format(n + 1, n + 1, np.real(self.network.z0[0, n])))

                # add dummy voltage sources (V=0) to measure the input current
                f.write("V{} p{} a{} 0\n".format(n + 1, n + 1, n + 1))

                # CCVS and VCVS driving the transfer admittances with a = V/2/sqrt(Z0) + I/2*sqrt(Z0)
                # In
                f.write("H{} nt{} nts{} V{} {}\n".format(n + 1, n + 1, n + 1, n + 1, np.real(self.network.z0[0, n])))
                # Vn
                f.write("E{} nts{} 0 p{} 0 {}\n".format(n + 1, n + 1, n + 1, 1))

                for j in range(self.network.nports):
                    f.write("* transfer network for s{}{}\n".format(n + 1, j + 1))

                    # stacking order in VectorFitting class variables:
                    # s11, s12, s13, ..., s21, s22, s23, ...
                    i_response = n * self.network.nports + j

                    # add CCCS to generate the scattered current I_nj at port n
                    # control current is measured by the dummy voltage source at the transfer network Y_nj
                    # the scattered current is injected into the port (source positive connected to ground)
                    f.write(
                        "F{}{} 0 a{} V{}{} {}\n".format(
                            n + 1,
                            j + 1,
                            n + 1,
                            n + 1,
                            j + 1,
                            formatter(1 / np.real(self.network.z0[0, n])),
                        )
                    )
                    f.write(
                        "F{}{}_inv a{} 0 V{}{}_inv {}\n".format(
                            n + 1,
                            j + 1,
                            n + 1,
                            n + 1,
                            j + 1,
                            formatter(1 / np.real(self.network.z0[0, n])),
                        )
                    )

                    # add dummy voltage source (V=0) in series with Y_nj to measure current through transfer admittance
                    f.write("V{}{} nt{} nt{}{} 0\n".format(n + 1, j + 1, j + 1, n + 1, j + 1))
                    f.write("V{}{}_inv nt{} nt{}{}_inv 0\n".format(n + 1, j + 1, j + 1, n + 1, j + 1))

                    # add corresponding transfer admittance Y_nj, which is modulating the control current
                    # the transfer admittance is a parallel circuit (sum) of individual admittances
                    f.write("* transfer admittances for S{}{}\n".format(n + 1, j + 1))

                    # start with proportional and constant term of the model
                    # H(s) = d + s * e  model
                    # Y(s) = G + s * C  equivalent admittance
                    g = self.constant_coeff[i_response]
                    c = self.proportional_coeff[i_response]

                    # add R for constant term
                    if g < 0:
                        f.write("R{}{} nt{}{}_inv 0 {}\n".format(n + 1, j + 1, n + 1, j + 1, formatter(np.abs(1 / g))))
                    elif g > 0:
                        f.write("R{}{} nt{}{} 0 {}\n".format(n + 1, j + 1, n + 1, j + 1, formatter(1 / g)))

                    # add C for proportional term
                    if c < 0:
                        f.write("C{}{} nt{}{}_inv 0 {}\n".format(n + 1, j + 1, n + 1, j + 1, formatter(np.abs(c))))
                    elif c > 0:
                        f.write("C{}{} nt{}{} 0 {}\n".format(n + 1, j + 1, n + 1, j + 1, formatter(c)))

                    # add pairs of poles and residues
                    for i_pole in range(len(self.poles)):
                        pole = self.poles[i_pole]
                        residue = self.residues[i_response, i_pole]
                        node = get_new_subckt_identifier() + " nt{}{}".format(n + 1, j + 1)

                        if np.real(residue) < 0.0:
                            # multiplication with -1 required, otherwise the values for RLC would be negative
                            # this gets compensated by inverting the transfer current direction for this subcircuit
                            residue = -1 * residue
                            node += "_inv"

                        if np.imag(pole) == 0.0:
                            # real pole; add rl_admittance
                            l = 1 / np.real(residue)
                            r = -1 * np.real(pole) / np.real(residue)
                            f.write(node + " 0 rl_admittance res={} ind={}\n".format(formatter(r), formatter(l)))
                        else:
                            # complex pole of a conjugate pair; add rcl_vccs_admittance
                            l = 1 / (2 * np.real(residue))
                            b = -2 * (np.real(residue) * np.real(pole) + np.imag(residue) * np.imag(pole))
                            r = -1 * np.real(pole) / np.real(residue)
                            c = 2 * np.real(residue) / (np.abs(pole) ** 2)
                            gm_add = b * l * c
                            if gm_add < 0:
                                m = -1
                            else:
                                m = 1
                            f.write(
                                node
                                + " 0 rcl_vccs_admittance res={} cap={} ind={} gm={} mult={}\n".format(
                                    formatter(r),
                                    formatter(c),
                                    formatter(l),
                                    formatter(np.abs(gm_add)),
                                    int(m),
                                )
                            )

            f.write(".ENDS s_equivalent\n")

            f.write("*\n")

            # subcircuit for an active RCL+VCCS equivalent admittance Y(s) of a complex-conjugate pole-residue pair H(s)
            # Residue: c = c' + j * c"
            # Pole: p = p' + j * p"
            # H(s)  = c / (s - p) + conj(c) / (s - conj(p))
            #       = (2 * c' * s - 2 * (c'p' + c"p")) / (s ** 2 - 2 * p' * s + |p| ** 2)
            # Y(S)  = (1 / L * s + b) / (s ** 2 + R / L * s + 1 / (L * C))
            f.write(".SUBCKT rcl_vccs_admittance n_pos n_neg res=1k cap=1n ind=100p gm=1m mult=1\n")
            f.write("L1 n_pos 1 {ind}\n")
            f.write("C1 1 2 {cap}\n")
            f.write("R1 2 n_neg {res}\n")
            f.write("G1 n_pos n_neg 1 2 {gm} m={mult}\n")
            f.write(".ENDS rcl_vccs_admittance\n")

            f.write("*\n")

            # subcircuit for a passive RL equivalent admittance Y(s) of a real pole-residue pair H(s)
            # H(s) = c / (s - p)
            # Y(s) = 1 / L / (s + s * R / L)
            f.write(".SUBCKT rl_admittance n_pos n_neg res=1k ind=100p\n")
            f.write("L1 n_pos 1 {ind}\n")
            f.write("R1 1 n_neg {res}\n")
            f.write(".ENDS rl_admittance\n")
