"""Tiny round-trip test for haritica-clustering-helper.

Generates a synthetic h5ad with a known PCA, invokes the helper as a
subprocess, parses the CSV output, and asserts that:
  - the file is the expected shape (one row per cell + header)
  - the cluster labels are non-negative integers
  - exit code is zero

This test does NOT verify cluster *quality* (that's covered in the main
Haritica repo's `tests/test_singlecell_helper_e2e.py`).
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import anndata as ad
import numpy as np


def _make_fixture(tmpdir: Path) -> Path:
    rng = np.random.default_rng(0)
    n_cells = 200
    n_pcs = 10
    X_pca = rng.standard_normal((n_cells, n_pcs)).astype(np.float32)
    obs_index = [f"cell_{i:04d}" for i in range(n_cells)]
    a = ad.AnnData(X=np.zeros((n_cells, 0), dtype=np.float32))
    a.obs.index = obs_index
    a.obsm["X_pca"] = X_pca
    p = tmpdir / "in.h5ad"
    a.write_h5ad(p)
    return p


def test_round_trip_leiden():
    here = Path(__file__).parent.parent
    helper = sys.executable, str(here / "helper.py")  # run via python in dev
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_path = _make_fixture(td)
        out_path = td / "out.csv"
        cmd = list(helper) + [
            "--in", str(in_path),
            "--out", str(out_path),
            "--method", "leiden",
            "--resolution", "0.5",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        with open(out_path) as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["cell_index", "cluster"]
        assert len(rows) == 201  # 1 header + 200 cells
        for r in rows[1:]:
            assert r[0].startswith("cell_")
            int(r[1])  # parseable as int


if __name__ == "__main__":
    test_round_trip_leiden()
    print("OK")
