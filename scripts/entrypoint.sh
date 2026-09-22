#!/bin/sh
# The image's one entrypoint. Which mode runs is decided by the first argument, because that is the
# only lever SageMaker gives a Training job: it launches the container as `<image> train`, appending
# that literal word and nothing else. A Processing job is the other way round - it replaces the
# entrypoint outright with its `ContainerEntrypoint` - so `SageMakerJobRunner` names `job` there.
#
# Anything this script does not recognise is exec'd as a command, so `docker run <image> bash` and
# `docker run <image> alembic current` both work without a second image or a --entrypoint override.
set -eu

mode="${1:-serve}"

case "${mode}" in
  serve)
    shift 2>/dev/null || true
    # One worker: a run is carried by the thread pool inside the process, and a second worker would
    # be a second, unrelated pool that the first knows nothing about. Scaling is the ECS service's
    # job, where the load balancer knows which task it sent a request to.
    exec python -m uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 "$@"
    ;;
  train|job)
    # The same body either way: the mode word only tells us which SageMaker job type launched us,
    # and `scripts/run_job_entrypoint.py` reads what to do from the job spec rather than from argv.
    shift
    exec python -m scripts.run_job_entrypoint "$@"
    ;;
  migrate)
    shift
    exec alembic -c /app/alembic.ini upgrade "${1:-head}"
    ;;
  serve-help|--help|-h)
    echo "usage: <image> [serve|train|job|migrate|<command> ...]" >&2
    exit 0
    ;;
  *)
    exec "$@"
    ;;
esac
