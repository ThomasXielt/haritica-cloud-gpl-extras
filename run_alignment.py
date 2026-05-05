"""GPL Stage-1 Alignment Wrapper

Downloads FASTQ files (and the HISAT2 index + optional GTF) from S3,
runs HISAT2 align, optionally runs featureCounts, then uploads the
resulting BAM(s) (and counts.txt if generated) back to S3 under
$HARITICA_GPL_OUTPUT_PREFIX. Writes a `_COMPLETE` marker LAST so
stage 2 can wait on the marker without racing the upload.

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


def main() -> int:
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
    paired = bool(tool_params.get("paired", False))
    bam_paths: List[Path] = []
    if paired:
        # Pair adjacent files: [r1_a, r2_a, r1_b, r2_b, ...]
        if len(fastq_paths) % 2 != 0:
            print("[run_alignment] ERROR: paired=true but odd number of FASTQ inputs")
            return 2
        pairs = list(zip(fastq_paths[0::2], fastq_paths[1::2]))
        for r1, r2 in pairs:
            sample = r1.stem.replace(".fastq", "").replace(".fq", "")
            sam = out_dir / f"{sample}.sam"
            bam = out_dir / f"{sample}.bam"
            _run([
                "hisat2", "-x", index_prefix,
                "-1", str(r1), "-2", str(r2),
                "-S", str(sam), "-p", "8",
            ])
            _run(["samtools", "sort", "-@", "8", "-o", str(bam), str(sam)])
            _run(["samtools", "index", str(bam)])
            sam.unlink(missing_ok=True)
            bam_paths.append(bam)
    else:
        for r1 in fastq_paths:
            sample = r1.stem.replace(".fastq", "").replace(".fq", "")
            sam = out_dir / f"{sample}.sam"
            bam = out_dir / f"{sample}.bam"
            _run([
                "hisat2", "-x", index_prefix,
                "-U", str(r1),
                "-S", str(sam), "-p", "8",
            ])
            _run(["samtools", "sort", "-@", "8", "-o", str(bam), str(sam)])
            _run(["samtools", "index", str(bam)])
            sam.unlink(missing_ok=True)
            bam_paths.append(bam)

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

    # ----- Upload outputs -----
    uploaded = _upload_dir(s3, out_bucket, out_prefix, out_dir)
    print(f"[run_alignment] uploaded {len(uploaded)} output objects under s3://{out_bucket}/{out_prefix}")

    # ----- Write _COMPLETE marker LAST -----
    marker_key = f"{out_prefix.rstrip('/')}/_COMPLETE"
    s3.put_object(Bucket=out_bucket, Key=marker_key, Body=b"")
    print(f"[run_alignment] wrote completion marker s3://{out_bucket}/{marker_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
