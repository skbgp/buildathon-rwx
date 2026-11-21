FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS sqlglot-base

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    SKIP_INTEGRATION=0 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

ADD --checksum=sha256:93b2445f15f395b81c20556777ece19a6c7507df950f13fa9fac4a3be739af0d https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
COPY requirements.lock /tmp/requirements.lock
RUN mkdir /sqlglot \
    && tar -xzf /tmp/sqlglot.tar.gz --strip-components=1 -C /sqlglot \
    && python -m pip install --no-cache-dir -r /tmp/requirements.lock \
    && rm /tmp/sqlglot.tar.gz /tmp/requirements.lock

WORKDIR /app

FROM sqlglot-base AS coverage-index
RUN python -m pip install --no-cache-dir coverage==7.17.4
COPY blast_radius /app/blast_radius
RUN python -m blast_radius.make_index /sqlglot /app/coverage.json.gz

FROM sqlglot-base AS operand-index
COPY blast_radius /app/blast_radius
COPY tools/operand_startup /app/operand_startup
RUN mkdir /tmp/operand-coverage \
    && RWX_OBSERVE_ROOT=/sqlglot RWX_OBSERVE_OUTPUT=/tmp/operand-coverage \
       PYTHONPATH=/app/operand_startup:/app python -c 'from blast_radius.operand_index import run; raise SystemExit(run())' \
    && python -m blast_radius.operand_index /tmp/operand-coverage /app/operands.json.gz

FROM sqlglot-base AS configuration-index
COPY blast_radius /app/blast_radius
COPY tools/operand_startup /app/operand_startup
ARG PROFILE=1
RUN mkdir /tmp/profile \
    && RWX_OBSERVE_ROOT=/sqlglot RWX_OBSERVE_OUTPUT=/tmp/profile RWX_OBSERVE_MAPS=$PROFILE \
       PYTHONPATH=/app/operand_startup:/app python -c 'from blast_radius.operand_index import run; raise SystemExit(run())' \
    && python -m blast_radius.operand_index maps /tmp/profile /app/profile.json.gz

FROM sqlglot-base AS expression-index
COPY blast_radius /app/blast_radius
COPY tools/operand_startup /app/operand_startup
RUN mkdir /tmp/profile \
    && RWX_OBSERVE_ROOT=/sqlglot RWX_OBSERVE_OUTPUT=/tmp/profile RWX_OBSERVE_MAPS=lazy \
       PYTHONPATH=/app/operand_startup:/app python -c 'from blast_radius.scalar_index import run; raise SystemExit(run())' \
    && python -m blast_radius.operand_index maps /tmp/profile /app/expressions.json.gz

FROM sqlglot-base AS scalar-index
COPY blast_radius /app/blast_radius
RUN mkdir /tmp/profile \
    && RWX_OBSERVE_ROOT=/sqlglot RWX_OBSERVE_OUTPUT=/tmp/profile \
       PYTHONPATH=/app/blast_radius/scalar_startup:/app python -c 'from blast_radius.scalar_index import run; raise SystemExit(run())' \
    && python -m blast_radius.scalar_index /tmp/profile /app/scalars.json.gz

FROM sqlglot-base AS submission
COPY blast_radius /app/blast_radius
COPY coverage.json.gz operands.json.gz configurations.json.gz dispatch.json.gz scalars.json.gz expressions.json.gz /app/
USER 65534:65534
CMD ["python", "-m", "blast_radius.select"]
