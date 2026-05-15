"""GPL Stage-1 Alignment Wrapper

Downloads FASTQ files (and the HISAT2 index + optional GTF) from S3,
runs HISAT2 align, optionally runs featureCounts, then uploads the
resulting BAM(s) (and counts.txt if generated) back to S3 under
$HARITICA_GPL_OUTPUT_PREFIX. Writes a `manifest.json` (per-sample
control/treatment grouping) then a `_COMPLETE` marker LAST so stage 2
can wait on the marker without racing the upload.

Input contract
--------------
- HARITICA_GPL_INPUT_KEYS (JSON array): flat list of every FASTQ S3 key
  (all r1 + r2) — used purely for the bulk download.
- HARITICA_GPL_TOOL_PARAMS (JSON object): tool params. When it carries a
  non-empty "samples" list, alignment is driven off that list (the
  writer is the single source of truth for control/treatment grouping):
    {"paired": bool, "strandedness": "0"|"1"|"2"|null, "gtf_key": null,
     "hisat2_index_key": "genomes/{genome_id}/{index_basename}",
     "samples": [ {"sample_id": str, "group": "control"|"treatment",
                   "r1_key": "<s3 key>", "r2_key": "<s3 key>"|null}, ...]}
  Each sample's r1_key/r2_key are mapped to its downloaded local file by
  basename — one BAM per sample, NO replicate merging. When "samples" is
  absent/empty the legacy flat-list stem-inference path is used as a
  back-compat fallback.

Output contract
---------------
- {sample_id}.bam + {sample_id}.bam.bai for every sample.
- manifest.json (JSON array): [{"sample_id", "group", "bam"}, ...] —
  the stage-2 consumer assigns BAMs to control/treatment by `group`,
  never by sort order.
- _COMPLETE marker — written LAST, after the manifest.

License: GPL-3.0-or-later (this wrapper aggregates GPL-3 binaries —
HISAT2, subread/featureCounts — plus the SciPy stack). The wrapper
itself is GPL-3 to satisfy the FSF "mere aggregation" boundary with
the closed-source main image (`haritica-{env}-api`), which talks to
this image only via S3.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urlparse

import boto3


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"[run_alignment] ERROR: required env var {name} is not set")
        sys.exit(2)
    return val or ""


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Return (bucket, key_prefix) from a full s3:// URI."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"not an s3:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _run(cmd: List[str], *, cwd: Path | None = None) -> None:
    print(f"[run_alignment] $ {shlex.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=cwd)


def _download_keys(s3, bucket: str, keys: Iterable[str], dest: Path) -> List[Path]:
    out_paths: List[Path] = []
    for key in keys:
        if not key:
            continue
        local = dest / Path(key).name
        print(f"[run_alignment] downloading s3://{bucket}/{key} -> {local}")
        s3.download_file(bucket, key, str(local))
        out_paths.append(local)
    return out_paths


def _upload_dir(s3, dest_bucket: str, dest_prefix: str, source: Path) -> List[str]:
    """Upload every regular file under `source` to s3://dest_bucket/dest_prefix.
    Returns the list of uploaded keys (without the bucket portion)."""
    uploaded: List[str] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source).as_posix()
        key = f"{dest_prefix.rstrip('/')}/{rel}"
        print(f"[run_alignment] uploading {path} -> s3://{dest_bucket}/{key}")
        s3.upload_file(str(path), dest_bucket, key)
        uploaded.append(key)
    return uploaded


def _align_one(
    index_prefix: str,
    sample: str,
    r1: Path,
    r2: Path | None,
    out_dir: Path,
) -> Path:
    """Align a single sample (paired if r2 is given, else single-end) with
    HISAT2, sort + index, and return the {sample}.bam path. samtools index
    produces the sibling {sample}.bam.bai."""
    sam = out_dir / f"{sample}.sam"
    bam = out_dir / f"{sample}.bam"
    hisat2_cmd = ["hisat2", "-x", index_prefix]
    if r2 is not None:
        hisat2_cmd += ["-1", str(r1), "-2", str(r2)]
    else:
        hisat2_cmd += ["-U", str(r1)]
    hisat2_cmd += ["-S", str(sam), "-p", "8"]
    _run(hisat2_cmd)
    _run(["samtools", "sort", "-@", "8", "-o", str(bam), str(sam)])
    _run(["samtools", "index", str(bam)])
    sam.unlink(missing_ok=True)
    return bam


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
            "[run_alignment] BATCH_JOB_BASE_ROLE_ARN or HARITICA_USER_ID "
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
        f"[run_alignment] installed scoped credentials "
        f"(user={user_id[:8]}..., session={session_name})",
        flush=True,
    )


def main() -> int:
    _install_scoped_session()
    input_bucket = _env("S3_INPUT_BUCKET", required=True)
    references_bucket = _env("S3_REFERENCES_BUCKET")
    results_bucket = _env("S3_RESULTS_BUCKET", required=True)
    output_prefix_uri = _env("HARITICA_GPL_OUTPUT_PREFIX", required=True)
    input_keys = json.loads(_env("HARITICA_GPL_INPUT_KEYS", "[]"))
    tool_params = json.loads(_env("HARITICA_GPL_TOOL_PARAMS", "{}"))

    out_bucket, out_prefix = _parse_s3_uri(output_prefix_uri)
    if out_bucket != results_bucket:
        # Allow it but log the mismatch — in practice the output prefix is
        # always under S3_RESULTS_BUCKET (see batch_submitter._plan_pipeline).
        print(
            f"[run_alignment] note: output bucket '{out_bucket}' != "
            f"S3_RESULTS_BUCKET '{results_bucket}'"
        )

    work = Path(_env("HARITICA_WORK_DIR", "/mnt/nvme"))
    work.mkdir(parents=True, exist_ok=True)
    fastq_dir = work / "fastq"
    fastq_dir.mkdir(exist_ok=True)
    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)

    s3 = boto3.client("s3")

    # ----- Download FASTQ files -----
    fastq_paths = _download_keys(s3, input_bucket, input_keys, fastq_dir)
    if not fastq_paths:
        print("[run_alignment] ERROR: no FASTQ files to align")
        return 2

    # ----- Resolve HISAT2 index -----
    index_key = tool_params.get("hisat2_index_key") or ""
    index_prefix: str | None = None
    if index_key and references_bucket:
        # Index is a set of .ht2 files sharing a basename. The key may point at
        # the basename (e.g. "indexes/grcz11/genome") or any one .ht2 file.
        idx_dir = work / "hisat2_index"
        idx_dir.mkdir(exist_ok=True)
        # List all keys under the index prefix
        list_prefix = index_key.rsplit("/", 1)[0] if "/" in index_key else index_key
        # Pull every sibling .ht2 file
        paginator = s3.get_paginator("list_objects_v2")
        downloaded_any = False
        for page in paginator.paginate(Bucket=references_bucket, Prefix=list_prefix):
            for entry in page.get("Contents", []) or []:
                k = entry["Key"]
                if not (k.endswith(".ht2") or k.endswith(".ht2l")):
                    continue
                local = idx_dir / Path(k).name
                print(f"[run_alignment] downloading index s3://{references_bucket}/{k} -> {local}")
                s3.download_file(references_bucket, k, str(local))
                downloaded_any = True
        if downloaded_any:
            # Strip any .#.ht2 suffix to find the basename
            sample = next(idx_dir.glob("*.ht2"), None) or next(idx_dir.glob("*.ht2l"), None)
            if sample:
                base = sample.name
                # e.g. "genome.1.ht2" -> "genome"
                if ".ht2" in base:
                    base = base.rsplit(".", 2)[0]
                index_prefix = str(idx_dir / base)
        else:
            print(f"[run_alignment] WARN: no .ht2 files found under s3://{references_bucket}/{list_prefix}")
    if not index_prefix:
        print("[run_alignment] ERROR: HISAT2 index could not be resolved (set tool_params.hisat2_index_key + S3_REFERENCES_BUCKET)")
        return 2

    # ----- Run HISAT2 -----
    # `bam_paths` feeds the optional featureCounts call; `manifest_entries`
    # records the control/treatment grouping for the stage-2 consumer.
    paired = bool(tool_params.get("paired", False))
    bam_paths: List[Path] = []
    manifest_entries: List[dict] = []

    samples = tool_params.get("samples") or []
    if samples:
        # ---- Preferred path: explicit `samples` array from the writer ----
        # The writer is the single source of truth for control/treatment.
        # Map each sample's r1_key/r2_key onto its downloaded local file by
        # basename. One BAM per sample, NO replicate merging.
        by_basename = {p.name: p for p in fastq_paths}

        def _resolve(key: str | None) -> Path | None:
            if not key:
                return None
            local = by_basename.get(Path(key).name)
            if local is None:
                # A wrong split = silently inverted DE results — fail loud.
                print(
                    f"[run_alignment] ERROR: FASTQ key {key!r} (basename "
                    f"{Path(key).name!r}) was not among the downloaded inputs "
                    f"{sorted(by_basename)}"
                )
                sys.exit(2)
            return local

        seen_ids: set[str] = set()
        for entry in samples:
            sample_id = str(entry.get("sample_id") or "").strip()
            group = str(entry.get("group") or "").strip()
            if not sample_id:
                print(f"[run_alignment] ERROR: sample entry missing sample_id: {entry!r}")
                return 2
            if sample_id in seen_ids:
                print(f"[run_alignment] ERROR: duplicate sample_id {sample_id!r}")
                return 2
            seen_ids.add(sample_id)
            if group not in ("control", "treatment"):
                print(
                    f"[run_alignment] ERROR: sample {sample_id!r} has invalid group "
                    f"{group!r} (expected 'control' or 'treatment')"
                )
                return 2
            r1 = _resolve(entry.get("r1_key"))
            if r1 is None:
                print(f"[run_alignment] ERROR: sample {sample_id!r} has no r1_key")
                return 2
            # Single-end when r2_key is null/absent; paired otherwise.
            r2 = _resolve(entry.get("r2_key")) if entry.get("r2_key") else None
            print(
                f"[run_alignment] aligning sample={sample_id} group={group} "
                f"r1={r1.name}" + (f" r2={r2.name}" if r2 else " (single-end)")
            )
            bam = _align_one(index_prefix, sample_id, r1, r2, out_dir)
            bam_paths.append(bam)
            manifest_entries.append(
                {"sample_id": sample_id, "group": group, "bam": bam.name}
            )
    elif paired:
        # ---- Back-compat fallback: flat list, pair adjacent files ----
        # [r1_a, r2_a, r1_b, r2_b, ...] — used only when `samples` is absent.
        if len(fastq_paths) % 2 != 0:
            print("[run_alignment] ERROR: paired=true but odd number of FASTQ inputs")
            return 2
        pairs = list(zip(fastq_paths[0::2], fastq_paths[1::2]))
        for r1, r2 in pairs:
            sample = r1.stem.replace(".fastq", "").replace(".fq", "")
            bam = _align_one(index_prefix, sample, r1, r2, out_dir)
            bam_paths.append(bam)
            manifest_entries.append(
                {"sample_id": sample, "group": "", "bam": bam.name}
            )
    else:
        # ---- Back-compat fallback: flat list, single-end ----
        for r1 in fastq_paths:
            sample = r1.stem.replace(".fastq", "").replace(".fq", "")
            bam = _align_one(index_prefix, sample, r1, None, out_dir)
            bam_paths.append(bam)
            manifest_entries.append(
                {"sample_id": sample, "group": "", "bam": bam.name}
            )

    # ----- Optional featureCounts -----
    gtf_key = tool_params.get("gtf_key")
    if gtf_key and references_bucket:
        gtf_local = work / Path(gtf_key).name
        print(f"[run_alignment] downloading GTF s3://{references_bucket}/{gtf_key} -> {gtf_local}")
        s3.download_file(references_bucket, gtf_key, str(gtf_local))

        counts_path = out_dir / "counts.txt"
        strandedness = str(tool_params.get("strandedness") or "0")
        fc_cmd = [
            "featureCounts",
            "-T", "8",
            "-s", strandedness,
            "-a", str(gtf_local),
            "-o", str(counts_path),
        ]
        if paired:
            fc_cmd.append("-p")
        fc_cmd.extend(str(b) for b in bam_paths)
        _run(fc_cmd)

    # ----- Write manifest.json (BEFORE upload, so _upload_dir ships it) -----
    # The stage-2 consumer reads this to assign BAMs to control/treatment by
    # `group` — never by sort order. Lands at {out_prefix}/manifest.json.
    manifest_local = out_dir / "manifest.json"
    with open(manifest_local, "w") as f:
        json.dump(manifest_entries, f, indent=2)
    print(f"[run_alignment] wrote manifest.json with {len(manifest_entries)} sample(s)")

    # ----- Upload outputs (BAMs, .bam.bai, manifest.json) -----
    uploaded = _upload_dir(s3, out_bucket, out_prefix, out_dir)
    print(f"[run_alignment] uploaded {len(uploaded)} output objects under s3://{out_bucket}/{out_prefix}")

    # ----- Write _COMPLETE marker LAST (the very last S3 write) -----
    marker_key = f"{out_prefix.rstrip('/')}/_COMPLETE"
    s3.put_object(Bucket=out_bucket, Key=marker_key, Body=b"")
    print(f"[run_alignment] wrote completion marker s3://{out_bucket}/{marker_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
