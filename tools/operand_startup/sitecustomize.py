"""Install observation before SQLGlot imports, including in spawn workers."""
import os
if os.environ.get("RWX_OBSERVE_ROOT"):
    from blast_radius.operand_index import install
    install()
