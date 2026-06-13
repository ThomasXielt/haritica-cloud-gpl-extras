# SPDX-License-Identifier: GPL-3.0-or-later
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


def _install_scoped_session() -> None:
    """Re-assume the inner BatchJobBaseRole with the haritica-user-id session
    tag so per-user-tag-conditioned S3 reads work.

    The Batch container starts running as the OUTER BatchJobBaseRole, whose
    inline policy grants ONLY `sts:AssumeRole` + `sts:TagSession` on the inner
    `haritica-{env}-batch-job-base` role — it has no direct S3 permissions.
    The inner role holds the actual S3 grants, scoped by the haritica-user-id
    session tag (per-user prefix isolation in S3 bucket policies). Without this
    assume-role-with-tag dance, every S3 call from inside the GPL stage fails
    with 403 Forbidden.

    Mirrors what the closed-source main worker's
    server/aws_creds.install_scoped_default_session() does — inline here so
    this GPL-3 wrapper doesn't import any closed-source code (the
    closed-source / GPL-3 isolation boundary is via S3, not Python imports).

    Required env vars (all set by server/batch_submitter._plan_pipeline at
    submission time):
      BATCH_JOB_BASE_ROLE_ARN — the inner role to assume (has S3 perms)
      HARITICA_USER_ID        — the haritica-user-id session tag value
      HARITICA_JOB_ID         — used to build a unique RoleSessionName

    No-op when those env vars are unset (e.g. local debugging) — the default
    credential chain handles that case.
    """
    role_arn = os.environ.get("BATCH_JOB_BASE_ROLE_ARN", "")
    user_id = os.environ.get("HARITICA_USER_ID", "")
    job_id = os.environ.get("HARITICA_JOB_ID", "unknown")
    if not (role_arn and user_id):
        print(
            "[run_clustering] BATCH_JOB_BASE_ROLE_ARN or HARITICA_USER_ID "
            "unset — using default credential chain",
            flush=True,
        )
        return

    import botocore.session
    from botocore.credentials import RefreshableCredentials

    session_name = f"haritica-job-{job_id[:32]}"

    def _refresh() -> dict:
        # Bare boto3 client here, NOT the default session — avoids infinite
        # recursion if anything in the default session ever calls back into us
        # during refresh.
        sts = boto3.Session().client("sts")
        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            Tags=[{"Key": "haritica-user-id", "Value": user_id}],
            DurationSeconds=3600,  # role-chaining cap; refresh handles the rest
        )
        c = resp["Credentials"]
        return {
            "access_key": c["AccessKeyId"],
            "secret_key": c["SecretAccessKey"],
            "token": c["SessionToken"],
            "expiry_time": c["Expiration"].isoformat(),
        }

    creds = RefreshableCredentials.create_from_metadata(
        metadata=_refresh(),
        refresh_using=_refresh,
        method="sts-assume-role",
    )
    botocore_session = botocore.session.get_session()
    botocore_session._credentials = creds
    boto3.DEFAULT_SESSION = boto3.Session(botocore_session=botocore_session)
    print(
        f"[run_clustering] installed scoped credentials "
        f"(user={user_id[:8]}..., session={session_name})",
        flush=True,
    )


def _load_embedding_h5py(h5ad_local: str, use_rep: str):
    """Skew-proof minimal load: pull ONLY ``obsm[use_rep]`` + the obs index via
    raw h5py and build a bare AnnData. Used as a fallback when a full
    ``anndata.read_h5ad`` fails — most commonly an anndata version skew where
    the (newer) writer encoded a ``None`` scalar (e.g. ``uns['log1p']['base']``)
    with an IOSpec the (older) reader can't decode. The clustering runner needs
    nothing from ``uns``/``var``/``layers``/``X``, so the embedding + index is
    sufficient and is immune to writer/reader anndata version drift."""
    import h5py
    import numpy as np

    with h5py.File(str(h5ad_local), "r") as f:
        obsm_key = f"obsm/{use_rep}"
        if obsm_key not in f:
            print(
                f"[run_clustering] ERROR: {use_rep!r} not found in obsm — run PCA "
                "before clustering (the embedding must already exist in the h5ad)",
                flush=True,
            )
            sys.exit(2)
        pca = np.asarray(f[obsm_key][...])
        obs = f["obs"]
        idx_key = obs.attrs.get("_index", "_index")
        if isinstance(idx_key, bytes):
            idx_key = idx_key.decode()
        if idx_key in obs:
            raw_index = obs[idx_key][...]
            index = [
                x.decode() if isinstance(x, (bytes, bytearray)) else str(x)
                for x in raw_index
            ]
        else:
            # anndata always writes the index dataset; this branch is a last
            # resort. A positional index will fail the stage-2 cell_index match
            # (surfacing a clear error) rather than silently mislabel cells.
            index = [str(i) for i in range(pca.shape[0])]

    ad = anndata.AnnData(
        X=np.zeros((pca.shape[0], 1), dtype="float32"),
        obsm={use_rep: pca},
    )
    ad.obs_names = index
    return ad


def _load_embedding(h5ad_local: str, use_rep: str):
    """Load the h5ad into an AnnData carrying the clustering embedding. Tries a
    full ``anndata.read_h5ad`` first; on ANY read failure falls back to the
    h5py-direct minimal load (version-skew-proof)."""
    try:
        ad = anndata.read_h5ad(str(h5ad_local))
    except Exception as e:  # noqa: BLE001 — fall back on ANY read failure
        print(
            f"[run_clustering] full anndata read failed "
            f"({type(e).__name__}: {e}); falling back to h5py-direct minimal "
            f"load of obsm[{use_rep!r}] + obs index",
            flush=True,
        )
        return _load_embedding_h5py(h5ad_local, use_rep)
    if use_rep not in ad.obsm:
        print(
            f"[run_clustering] ERROR: {use_rep!r} missing from obsm — run PCA "
            "before clustering.",
            flush=True,
        )
        sys.exit(2)
    return ad


def main() -> int:
    _install_scoped_session()
    # NOTE: the sc-compute h5ad lives in S3_RESULTS_BUCKET, so the writer
    # (batch_submitter._plan_pipeline) overrides the S3_INPUT_BUCKET env var
    # forwarded to this stage to point at S3_RESULTS_BUCKET's value. From the
    # runner's POV it is still just "the bucket the h5ad input is in".
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
    if method not in ("leiden", "louvain"):
        print(f"[run_clustering] ERROR: unknown clustering method '{method}' (expected leiden|louvain)")
        return 2
    n_neighbors = int(tool_params.get("n_neighbors", 15))
    random_state = int(tool_params.get("random_state", 0))
    use_rep = tool_params.get("use_rep", "X_pca")

    # Cluster at EVERY requested resolution so the stage-2 gallery shows distinct
    # panels. Prefer the full `resolutions` list; fall back to the single
    # `resolution` for back-compat with older callers.
    resolutions = tool_params.get("resolutions")
    if not resolutions:
        resolutions = [float(tool_params.get("resolution", 1.0))]
    resolutions = [float(r) for r in resolutions]

    work = Path(_env("HARITICA_WORK_DIR", "/mnt/nvme"))
    work.mkdir(parents=True, exist_ok=True)
    h5ad_local = work / Path(h5ad_key).name
    labels_local = work / "cluster_labels.csv"

    s3 = boto3.client("s3")
    print(f"[run_clustering] downloading s3://{input_bucket}/{h5ad_key} -> {h5ad_local}")
    s3.download_file(input_bucket, h5ad_key, str(h5ad_local))

    print(f"[run_clustering] loading embedding ({h5ad_local}, use_rep={use_rep})")
    ad = _load_embedding(str(h5ad_local), use_rep)

    print(f"[run_clustering] computing neighbors (n_neighbors={n_neighbors}, use_rep={use_rep})")
    sc.pp.neighbors(ad, n_neighbors=n_neighbors, use_rep=use_rep)

    # Community detection is cheap once the kNN graph exists, so loop all
    # resolutions on the one graph. Column `cluster_res{i}` per resolution index;
    # the stage-2 gallery loop iterates the SAME ordered resolutions list and maps
    # resolution i -> cluster_res{i}. `cluster` (== resolutions[0]) is kept for
    # back-compat with single-resolution consumers.
    cluster_fn = sc.tl.leiden if method == "leiden" else sc.tl.louvain
    label_cols = []
    for i, res in enumerate(resolutions):
        col = f"cluster_res{i}"
        print(
            f"[run_clustering] running {method} "
            f"(resolution={res}, idx={i}, random_state={random_state}) -> {col}"
        )
        cluster_fn(ad, resolution=res, random_state=random_state, key_added=col)
        label_cols.append(col)

    print(f"[run_clustering] writing labels CSV {labels_local} ({len(label_cols)} resolution(s))")
    idx = ad.obs.index.astype(str).tolist()
    cols_data = [ad.obs[c].astype(str).tolist() for c in label_cols]
    first = cols_data[0]  # resolutions[0] -> back-compat `cluster`
    with open(labels_local, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell_index", "cluster"] + label_cols)
        for row_i, cell in enumerate(idx):
            w.writerow([cell, first[row_i]] + [cd[row_i] for cd in cols_data])

    label_key = f"{out_prefix.rstrip('/')}/cluster_labels.csv"
    print(f"[run_clustering] uploading -> s3://{out_bucket}/{label_key}")
    s3.upload_file(str(labels_local), out_bucket, label_key)

    marker_key = f"{out_prefix.rstrip('/')}/_COMPLETE"
    s3.put_object(Bucket=out_bucket, Key=marker_key, Body=b"")
    print(f"[run_clustering] wrote completion marker s3://{out_bucket}/{marker_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
