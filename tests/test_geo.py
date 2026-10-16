"""Matching, grouping and file output on synthetic geometry. No network."""
from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import click
import geopandas as gpd
from shapely.geometry.base import BaseGeometry
import pytest

USED = {"ADM1": True, "ADM2": True}

def read(path: Path) -> gpd.GeoDataFrame:
    gdf: gpd.GeoDataFrame = gpd.read_file(path)  # pyright: ignore[reportUnknownMemberType]
    return gdf

def names(gdf: gpd.GeoDataFrame, col: str, value: str) -> list[str]:
    return sorted(gdf.loc[gdf[col] == value, "shapeName"].tolist())


# ---- attach_parent --------------------------------------------------------
def test_attach_parent_assigns_the_containing_state(counties: gpd.GeoDataFrame) -> None:
    row = counties[counties.shapeName == "Hamilton County"].iloc[0]
    assert (row["parent"], row["parent_iso"]) == ("Ohio", "US-OH")


def test_same_named_counties_get_different_parents(counties: gpd.GeoDataFrame) -> None:
    butlers = counties[counties.shapeName == "Butler County"]
    assert sorted(butlers["parent_iso"]) == ["US-KY", "US-OH"]


def test_attach_parent_keeps_row_count_and_input_untouched(tool: ModuleType, states: gpd.GeoDataFrame,
                                                           counties: gpd.GeoDataFrame) -> None:
    bare = counties.drop(columns=["parent", "parent_iso"])
    out = tool.attach_parent(bare, states)
    assert len(out) == len(bare) and "parent" not in bare.columns


def test_unit_outside_every_state_gets_no_parent(tool: ModuleType, states: gpd.GeoDataFrame) -> None:
    from shapely.geometry import box
    stray = gpd.GeoDataFrame({"shapeName": ["Nowhere"], "shapeISO": [None]},
                             geometry=[box(50, 50, 51, 51)], crs=4326)
    assert tool.attach_parent(stray, states)["parent"].isna().all()


# ---- ADM1 assignment ------------------------------------------------------
def test_adm1_matched_by_iso_code(tool: ModuleType, country: Any, states: gpd.GeoDataFrame, labels: dict[str, str]) -> None:
    out = tool.assign_adm1_groups(states, country.regions, "region", labels)
    assert names(out, "region", "us-midwest") == ["Indiana", "Michigan", "Ohio"]
    assert names(out, "region", "us-southeast") == ["Kentucky", "Tennessee"]


def test_adm1_matched_by_name_too(tool: ModuleType, raw: dict[str, Any], states: gpd.GeoDataFrame) -> None:
    raw["regions"] = [{"x": {"states": ["Ohio", "  indiana "]}}]
    c = tool.Country.model_validate(raw)
    out = tool.assign_adm1_groups(states, c.regions, "region", c.labels())
    assert names(out, "region", "x") == ["Indiana", "Ohio"]


def test_adm1_unmatched_member_warns(tool: ModuleType, raw: dict[str, Any], states: gpd.GeoDataFrame,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    raw["regions"] = [{"x": {"states": ["us-oh", "us-zz"]}}]
    c = tool.Country.model_validate(raw)
    tool.assign_adm1_groups(states, c.regions, "region", c.labels())
    assert "us-zz" in capsys.readouterr().err


def test_adm1_overlap_overwrites_and_warns(tool: ModuleType, raw: dict[str, Any], states: gpd.GeoDataFrame,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    raw["regions"] = [{"a": {"states": ["us-oh"]}}, {"b": {"states": ["us-oh"]}}]
    c = tool.Country.model_validate(raw)
    out = tool.assign_adm1_groups(states, c.regions, "region", c.labels())
    assert names(out, "region", "b") == ["Ohio"] and "Ohio" in capsys.readouterr().err


def test_assign_does_not_mutate_input(tool: ModuleType, country: Any, states: gpd.GeoDataFrame, labels: dict[str, str]) -> None:
    tool.assign_adm1_groups(states, country.regions, "region", labels)
    assert "region" not in states.columns


def test_group_without_adm1_members_is_skipped(tool: ModuleType, country: Any, states: gpd.GeoDataFrame, labels: dict[str, str]) -> None:
    out = tool.assign_adm1_groups(states, country.metros, "metro", labels)
    assert out["metro"].isna().all()


# ---- ADM2 assignment ------------------------------------------------------
def test_adm2_assignment_uses_parent_to_split_butlers(tool: ModuleType, country: Any, counties: gpd.GeoDataFrame,
                                                      labels: dict[str, str]) -> None:
    out = tool.assign_adm2_groups(counties, country.metros, "metro", labels)
    cvg = out[out.metro == "cvg"]
    assert sorted(zip(cvg.shapeName, cvg.parent_iso)) == [
        ("Boone County", "US-KY"), ("Butler County", "US-OH"),
        ("Hamilton County", "US-OH"), ("Kenton County", "US-KY")]
    ky_butler = out[(out.shapeName == "Butler County") & (out.parent_iso == "US-KY")].iloc[0]
    assert ky_butler["metro"] is None or ky_butler["metro"] != ky_butler["metro"]


def test_adm2_matching_ignores_county_suffix_and_case(tool: ModuleType, country: Any, counties: gpd.GeoDataFrame,
                                                      labels: dict[str, str]) -> None:
    out = tool.assign_adm2_groups(counties, country.metros, "metro", labels)
    assert set(out.loc[out.metro == "day", "shapeName"]) == {"Montgomery County", "Greene County"}


def test_adm2_parent_can_be_a_state_name(tool: ModuleType, raw: dict[str, Any], counties: gpd.GeoDataFrame) -> None:
    raw["metros"] = [{"m": {"counties": {"Ohio": ["Hamilton"]}}}]
    c = tool.Country.model_validate(raw)
    out = tool.assign_adm2_groups(counties, c.metros, "metro", c.labels())
    assert names(out, "metro", "m") == ["Hamilton County"]


def test_adm2_unmatched_warns(tool: ModuleType, raw: dict[str, Any], counties: gpd.GeoDataFrame,
                              capsys: pytest.CaptureFixture[str]) -> None:
    raw["metros"] = [{"m": {"counties": {"us-oh": ["Atlantis"]}}}]
    c = tool.Country.model_validate(raw)
    tool.assign_adm2_groups(counties, c.metros, "metro", c.labels())
    assert "Atlantis" in capsys.readouterr().err


def test_adm2_wrong_parent_does_not_match(tool: ModuleType, raw: dict[str, Any], counties: gpd.GeoDataFrame) -> None:
    raw["metros"] = [{"m": {"counties": {"us-in": ["Hamilton"]}}}]       # Hamilton is in Ohio
    c = tool.Country.model_validate(raw)
    assert tool.assign_adm2_groups(counties, c.metros, "metro", c.labels())["metro"].isna().all()


# ---- filtering ------------------------------------------------------------
def test_adm1_in_use_keeps_group_members_and_county_parents(tool: ModuleType, country: Any, states: gpd.GeoDataFrame) -> None:
    assert tool.adm1_in_use(states, country).all()                       # all five are in a group


def test_adm1_in_use_drops_unused_states(tool: ModuleType, raw: dict[str, Any], states: gpd.GeoDataFrame) -> None:
    raw["regions"] = [{"x": {"states": ["us-oh"]}}]
    raw["meshregions"] = []
    raw["metros"] = [{"m": {"counties": {"us-ky": ["Boone"]}}}]
    raw["manifest"]["layers"] = [{"id": "a", "type": "regions"}]
    raw["output"] = {"base_layers": False}
    c = tool.Country.model_validate(raw)
    kept = states.loc[tool.adm1_in_use(states, c), "shapeName"].tolist()
    assert sorted(kept) == ["Kentucky", "Ohio"]


def test_members_only_drops_rows_in_no_group(tool: ModuleType, country: Any, counties: gpd.GeoDataFrame,
                                             labels: dict[str, str]) -> None:
    out = tool.assign_adm2_groups(counties, country.metros, "metro", labels)
    kept = tool.members_only(out)
    assert len(kept) == 6 and "Butler County" in set(kept.shapeName)
    assert set(kept[kept.shapeName == "Butler County"]["parent_iso"]) == {"US-OH"}


def test_members_only_without_group_columns_is_a_noop(tool: ModuleType, states: gpd.GeoDataFrame) -> None:
    assert len(tool.members_only(states)) == len(states)


# ---- group_members --------------------------------------------------------
def test_group_members_mixes_levels_and_tags_them(tool: ModuleType, raw: dict[str, Any], states: gpd.GeoDataFrame,
                                                  counties: gpd.GeoDataFrame) -> None:
    raw["regions"][0]["us-midwest"]["counties"] = {"us-ky": ["Boone"]}
    c = tool.Country.model_validate(raw)
    gdf = tool.group_members(states, counties, USED, c.regions[0], c.labels())
    assert sorted(gdf["level"].unique()) == ["ADM1", "ADM2"]
    assert (gdf.level == "ADM1").sum() == 3 and (gdf.level == "ADM2").sum() == 1


def test_group_members_returns_none_when_nothing_matches(tool: ModuleType, raw: dict[str, Any], states: gpd.GeoDataFrame) -> None:
    raw["regions"] = [{"x": {"states": ["us-zz"]}}]
    c = tool.Country.model_validate(raw)
    assert tool.group_members(states, None, USED, c.regions[0], c.labels()) is None


def test_group_members_respects_unused_levels(tool: ModuleType, country: Any, states: gpd.GeoDataFrame,
                                              counties: gpd.GeoDataFrame, labels: dict[str, str]) -> None:
    gdf = tool.group_members(states, counties, {"ADM1": True, "ADM2": False}, country.metros[0], labels)
    assert gdf is None


def test_group_members_fills_blank_iso_from_the_yaml_token(tool: ModuleType, raw: dict[str, Any], states: gpd.GeoDataFrame) -> None:
    states.loc[states.shapeName == "Ohio", "shapeISO"] = ""
    raw["regions"] = [{"x": {"states": ["Ohio"]}}]
    c = tool.Country.model_validate(raw)
    gdf = tool.group_members(states, None, USED, c.regions[0], c.labels())
    assert gdf.iloc[0]["shapeISO"] == "ohio"


# ---- write_layer ----------------------------------------------------------
def test_write_layer_skips_all_blank_and_all_nan_columns(tool: ModuleType, states: gpd.GeoDataFrame, tmp_path: Path) -> None:
    states["empty"] = ""
    states["nan"] = None
    states["real"] = "v"
    tool.write_layer(states, tmp_path / "o.geojson", ["shapeName", "empty", "nan", "real"])
    props = json.loads((tmp_path / "o.geojson").read_text())["features"][0]["properties"]
    assert set(props) == {"shapeName", "real"}


# ---- write_group_files ----------------------------------------------------
def run_files(tool: ModuleType, c: Any, ftype: str, tmp: Path, states: Any, counties: Any,
              write: bool = True, dissolve: bool = False, name_prop: str | None = None,
              taken: dict[str, tuple[str, str]] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    return tool.write_group_files(tmp, ftype, c.groups(ftype), c.labels(), states, counties, USED,
                                  name_prop, taken if taken is not None else {}, "USA",
                                  write=write, dissolve=dissolve)


def test_one_file_per_group(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    fmap, frames = run_files(tool, country, "regions", tmp_path, states, counties)
    assert fmap == {"us-midwest": "us-midwest.geojson", "us-southeast": "us-southeast.geojson"}
    assert set(frames) == set(fmap) and all((tmp_path / f).exists() for f in fmap.values())


def test_group_file_contains_only_its_members_and_group_column(tool: ModuleType, country: Any, states: Any,
                                                              counties: Any, tmp_path: Path) -> None:
    run_files(tool, country, "regions", tmp_path, states, counties)
    feats = json.loads((tmp_path / "us-midwest.geojson").read_text())["features"]
    assert sorted(f["properties"]["shapeName"] for f in feats) == ["Indiana", "Michigan", "Ohio"]
    assert {f["properties"]["region"] for f in feats} == {"us-midwest"}


def test_write_false_returns_frames_but_no_files(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    fmap, frames = run_files(tool, country, "regions", tmp_path, states, counties, write=False)
    assert fmap == {} and set(frames) == {"us-midwest", "us-southeast"} and not list(tmp_path.iterdir())


def test_dissolve_gives_one_feature_per_group(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    run_files(tool, country, "regions", tmp_path, states, counties, dissolve=True, name_prop="name")
    gj = json.loads((tmp_path / "us-midwest.geojson").read_text())
    assert len(gj["features"]) == 1
    assert gj["features"][0]["properties"] == {"region": "us-midwest", "name": "us-midwest"}


def test_dissolved_geometry_is_the_union(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    run_files(tool, country, "regions", tmp_path, states, counties, dissolve=True)
    gdf: gpd.GeoDataFrame = read(tmp_path / "us-midwest.geojson")
    g = cast(BaseGeometry, gdf.geometry.iloc[0])
    assert g.geom_type == "Polygon" and g.area == pytest.approx(100 + 100 + 200)


def test_dissolved_mixed_levels_do_not_crash(tool: ModuleType, raw: dict[str, Any], states: Any, counties: Any, tmp_path: Path) -> None:
    raw["regions"][0]["us-midwest"]["counties"] = {"us-ky": ["Boone"]}
    c = tool.Country.model_validate(raw)
    run_files(tool, c, "regions", tmp_path, states, counties, dissolve=True)
    assert len(read(tmp_path / "us-midwest.geojson")) == 1


def test_county_group_file_has_parent_iso(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    run_files(tool, country, "metros", tmp_path, states, counties)
    df = read(tmp_path / "cvg.geojson")
    assert set(df["parent_iso"]) == {"us-oh", "us-ky"} and len(df) == 4


def test_name_property_is_added_when_missing(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    run_files(tool, country, "metros", tmp_path, states, counties, name_prop="iata")
    assert set(read(tmp_path / "cvg.geojson")["iata"]) == {"cvg"}


def test_file_name_clash_between_types_is_an_error(tool: ModuleType, raw: dict[str, Any], states: Any, counties: Any, tmp_path: Path) -> None:
    raw["regions"] = [{"a b": {"states": ["us-oh"]}}]
    raw["meshregions"] = [{"a_b": {"states": ["us-in"]}}]
    c = tool.Country.model_validate(raw)
    taken: dict[str, tuple[str, str]] = {}
    run_files(tool, c, "regions", tmp_path, states, counties, taken=taken)
    with pytest.raises(click.ClickException, match="rename one group"):
        run_files(tool, c, "meshregions", tmp_path, states, counties, taken=taken)


def test_group_with_no_matches_writes_nothing_and_warns(tool: ModuleType, raw: dict[str, Any], states: Any, counties: Any,
                                                        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    raw["regions"] = [{"x": {"states": ["us-zz"]}}]
    c = tool.Country.model_validate(raw)
    fmap, _ = run_files(tool, c, "regions", tmp_path, states, counties)
    assert fmap == {} and "no matching units" in capsys.readouterr().err


# ---- manifest end to end --------------------------------------------------
def write_manifest(tool: ModuleType, c: Any, tmp: Path, states: Any, counties: Any) -> tuple[list[str], dict[str, Any]]:
    files = c.manifest.write(tmp, "USA", c, states, counties, USED)
    return files, json.loads((tmp / "layers.json").read_text())


def layer(doc: dict[str, Any], id_: str) -> dict[str, Any]:
    return next(x for x in doc["layers"] if x["id"] == id_)


def test_manifest_lists_layers_in_yaml_order(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    _, doc = write_manifest(tool, country, tmp_path, states, counties)
    assert [x["id"] for x in doc["layers"]] == ["macro", "mesh", "state", "metro"]
    assert doc["version"] == 1


def test_manifest_passes_extra_keys_through(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    _, doc = write_manifest(tool, country, tmp_path, states, counties)
    assert layer(doc, "macro")["colorBy"] == "feature"
    assert layer(doc, "metro")["optional"] is True and layer(doc, "metro")["allowOverlap"] is True


def test_group_layer_without_name_property_lists_name_and_url(tool: ModuleType, country: Any, states: Any,
                                                              counties: Any, tmp_path: Path) -> None:
    _, doc = write_manifest(tool, country, tmp_path, states, counties)
    assert layer(doc, "macro")["url"] == [{"url": "us-midwest.geojson", "name": "us-midwest"},
                                          {"url": "us-southeast.geojson", "name": "us-southeast"}]
    assert layer(doc, "macro")["codes"] == ["us-midwest", "us-southeast"]


def test_name_property_layer_gets_plain_urls(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    _, doc = write_manifest(tool, country, tmp_path, states, counties)
    m = layer(doc, "metro")
    assert m["url"] == ["cvg.geojson", "day.geojson"] and m["nameProperty"] == "iata"


def test_in_layer_reuses_files_and_groups_by_column(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    _, doc = write_manifest(tool, country, tmp_path, states, counties)
    mesh = layer(doc, "mesh")
    assert mesh["url"] == ["us-midwest.geojson", "us-southeast.geojson"]    # regions' files
    assert mesh["groupBy"] == "meshregion"      # column of the layer type, read from the regions files
    assert mesh["codes"] == ["oki"]


def test_state_layer_uses_the_single_states_file(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    _, doc = write_manifest(tool, country, tmp_path, states, counties)
    s = layer(doc, "state")
    assert s["url"] == ["states.geojson"] and s["nameProperty"] == "shapeISO"
    assert s["codes"] == ["us-oh", "us-in", "us-mi", "us-ky", "us-tn"]


def test_no_per_state_files_are_written(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    files, _ = write_manifest(tool, country, tmp_path, states, counties)
    assert not any(f.startswith("us-oh") or f == "ohio.geojson" for f in files)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(set(files))


def test_returned_file_list_matches_disk(tool: ModuleType, country: Any, states: Any, counties: Any, tmp_path: Path) -> None:
    files, _ = write_manifest(tool, country, tmp_path, states, counties)
    assert "layers.json" in files and all((tmp_path / f).exists() for f in files)


def test_only_referenced_group_types_get_files(tool: ModuleType, raw: dict[str, Any], states: Any, counties: Any, tmp_path: Path) -> None:
    raw["manifest"]["layers"] = [{"id": "macro", "type": "regions"}]
    raw["output"] = {"base_layers": False}
    c = tool.Country.model_validate(raw)
    files, _ = write_manifest(tool, c, tmp_path, states, counties)
    assert sorted(files) == ["layers.json", "us-midwest.geojson", "us-southeast.geojson"]


def test_in_layer_without_its_own_files_still_writes_the_source_files(tool: ModuleType, raw: dict[str, Any], states: Any,
                                                                      counties: Any, tmp_path: Path) -> None:
    raw["manifest"]["layers"] = [{"id": "mesh", "type": "meshregions", "in": "regions"}]
    raw["output"] = {"base_layers": False}
    c = tool.Country.model_validate(raw)
    files, doc = write_manifest(tool, c, tmp_path, states, counties)
    assert "us-midwest.geojson" in files and "oki.geojson" not in files
    assert doc["layers"][0]["codes"] == ["oki"]


def test_dissolved_type_is_written_dissolved_via_manifest(tool: ModuleType, raw: dict[str, Any], states: Any,
                                                         counties: Any, tmp_path: Path) -> None:
    raw["output"]["dissolve"] = ["metros"]
    c = tool.Country.model_validate(raw)
    write_manifest(tool, c, tmp_path, states, counties)
    assert len(read(tmp_path / "cvg.geojson")) == 1


def test_manifest_file_name_is_configurable(tool: ModuleType, raw: dict[str, Any], states: Any, counties: Any, tmp_path: Path) -> None:
    raw["manifest"]["file"] = "index.json"
    c = tool.Country.model_validate(raw)
    files = c.manifest.write(tmp_path, "USA", c, states, counties, USED)
    assert "index.json" in files and (tmp_path / "index.json").exists()

def test_unknown_layer_key_still_passes_through(
    tool: ModuleType, raw: dict[str, Any], states: gpd.GeoDataFrame,
    counties: gpd.GeoDataFrame, tmp_path: Path,
) -> None:
    raw["manifest"]["layers"][0]["myFutureKey"] = 5
    c = tool.Country.model_validate(raw)
    _, doc = write_manifest(tool, c, tmp_path, states, counties)
    assert layer(doc, "macro")["myFutureKey"] == 5