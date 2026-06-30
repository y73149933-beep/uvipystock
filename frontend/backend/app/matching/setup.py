"""Build script for the Cython matching engine.

Usage
-----
.. code-block:: bash

    cd backend/app/matching
    python setup.py build_ext --inplace

This compiles `engine.pyx` into `engine.<abi>.so` next to it, importable
as `from app.matching.engine import CMatchingEngine`.

The same script is invoked by `infra/scripts/build_cython.sh` during
Docker image build.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from setuptools import Extension, setup
from Cython.Build import cythonize

# ─── Locate this directory (so the script works from any CWD) ────────────────
HERE = Path(__file__).resolve().parent

# ─── Extension definition ────────────────────────────────────────────────────
# The .so will be placed next to engine.pyx via `build_dir` and `--inplace`.
extensions = [
    Extension(
        name="app.matching.engine",
        sources=[str(HERE / "engine.pyx")],
        extra_compile_args=["-O3", "-ffast-math", "-Wall"],
        # Uncomment for profiling-grade builds:
        # extra_compile_args=["-O3", "-g", "-pg"],
    ),
]

# ─── Cythonize ───────────────────────────────────────────────────────────────
# The .pxd next to engine.pyx is picked up automatically.
compiler_directives = {
    "language_level": 3,
    "boundscheck": False,
    "wraparound": False,
    "cdivision": True,
    "initializedcheck": False,
    "embedsignature": True,  # inspectable signatures in REPL
}

setup(
    name="crypto-exchange-matching-engine",
    ext_modules=cythonize(
        extensions,
        compiler_directives=compiler_directives,
        # Force rebuild if .pyx changed — avoids stale .so after edits.
        force=True,
    ),
    # No package_dir / packages: we are building a single .so that lives
    # next to engine.pyx. The module is importable as `app.matching.engine`
    # because the .so filename encodes the full dotted name.
    zip_safe=False,
)

if "--inplace" in sys.argv:
    print("\n✓ Cython matching engine compiled inplace.")
    so_files = list(HERE.glob("engine*.so"))
    for f in so_files:
        print(f"  {f}")
