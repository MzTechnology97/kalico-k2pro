from klippy.extras.box_auto_mapping import (
    color_cost,
    material_cost,
    suggest_mapping,
)


def tool(tool_id, material, color, name=""):
    return {
        "tool": tool_id,
        "material": material,
        "color": color,
        "name": name,
    }


def slot(index, material, color, name="", external=False, loaded=False):
    return {
        "index": index,
        "material": material,
        "color": color,
        "name": name,
        "external": external,
        "loaded": loaded,
    }


def test_exact_material_and_color_match():
    mapping, unresolved = suggest_mapping(
        [tool(0, "PETG", "#00C1AE", "Generic PETG")],
        [slot(2, "PETG", "#00C1AE", "Generic PETG")],
    )
    assert mapping == {0: 2}
    assert unresolved == []


def test_related_material_family_is_allowed():
    assert material_cost("PLA+", "PLA") == 0.05
    assert material_cost("PETG-CF", "PETG") == 0.05
    assert material_cost("ABS", "PLA") is None


def test_different_hue_is_not_accepted():
    assert color_cost("#FF0000", "#00FF00") is None


def test_each_tool_prefers_a_distinct_matching_slot():
    tools = [
        tool(0, "PETG", "#FF0000"),
        tool(1, "PETG", "#0000FF"),
    ]
    slots = [
        slot(1, "PETG", "#FF0000"),
        slot(2, "PETG", "#0000FF"),
    ]
    mapping, unresolved = suggest_mapping(tools, slots)
    assert mapping == {0: 1, 1: 2}
    assert unresolved == []


def test_single_tool_can_fall_back_to_only_loaded_source():
    mapping, unresolved = suggest_mapping(
        [tool(0, "ABS", "#111111")],
        [slot(4, "", "", external=True, loaded=True)],
    )
    assert mapping == {0: 4}
    assert unresolved == []


def test_multicolor_unresolved_tool_is_reported():
    mapping, unresolved = suggest_mapping(
        [
            tool(0, "PETG", "#FF0000"),
            tool(1, "PLA", "#00FF00"),
        ],
        [slot(1, "PETG", "#FF0000")],
    )
    assert mapping == {0: 1}
    assert unresolved == [1]