# Haritica Cloud GPL-Extras Image
#
# This image contains the GPL-3 licensed bioinformatics tools required by
# stage-1 GPL prep jobs in the AWS Batch pipeline:
#   - HISAT2 v2.2.1 (GPL-3) — RNA-seq aligner
#   - subread/featureCounts v2.0.6 (GPL-3) — gene-level read counting
#   - leidenalg / louvain / igraph (GPL-3) — single-cell community detection
#
# RUNTIME POLICY: NO closed-source Haritica server code lives in this image.
# Only the thin GPL-3 wrapper script (gpl-extras/entrypoint.sh) and standard
# scientific Python libraries (numpy, pandas, anndata, scanpy) needed to run
# the wrapper are present. Communication with stage-2 (the closed-source
# main image, haritica-{env}-api) happens exclusively via S3 — FSF "mere
# aggregation". See docs/LICENSING-CLOUD.md for the full architecture.
#
# This Dockerfile is also published to the public GPL-3 source repo at
# github.com/ThomasXielt/haritica-cloud-gpl-extras (FSF mere-aggregation
# pattern; see WRITTEN_OFFER.txt). The CI build job for this image is
# colocated with the main cloud-deploy workflow but produces an
# independently-tagged ECR artifact (haritica-{env}-gpl-extras).

FROM python:3.11-slim

# ---------------------------------------------------------------------------
# Stage 1: System tooling (apt) — needed for HISAT2/subread builds + samtools
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    unzip \
    bzip2 \
    samtools \
    awscli \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# HISAT2 v2.2.1 (GPL-3) — RNA-seq aligner
#
# Source: upstream binary distribution from the HISAT2 BioHPC mirror
# (https://daehwankimlab.github.io/hisat2/download/). The s3 URL the desktop
# installer resolves (genome-idx.s3.amazonaws.com/hisat/...) returns 404 on
# Linux x86_64; the swmed BioHPC link is the canonical upstream alternative.
# ---------------------------------------------------------------------------
RUN curl -fsSL "https://cloud.biohpc.swmed.edu/index.php/s/oTtGWbWjaxsQ2Ho/download" \
      -o /tmp/hisat2.zip \
    && unzip -q /tmp/hisat2.zip -d /opt/ \
    && ln -sf /opt/hisat2-2.2.1/hisat2 /usr/local/bin/hisat2 \
    && ln -sf /opt/hisat2-2.2.1/hisat2-align-s /usr/local/bin/hisat2-align-s \
    && ln -sf /opt/hisat2-2.2.1/hisat2-align-l /usr/local/bin/hisat2-align-l \
    && ln -sf /opt/hisat2-2.2.1/hisat2-build /usr/local/bin/hisat2-build \
    && ln -sf /opt/hisat2-2.2.1/hisat2-inspect /usr/local/bin/hisat2-inspect \
    && rm /tmp/hisat2.zip

# ---------------------------------------------------------------------------
# subread v2.0.6 (GPL-3) — provides featureCounts for gene-level counting
# ---------------------------------------------------------------------------
RUN curl -fsSL https://sourceforge.net/projects/subread/files/subread-2.0.6/subread-2.0.6-Linux-x86_64.tar.gz/download \
      -o /tmp/subread.tar.gz \
    && tar xzf /tmp/subread.tar.gz -C /opt/ \
    && ln -s /opt/subread-2.0.6-Linux-x86_64/bin/featureCounts /usr/local/bin/featureCounts \
    && ln -s /opt/subread-2.0.6-Linux-x86_64/bin/subjunc /usr/local/bin/subjunc \
    && ln -s /opt/subread-2.0.6-Linux-x86_64/bin/subread-align /usr/local/bin/subread-align \
    && rm /tmp/subread.tar.gz

# ---------------------------------------------------------------------------
# Stage 2: Python deps — leidenalg/louvain/igraph + the SciPy stack the
# wrapper uses for h5ad I/O and AnnData operations.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir \
      'numpy<2.0' \
      'pandas>=2.0,<3.0' \
      'scipy>=1.10' \
      'anndata>=0.10,<0.11' \
      'scanpy>=1.10,<2.0' \
      'leidenalg>=0.10,<0.11' \
      'louvain>=0.8,<0.9' \
      'igraph>=0.11,<0.12' \
      'boto3>=1.28'

# ---------------------------------------------------------------------------
# Stage 3: Wrapper + license artifacts
# ---------------------------------------------------------------------------
COPY gpl-extras/entrypoint.sh /entrypoint.sh
COPY gpl-extras/run_alignment.py /opt/gpl-wrapper/run_alignment.py
COPY gpl-extras/run_clustering.py /opt/gpl-wrapper/run_clustering.py
COPY gpl-extras/LICENSE /usr/share/haritica/LICENSE.notes
COPY gpl-extras/WRITTEN_OFFER.txt /usr/share/haritica/WRITTEN_OFFER.txt
# Bake the canonical GPL-3.0 text into the repo (gpl-extras/LICENSE.gpl-3.0.txt)
# instead of curl-ing it at build time. The CI runner's network can be flaky
# reaching www.gnu.org (observed: 133s timeouts in build-gpl-extras job).
# The committed text matches https://www.gnu.org/licenses/gpl-3.0.txt verbatim
# (35149 bytes, 674 lines, fetched 2026-05-03).
COPY gpl-extras/LICENSE.gpl-3.0.txt /usr/share/haritica/LICENSE
RUN chmod +x /entrypoint.sh

# Default working dir matches what the BatchStack mounts via the `nvme` volume
# (sourcePath /mnt/nvme on the EC2 host). The job definition sets
# HARITICA_WORK_DIR=/mnt/nvme but we make /work the in-image default for
# easier local docker testing.
ENV HARITICA_WORK_DIR=/mnt/nvme
WORKDIR /tmp

# Default command (overridden by the job def's `command: ["/entrypoint.sh"]`).
CMD ["/entrypoint.sh"]
