#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""haritica-clustering-helper

Stand-alone GPL-3 PyInstaller binary that runs Leiden / Louvain community
detection on an h5ad input and writes labels CSV. Invoked via subprocess by
the main Haritica sidecar when the user opts into community-detection
clustering.

Distribution: GPL-3 source repo at github.com/ThomasXielt/haritica-cloud-gpl-extras
(staged at helper-repo/ in the Haritica monorepo).
License: GPL-3.0-or-later. See LICENSE and WRITTEN_OFFER.txt.

Implementation note (v1.0.2): historically this wrapper called
``scanpy.pp.neighbors`` + ``scanpy.tl.leiden``/``louvain``. Scanpy
transitively imports numba + llvmlite, which load native llvmlite.dll
at import time and require the matching MSVC runtime — a dep chain that
PyInstaller's onefile bundler cannot reliably reproduce across the
matrix of Windows/macOS/Linux build hosts. Dropping scanpy in favor of
sklearn.NearestNeighbors for KNN + leidenalg/louvain directly removes
~200 MB from the binary and makes the build deterministic.
"""
from __future__ import annotations

import argparse
import csv
import sys
import traceback

import anndata
import igraph as ig
import leidenalg
import louvain
import numpy as np
from sklearn.neighbors import NearestNeighbors

__version__ = "1.0.4"


def _build_knn_graph(
    X: np.ndarray, n_neighbors: int
) -> ig.Graph:
    """KNN graph on the embedding — same shape as scanpy.pp.neighbors with
    method='gauss' would build (undirected, k nearest neighbors per cell,
    unweighted edges)."""
    n_obs = X.shape[0]
    # n_neighbors includes the cell itself in sklearn; ask for k+1 then drop self-loops.
    knn = NearestNeighbors(
        n_neighbors=min(n_neighbors + 1, n_obs),
        algorithm="auto",
        metric="euclidean",
        n_jobs=-1,
    )
    knn.fit(X)
    _, indices = knn.kneighbors(X)
    sources: list[int] = []
    targets: list[int] = []
    for i in range(n_obs):
        for j in indices[i]:
            if i == j:
                continue  # drop self-loop
            sources.append(int(i))
            targets.append(int(j))
    g = ig.Graph(n=n_obs, edges=list(zip(sources, targets)), directed=False)
    g.simplify(multiple=True, loops=True)
    return g


def main() -> int:
    p = argparse.ArgumentParser(prog="haritica-clustering-helper")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--in", dest="in_path", required=True, help="Input h5ad path")
    p.add_argument("--out", dest="out_path", required=True, help="Output CSV path")
    p.add_argument("--method", choices=["leiden", "louvain"], required=True)
    p.add_argument("--resolution", type=float, default=1.0)
    p.add_argument("--random-state", type=int, default=0)
    p.add_argument("--n-neighbors", type=int, default=15)
    p.add_argument("--use-rep", default="X_pca")
    p.add_argument("--n-pcs", type=int, default=None)
    args = p.parse_args()

    try:
        ad = anndata.read_h5ad(args.in_path)
        if args.use_rep not in ad.obsm:
            sys.stderr.write(
                f"ERROR: embedding '{args.use_rep}' not found in input .obsm. "
                f"Available: {list(ad.obsm.keys())}\n"
            )
            return 1

        X = np.asarray(ad.obsm[args.use_rep], dtype=np.float64)
        if args.n_pcs is not None:
            X = X[:, : args.n_pcs]

        g = _build_knn_graph(X, args.n_neighbors)

        if args.method == "leiden":
            partition = leidenalg.find_partition(
                g,
                leidenalg.RBConfigurationVertexPartition,
                resolution_parameter=args.resolution,
                seed=args.random_state,
            )
        else:
            partition = louvain.find_partition(
                g,
                louvain.RBConfigurationVertexPartition,
                resolution_parameter=args.resolution,
                seed=args.random_state,
            )

        labels = np.asarray(partition.membership, dtype=np.int64)

        with open(args.out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["cell_index", "cluster"])
            for idx, lbl in zip(ad.obs.index, labels):
                w.writerow([str(idx), str(int(lbl))])
        return 0
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        sys.stderr.write(f"\nERROR: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
