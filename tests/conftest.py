import pytest

from localclass.config import Limits
from localclass.testing.harness import Cluster


def fast_limits(**over) -> Limits:
    lim = Limits(anti_entropy_interval=1.0, heartbeat_interval=1.0, heartbeat_timeout=4.0, sync_page_size=50)
    for k, v in over.items():
        setattr(lim, k, v)
    return lim


@pytest.fixture
async def cluster(tmp_path):
    clusters = []

    async def make(labels, **kw):
        c = Cluster(tmp_path / "cluster", labels, limits=kw.pop("limits", fast_limits()), **kw)
        await c.start()
        clusters.append(c)
        return c
    yield make
    for c in clusters:
        await c.stop()
