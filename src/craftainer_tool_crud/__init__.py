"""Generic CRUD framework.

Router factories, interface, repositories, and model/view bases that a
resource package under `app.crud_1` builds on.
"""

import warnings

warnings.warn(
    "craftainer-tool-crud is renamed to craftainer-crud; "
    "install craftainer-crud and import craftainer_crud. "
    "This package receives no further releases.",
    DeprecationWarning,
    stacklevel=2,
)
