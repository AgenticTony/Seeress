"""Naming a proposal, and keeping it: the file a proposal is remembered in.

The contract tests in `tests/contract/` hold the shape of a proposal. These hold what is done
with one: the name it is known by, and the line it is written as.
"""

import pytest

from seeress import flows, proposals

SITES = ["mail.example.net", "intranet_server", "sap-01.corp.example"]


# ----------------------------------------------------------------- the name ---


def test_a_sequence_of_sites_always_has_the_same_name() -> None:
    assert proposals.name_for(SITES) == proposals.name_for(SITES)
    assert proposals.name_for(SITES) == proposals.name_for(tuple(SITES))


def test_two_sequences_that_differ_never_share_a_name() -> None:
    one_more = [*SITES, "one-more.example"]

    names = {
        proposals.name_for(sites) for sites in (SITES, SITES[:2], one_more, list(reversed(SITES)))
    }

    assert len(names) == 4


def test_two_long_sequences_that_differ_only_beyond_the_cut_have_different_names() -> None:
    first = [f"s{i}.example" for i in range(12)]
    second = [*first[:11], "other.example"]

    assert proposals.name_for(first)[:31] == proposals.name_for(second)[:31]
    assert proposals.name_for(first) != proposals.name_for(second)


def test_the_name_shows_the_sites_and_ends_in_eight_hexadecimal_digits() -> None:
    name = proposals.name_for(["mail.example.net", "dms.example"])

    _, _, digits = name.rpartition("-")

    assert name.startswith("mail-example-net-dms-example-")
    assert len(digits) == 8
    assert all(digit in "0123456789abcdef" for digit in digits)


@pytest.mark.parametrize(
    "sites",
    [
        pytest.param(["a"], id="one site of one letter"),
        pytest.param(
            ["-lead.example", ".dots.example", "__"],
            id="sites that begin with no letter or digit",
        ),
        pytest.param([f"s{i}.{'x' * 40}.example" for i in range(12)], id="twelve long sites"),
        pytest.param(["a" * 60], id="a site of sixty characters"),
        pytest.param(["-", ".", "_"], id="sites that are no letter and no digit at all"),
    ],
)
def test_the_name_is_always_a_name_a_flow_may_have(sites: list[str]) -> None:
    name = proposals.name_for(sites)

    assert flows.NAME.fullmatch(name)
    assert proposals.NAME_SHAPE.fullmatch(name)
    assert 1 <= len(name) <= 40
