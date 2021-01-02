import functools
import logging
import os
import sys
from typing import List

import joblib
import natsort
import numba as nb
import numpy as np
import pandas as pd
import scipy.sparse as ss
from sklearn.utils import murmurhash3_32

import config
from cluster import cluster, spectrum
from ms_io import ms_io


def main():
    # Configure logging.
    logging.captureWarnings(True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        '{asctime} {levelname} [{name}/{processName}] {module}.{funcName} : '
        '{message}', style='{'))
    root.addHandler(handler)
    # Disable dependency non-critical log messages.
    logging.getLogger('faiss').setLevel(logging.WARNING)
    logging.getLogger('numba').setLevel(logging.WARNING)
    logging.getLogger('numexpr').setLevel(logging.WARNING)
    # Initialize the logger.
    logger = logging.getLogger('spectrum_clustering')

    if os.path.isdir(config.work_dir):
        logging.warning('Working directory %s already exists, previous '
                        'results might get overwritten', config.work_dir)
    else:
        os.makedirs(config.work_dir)

    # Read the spectra from the input files.
    spectra = {charge: [] for charge in config.charges}
    logger.info('Read spectra from %d peak file(s)', len(config.filenames))
    for file_spectra in joblib.Parallel(n_jobs=-1)(
            joblib.delayed(_read_process_spectra)(filename)
            for filename in config.filenames):
        for spec in file_spectra:
            spectra[spec.precursor_charge].append(spec)

    # Pre-compute the index hash mappings.
    vec_len, min_mz, max_mz = spectrum.get_dim(config.min_mz, config.max_mz,
                                               config.fragment_mz_tolerance)
    hash_lookup = np.asarray([murmurhash3_32(i, 0, True) % config.hash_len
                              for i in range(vec_len)], np.uint32)
    vectorize = functools.partial(
        spectrum.to_vector_parallel, dim=config.hash_len, min_mz=min_mz,
        max_mz=max_mz, bin_size=config.fragment_mz_tolerance,
        hash_lookup=hash_lookup, norm=True)

    # Cluster the spectra per charge.
    clusters_all, current_label, representatives = [], 0, []
    for charge, spectra_charge in spectra.items():
        logger.info('Cluster %d spectra with precursor charge %d',
                    len(spectra_charge), charge)
        dist_filename = os.path.join(config.work_dir, f'dist_{charge}.npz')
        if not os.path.isfile(dist_filename):
            # Make sure the spectra are sorted by precursor m/z.
            logger.debug('Sort the spectra by their precursor m/z')
            spectra_charge.sort(key=lambda spec: spec.precursor_mz)
            pairwise_dist_matrix = cluster.compute_pairwise_distances(
                nb.typed.List(spectra_charge), vectorize,
                config.precursor_tol_mass, config.precursor_tol_mode,
                config.mz_interval, config.n_neighbors, config.n_neighbors_ann,
                config.precursor_tol_mass, config.precursor_tol_mode,
                config.batch_size, config.n_probe,
                os.path.join(config.work_dir, str(charge)))
            logger.debug('Export pairwise distance matrix to file %s',
                         dist_filename)
            ss.save_npz(dist_filename, pairwise_dist_matrix)
            # The spectra are already sorted.
            precursor_mzs = np.asarray(([spec.precursor_mz
                                         for spec in spectra_charge]))
            identifiers = [spec.identifier for spec in spectra_charge]
        else:
            logger.debug('Load previously computed pairwise distance matrix '
                         'from file %s', dist_filename)
            pairwise_dist_matrix = ss.load_npz(dist_filename)
            precursor_mzs, identifiers = [], []
            for spec in spectra_charge:
                precursor_mzs.append(spec.precursor_mz)
                identifiers.append(spec.identifier)
            order = np.argsort(precursor_mzs)
            precursor_mzs = np.asarray(precursor_mzs)[order]
            identifiers = np.asarray(identifiers)[order]
        clusters = cluster.generate_clusters(
            pairwise_dist_matrix, config.eps, config.min_samples,
            precursor_mzs, config.precursor_tol_mass,
            config.precursor_tol_mode)
        # Make sure that different charges have non-overlapping cluster labels.
        mask_no_noise = clusters != -1
        clusters[mask_no_noise] += current_label
        current_label = np.amax(clusters[mask_no_noise]) + 1
        # Extract cluster representatives (medoids).
        # FIXME
        # for cluster_label, representative_i in \
        #         cluster.get_cluster_representatives(
        #             clusters[mask_no_noise], pairwise_dist_matrix):
        #     representative = spectra_raw[spectra_charge[representative_i]
        #                                  .identifier]
        #     representative.cluster = cluster_label
        #     representatives.append(representative)
        # Save cluster assignments.
        clusters_all.append(pd.DataFrame({'identifier': identifiers,
                                          'cluster': clusters}))

    # Export cluster memberships and representative spectra.
    logger.debug('Export cluster assignments')
    clusters_all = (pd.concat(clusters_all, ignore_index=True)
                    .sort_values('identifier', key=natsort.natsort_keygen()))
    clusters_all.to_csv(os.path.join(config.work_dir, 'clusters.csv'),
                        index=False)
    logger.debug('Export cluster representative spectra')
    representatives.sort(key=lambda spec: spec.cluster)
    ms_io.write_spectra(os.path.join(config.work_dir, 'clusters.mgf'),
                        representatives)

    logging.shutdown()


def _read_process_spectra(filename: str) -> List[spectrum.MsmsSpectrumNb]:
    """
    Get high-quality processed MS/MS spectra from the given file.

    Parameters
    ----------
    filename : str
        The path of the peak file to be read.

    Returns
    -------
    List[spectrum.MsmsSpectrumNb]
        The processed spectra in the given file.
    """
    spectra = []
    for spec_raw in ms_io.get_spectra(filename):
        spec_raw.identifier = f'mzspec:{config.pxd}:{spec_raw.identifier}'
        # Discard low-quality spectra.
        spec_processed = spectrum.process_spectrum(
            spec_raw, config.min_peaks, config.min_mz_range, config.min_mz,
            config.max_mz, config.remove_precursor_tolerance,
            config.min_intensity, config.max_peaks_used, config.scaling)
        if (spec_processed is not None
                and spec_processed.precursor_charge in config.charges):
            spectra.append(spec_processed)
    spectra.sort(key=lambda spec: spec.precursor_mz)
    return spectra


if __name__ == '__main__':
    main()
