"""Install clean-suite dialect tracing in the main interpreter and spawn workers."""
import os

if os.environ.get("RWX_OBSERVE_ROOT"):
    from blast_radius.scalar_index import install
    install()
