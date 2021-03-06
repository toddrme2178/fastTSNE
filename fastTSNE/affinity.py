import logging

import numpy as np
from scipy.sparse import csr_matrix

from . import _tsne
from .nearest_neighbors import BallTree, NNDescent, KNNIndex, VALID_METRICS

log = logging.getLogger(__name__)


class Affinities:
    """Compute the affinities among some initial data and new data.

    t-SNE takes as input an affinity matrix :math:`P`, and does not really care
    about anything else about the data. This means we can use t-SNE for any data
    where we are able to express interactions between samples with an affinity
    matrix.

    Attributes
    ----------
    P: array_like
        The affinity matrix expressing interactions between all data samples.

    """

    def __init__(self):
        self.P = None

    def to_new(self, data):
        """Compute the affinities of new data points to the existing ones.

        This is especially useful for `transform` where we need the conditional
        probabilities from the existing to the new data.

        """


class PerplexityBasedNN(Affinities):
    """Compute affinities using the nearest neighbors defined by perplexity.

    Parameters
    ----------
    data: np.ndarray
        The data matrix.
    perplexity: float
        Perplexity can be thought of as the continuous :math:`k` number of
        neighbors to consider for each data point. To avoid confusion, note that
        perplexity linearly impacts runtime.
    method: str
        Specifies the nearest neighbor method to use. Can be either ``exact`` or
        ``approx``. ``exact`` uses space partitioning binary trees from
        scikit-learn while ``approx`` makes use of nearest neighbor descent.
        Note that ``approx`` has a bit of overhead and will be slower on smaller
        data sets than exact search.
    metric: str
        The metric to be used to compute affinities between points in the
        original space.
    metric_params: Optional[dict]
        Additional keyword arguments for the metric function.
    symmetrize: bool
        Symmetrize affinity matrix. Standard t-SNE symmetrizes the interactions
        but when embedding new data, symmetrization is not performed.
    n_jobs: int
        The number of jobs to run in parallel. This follows the scikit-learn
        convention, ``-1`` meaning all processors, ``-2`` meaning all but one
        processor and so on.
    random_state: Optional[Union[int, RandomState]]
        The random state parameter follows the convention used in scikit-learn.
        If the value is an int, random_state is the seed used by the random
        number generator. If the value is a RandomState instance, then it will
        be used as the random number generator. If the value is None, the random
        number generator is the RandomState instance used by `np.random`.

    """

    def __init__(self, data, perplexity=30, method='exact', metric='euclidean',
                 metric_params=None, symmetrize=True, n_jobs=1, random_state=None):
        self.n_samples = data.shape[0]
        self.perplexity = self.check_perplexity(perplexity)

        self.knn_index = build_knn_index(data, method, metric, metric_params, n_jobs, random_state)

        # Find and store the nearest neighbors so we can reuse them if the
        # perplexity is ever lowered
        k_neighbors = min(self.n_samples - 1, int(3 * self.perplexity))
        self.__neighbors, self.__distances = self.knn_index.query_train(data, k=k_neighbors)

        self.P = joint_probabilities_nn(self.__neighbors, self.__distances, [self.perplexity],
                                        symmetrize=symmetrize, n_jobs=n_jobs)

        self.n_jobs = n_jobs

    def set_perplexity(self, new_perplexity):
        # If the value hasn't changed, there's nothing to do
        if new_perplexity == self.perplexity:
            return
        # Verify that the perplexity isn't too large
        new_perplexity = self.check_perplexity(new_perplexity)
        # Recompute the affinity matrix
        k_neighbors = min(self.n_samples - 1, int(3 * new_perplexity))
        if k_neighbors > self.__neighbors.shape[1]:
            raise RuntimeError(
                'The desired perplexity `%.2f` is larger than the initial one '
                'used. This would need to recompute the nearest neighbors, '
                'which is not efficient. Please create a new `%s` instance '
                'with the increased perplexity.' % (
                    new_perplexity, self.__class__.__name__))

        self.perplexity = new_perplexity
        self.P = joint_probabilities_nn(
            self.__neighbors[:, :k_neighbors], self.__distances[:, :k_neighbors],
            [self.perplexity], symmetrize=True, n_jobs=self.n_jobs,
        )

    def to_new(self, data, perplexity=None, return_distances=False):
        perplexity = perplexity if perplexity is not None else self.perplexity
        perplexity = self.check_perplexity(perplexity)
        k_neighbors = min(self.n_samples - 1, int(3 * perplexity))

        neighbors, distances = self.knn_index.query(data, k_neighbors)

        P = joint_probabilities_nn(
            neighbors, distances, [perplexity], symmetrize=False,
            n_reference_samples=self.n_samples, n_jobs=self.n_jobs,
        )

        if return_distances:
            return P, neighbors, distances

        return P

    def check_perplexity(self, perplexity):
        """Check for valid perplexity value."""
        if perplexity <= 0:
            raise ValueError('Perplexity must be >=0. %.2f given' % perplexity)

        if self.n_samples - 1 < 3 * perplexity:
            old_perplexity, perplexity = perplexity, (self.n_samples - 1) / 3
            log.warning('Perplexity value %d is too high. Using perplexity '
                        '%.2f instead' % (old_perplexity, perplexity))

        return perplexity


def build_knn_index(data, method, metric, metric_params=None, n_jobs=1, random_state=None):
    methods = {'exact': BallTree, 'approx': NNDescent}
    if isinstance(method, KNNIndex):
        knn_index = method

    elif method not in methods:
        raise ValueError('Unrecognized nearest neighbor algorithm `%s`. '
                         'Please choose one of the supported methods or '
                         'provide a valid `KNNIndex` instance.' % method)
    else:
        if metric not in VALID_METRICS:
            raise ValueError('Unrecognized distance metric `%s`. Please '
                             'choose one of the supported methods.' % metric)
        knn_index = methods[method](metric=metric, metric_params=metric_params,
                                    n_jobs=n_jobs, random_state=random_state)

    knn_index.build(data)

    return knn_index


def joint_probabilities_nn(neighbors, distances, perplexities, symmetrize=True,
                           n_reference_samples=None, n_jobs=1):
    """Compute the conditional probability matrix P_{j|i}.

    This method computes an approximation to P using the nearest neighbors.

    Parameters
    ----------
    neighbors: np.ndarray
        A `n_samples * k_neighbors` matrix containing the indices to each
        points' nearest neighbors in descending order.
    distances: np.ndarray
        A `n_samples * k_neighbors` matrix containing the distances to the
        neighbors at indices defined in the neighbors parameter.
    perplexities: double
        The desired perplexity of the probability distribution.
    symmetrize: bool
        Whether to symmetrize the probability matrix or not. Symmetrizing is
        used for typical t-SNE, but does not make sense when embedding new data
        into an existing embedding.
    n_reference_samples: int
        The number of samples in the existing (reference) embedding. Needed to
        properly construct the sparse P matrix.
    n_jobs: int
        Number of threads.

    Returns
    -------
    csr_matrix
        A `n_samples * n_reference_samples` matrix containing the probabilities
        that a new sample would appear as a neighbor of a reference point.

    """
    n_samples, k_neighbors = distances.shape

    if n_reference_samples is None:
        n_reference_samples = n_samples

    # Compute asymmetric pairwise input similarities
    conditional_P = _tsne.compute_gaussian_perplexity(
        distances, np.array(perplexities, dtype=float), num_threads=n_jobs)
    conditional_P = np.asarray(conditional_P)

    P = csr_matrix((conditional_P.ravel(), neighbors.ravel(),
                    range(0, n_samples * k_neighbors + 1, k_neighbors)),
                   shape=(n_samples, n_reference_samples))

    # Symmetrize the probability matrix
    if symmetrize:
        P = (P + P.T) / 2

    # Convert weights to probabilities
    P /= np.sum(P)

    return P


class FixedSigmaNN(Affinities):

    def __init__(self, data, sigma, k=30, method='exact', metric='euclidean',
                 metric_params=None, symmetrize=True, n_jobs=1, random_state=None):
        self.n_samples = n_samples = data.shape[0]

        if k >= self.n_samples:
            raise ValueError('`k` (%d) cannot be larger than N-1 (%d).' % (k, self.n_samples))

        knn_index = build_knn_index(data, method, metric, metric_params, n_jobs, random_state)
        neighbors, distances = knn_index.query_train(data, k=k)

        self.knn_index = knn_index

        # Compute asymmetric pairwise input similarities
        conditional_P = np.exp(-distances ** 2 / (2 * sigma ** 2))
        conditional_P /= np.sum(conditional_P, axis=1)[:, np.newaxis]

        P = csr_matrix((conditional_P.ravel(), neighbors.ravel(),
                        range(0, n_samples * k + 1, k)),
                       shape=(n_samples, n_samples))

        # Symmetrize the probability matrix
        if symmetrize:
            P = (P + P.T) / 2

        # Convert weights to probabilities
        P /= np.sum(conditional_P)

        self.sigma = sigma
        self.k = k
        self.P = P
        self.n_jobs = n_jobs

    def to_new(self, data, k=None, sigma=None, return_distances=False):
        n_samples = data.shape[0]
        n_reference_samples = self.n_samples

        if k is None:
            k = self.k
        elif k >= n_reference_samples:
            raise ValueError('`k` (%d) cannot be larger than the number of '
                             'reference samples (%d).' % (k, self.n_samples))

        if sigma is None:
            sigma = self.sigma

        # Find nearest neighbors and the distances to the new points
        neighbors, distances = self.knn_index.query(data, k)

        # Compute asymmetric pairwise input similarities
        conditional_P = np.exp(-distances ** 2 / (2 * sigma ** 2))
        conditional_P /= np.sum(conditional_P, axis=1)[:, np.newaxis]

        P = csr_matrix((conditional_P.ravel(), neighbors.ravel(),
                        range(0, n_samples * k + 1, k)),
                       shape=(n_samples, n_reference_samples))

        # Convert weights to probabilities
        P /= np.sum(conditional_P)

        if return_distances:
            return P, neighbors, distances

        return P


class Multiscale(Affinities):
    def __init__(self, data, perplexities, method='exact', metric='euclidean',
                 metric_params=None, symmetrize=True, n_jobs=1, random_state=None):
        self.n_samples = data.shape[0]

        # We will compute the nearest neighbors to the max value of perplexity,
        # smaller values can just use indexing to truncate unneeded neighbors
        perplexities = self.check_perplexities(perplexities)
        max_perplexity = np.max(perplexities)
        k_neighbors = min(self.n_samples - 1, int(3 * max_perplexity))

        knn_index = build_knn_index(data, method, metric, metric_params, n_jobs, random_state)
        neighbors, distances = knn_index.query_train(data, k=k_neighbors)

        self.knn_index = knn_index
        self.P = joint_probabilities_nn(neighbors, distances, perplexities,
                                        symmetrize=symmetrize, n_jobs=n_jobs)

        self.perplexities = perplexities
        self.n_jobs = n_jobs

    def to_new(self, data, perplexities=None, return_distances=False):
        perplexities = perplexities if perplexities is not None else self.perplexities
        perplexities = self.check_perplexities(perplexities)

        max_perplexity = np.max(perplexities)
        k_neighbors = min(self.n_samples - 1, int(3 * max_perplexity))

        neighbors, distances = self.knn_index.query(data, k_neighbors)

        P = joint_probabilities_nn(
            neighbors, distances, perplexities, symmetrize=False,
            n_reference_samples=self.n_samples, n_jobs=self.n_jobs,
        )

        if return_distances:
            return P, neighbors, distances

        return P

    def check_perplexities(self, perplexities):
        """Check and correct/truncate perplexities.

        If a perplexity is too large, it is corrected to the largest allowed
        value. It is then inserted into the list of perplexities only if that
        value doesn't already exist in the list.

        """
        usable_perplexities = []
        for perplexity in sorted(perplexities):
            if 3 * perplexity > self.n_samples - 1:
                new_perplexity = (self.n_samples - 1) / 3

                if new_perplexity in usable_perplexities:
                    log.warning('Perplexity value %d is too high. Dropping '
                                'because the max perplexity is already in the '
                                'list.' % perplexity)
                else:
                    usable_perplexities.append(new_perplexity)
                    log.warning('Perplexity value %d is too high. Using '
                                'perplexity %.2f instaed' % (perplexity, new_perplexity))
            else:
                usable_perplexities.append(perplexity)

        return usable_perplexities
