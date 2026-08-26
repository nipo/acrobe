"""Setuptools shim for the optional Cython extension.

Project metadata lives in pyproject.toml. The only reason this file
exists is to compile ``acrobe/_bitstring_cy.pyx`` to a native
extension. A successful build provides accelerated implementations
of the BitString classes; ``acrobe.bitstring`` falls back to its
pure-Python bodies whenever the extension can't be loaded, so the
build is genuinely optional.
"""

from setuptools import setup

try:
    from Cython.Build import cythonize
    ext_modules = cythonize(
        ["acrobe/_bitstring_cy.pyx"],
        language_level=3,
        compiler_directives={
            "boundscheck": False,
            "wraparound": False,
        },
    )
except ImportError:
    # Cython missing at build time — ship pure-Python only. This is a
    # supported configuration; bitstring.py works without the
    # extension and tests/benchmarks transparently target whichever
    # implementation is present.
    ext_modules = []


setup(ext_modules=ext_modules)
