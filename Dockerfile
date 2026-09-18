# Plain single-stage build -- no compiled extensions of our own, and
# uvicorn[standard]/entsoe-py/pandas all ship manylinux wheels for this
# base image, so there's no build-toolchain stage to bother with.
FROM python:3.12-slim

WORKDIR /app

# Only what `pip install` actually needs -- pyproject.toml's own
# package-discovery ([tool.setuptools.packages.find] where=["src"])
# means the real source tree has to be present at install time, so this
# doesn't get the classic "copy manifest, install, then copy code"
# layer-caching win; kept as two COPY lines anyway since it's still the
# clearest way to say "these two things are the whole build input".
COPY pyproject.toml ./
COPY src ./src

# Editable (-e), not a normal install -- load-bearing, not a dev leftover:
# storage.DEFAULT_DB_PATH is computed as `Path(__file__).resolve().parents[2]`
# (storage.py -> gbpw -> src -> repo root), which only lands on /app if
# __file__ still points into /app/src/gbpw/storage.py. A regular
# `pip install .` copies the package into site-packages instead, silently
# repointing the database at a path inside the image rather than the
# /app/data volume mounted below -- confirmed by checking what
# DEFAULT_DB_PATH actually resolves to before writing this.
RUN pip install --no-cache-dir -e .

# data/ (SQLite db) and out/ (built weekly-report HTML) must be mounted
# from the host or a named volume in any real deployment -- see the
# README's Deployment section. Declaring them here is a safety net
# (anything written to an undeclared path is lost on container removal
# regardless), not a substitute for actually mounting a volume when you
# run the image.
VOLUME ["/app/data", "/app/out"]

EXPOSE 5000

# Run exactly one container from this image at a time -- see the
# README's Deployment section for why (background_refresh.py's polling
# loop runs inside the app process itself, so a second instance means a
# second independent poller hitting the same external APIs and the same
# SQLite file, not more capacity).
CMD ["gbpw", "serve", "--host", "0.0.0.0", "--port", "5000"]
