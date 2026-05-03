"""ARM debug components.

Importing this package triggers the registration of ARM JTAG-DP
TAPs (and, as later slices land, AP types and CoreSight
components) in the relevant registries.
"""

from . import jtag_dp  # noqa: F401
