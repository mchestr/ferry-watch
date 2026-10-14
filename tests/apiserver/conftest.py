from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.apiserver.cluster import Cluster, running_cluster


@pytest.fixture(scope="session")
def cluster() -> Iterator[Cluster]:
    with running_cluster() as c:
        yield c
