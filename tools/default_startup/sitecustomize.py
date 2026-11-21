import os
if os.environ.get("RWX_OBSERVE_ROOT"):
    from blast_radius.default_index import install
    install()
