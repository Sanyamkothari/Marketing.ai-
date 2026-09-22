#!/usr/bin/env bash
# Build the image, push it, and write the DIGEST it was given to `.image-digest`.
#
# The digest is the point. A tag can be moved after it has been tested - by a later build, by a
# person, by a retry - so a deployment that names a tag is a deployment that cannot say what it is
# running. `make aws-deploy` reads this file and passes the digest to `cdk deploy`, so the image the
# service runs is byte-for-byte the image the pipeline built and measured.
#
# Environment:
#   ECR_REGISTRY    required, e.g. 111122223333.dkr.ecr.ap-south-1.amazonaws.com
#   ECR_REPOSITORY  required, e.g. marketing-ai
#   IMAGE_TAG       optional, defaults to the short commit; the digest is what matters either way
#   PLATFORM        optional, defaults to linux/amd64 (Fargate and SageMaker CPU instances)
set -euo pipefail

: "${ECR_REGISTRY:?set ECR_REGISTRY, e.g. 111122223333.dkr.ecr.ap-south-1.amazonaws.com}"
: "${ECR_REPOSITORY:?set ECR_REPOSITORY, e.g. marketing-ai}"

IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
PLATFORM="${PLATFORM:-linux/amd64}"
REPO="${ECR_REGISTRY}/${ECR_REPOSITORY}"
DIGEST_FILE="${DIGEST_FILE:-.image-digest}"

echo "building ${REPO}:${IMAGE_TAG} for ${PLATFORM}"
docker buildx build \
  --platform "${PLATFORM}" \
  --target api \
  --tag "${REPO}:${IMAGE_TAG}" \
  --provenance=false \
  --push \
  .

# `--push` already uploaded it; ask the registry what digest it stored rather than trusting a local
# one, because a manifest list and a single-platform manifest have different digests and only the
# registry knows which one this repository now points at.
digest="$(docker buildx imagetools inspect "${REPO}:${IMAGE_TAG}" --format '{{json .Manifest.Digest}}' | tr -d '"')"
if [[ -z "${digest}" || "${digest}" != sha256:* ]]; then
  echo "the registry did not report a sha256 digest for ${REPO}:${IMAGE_TAG}" >&2
  exit 1
fi

printf '%s@%s\n' "${REPO}" "${digest}" > "${DIGEST_FILE}"
echo "pushed ${REPO}@${digest}"
echo "wrote ${DIGEST_FILE}"
