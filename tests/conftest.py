import pytest

from openlr_demo import osm_fetch
from openlr_demo.network import RoadNetwork


@pytest.fixture(scope="session")
def osm():
    return RoadNetwork.from_osm(osm_fetch.load(osm_fetch.DEFAULT_OUTPUT))
