# SPDX-License-Identifier: GPL-3.0-or-later
"""GPL Stage-1 Genome-Build Wrapper — HISAT2 index builder.

Builds a HISAT2 genome index (the GPL `hisat2-build` binary lives ONLY in this
image) from a FASTA already staged in S3, and uploads the resulting `.ht2`
files back to the references bucket. This closes the "HISAT2 build-mode blocked
in cloud" gap: `hisat2-build` is GPL-3 (only here), while the closed-source
genome-build worker (cloud_genome_build_worker) lives ONLY in the main image —
so a build job routed to this image had no runner and died at container start.
This thin wrapper is the third GPL runner (alongside alignment/clustering),
dispatched by entrypoint.sh on HARITICA_GPL_RUNNER=genome_build.

License boundary: like run_alignment.py / run_clustering.py this wrapper is
GPL-3 and imports NO closed-source Haritica code. It talks to the main image
ONLY via S3 (FSF "mere aggregation"). See docs/LICENSING-CLOUD.md.

Input contract (env vars set by server/cloud_genome_builder.submit_build)
------------------------------------------------------------------------
- HARITICA_BUILD_GENOME_ID   genome id, e.g. "GRCm39"
- HARITICA_BUILD_COMPONENT   must be "hisat2_index" (this runner only builds that)
- S3_REFERENCES_BUCKET       the references bucket (read FASTA, write index)
- BATCH_JOB_BASE_ROLE_ARN    role to AssumeRole for scoped S3 creds (the
                             GenomeBuildBaseRole — read references + write
                             references/genomes/*)
- HARITICA_USER_ID           session tag for the AssumeRole
- HARITICA_JOB_ID            correlation / RoleSessionName
- HARITICA_WORK_DIR          large scratch dir (default /mnt/nvme)

Input FASTA:  s3://{bucket}/genomes/{id}/{id}.fa.gz
Output index: s3://{bucket}/genomes/{id}/hisat2_index/{id}.{1..8}.ht2
              (the canonical HISAT2 subdir layout the probe credits and the
               main-image resolver + run_alignment.py both read).
"""
from __future__ import annotations

import gzip
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

import boto3


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"[run_genome_build] ERROR: required env var {name} is not set")
        sys.exit(2)
    return val or ""


def _run(cmd: List[str], *, cwd: Path | None = None) -> None:
    print(f"[run_genome_build] $ {shlex.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, cwd=cwd)


def _install_scoped_session() -> None:
    """Re-assume BATCH_JOB_BASE_ROLE_ARN with the haritica-user-id session tag.

    Identical mechanism to run_alignment.py — inlined here so this GPL-3 wrapper
    imports NO closed-source code. For genome builds submit_build overrides
    BATCH_JOB_BASE_ROLE_ARN to the GenomeBuildBaseRole, which grants read on the
    references bucket + write on references/genomes/*. No-op when the env vars
    are unset (local debugging uses the default credential chain).
    """
    role_arn = os.environ.get("BATCH_JOB_BASE_ROLE_ARN", "")
    user_id = os.environ.get("HARITICA_USER_ID", "")
    job_id = os.environ.get("HARITICA_JOB_ID", "unknown")
    if not (role_arn and user_id):
        print(
            "[run_genome_build] BATCH_JOB_BASE_ROLE_ARN or HARITICA_USER_ID "
            "unset — using default credential chain",
            flush=True,
        )
        return

    import botocore.session
    from botocore.credentials import RefreshableCredentials

    session_name = f"haritica-build-{job_id[:32]}"

    def _refresh() -> dict:
        sts = boto3.Session().client("sts")
        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            Tags=[{"Key": "haritica-user-id", "Value": user_id}],
            DurationSeconds=3600,
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
        f"[run_genome_build] installed scoped credentials "
        f"(user={user_id[:8]}..., session={session_name})",
        flush=True,
    )


def main() -> int:
    _install_scoped_session()

    genome_id = _env("HARITICA_BUILD_GENOME_ID", required=True)
    component = _env("HARITICA_BUILD_COMPONENT", required=True)
    bucket = _env("S3_REFERENCES_BUCKET", required=True)

    if component != "hisat2_index":
        # The GPL image only builds the GPL component. Everything else is built
        # in the main (permissive) image; routing it here is a wiring bug.
        print(
            f"[run_genome_build] ERROR: this runner only builds hisat2_index, "
            f"got component={component!r}"
        )
        return 2

    work = Path(_env("HARITICA_WORK_DIR", "/mnt/nvme"))
    work.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client("s3")

    # ----- Download the staged FASTA -----
    fasta_key = f"genomes/{genome_id}/{genome_id}.fa.gz"
    fasta_gz = work / f"{genome_id}.fa.gz"
    print(
        f"[run_genome_build] downloading s3://{bucket}/{fasta_key} -> {fasta_gz}",
        flush=True,
    )
    s3.download_file(bucket, fasta_key, str(fasta_gz))

    # hisat2-build needs an UNCOMPRESSED FASTA.
    fasta = work / f"{genome_id}.fa"
    print(f"[run_genome_build] decompressing -> {fasta}", flush=True)
    with gzip.open(fasta_gz, "rb") as src, open(fasta, "wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    fasta_gz.unlink(missing_ok=True)

    # ----- Build the HISAT2 index -----
    # Plain linear FM index (NO --snp/--ss/--exon): peaks ~6-8 GB even for human.
    # Index basename = genome_id, so outputs are {genome_id}.{1..8}.ht2 — the
    # name the resolver derives from the *.1.ht2 sentinel.
    threads = str(os.cpu_count() or 8)
    index_base = work / genome_id
    _run(["hisat2-build", "-p", threads, str(fasta), str(index_base)], cwd=work)
    fasta.unlink(missing_ok=True)

    ht2_files = sorted(work.glob(f"{genome_id}.*.ht2")) + sorted(
        work.glob(f"{genome_id}.*.ht2l")
    )
    if not ht2_files:
        print("[run_genome_build] ERROR: hisat2-build produced no .ht2 files")
        return 2

    # ----- Upload to the canonical HISAT2 subdir layout -----
    out_prefix = f"genomes/{genome_id}/hisat2_index"
    uploaded: List[str] = []
    for f in ht2_files:
        key = f"{out_prefix}/{f.name}"
        size_mb = f.stat().st_size / (1024 * 1024)
        print(
            f"[run_genome_build] uploading {f.name} ({size_mb:.1f} MB) -> "
            f"s3://{bucket}/{key}",
            flush=True,
        )
        s3.upload_file(str(f), bucket, key)
        uploaded.append(key)

    print(
        f"[run_genome_build] DONE: uploaded {len(uploaded)} .ht2 file(s) for "
        f"{genome_id} under s3://{bucket}/{out_prefix}/",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
