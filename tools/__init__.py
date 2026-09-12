"""Developer tooling that is not part of any deployable.

One module today: :mod:`tools.local_env`, which writes the ``.env`` a real-mode local run
needs. It lives here rather than inside a workspace member because nothing shipped reads
Secret Manager — a deployed service is handed its values by its service definition — so
the dependency belongs to the dev group and the code belongs outside every image.
"""
