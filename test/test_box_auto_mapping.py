from klippy.extras.box_auto_mapping import (
    color_cost,
    evaluate_mapping,
    is_material_variant,
    material_cost,
    suggest_mapping,
    suggest_mapping_report,
)


def tool(tool_id, material, color, name="", length_mm=None):
    return {
        "tool": tool_id,
        "material": material,
        "color": color,
        "name": name,
        "length_mm": length_mm,
    }


def slot(index, material, color, name="", external=False, loaded=False,
         rfid_percent=None, remaining_m=None):
    return {
        "index": index,
        "material": material,
        "color": color,
        "name": name,
        "external": external,
        "loaded": loaded,
        "present": True,
        "rfid_percent": rfid_percent,
        "rfid_remaining_m": remaining_m,
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
    assert material_cost("PA6-CF", "PA12-CF") == 0.05


def test_filled_variant_is_never_matched_automatically():
    assert material_cost("PETG-CF", "PETG") is None
    assert material_cost("PLA", "PLA-GF") is None
    assert is_material_variant("PETG", "PETG-CF")
    assert not is_material_variant("PLA", "PLA+")
    report = suggest_mapping_report(
        [tool(0, "PETG", "#000000", "Generic PETG @K2 Pro-all")],
        [slot(0, "PETG-CF", "#000000", "Generic PETG-CF"),
         slot(1, "PETG", "#000000", "Generic PETG")],
    )
    assert report["map"] == {0: 1}
    assert report["warnings"] == []

    # Only the filled variant is loaded: the tool stays unresolved, even with
    # an identical preset name.
    report = suggest_mapping_report(
        [tool(0, "PETG", "#000000", "Generic PETG-CF")],
        [slot(0, "PETG-CF", "#000000", "Generic PETG-CF")],
    )
    assert report["map"] == {}
    assert report["unresolved"] == [0]

    # Nor through the loaded-filament fallback of single-tool prints.
    report = suggest_mapping_report(
        [tool(0, "PETG", "#FF0000")],
        [slot(0, "PETG-CF", "#000000", loaded=True)],
    )
    assert report["unresolved"] == [0]
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


def test_equal_matches_prefer_lowest_known_rfid_remaining():
    mapping, unresolved = suggest_mapping(
        [tool(0, "PETG", "#000000", "Generic PETG")],
        [
            slot(1, "PETG", "#000000", "Generic PETG", rfid_percent=72),
            slot(2, "PETG", "#000000", "Generic PETG", rfid_percent=18),
            slot(3, "PETG", "#000000", "Generic PETG"),
        ],
    )
    assert mapping == {0: 2}
    assert unresolved == []

def test_generic_profile_is_safe_fallback_when_orca_name_does_not_match():
    tools = [
        tool(0, "PETG", "#112233", "My tuned PETG"),
    ]
    slots = [
        dict(slot(1, "PETG", "#112233", "Brand X PETG"), brand="Brand X"),
        dict(slot(2, "PETG", "#112233", "Generic PETG"), brand="Generic"),
    ]

    mapping, unresolved = suggest_mapping(tools, slots)

    assert mapping == {0: 2}
    assert unresolved == []


def test_cube_on_an_almost_empty_spool_maps_and_warns():
    # Cubo_PLA_20m57s.gcode on the reference CFS (2026-10-04).
    tools = [tool(0, "PLA", "#C0C0C0", "Bambu PLA Basic @K2", length_mm=3437.37)]
    slots = [
        slot(0, "PETG-CF", "#000000", "Generic PETG-CF"),
        slot(1, "PLA", "#B1BEC6", "Bambulab PLA Basic", rfid_percent=0.3, remaining_m=1.06),
        slot(2, "PETG-CF", "#000000", "Generic PETG-CF"),
        slot(3, "PLA", "#6C4E43", "Bambulab PLA Basic", rfid_percent=14.7, remaining_m=48.6),
    ]
    report = suggest_mapping_report(tools, slots)
    assert report["map"] == {0: 1}
    assert report["unresolved"] == []
    warning = report["warnings"][0]
    assert warning["kind"] == "low_filament" and warning["slot"] == 1
    assert warning["remaining_m"] == 1.06 and warning["needed_m"] > 3.4


def test_equivalent_spool_with_enough_filament_wins_over_lowest_remaining():
    tools = [tool(0, "PLA", "#FFFFFF", length_mm=20000)]
    slots = [
        slot(1, "PLA", "#FFFFFF", rfid_percent=2, remaining_m=6.0),
        slot(2, "PLA", "#FFFFFF", rfid_percent=60, remaining_m=200.0),
    ]
    report = suggest_mapping_report(tools, slots)
    assert report["map"] == {0: 2}
    assert report["warnings"] == []
    # Without a length the lowest remaining spool is still used up first.
    assert suggest_mapping([tool(0, "PLA", "#FFFFFF")], slots)[0] == {0: 1}


def test_runout_swap_partners_count_towards_the_filament_needed():
    tools = [tool(0, "PLA", "#FFFFFF", length_mm=20000)]
    slots = [
        slot(1, "PLA", "#FFFFFF", rfid_percent=5, remaining_m=12.0),
        slot(2, "PLA", "#FFFFFF", rfid_percent=5, remaining_m=12.0),
    ]
    assert suggest_mapping_report(tools, slots)["warnings"][0]["kind"] == "low_filament"
    assert suggest_mapping_report(tools, slots, swap=True)["warnings"] == []


def test_unknown_remaining_is_treated_as_enough():
    tools = [tool(0, "PLA", "#FFFFFF", length_mm=50000)]
    assert suggest_mapping_report(tools, [slot(1, "PLA", "#FFFFFF")])["warnings"] == []


def test_explicit_map_warnings():
    tools = [tool(0, "PETG", "#000000", length_mm=5000)]
    slots = [slot(3, "PETG-CF", "#000000", remaining_m=2.0)]
    kinds = sorted(w["kind"] for w in evaluate_mapping(tools, slots, {0: 3}))
    assert kinds == ["low_filament", "material_variant"]
