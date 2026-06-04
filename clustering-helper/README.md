# haritica-clustering-helper

GPL-3.0 stand-alone CLI that performs Leiden / Louvain community detection on
an h5ad input. Built as a PyInstaller `--onedir` bundle (launcher + `_internal/`)
and shipped, zipped, on GitHub Releases for Windows x86_64, macOS arm64, macOS
x86_64, and Linux x86_64. Haritica's main (closed-source, permissive-licensed)
sidecar subprocesses to this helper at runtime when the user opts into community
detection clustering — fork+exec, "mere aggregation" by FSF doctrine.

> This directory is **staged** inside the Haritica monorepo so the planning /
> review can happen in one place. It is published to the public GPL-3 repo
> `github.com/ThomasXielt/haritica-cloud-gpl-extras` under the
> `clustering-helper/` directory. Pushing a `clustering-helper-vX.Y.Z` tag there
> runs `.github/workflows/release.yml`, which builds and publishes the four
> per-platform onedir `.zip` bundles (+ `.sha256` sidecars) to a release of the
> same name. The desktop installer pins that tag in
> `analyses/optional_tools.py::_CLUSTERING_HELPER_TAG`.

## Layout

| File | Purpose |
|---|---|
| `helper.py` | Single-file CLI entry-point (`__version__` must match the release tag) |
| `requirements.txt` | anndata, h5py, scikit-learn, leidenalg, louvain, igraph, numpy, pandas, scipy |
| `pyinstaller.spec` | `--onedir` build; `upx=False` (preserves macOS signatures); console on for stderr capture |
| `LICENSE` | GPL-3.0 verbatim |
| `WRITTEN_OFFER.txt` | GPL-3 §6 source-offer accompanying every binary release |
| `.github/workflows/release.yml` | Cross-platform build matrix (Windows/macOS native + Linux in manylinux2014) |
| `tests/test_helper.py` | h5ad round-trip on a tiny fixture |

## CLI contract (consumed by Haritica's `analyses/clustering_helper_client.py`)

```
haritica-clustering-helper \
    --in   <input.h5ad> \
    --out  <output.csv> \
    --method <leiden|louvain> \
    --resolution <float> \
    [--random-state INT] \
    [--n-neighbors INT] \
    [--use-rep STR] \
    [--n-pcs INT]
```

Output CSV: header `cell_index,cluster`, one row per cell. Exit non-zero on
failure with a human-readable error to stderr.
