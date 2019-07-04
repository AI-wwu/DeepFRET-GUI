import multiprocessing
multiprocessing.freeze_support()

import matplotlib
import sklearn.neighbors

import itertools
from ui.misc import ProgressBar
from typing import Union, Tuple
import scipy.signal
import numpy as np
import pandas as pd
import scipy.signal
import scipy.optimize
import scipy.stats
import sklearn.cluster
import sklearn.mixture
import hmmlearn.hmm


def leastsq_line(x: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    """
    Return the least-squares solution to a linear matrix equation.
    """
    A = np.vstack([x, np.ones(len(x))]).T
    slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
    return slope, intercept


def correctDA(intensities, alpha=0, delta=0):
    """
    Calculates corrected Dexc-Aem intensity, for use in E and S calculations
    """
    grn_int, grn_bg, acc_int, acc_bg, red_int, red_bg = intensities

    I_DD = grn_int - grn_bg
    I_DA = acc_int - acc_bg
    I_AA = red_int - red_bg
    F_DA = I_DA - (alpha * I_DD) - (delta * I_AA)

    return F_DA, I_DD, I_DA, I_AA


def calcFRET(intensities, alpha=0, delta=0, clip_range=(-0.3, 1.3)):
    """
    Calculates raw FRET efficiency from donor (Dexc-Dem) and acceptor (Dexc-Aem).
    Note that iSMS has the option of subtracting background or not, and calculate E (apparent E) from that.
    """

    cmin, cmax = clip_range

    F_DA, I_DD, I_DA, I_AA = correctDA(intensities, alpha, delta)

    E = F_DA / (I_DD + F_DA)
    E = np.clip(E, cmin, cmax, out=E)
    E = np.reshape(E, -1)

    return E


def calcStoi(
    intensities, alpha=0, delta=0, beta=1, gamma=1, clip_range=(-0.3, 1.3)
):
    """
    Calculates raw calcStoi from donor (Dexc-Dem), acceptor (Dexc-Aem) and direct emission of acceptor ("ALEX", Aexc-Aem)
    Note that iSMS has the option of subtracting background or not, and calculate S (apparent S) from that.
    """
    cmin, cmax = clip_range

    F_DA, I_DD, I_DA, I_AA = correctDA(intensities, alpha, delta)

    inv_beta = 1 / beta

    S = (gamma * I_DD + F_DA) / (gamma * I_DD + F_DA + (inv_beta * I_AA))
    S = np.clip(S, cmin, cmax, out=S)
    S = np.reshape(S, -1)

    return S


def correctedES(intensities, alpha, delta, beta, gamma, clip_range=(-0.3, 1.3)):
    """
    Calculates the fully corrected FRET and stoichiometry, given all the correction factors.
    This is only used for the combined 2D histogram, not for single traces.
    """
    cmin, cmax = clip_range

    F_DA, I_DD, I_DA, I_AA = correctDA(intensities, alpha, delta)
    F_DD = gamma * I_DD
    F_AA = (1 / beta) * I_AA

    E = F_DA / (F_DA + F_DD)
    S = (F_DA + F_DD) / (F_DD + F_DA + F_AA)

    E = np.clip(E, cmin, cmax, out=E)
    S = np.clip(S, cmin, cmax, out=S)

    return E, S


def dropBleachedFrames(
    intensities, bleaches, max_frames=None, alpha=0, delta=0, beta=1, gamma=1
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Removes all frames after bleaching
    """
    bleach = min_real(bleaches)

    if beta == 1 and gamma == 1:
        E_trace = calcFRET(intensities, alpha, delta)
        S_trace = calcStoi(intensities, alpha, delta)
    else:
        E_trace, S_trace = correctedES(intensities, alpha, delta, beta, gamma)

    E_trace_ = E_trace[:bleach][:max_frames]
    S_trace_ = S_trace[:bleach][:max_frames]

    return E_trace_, S_trace_


def alphaFactor(DD, DA):
    """
    Alpha factor for donor-only population.
    Use the donor and acceptor intensities minus background.
    """
    E_app = DA / (DD + DA)
    alpha = np.median(E_app / (1 - E_app))
    return alpha


def deltaFactor(DD, DA, AA):
    """
    Delta factor for acceptor-only population.
    Use the donor and acceptor intensities minus background.
    """
    S_app = (DD + DA) / (DD + DA + AA)
    delta = np.median(S_app / (1 - S_app))
    return delta


def betaGammaFactor(E_app, S_app):
    """
    Calculates global beta and gamma factors from apparent E and S (alpha and delta already applied).
    """
    # Sanitize inputs to avoid values tending towards infinity
    cond = (S_app > 0.3) & (S_app < 0.7)
    E_app = E_app[cond]
    S_app = S_app[cond]

    slope, intercept = leastsq_line(x=E_app, y=1 / S_app)

    beta = intercept + slope - 1
    gamma = (intercept - 1) / (intercept + slope - 1)

    if np.any(np.isnan((beta, gamma, slope, intercept))):
        beta, gamma = 1, 1

    return beta, gamma


def trimES(E: list, S: list):
    """
    Trims out-of-range values of E/S values
    """
    E, S = np.array(E), np.array(S)
    idx = (S > -0.3) & (S < 1.3)
    E, S = E[idx], S[idx]
    return E, S


def fit_hmm(X, y, n_components_max=3, bic_tol=20):
    """
    Parameters
    ----------
    X:
        Timeseries of shape (-1, n_features), e.g. DD/DA
    y:
        Observed y, e.g. FRET
    bic_tol:
        Heuristic tolerance value for BIC to prevent overfitting. Increase to punish overfitting more.
    exclude_endpoint_transitions:
        Whether to calculate lifetimes for first and last transition
        Normally not done, because the transitions are artificially shortened
        by the observation window

    Returns
    -------
    Hidden y
    """

    def _bic(data, k_states, logL):
        """Bayesian Information Criterion"""
        p = k_states * 4 + k_states ** 2
        N = len(data)
        log = np.log
        return -2 * logL + p * log(N)

    def _heuristic_bic(s, tol):
        """
        Heuristic for determining the lowest reasonable BIC by setting a tolerance for the difference in BIC
        """
        s = np.array(s)
        best_idx, sec_best_idx = np.argpartition(s, 2)[:2]

        if sec_best_idx < best_idx:
            if s[best_idx] + tol > s[sec_best_idx]:
                best_idx = sec_best_idx
        return best_idx

    scores, models = [], []
    for components in range(1, n_components_max + 1):
        model = hmmlearn.hmm.GMMHMM(
            n_components=components,
            covariance_type="full",
            n_iter=1000,
            algorithm="viterbi",
        )
        model.fit(X)
        try:
            logL = model.score(
                X
            )  # This thing is numerically unstable for some reason
            BIC = _bic(X, logL=logL, k_states=components)
        except ValueError:
            BIC = 10e5

        models.append(model)
        scores.append(BIC)

    idx_best = _heuristic_bic(scores, tol=bic_tol)
    best_model = models[idx_best]

    hf = pd.DataFrame()
    hf["state"] = best_model.predict(X)
    hf["y_obs"] = y
    hf["y_fit"] = hf.groupby(["state"], as_index=False)["y_obs"].transform(
        "median"
    )
    hf["time"] = hf["y_fit"].index + 1

    # Calculate lifetimes now, by making a copy to work on
    lf = hf.copy()

    # # Find y_after from y_before
    lf["y_after"] = np.roll(lf["y_fit"], -1)

    # Find out when there's a change in state, depending on the minimum transition size set
    lf["state_jump"] = lf["y_fit"].transform(
        lambda group: (abs(group.diff()) > 0).cumsum()
    )

    # Drop duplicates
    lf.drop_duplicates(subset="state_jump", keep="last", inplace=True)

    # Find the difference for every time
    lf["lifetime"] = np.append(np.nan, np.diff(lf["time"]))

    lf.rename(columns={"y_fit": "y_before"}, inplace=True)
    lf = lf[["y_before", "y_after", "lifetime"]]
    lf = lf[:-1]

    idealized = hf["y_fit"].values
    idealized_idx = hf["time"].values
    lifetimes = lf

    return idealized, idealized_idx, lifetimes


def fit_gaussian_mixture(arr, k_states):
    """
    Fits k gaussians to a set of data.

    Parameters
    ----------
    arr:
        Input data (wil be unravelled to single-sample shape)
    k_states:
        Maximum number of states to test for:
    auto:
        If true, best number of gaussians is is based on minimized BIC, and k_states becomes the upper limit to fit.
        If false, only k states are tested.

    Returns
    -------
    Parameters zipped as (means, sigmas, weights), BICs and best k if found by BIC method
    Returned as a dictionary to avoid unpacking the wrong things when having few parameters

    Examples
    --------
    # For plotting the returned parameters:
    for i, params in enumerate(gaussfit_params):
        m, s, w = params

        ax.plot(xpts, w * scipy.stats.norm.pdf(xpts, m, s))
        sum.append(np.array(w * stats.norm.pdf(xpts, m, s)))

    joint = np.sum(sum, axis = 0)
    ax.plot(xpts, joint, color = "black", alpha = 0.05)

    """
    if len(arr) < 2:
        return None, None

    arr = arr.reshape(-1, 1)

    bics_ = []
    gs_ = []
    best_k = None
    if type(k_states) == range:
        for k in k_states:
            g = sklearn.mixture.GaussianMixture(n_components=k)
            g.fit(arr)
            bic = g.bic(arr)
            gs_.append(g)
            bics_.append(bic)

        best_k = np.argmin(bics_).astype(int) + 1
        g = sklearn.mixture.GaussianMixture(n_components=best_k)
    else:
        g = sklearn.mixture.GaussianMixture(n_components=k_states)

    g.fit(arr)

    weights = g.weights_.ravel()
    means = g.means_.ravel()
    sigs = np.sqrt(g.covariances_.ravel())

    params = [(m, s, w) for m, s, w in zip(means, sigs, weights)]
    params = sorted(params, key=lambda tup: tup[0])

    return dict(params=params, bics=bics_, best_k=best_k)


def sample_max_normalize_3d(X):
    """
    Sample-wise max-value normalization of 3D array (tensor).
    This is not feature-wise normalization, to keep the ratios between features intact!
    """
    if len(X.shape) == 2:
        X = X[np.newaxis, :, :]
    assert len(X.shape) == 3
    arr_max = np.max(X, axis=(1, 2), keepdims=True)
    X = X / arr_max
    return np.squeeze(X)


def seq_probabilities(yi, skip_threshold=0.5, skip_column=0):
    """
    Calculates class-wise probabilities over the entire trace for a one-hot encoded
    sequence prediction. Skips values where the first value is above threshold (bleaching)
    """
    assert len(yi.shape) == 2

    p = yi[yi[:, skip_column] < skip_threshold]  # Discard rows above threshold
    if len(p) > 0:
        p = p.sum(axis=0) / len(p)  # Sum rows for each class
        p = p / p.sum()  # Normalize probabilities to 1
        # p[skip_column] = 0
    else:
        p = np.zeros(yi.shape[1])

    confidence = p[[2, 3]].sum()
    return p, confidence


def find_bleach(p_bleach, threshold=0.5, window=7):
    """
    Finds bleaching given a list of frame-wise probabilities.
    The majority of datapoints in a given window must be above the threshold
    """
    is_bleached = scipy.signal.medfilt(p_bleach > threshold, window)
    bleach_frame = np.argmax(is_bleached)
    if bleach_frame == 0:
        bleach_frame = None
    return bleach_frame


def predict_single(xi, model):
    """
    Fixes dimensions to allow allow Keras prediction on a single sample

    np.newaxis adds an empty dimension to create a single-sample tensor, and
    np.squeeze removes the empty dimension after prediction
    """
    yi = np.squeeze(model.predict(xi[np.newaxis, :, :]))
    return yi


def predict_batch(X, model, batch_size=256, progressbar: ProgressBar = None):
    """
    Predicts on batches in a loop
    """
    batches = (X.shape[0] // batch_size) + 1
    y_pred = np.zeros(shape=(X.shape[0], X.shape[1], 6))

    for i in range(batches):
        i1 = i * batch_size
        i2 = i1 + batch_size
        y_pred[i1:i2, :, :] = model.predict_on_batch(X[i1:i2])

        if progressbar is not None:
            progressbar.increment()

            if progressbar.wasCanceled():
                break

    return y_pred


def count_adjacent_values(arr):
    """
    Returns start index and length of segments of equal values.

    Example for plotting several axvspans:
    --------------------------------------
    adjs, lns = lib.count_adjacent_true(score)
    t = np.arange(1, len(score) + 1)

    for ax in axes:
        for starts, ln in zip(adjs, lns):
            alpha = (1 - np.mean(score[starts:starts + ln])) * 0.15
            ax.axvspan(xmin = t[starts], xmax = t[starts] + (ln - 1), alpha = alpha, color = "red", zorder = -1)
    """
    arr = arr.ravel()

    n = 0
    same = [(g, len(list(l))) for g, l in itertools.groupby(arr)]
    starts = []
    lengths = []
    for v, l in same:
        _len = len(arr[n : n + l])
        _idx = n
        n += l
        lengths.append(_len)
        starts.append(_idx)
    return starts, lengths


def all_equal(iterator):
    """
    Checks if all elements in an iterator are equal
    """
    iterator = iter(iterator)
    try:
        first = next(iterator)
    except StopIteration:
        return True
    return all(first == rest for rest in iterator)


def min_real(ls) -> Union[Union[int, float], None]:
    """
    Finds the minimum value of a list, ignoring None.
    Returns None if all values are None
    """
    ls = np.array(ls)
    ls = ls[ls != None]
    val = None if len(ls) == 0 else min(ls)
    return val


def contour2D(
    xdata,
    ydata,
    bandwidth=0.1,
    n_colors=2,
    kernel="gaussian",
    extend_grid=1,
    shade_lowest=False,
    resolution=100,
    cbins="auto",
):
    """
    Calculates the 2D kernel density estimate for a dataset.

    Valid kernels for sklearn are
    ['gaussian' | 'tophat' | 'epanechnikov' | 'exponential' | 'linear' | 'cosine']

    Example
    -------
    X, Y, Z, levels = countour_2d(x, y, shade_lowest = False)

    fig, ax = plt.subplots()
    c = ax.contourf(X,Y,Z, levels=levels, cmap = "inferno")

    Alternatively, unpack like

    contour = countour_2d(xdata, ydata)
    c = ax.contourf(*contour)

    For optional colorbar, add fig.colorbar(c)
    """

    if kernel == "epa":
        kernel = "epanechnikov"

    if bandwidth == "auto":
        bandwidth = (len(xdata) * 4 / 4.0) ** (-1.0 / (6))

    # Stretch the min/max values to make sure that the KDE goes beyond the outermost points
    meanx = np.mean(xdata) * extend_grid
    meany = np.mean(ydata) * extend_grid

    # Create a grid for KDE
    X, Y = np.mgrid[
        min(xdata) - meanx : max(xdata) + meanx : complex(resolution),
        min(ydata) - meany : max(ydata) + meany : complex(resolution),
    ]

    positions = np.vstack([X.ravel(), Y.ravel()])
    values = np.vstack([xdata, ydata])

    # Define KDE with specified bandwidth
    kernel_sk = sklearn.neighbors.KernelDensity(
        kernel=kernel, bandwidth=bandwidth
    ).fit(list(zip(*values)))
    Z = np.exp(kernel_sk.score_samples(list(zip(*positions))))

    Z = np.reshape(Z.T, X.shape)

    if not shade_lowest:
        n_colors += 1

    locator = matplotlib.ticker.MaxNLocator(n_colors, min_n_ticks=n_colors)

    if type(cbins) == np.ndarray:
        levels = cbins
    elif cbins is "auto":
        levels = locator.tick_values(Z.min(), Z.max())
    else:
        raise ValueError

    if not shade_lowest:
        levels = levels[1:]

    return X, Y, Z, levels


def estimate_bw(n, d, factor):
    """
    Estimate optimal bandwidth parameter, based on Silverman's rule (see SciPy docs)

    Parameters
    ----------
    n:
        Number of data points
    d:
        Number of dimensions
    factor:
        Multiply by constant for better adjustment

    Returns
    -------
    Optimal smoothing bandwidth
    """
    return ((n * (d + 2) / 4.0) ** (-1.0 / (d + 4))) * factor ** 2