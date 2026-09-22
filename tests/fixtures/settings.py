"""Builders for `engine.settings.Settings`, for the tests that need a described deployment.

Deliberately not in `tests/conftest.py`: that file's docstring says only the two path fixtures live
there so owners never contend over it (design section 11). A module that wants these imports them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.settings import Settings


def local_settings(**overrides: Any) -> Settings:
    """A laptop: local filesystem, SQLite, thread pool. The defaults, said out loud."""
    values: dict[str, Any] = {"data_dir": Path("data")}
    values.update(overrides)
    return Settings(**values)


def s3_settings(**overrides: Any) -> Settings:
    """An S3-backed deployment with nothing else switched on."""
    values: dict[str, Any] = {
        "env": "dev",
        "aws_region": "ap-south-1",
        "storage_backend": "s3",
        "s3_bucket": "marketing-ai-test",
    }
    values.update(overrides)
    return Settings(**values)


def sagemaker_settings(**overrides: Any) -> Settings:
    """The smallest coherent SageMaker deployment: S3 storage plus everything a job needs."""
    values: dict[str, Any] = {
        "job_backend": "sagemaker",
        "sagemaker_role_arn": "arn:aws:iam::111122223333:role/marketing-ai-dev-sagemaker",
        "sagemaker_image_uri": "111122223333.dkr.ecr.ap-south-1.amazonaws.com/marketing-ai:test",
        "sagemaker_instance_type": "ml.m5.2xlarge",
        "sagemaker_processing_instance_type": "ml.m5.xlarge",
    }
    values.update(overrides)
    return s3_settings(**values)
