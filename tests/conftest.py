"""Fixtures. No test touches the network.

The module file is named region-import-tool.py (hyphens), which `import` cannot
load, so it is loaded by path. Point TOOL at your file.

Fake geography (degrees, x = lon, y = lat):

    Michigan   y 10..20, x -10..10
    Indiana    y  0..10, x -10..0      Ohio      y  0..10, x 0..10
    Tennessee  y -10..0, x -10..0      Kentucky  y -10..0, x 0..10

Counties sit inside Ohio (Hamilton, Butler, Montgomery, Greene) and Kentucky
(Boone, Kenton and a second Butler County, to prove same-named counties are
told apart by their parent state).
"""
from __future__ import annotations

import copy

from types import ModuleType
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import box

import region_import_tool.main as _tool

@pytest.fixture(scope="session")
def tool() -> ModuleType:
    return _tool

@pytest.fixture
def raw() -> dict[str, Any]:
    """One country block as the YAML would parse it. Tests mutate copies of it."""
    return copy.deepcopy({
        "levels": {"states": "ADM1", "counties": "ADM2"},
        "regions": [{"us-midwest": {"states": ["us-oh", "us-in", "us-mi"]}},
                    {"us-southeast": {"states": ["us-ky", "us-tn"]}}],
        "meshregions": [{"oki": {"states": ["us-in", "us-ky", "us-oh"]}}],
        "metros": [{"cvg": {"counties": {"us-oh": ["Hamilton", "Butler"],
                                         "us-ky": ["Boone", "Kenton"]}}},
                   {"day": {"counties": {"us-oh": ["Montgomery", "Greene"]}}}],
        "output": {"base_layers": ["states"]},
        "manifest": {"layers": [
            {"id": "macro", "label": "Regions", "type": "regions", "colorBy": "feature"},
            {"id": "mesh", "label": "Meshes", "type": "meshregions", "in": "regions"},
            {"id": "state", "label": "State", "type": "states", "nameProperty": "shapeISO"},
            {"id": "metro", "label": "Metro", "type": "metros", "nameProperty": "iata",
             "optional": True, "allowOverlap": True},
        ]},
    })


@pytest.fixture
def country(tool: ModuleType, raw: dict[str, Any]) -> Any:
    return tool.Country.model_validate(raw)


@pytest.fixture
def states() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"shapeName": ["Ohio", "Indiana", "Michigan", "Kentucky", "Tennessee"],
         "shapeISO": ["US-OH", "US-IN", "US-MI", "US-KY", "US-TN"]},
        geometry=[box(0, 0, 10, 10), box(-10, 0, 0, 10), box(-10, 10, 10, 20),
                  box(0, -10, 10, 0), box(-10, -10, 0, 0)], crs=4326)


@pytest.fixture
def counties(tool: ModuleType, states: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    raw_counties = gpd.GeoDataFrame(
        {"shapeName": ["Hamilton County", "Butler County", "Boone County", "Kenton County",
                       "Montgomery County", "Greene County", "Butler County"],
         "shapeISO": [None] * 7},
        geometry=[box(.1, 5, 5, 9.9), box(5.1, 5, 9.9, 9.9),          # Hamilton, Butler (OH)
                  box(.1, -5, 5, -.5), box(5.1, -9.9, 9.9, -.5),      # Boone, Kenton (KY)
                  box(.1, .1, 5, 4.9), box(5.1, .1, 9.9, 4.9),        # Montgomery, Greene (OH)
                  box(.1, -9.9, 5, -5.1)],                            # Butler (KY)
        crs=4326)
    return tool.attach_parent(raw_counties, states)


@pytest.fixture
def labels(country: Any) -> dict[str, str]:
    return country.labels()