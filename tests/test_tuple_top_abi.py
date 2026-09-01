from __future__ import annotations

from zlang import compile_source
from zlang.ir import build_top_physical_abi


SOURCE = """
module TupleLeafABI {
    in p : (u8,(bit,u4))
    out q : (u8,(bit,u4))
    q = p
}
"""


def test_nested_tuple_public_leaves_have_positional_names_and_exact_slices() -> None:
    abi = build_top_physical_abi(
        compile_source(SOURCE, include_clash=False).ir
    )
    leaves = {leaf.leaf_semantic_id: leaf for leaf in abi.leaves}

    assert tuple(leaves) == (
        "port:p.item0",
        "port:p.item1.item0",
        "port:p.item1.item1",
        "port:q.item0",
        "port:q.item1.item0",
        "port:q.item1.item1",
    )
    assert tuple(leaf.external_name for leaf in leaves.values()) == (
        "p__item0",
        "p__item1__item0",
        "p__item1__item1",
        "q__item0",
        "q__item1__item0",
        "q__item1__item1",
    )
    assert tuple(leaf.member_path for leaf in leaves.values()) == (
        ("p", "item0"),
        ("p", "item1", "item0"),
        ("p", "item1", "item1"),
        ("q", "item0"),
        ("q", "item1", "item0"),
        ("q", "item1", "item1"),
    )
    assert tuple(
        (leaf.packed_msb, leaf.packed_lsb)
        for leaf in leaves.values()
    ) == (
        (12, 5),
        (4, 4),
        (3, 0),
        (12, 5),
        (4, 4),
        (3, 0),
    )


def test_vector_of_tuples_preserves_public_arrays_and_msb_first_aos_slices() -> None:
    source = """
    module TupleArrayABI {
        in p : vec<2,(u4,bit)>
        out q : vec<2,(u4,bit)>
        q = p
    }
    """
    abi = build_top_physical_abi(
        compile_source(source, include_clash=False).ir
    )
    leaves = {leaf.leaf_semantic_id: leaf for leaf in abi.leaves}

    first = leaves["port:p.item0"]
    second = leaves["port:p.item1"]
    assert (first.external_name, second.external_name) == (
        "p__item0",
        "p__item1",
    )
    assert first.array_dimensions == second.array_dimensions == (2,)
    assert str(first.canonical_type) == "vec<2,u4>"
    assert str(second.canonical_type) == "vec<2,bit>"
    assert tuple(
        (item.indices, item.msb, item.lsb)
        for item in first.packed_element_slices
    ) == (((0,), 9, 6), ((1,), 4, 1))
    assert tuple(
        (item.indices, item.msb, item.lsb)
        for item in second.packed_element_slices
    ) == (((0,), 5, 5), ((1,), 0, 0))
    assert first.packed_msb is first.packed_lsb is None
    assert second.packed_msb is second.packed_lsb is None
