"""Altera bitstream format parsers (POF, SOF, RBF, CMF) as VFS Nodes.

Importing this package registers the formats; submodules expose
the public symbols (magics, helpers, Node classes). Tests and
peers import from the submodules directly — nothing is re-exported
here.
"""

from . import sof   # noqa: F401  registers altera_sof
from . import pof   # noqa: F401  registers altera_pof
from . import rbf_cyclone10  # noqa: F401  registers altera_rbf
from . import cmf   # noqa: F401  registers altera_cmf
