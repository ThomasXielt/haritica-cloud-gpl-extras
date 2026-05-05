#!/bin/bash
# Haritica GPL-Extras entrypoint
#
# Dispatches stage-1 GPL prep work based on $HARITICA_GPL_RUNNER:
#   - "alignment": HISAT2 align FASTQ -> BAM, optional featureCounts -> counts
#   - "clustering": leiden/louvain on h5ad -> labels CSV
#
# Inputs/outputs are passed via S3, structured by these env vars:
#   HARITICA_GPL_INPUT_KEYS     JSON array of S3 keys (within $S3_INPUT_BUCKET)
#   HARITICA_GPL_OUTPUT_PREFIX  Full s3:// URI to write outputs under
#   HARITICA_GPL_TOOL_PARAMS    JSON object of tool-specific knobs
#   HARITICA_USER_ID            For path scoping / IAM session tag
#   HARITICA_JOB_ID             Job correlation
#
# After successful work the wrapper writes a zero-byte _COMPLETE marker
# to the output prefix LAST. Stage 2 (closed-source main image) waits on
# the marker to avoid a Spot-interruption mid-upload race.

set -euo pipefail

WORK_DIR="${HARITICA_WORK_DIR:-/mnt/nvme}"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

RUNNER="${HARITICA_GPL_RUNNER:-}"
case "$RUNNER" in
  alignment)
    echo "[gpl-extras] dispatching alignment runner (job=$HARITICA_JOB_ID user=$HARITICA_USER_ID)"
    exec python3 /opt/gpl-wrapper/run_alignment.py
    ;;
  clustering)
    echo "[gpl-extras] dispatching clustering runner (job=$HARITICA_JOB_ID user=$HARITICA_USER_ID)"
    exec python3 /opt/gpl-wrapper/run_clustering.py
    ;;
  "")
    echo "[gpl-extras] ERROR: HARITICA_GPL_RUNNER not set"
    exit 2
    ;;
  *)
    echo "[gpl-extras] ERROR: unknown runner '$RUNNER' (expected 'alignment' or 'clustering')"
    exit 2
    ;;
esac
