import pytest
from openlr import FOW, FRC, binary_decode

from openlr_demo.decoder import decode_binary
from openlr_demo.encoder import EncodingError, LineEncoder
from openlr_demo.evaluation import PathResult, compare
from openlr_demo.geo import angle_diff
from toy import A, C, D, E, F2, M, P, Q, R, W, Y, Z, detour_network, path, y_split_network


@pytest.fixture(scope="module")
def net():
    return detour_network()


def nodes_of(encoded):
    return [lrp.node for lrp in encoded.lrps]


def test_rule4_valid_nodes(net):
    assert net.is_valid_node(A)  # junction
    assert net.is_valid_node(W)  # dead end
    assert not net.is_valid_node(D)  # 2 in / 2 out, only two neighbours
    assert not net.is_valid_node(F2)  # 1 in / 1 out


def test_intermediate_lrp_where_shortest_path_deviates(net):
    location = path(net, W, A, D, C, E)
    enc = LineEncoder(net).encode(location)
    # The shortest path W-A ... C-E would take the diagonal, so an intermediate LRP pins A -> D.
    assert nodes_of(enc) == [W, A, E]
    assert all(lrp.node_valid for lrp in enc.lrps)
    assert [lrp.dnp for lrp in enc.lrps] == [pytest.approx(300, abs=2), pytest.approx(1200, abs=2), 0]
    first, mid, last = enc.lrps
    assert (first.frc, first.fow, first.bear) == (FRC.FRC4, FOW.SINGLE_CARRIAGEWAY, 90)
    assert mid.bear == 0  # A -> D points north
    assert last.bear == 270  # last LRP: bearing looks back along the line (Table 7)
    assert last.lfrcnp == FRC.FRC7 and last.dnp == 0


def test_expansion_to_valid_node_with_offset(net):
    location = path(net, D, C, E)  # starts at the invalid node D
    enc = LineEncoder(net).encode(location)
    assert nodes_of(enc) == [A, E]
    assert enc.lines[0].start == A and enc.p_off == pytest.approx(400, abs=2) and enc.n_off == 0
    decoded = decode_binary(enc.binary, net)
    assert compare(PathResult(location), decoded.result).correct


def test_rule1_prefers_valid_nodes(net):
    enc = LineEncoder(net, max_lrp_distance=1000).encode(path(net, W, A, D, C, E))
    assert nodes_of(enc) == [W, A, C, E]  # C instead of the closer but invalid D
    assert max(lrp.dnp for lrp in enc.lrps) <= 1000


def test_rule5_drops_last_lrp_inside_negative_offset(net):
    location = path(net, W, A, D)  # ends at the invalid node D -> expanded to C, n_off = 500
    enc = LineEncoder(net, max_lrp_distance=450).encode(location)
    assert nodes_of(enc) == [W, A, D]
    assert [ln.id for ln in enc.lines] == [ln.id for ln in location]
    assert enc.n_off == 0
    assert any("removed last LRP" in s for s in enc.steps)
    assert compare(PathResult(location), decode_binary(enc.binary, net).result).correct


def test_binary_round_trip(net):
    enc = LineEncoder(net).encode(path(net, W, A, D, C, E), p_off=120, n_off=80)
    ref = binary_decode(enc.binary)
    assert len(ref.points) == len(enc.lrps)
    for got, lrp in zip(ref.points, enc.lrps):
        assert got.lon == pytest.approx(lrp.lon, abs=3e-5)
        assert got.lat == pytest.approx(lrp.lat, abs=3e-5)
        assert (got.frc, got.fow, got.lfrcnp) == (lrp.frc, lrp.fow, lrp.lfrcnp)
        assert angle_diff(got.bear, lrp.bear) <= 11.25 / 2 + 0.5  # 32 sectors, decoded as rounded midpoint
        assert abs(got.dnp - lrp.dnp) <= 58.6 / 2 + 0.5  # DNP intervals of 58.6 m
    assert ref.poffs * enc.lrps[0].dnp == pytest.approx(120, abs=2)
    assert ref.noffs * enc.lrps[-2].dnp == pytest.approx(80, abs=6)


def test_decode_with_offsets(net):
    location = PathResult(path(net, W, A, D, C, E), 120, 80)
    enc = LineEncoder(net).encode(location.lines, location.p_off, location.n_off)
    metrics = compare(location, decode_binary(enc.binary, net).result)
    assert metrics.precision > 0.98 and metrics.recall > 0.98


def test_invalid_locations(net):
    with pytest.raises(EncodingError):
        LineEncoder(net).encode([])
    with pytest.raises(EncodingError):
        LineEncoder(net).encode([*path(net, W, A), *path(net, D, C)])  # not connected
    with pytest.raises(EncodingError):
        LineEncoder(net).encode(path(net, W, A), p_off=200, n_off=100)  # offsets >= length


def test_y_split_gets_an_extra_lrp():
    net = y_split_network()
    location = path(net, Y, P, M, Z)
    plain = LineEncoder(net, disambiguate=False).encode(location)
    pinned = LineEncoder(net).encode(location)
    assert nodes_of(plain) == [Y, Z]
    assert nodes_of(pinned) == [Y, P, Z]  # step 8: extra LRP right behind the split
    decoded = decode_binary(pinned.binary, net).result
    assert [ln.id for ln in decoded.lines] == [ln.id for ln in location]
    assert Q not in {ln.end for ln in decoded.lines}
    assert R not in nodes_of(pinned) and M not in nodes_of(pinned)
