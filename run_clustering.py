"""GPL Stage-1 Clustering Wrapper

Downloads an h5ad from S3, runs Leiden or Louvain community detection,
then uploads a cluster-labels CSV (cell_index,cluster) back to S3 under
$HARITICA_GPL_OUTPUT_PREFIX. Writes a `_COMPLETE` marker LAST.

License: GPL-3.0-or-later (this wrapper aggregates GPL-3 libraries —
leidenalg, louvain, igraph). Same FSF "mere aggregation" boundary
as run_alignment.py.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from typing import Tuple
from urllib.parse import urlparse

import anndata
import boto3
import scanpy as sc


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"[run_clustering] ERROR: required env var {name} is not set")
        sys.exit(2)
    return val or ""


def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"not an s3:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def main() -> int:
    input_bucket = _env("S3_INPUT_BUCKET", required=True)
    output_prefix_uri = _env("HARITICA_GPL_OUTPUT_PREFIX", required=True)
    input_keys = json.loads(_env("HARITICA_GPL_INPUT_KEYS", "[]"))
    tool_params = json.loads(_env("HARITICA_GPL_TOOL_PARAMS", "{}"))

    if not input_keys or not input_keys[0]:
        print("[run_clustering] ERROR: HARITICA_GPL_INPUT_KEYS is empty")
        return 2
    h5ad_key = input_keys[0]
    out_bucket, out_prefix = _parse_s3_uri(output_prefix_uri)

    method = tool_params.get("method", "leiden")
    resolution = float(tool_params.get("resolution", 1.0))
    n_neighbors = int(tool_params.get("n_neighbors", 15))
    random_state = int(tool_params.get("random_state", 0))
    use_rep = tool_params.get("use_rep", "X_pca")

    work = Path(_env("HARITICA_WORK_DIR", "/mnt/nvme"))
    work.mkdir(parents=True, exist_ok=True)
    h5ad_local = work / Path(h5ad_key).name
    labels_local = work / "cluster_labels.csv"

    s3 = boto3.client("s3")
    print(f"[run_clustering] downloading s3://{input_bucket}/{h5ad_key} -> {h5ad_local}")
    s3.download_file(input_bucket, h5ad_key, str(h5ad_local))

    print(f"[run_clustering] reading h5ad ({h5ad_local})")
    ad = anndata.read_h5ad(str(h5ad_local))

    print(f"[run_clustering] computing neighbors (n_neighbors={n_neighbors}, use_rep={use_rep})")
    sc.pp.neighbors(ad, n_neighbors=n_neighbors, use_rep=use_rep)

    if method == "leiden":
        print(f"[run_clustering] running leiden (resolution={resolution}, random_state={random_state})")
        sc.tl.leiden(ad, resolution=resolution, random_state=random_state, key_added="cluster")
    elif method == "louvain":
        print(f"[run_clustering] running louvain (resolution={resolution}, random_state={random_state})")
        sc.tl.louvain(ad, resolution=resolution, random_state=random_state, key_added="cluster")
    else:
        print(f"[run_clustering] ERROR: unknown clustering method '{method}' (expected leiden|louvain)")
        return 2

    print(f"[run_clustering] writing labels CSV {labels_local}")
    with open(labels_local, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell_index", "cluster"])
        for idx, lbl in zip(ad.obs.index, ad.obs["cluster"].astype(str)):
            w.writerow([idx, lbl])

    label_key = f"{out_prefix.rstrip('/')}/cluster_labels.csv"
    print(f"[run_clustering] uploading -> s3://{out_bucket}/{label_key}")
    s3.upload_file(str(labels_local), out_bucket, label_key)

    marker_key = f"{out_prefix.rstrip('/')}/_COMPLETE"
    s3.put_object(Bucket=out_bucket, Key=marker_key, Body=b"")
    print(f"[run_clustering] wrote completion marker s3://{out_bucket}/{marker_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
