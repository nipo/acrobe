"""Texas Instruments debug components.

Importing this package registers TI vendor TAPs (IcePick router,
chip-specific secondary TAPs) in the relevant registries.
"""

from . import icepick  # noqa: F401
from . import cc2650  # noqa: F401
