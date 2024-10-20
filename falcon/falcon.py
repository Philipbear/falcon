import collections
import functools
import glob
import logging
import multiprocessing
import multiprocessing.sharedctypes
import multiprocessing.synchronize
import os
import queue
import shutil
import sys
import tempfile
import threading
from typing import Callable, Dict, List, Set, Tuple, Union

import joblib
import lance
import natsort
import numpy as np
import pandas as pd
import pyarrow as pa
import scipy.sparse as ss
from sklearn.random_projection import SparseRandomProjection

from . import __version__, seed
from .cluster import cluster, spectrum
from .config import config
from .ms_io import ms_io


logger = logging.getLogger("falcon")

seed.set_seeds()


def main(args: Union[str, List[str]] = None) -> int:
    # Configure logging.
    logging.captureWarnings(True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "{asctime} {levelname} [{name}/{processName}] {module}.{funcName} : "
            "{message}",
            style="{",
        )
    )
    root.addHandler(handler)
    # Disable dependency non-critical log messages.
    logging.getLogger("faiss").setLevel(logging.WARNING)
    logging.getLogger("numba").setLevel(logging.WARNING)
    logging.getLogger("numexpr").setLevel(logging.WARNING)

    # Load the configuration.
    config.parse(args)
    logger.info("falcon version %s", str(__version__))
    logger.debug("work_dir = %s", config.work_dir)
    logger.debug("overwrite = %s", config.overwrite)
    logger.debug("export_representatives = %s", config.export_representatives)
    logger.debug("precursor_tol = %.2f %s", *config.precursor_tol)
    logger.debug("rt_tol = %s", config.rt_tol)
    logger.debug("fragment_tol = %.2f", config.fragment_tol)
    logger.debug("eps = %.3f", config.eps)
    logger.debug("mz_interval = %d", config.mz_interval)
    logger.debug("low_dim = %d", config.low_dim)
    logger.debug("n_neighbors = %d", config.n_neighbors)
    logger.debug("batch_size = %d", config.batch_size)
    logger.debug("n_probe = %d", config.n_probe)
    logger.debug("min_peaks = %d", config.min_peaks)
    logger.debug("min_mz_range = %.2f", config.min_mz_range)
    logger.debug("min_mz = %.2f", config.min_mz)
    logger.debug("max_mz = %.2f", config.max_mz)
    logger.debug("remove_precursor_tol = %.2f", config.remove_precursor_tol)
    logger.debug("min_intensity = %.2f", config.min_intensity)
    logger.debug("max_peaks_used = %d", config.max_peaks_used)
    logger.debug("scaling = %s", config.scaling)

    rm_work_dir = False
    if config.work_dir is None:
        config.work_dir = tempfile.mkdtemp()
        rm_work_dir = True
    elif os.path.isdir(config.work_dir):
        logging.warning(
            "Working directory %s already exists, previous "
            "results might get overwritten",
            config.work_dir,
        )
    os.makedirs(config.work_dir, exist_ok=True)
    os.makedirs(os.path.join(config.work_dir, "spectra"), exist_ok=True)
    os.makedirs(os.path.join(config.work_dir, "nn"), exist_ok=True)

    # Clean all intermediate and final results if "overwrite" is specified,
    # otherwise abort if the output files already exist.
    exit_exists = False
    if os.path.isfile(f"{config.output_filename}.csv"):
        if config.overwrite:
            logger.warning(
                "Output file %s (cluster assignments) already "
                "exists and will be overwritten",
                f"{config.output_filename}.csv",
            )
            os.remove(f"{config.output_filename}.csv")
        else:
            logger.error(
                "Output file %s (cluster assignments) already "
                "exists, aborting...",
                f"{config.output_filename}.csv",
            )
            exit_exists = True
    if os.path.isfile(f"{config.output_filename}.mgf"):
        if config.overwrite:
            logger.warning(
                "Output file %s (cluster representatives) already "
                "exists and will be overwritten",
                f"{config.output_filename}.mgf",
            )
            os.remove(f"{config.output_filename}.mgf")
        else:
            logger.error(
                "Output file %s (cluster representatives) already "
                "exists, aborting...",
                f"{config.output_filename}.mgf",
            )
            exit_exists = True
    if exit_exists:
        logging.shutdown()
        return 1

    vec_len, min_mz, max_mz = spectrum.get_dim(
        config.min_mz, config.max_mz, config.fragment_tol
    )
    process_spectrum = functools.partial(
        spectrum.process_spectrum,
        min_peaks=config.min_peaks,
        min_mz_range=config.min_mz_range,
        mz_min=min_mz,
        mz_max=max_mz,
        remove_precursor_tolerance=config.remove_precursor_tol,
        min_intensity=config.min_intensity,
        max_peaks_used=config.max_peaks_used,
        scaling=None if config.scaling == "off" else config.scaling,
    )

    transformation = (
        SparseRandomProjection(config.low_dim, random_state=0)
        .fit(np.zeros((1, vec_len)))
        .components_.astype(np.float32)
        .T
    )
    vectorize = functools.partial(
        spectrum.to_vector,
        transformation=transformation,
        min_mz=min_mz,
        bin_size=config.fragment_tol,
        dim=vec_len,
        norm=True,
    )

    if config.overwrite:
        for filename in os.listdir(os.path.join(config.work_dir, "spectra")):
            os.remove(os.path.join(config.work_dir, "spectra", filename))
        for filename in os.listdir(os.path.join(config.work_dir, "nn")):
            os.remove(os.path.join(config.work_dir, "nn", filename))

    charge_path = os.path.join(config.work_dir, "spectra", "charges.joblib")
    if os.path.isfile(charge_path) and not config.overwrite:
        charges = joblib.load(charge_path)
    else:
        # Recalculate the charge buckets and recreate dataset.
        charges, _ = _prepare_spectra(process_spectrum, vectorize)
        joblib.dump(charges, charge_path)

    # Cluster the spectra per charge.
    clusters_all, current_label, representatives = [], 0, []
    for charge in charges:
        dataset_path = os.path.join(
            config.work_dir, "spectra", f"spectra_charge_{charge}.lance"
        )
        dataset = lance.dataset(dataset_path)
        dist_filename = os.path.join(
            config.work_dir, "nn", f"dist_{charge}.npz"
        )
        metadata_filename = os.path.join(
            config.work_dir, "nn", f"metadata_{charge}.parquet"
        )
        if not os.path.isfile(dist_filename) or not os.path.isfile(
            metadata_filename
        ):
            pairwise_dist_matrix, metadata = (
                cluster.compute_pairwise_distances(
                    dataset,
                    charge,
                    config.precursor_tol[0],
                    config.precursor_tol[1],
                    config.rt_tol,
                    config.n_neighbors,
                    config.batch_size,
                    config.n_probe,
                )
            )
            metadata.insert(2, "precursor_charge", charge)
            logger.debug(
                "Export pairwise distance matrix to file %s",
                dist_filename,
            )
            ss.save_npz(dist_filename, pairwise_dist_matrix, False)
            metadata.to_parquet(metadata_filename, index=False)
        else:
            logger.debug(
                "Load previously computed pairwise distance matrix "
                "from file %s",
                dist_filename,
            )
            pairwise_dist_matrix = ss.load_npz(dist_filename)
            metadata = pd.read_parquet(metadata_filename)
        # No valid spectra found with the current charge.
        if len(metadata) == 0:
            continue
        # Cluster using the pairwise distance matrix.
        clusters = cluster.generate_clusters(
            pairwise_dist_matrix,
            config.eps,
            metadata["precursor_mz"].values,
            metadata["retention_time"].values,
            config.precursor_tol[0],
            config.precursor_tol[1],
            config.rt_tol,
        )
        # Make sure that different charges have non-overlapping cluster labels.
        clusters += current_label
        # noinspection PyUnresolvedReferences
        current_label = np.amax(clusters) + 1
        # Save cluster assignments.
        metadata["cluster"] = clusters
        clusters_all.append(metadata)
        # Extract identifiers for cluster representatives (medoids).
        if config.export_representatives:
            charge_representatives = cluster.get_cluster_representatives(
                clusters,
                pairwise_dist_matrix.indptr,
                pairwise_dist_matrix.indices,
                pairwise_dist_matrix.data,
            )
            representatives.append(
                dataset.take(charge_representatives)
                .to_pandas()
                .apply(spectrum.df_row_to_spec, axis=1)
            )

    # Export cluster memberships and representative spectra.
    clusters_all = pd.concat(clusters_all, ignore_index=True).sort_values(
        ["filename", "spectrum_id"], key=natsort.natsort_keygen()
    )
    logger.info(
        "Export cluster assignments of %d spectra to %d unique "
        "clusters to output file %s",
        len(clusters_all),
        clusters_all["cluster"].nunique(),
        f"{config.output_filename}.csv",
    )
    # Perform IO in a separate worker process.
    write_csv_worker = threading.Thread(
        target=_write_cluster_info, args=(clusters_all,), daemon=True
    )
    write_csv_worker.start()
    if config.export_representatives:
        representatives = pd.concat(
            representatives, ignore_index=True
        ).tolist()
        logger.info(
            "Export %d cluster representative spectra to output file %s",
            len(representatives),
            f"{config.output_filename}.mgf",
        )
        # Perform IO in a separate worker process.
        write_mgf_worker = threading.Thread(
            target=ms_io.write_spectra,
            args=(f"{config.output_filename}.mgf", representatives),
            daemon=True,
        )
        write_mgf_worker.start()
        write_mgf_worker.join()
    write_csv_worker.join()

    if rm_work_dir:
        shutil.rmtree(config.work_dir)

    logging.shutdown()
    return 0


def _prepare_spectra(
    process_spectrum: Callable, vectorize: Callable
) -> Set[int]:
    """
    Read the spectra from the input peak files and partition to intermediate
    files split and sorted by precursor m/z.

    Parameters
    ----------
    process_spectrum : Callable
        The function to process the spectra.

    Returns
    -------
    Set[int]
        The precursor charges of the spectra.
    """
    input_filenames = [
        fn for pattern in config.input_filenames for fn in glob.glob(pattern)
    ]
    logger.info("Read spectra from %d peak file(s)", len(input_filenames))
    # Use multiple worker processes to read the peak files.
    max_file_workers = min(len(input_filenames), multiprocessing.cpu_count())
    # Restrict the number of spectra simultaneously in memory to avoid
    # excessive memory requirements.
    max_spectra_in_memory = 1_000_000
    spectra_queue = queue.Queue(maxsize=max_spectra_in_memory)
    # Read the peak files and put their spectra in the queue for consumption.
    low_quality_counter = 0
    for file_spectra, lqc in joblib.Parallel(n_jobs=max_file_workers)(
        joblib.delayed(_read_spectra)(file, process_spectrum)
        for file in input_filenames
    ):
        low_quality_counter += lqc
        for spec in file_spectra:
            spectra_queue.put(spec)

    # Write the spectra to a lance file.
    lance_locks = collections.defaultdict(multiprocessing.Lock)
    charges = set()
    schema = pa.schema(
        [
            pa.field("identifier", pa.string()),
            pa.field("precursor_mz", pa.float32()),
            pa.field("precursor_charge", pa.int8()),
            pa.field("mz", pa.list_(pa.float32())),
            pa.field("intensity", pa.list_(pa.float32())),
            pa.field("retention_time", pa.float32()),
            pa.field("filename", pa.string()),
            pa.field("vector", pa.list_(pa.float32())),
        ]
    )
    lance_writers = multiprocessing.pool.ThreadPool(
        max_file_workers,
        _write_spectra_lance,
        (
            spectra_queue,
            lance_locks,
            schema,
            charges,
            vectorize,
        ),
    )
    # Add sentinels to indicate stopping. This needs to happen after all files
    # have been read (by joining `peak_readers`).
    for _ in range(max_file_workers):
        spectra_queue.put(None)
    lance_writers.close()
    lance_writers.join()

    # Count the total number of spectra in the datasets.
    dataset_paths = [
        os.path.join(
            config.work_dir, "spectra", f"spectra_charge_{charge}.lance"
        )
        for charge in charges
    ]
    n_spectra = 0
    for dataset_path in dataset_paths:
        try:
            dataset = lance.dataset(dataset_path)
        except ValueError:
            charge = int(dataset_path.split("_")[-1].split(".")[0])
            logger.error("Failed to create dataset for charge %d", charge)
            charges.remove(charge)
            continue
        n_spectra += dataset.count_rows()
    logger.info(
        "Read %d spectra from %d peak files", n_spectra, len(input_filenames)
    )
    logger.info("Skipped %d low-quality spectra", low_quality_counter)
    return charges, dataset_paths


def _create_lance_dataset(
    charge: int, schema: pa.Schema
) -> lance.LanceDataset:
    """
    Create a lance dataset.

    Parameters
    ----------
    charge : int
        The precursor charge of the spectra.
    schema : pa.Schema
        The schema of the dataset.

    Returns
    -------
    lance.LanceDataset
        The lance dataset.
    """
    lance_path = os.path.join(
        config.work_dir, "spectra", f"spectra_charge_{charge}.lance"
    )
    dataset = lance.write_dataset(
        pa.Table.from_pylist([], schema),
        lance_path,
        mode="overwrite",
        data_storage_version="stable",
    )
    logger.debug("Creating lance dataset at %s", lance_path)
    return dataset


def _read_spectra(
    filename: str,
    process_spectrum: Callable,
) -> Tuple[List[Dict[str, Union[str, float, int, np.ndarray]]], int]:
    """
    Get the spectra from the given file.

    Parameters
    ----------
    filename : str
        The path of the peak file to be read.
    process_spectrum : Callable
        The function to process the spectra.

    Returns
    -------
    Tuple[List[Dict[str, Union[str, float, int, np.ndarray]]], int]
        The spectra read from the given file as a list of dictionaries and
        the number of low-quality spectra.
    """
    low_quality_counter = 0
    spectra = []
    filename = os.path.abspath(filename)
    for spec in ms_io.get_spectra(filename):
        spec.filename = filename
        spec = process_spectrum(spec)
        if spec is None:
            low_quality_counter += 1
        else:
            spectra.append(spec)
    return spectra, low_quality_counter


def _write_spectra_lance(
    spectra_queue: queue.Queue,
    lance_locks: Dict[int, multiprocessing.synchronize.Lock],
    schema: pa.Schema,
    charges: Set,
    vectorize: Callable,
) -> None:
    """
    Read spectra from a queue and write to a lance dataset.

    Parameters
    ----------
    spectra_queue : queue.Queue
        Queue from which to read spectra for writing to pickle files.
    lance_locks : Dict[int, multiprocessing.synchronize.Lock]
        Locks to synchronize writing to the dataset.
    schema : pa.Schema
        The schema of the dataset.
    charges : set
        The precursor charges of the spectra.
    vectorize : Callable
        The function to vectorize the spectra.
    """
    spec_to_write = collections.defaultdict(list)
    while True:
        spec = spectra_queue.get()
        if spec is None:
            # Write remaining spectra to the dataset.
            for charge in spec_to_write.keys():
                if len(spec_to_write[charge]) == 0:
                    continue
                _write_to_dataset(
                    spec_to_write[charge],
                    charge,
                    lance_locks[charge],
                    schema,
                    config.work_dir,
                    vectorize,
                )
                spec_to_write[charge].clear()
            return
        charge = spec["precursor_charge"]
        spec_to_write[charge].append(spec)
        charges.add(charge)
        if len(spec_to_write[charge]) >= 10_000:
            _write_to_dataset(
                spec_to_write[charge],
                charge,
                lance_locks[charge],
                schema,
                config.work_dir,
                vectorize,
            )
            spec_to_write[charge].clear()


def _write_to_dataset(
    spec_to_write: List[Dict],
    charge: int,
    lock: multiprocessing.synchronize.Lock,
    schema: pa.Schema,
    work_dir: str,
    vectorize: Callable,
) -> int:
    """
    Write a list of spectra to a lance dataset.

    Parameters
    ----------
    spec_to_write : List[Dict]
        The spectra to write.
    charge : int
        The precursor charge of the spectra.
    lock : multiprocessing.Lock
        Lock to synchronize writing to the dataset.
    schema : pa.Schema
        The schema of the dataset.
    work_dir : str
        The directory in which the dataset is stored.
    vectorize : Callable
        The function to vectorize the spectra.

    Returns
    -------
    int
        The number of spectra written to the dataset.
    """
    # Vectorize the spectra and add them to the dictionary.
    vectors = vectorize(spec_to_write)
    for i, vector in enumerate(vectors):
        spec_to_write[i]["vector"] = vector
    # Write the spectra to the dataset.
    new_rows = pa.Table.from_pylist(spec_to_write, schema)
    path = os.path.join(work_dir, "spectra", f"spectra_charge_{charge}.lance")
    with lock:
        if not os.path.exists(path):
            _create_lance_dataset(charge, schema)
        lance.write_dataset(new_rows, path, mode="append")
    return len(new_rows)


def _write_cluster_info(clusters: pd.DataFrame) -> None:
    """
    Export the clustering results to a CSV file.

    Parameters
    ----------
    clusters : pd.DataFrame
        The clustering results.
    """
    with open(f"{config.output_filename}.csv", "a") as f_out:
        # Metadata.
        f_out.write(f"# falcon version {__version__}\n")
        f_out.write(f"# work_dir = {config.work_dir}\n")
        f_out.write(f"# overwrite = {config.overwrite}\n")
        f_out.write(
            f"# export_representatives = " f"{config.export_representatives}\n"
        )
        f_out.write(
            f"# precursor_tol = {config.precursor_tol[0]:.2f} "
            f"{config.precursor_tol[1]}\n"
        )
        f_out.write(f"# rt_tol = {config.rt_tol}\n")
        f_out.write(f"# fragment_tol = {config.fragment_tol:.2f}\n")
        f_out.write(f"# eps = {config.eps:.3f}\n")
        f_out.write(f"# mz_interval = {config.mz_interval}\n")
        f_out.write(f"# low_dim = {config.low_dim}\n")
        f_out.write(f"# n_neighbors = {config.n_neighbors}\n")
        f_out.write(f"# batch_size = {config.batch_size}\n")
        f_out.write(f"# n_probe = {config.n_probe}\n")
        f_out.write(f"# min_peaks = {config.min_peaks}\n")
        f_out.write(f"# min_mz_range = {config.min_mz_range:.2f}\n")
        f_out.write(f"# min_mz = {config.min_mz:.2f}\n")
        f_out.write(f"# max_mz = {config.max_mz:.2f}\n")
        f_out.write(
            f"# remove_precursor_tol = " f"{config.remove_precursor_tol:.2f}\n"
        )
        f_out.write(f"# min_intensity = {config.min_intensity:.2f}\n")
        f_out.write(f"# max_peaks_used = {config.max_peaks_used}\n")
        f_out.write(f"# scaling = {config.scaling}\n")
        f_out.write("#\n")
        # Cluster assignments.
        clusters.to_csv(f_out, index=False, chunksize=1000000)


if __name__ == "__main__":
    sys.exit(main())
