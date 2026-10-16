"""Validation of the YAML: what loads, and what is rejected with a clear message."""
from __future__ import annotations

import copy
import io
from types import ModuleType
from typing import Any

import click
import pytest
from pydantic import ValidationError

def rejects(tool: ModuleType, raw: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as e:
        tool.Country.model_validate(raw)
    return str(e.value)


# ---- the happy path -------------------------------------------------------
def test_valid_config_loads(country: Any) -> None:
    assert [g.name for g in country.regions] == ["us-midwest", "us-southeast"]
    assert [g.name for g in country.metros] == ["cvg", "day"]
    assert country.manifest.layers[0].id == "macro"


def test_group_keys_other_than_regions_are_optional(tool: ModuleType, raw: dict[str, Any]) -> None:
    del raw["meshregions"], raw["metros"]
    raw["manifest"]["layers"] = [{"id": "macro", "type": "regions"}]
    c = tool.Country.model_validate(raw)
    assert c.meshregions == [] and c.metros == []


def test_group_defaults_are_not_shared_between_instances(tool: ModuleType, raw: dict[str, Any]) -> None:
    del raw["meshregions"], raw["metros"]
    raw["manifest"]["layers"] = [{"id": "macro", "type": "regions"}]
    a, b = tool.Country.model_validate(raw), tool.Country.model_validate(raw)
    a.metros.append("x")
    assert b.metros == []


def test_mixed_group_lists_states_and_counties(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["regions"][0]["us-midwest"]["counties"] = {"us-ky": ["Boone", "Kenton"]}
    g = tool.Country.model_validate(raw).regions[0]
    lab = {"states": "ADM1", "counties": "ADM2", "ADM1": "ADM1", "ADM2": "ADM2"}
    assert g.adm1(lab) == ["us-oh", "us-in", "us-mi"]
    assert g.adm2(lab) == {"us-ky": ["Boone", "Kenton"]}


def test_several_labels_for_one_level_are_merged(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["levels"]["provinces"] = "ADM1"
    raw["regions"][0]["us-midwest"]["provinces"] = ["us-wi"]
    g = tool.Country.model_validate(raw).regions[0]
    assert g.adm1(tool.Country.model_validate(raw).labels()) == ["us-oh", "us-in", "us-mi", "us-wi"]


def test_non_us_level_names(tool: ModuleType) -> None:
    raw: dict[str, Any] = {"levels": {"provinces": "ADM1", "divisions": "ADM2"},
           "regions": [{"can-atlantic": {"provinces": ["ca-nl", "ca-pe"]}}],
           "metros": [{"yyz": {"divisions": {"ca-on": ["Toronto"]}}}],
           "manifest": {"layers": [{"id": "p", "type": "provinces"}]}}
    c = tool.Country.model_validate(raw)
    assert c.label_for("ADM1") == "provinces" and c.levels_used() == {"ADM1": True, "ADM2": True}


# ---- levels ---------------------------------------------------------------
def test_levels_are_required(tool: ModuleType, raw: dict[str, Any]) -> None:
    del raw["levels"]
    assert "levels" in rejects(tool, raw)


def test_empty_levels_rejected(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["levels"] = {}
    assert "levels" in rejects(tool, raw)


def test_levels_only_accept_adm1_and_adm2(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["levels"]["wards"] = "ADM3"
    assert "levels" in rejects(tool, raw)


def test_adm2_label_needs_an_adm1_label(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["levels"] = {"counties": "ADM2"}
    raw["regions"] = []
    raw["meshregions"] = []
    raw["manifest"]["layers"] = [{"id": "metro", "type": "metros"}]
    assert "needs an ADM1 label" in rejects(tool, raw)


# ---- group shape ----------------------------------------------------------
@pytest.mark.parametrize("bad", [None, "us-oh", {"us-oh": ["x"]}, [], [1, 2], ["us-oh", 3]])
def test_state_members_must_be_a_nonempty_list_of_strings(tool: ModuleType, raw: dict[str, Any], bad: Any) -> None:
    raw["regions"][0]["us-midwest"]["states"] = bad
    rejects(tool, raw)


@pytest.mark.parametrize("bad", [None, ["Hamilton"], "Hamilton", {}, {"us-oh": []}, {"us-oh": None}, {"us-oh": [1]}])
def test_county_members_must_map_parent_to_nonempty_string_list(tool: ModuleType, raw: dict[str, Any], bad: Any) -> None:
    raw["metros"][0]["cvg"]["counties"] = bad
    rejects(tool, raw)


def test_yaml_booleans_in_names_are_rejected(tool: ModuleType, raw: dict[str, Any]) -> None:
    """Unquoted `No` or `On` parse as booleans; they must not slip through as names."""
    raw["metros"][0]["cvg"]["counties"] = {"us-oh": [False]}
    rejects(tool, raw)


def test_unknown_level_key_is_rejected(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["regions"][0]["us-midwest"] = {"state": ["us-oh"]}          # typo: singular
    assert "unknown level 'state'" in rejects(tool, raw)


def test_adm1_key_must_be_a_list_even_if_a_dict_would_parse(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["regions"][0]["us-midwest"] = {"states": {"us-oh": ["x"]}}
    rejects(tool, raw)


def test_adm2_key_must_be_a_mapping_even_if_a_list_would_parse(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["metros"][0]["cvg"] = {"counties": ["Hamilton"]}
    rejects(tool, raw)


def test_two_groups_in_one_list_item_rejected(tool: ModuleType, raw: dict[str, Any]) -> None:
    """A missing `-` in the YAML puts two group names in one mapping."""
    raw["regions"] = [{"a": {"states": ["us-oh"]}, "b": {"states": ["us-in"]}}]
    assert "one group per list item" in rejects(tool, raw)


def test_duplicate_group_names_rejected_across_types(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["metros"][0] = {"oki": {"counties": {"us-oh": ["Hamilton"]}}}
    assert "name already used in meshregions" in rejects(tool, raw)


def test_at_least_one_group_required(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["regions"] = raw["meshregions"] = raw["metros"] = []
    raw["manifest"]["layers"] = [{"id": "s", "type": "states"}]
    assert "at least one group" in rejects(tool, raw)


def test_unknown_top_level_key_rejected(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["regionz"] = []
    assert "regionz" in rejects(tool, raw)


# ---- output ---------------------------------------------------------------
def test_output_defaults(tool: ModuleType, raw: dict[str, Any]) -> None:
    del raw["output"]
    o = tool.Country.model_validate(raw).output
    assert (o.simplified, o.simplify, o.precision, o.members_only, o.base_layers, o.dissolve) == (
        True, 0.005, 4, True, True, [])


def test_dissolve_must_name_real_group_types(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["output"]["dissolve"] = ["metros", "bogus"]
    assert "dissolve" in rejects(tool, raw)


def test_dissolve_is_a_list_not_a_flag(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["output"]["dissolve"] = True
    rejects(tool, raw)


def test_removed_dissolve_cli_style_values_are_gone(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["output"]["dissolve"] = "metros"
    rejects(tool, raw)


def test_base_layers_entries_must_be_known_levels(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["output"]["base_layers"] = ["states", "wards"]
    assert "unknown level 'wards'" in rejects(tool, raw)


@pytest.mark.parametrize("value, lvl, expected", [
    (True, "ADM1", True), (False, "ADM1", False), ([], "ADM1", False),
    (["states"], "ADM1", True), (["states"], "ADM2", False), (["counties"], "ADM2", True),
    (["ADM1"], "ADM1", True),
])
def test_base_layer_on(tool: ModuleType, labels: dict[str, str], value: Any, lvl: str, expected: bool) -> None:
    assert tool.base_layer_on(value, lvl, labels) is expected


# ---- manifest -------------------------------------------------------------
def test_manifest_is_required(tool: ModuleType, raw: dict[str, Any]) -> None:
    del raw["manifest"]
    assert "manifest" in rejects(tool, raw)


def test_manifest_needs_layers(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["manifest"]["layers"] = []
    assert "layers" in rejects(tool, raw)


def test_duplicate_layer_ids_rejected(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["manifest"]["layers"][1]["id"] = "macro"
    assert "duplicate layer ids" in rejects(tool, raw)


def test_layer_type_must_be_group_type_or_adm1_label(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["manifest"]["layers"][0]["type"] = "region"                  # singular
    assert "must be one of" in rejects(tool, raw)


def test_layer_cannot_be_an_adm2_label(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["manifest"]["layers"][0]["type"] = "counties"
    assert "must be one of" in rejects(tool, raw)


def test_layer_in_must_be_a_group_type(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["manifest"]["layers"][1]["in"] = "states"
    assert "is not a group type" in rejects(tool, raw)


def test_layer_cannot_draw_from_a_dissolved_type(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["output"]["dissolve"] = ["regions"]                           # mesh layer uses `in: regions`
    assert "merges them into one shape" in rejects(tool, raw)


def test_dissolving_a_type_nobody_draws_from_is_fine(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["output"]["dissolve"] = ["metros", "meshregions"]
    raw["manifest"]["layers"][1] = {"id": "mesh", "type": "meshregions"}
    tool.Country.model_validate(raw)


def test_states_layer_needs_the_base_layer(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["output"]["base_layers"] = False
    assert "needs output.base_layers" in rejects(tool, raw)


def test_states_layer_with_in_does_not_need_the_base_layer(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["output"]["base_layers"] = False
    raw["manifest"]["layers"][2] = {"id": "state", "type": "states", "in": "regions"}
    tool.Country.model_validate(raw)


def test_layer_extra_keys_pass_through_and_in_is_aliased(country: Any) -> None:
    macro, mesh, _, metro = country.manifest.layers
    assert macro.colorBy == "feature"
    assert mesh.in_ == "regions"
    assert metro.optional is True and metro.allowOverlap is True


# ---- country codes and the whole file ------------------------------------
def test_iso3_accepts_real_and_geoboundaries_codes(tool: ModuleType, raw: dict[str, Any]) -> None:
    doc = tool.Doc.validate_python({"USA": raw, "XKX": copy.deepcopy(raw)})
    assert set(doc) == {"USA", "XKX"}


def test_iso3_is_uppercased(tool: ModuleType, raw: dict[str, Any]) -> None:
    assert list(tool.Doc.validate_python({"usa": raw})) == ["USA"]


@pytest.mark.parametrize("code", ["UK", "XXX", "USAA", "U1A"])
def test_iso3_rejects_bad_codes(tool: ModuleType, raw: dict[str, Any], code: str) -> None:
    with pytest.raises(ValidationError):
        tool.Doc.validate_python({code: raw})


def test_countries_are_validated_independently(tool: ModuleType, raw: dict[str, Any]) -> None:
    can = copy.deepcopy(raw)
    can["levels"] = {"provinces": "ADM1"}
    can["regions"] = [{"can-atlantic": {"provinces": ["ca-nl"]}}]
    can["meshregions"], can["metros"] = [], []
    can["output"] = {"base_layers": ["provinces"]}
    can["manifest"] = {"layers": [{"id": "p", "type": "provinces"}]}
    doc = tool.Doc.validate_python({"USA": raw, "CAN": can})
    assert doc["USA"].label_for("ADM1") == "states" and doc["CAN"].label_for("ADM1") == "provinces"


def test_one_bad_country_names_its_path(tool: ModuleType, raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["levels"] = {}
    with pytest.raises(click.ClickException) as e:
        tool.load_doc(io.StringIO(__import__("yaml").safe_dump({"USA": raw, "CAN": bad})))
    assert "CAN.levels" in e.value.format_message()


def test_empty_file_loads_as_no_countries(tool: ModuleType) -> None:
    assert tool.load_doc(io.StringIO("")) == {}


def test_top_level_must_be_a_mapping(tool: ModuleType) -> None:
    with pytest.raises(click.ClickException):
        tool.load_doc(io.StringIO("- USA\n- CAN\n"))


# ---- helpers on Country ---------------------------------------------------
def test_labels_include_literal_adm_codes(country: Any) -> None:
    assert country.labels() == {"ADM1": "ADM1", "ADM2": "ADM2", "states": "ADM1", "counties": "ADM2"}


def test_label_for_returns_first_label_and_raises_when_missing(tool: ModuleType, raw: dict[str, Any]) -> None:
    raw["levels"] = {"states": "ADM1", "provinces": "ADM1"}
    raw["metros"] = []
    c = tool.Country.model_validate(raw)
    assert c.label_for("ADM1") == "states"
    with pytest.raises(ValueError, match="no label defined for ADM2"):
        c.label_for("ADM2")


def test_levels_used(country: Any, tool: ModuleType, raw: dict[str, Any]) -> None:
    assert country.levels_used() == {"ADM1": True, "ADM2": True}
    raw["metros"] = []
    assert tool.Country.model_validate(raw).levels_used() == {"ADM1": True, "ADM2": False}


def test_adm1_tokens_include_parents_of_counties(country: Any) -> None:
    assert {"us-oh", "us-in", "us-mi", "us-ky", "us-tn"} <= country.adm1_tokens()
    assert "Hamilton" not in country.adm1_tokens()


def test_groups_lookup(country: Any) -> None:
    assert country.groups("metros") is country.metros

