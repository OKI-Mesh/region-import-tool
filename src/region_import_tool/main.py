#!/usr/bin/env python3
"""
Pull boundaries from geoBoundaries and use a YAML file to put admin units into
named groups (regions, meshregions, ...). Only the admin levels that some group
actually references are downloaded. If nothing changed since the last run
(YAML section + upstream data version), the country is skipped.

Usage:
    python region-import-tool.py groupings.yaml
    python region-import-tool.py groupings.yaml -c USA --dissolve --force

Requires: pip install geopandas pandas pyyaml requests click

YAML (one top-level block per ISO3 country code):

    USA:
      levels:                  # what this country calls each admin level
        states: ADM1
        counties: ADM2
      regions:
        - us-midwest:
            states: [us-ia, us-il, us-in, us-mi, us-oh, us-wi]
      meshregions:
        - oki:
            states: [us-in, us-ky, us-oh]
        - cincy-core:
            counties:          # ADM2 members are keyed by their parent ADM1
              us-oh: [Butler, Hamilton, Warren]
              us-ky: [Boone, Kenton]

Optional per-country `output:` block (the values shown are the defaults, except dissolve):

      output:
        simplified: true       # use geoBoundaries' pre-simplified geometry
        simplify: 0.005        # extra tolerance in degrees, shared borders kept aligned (0 = off)
        precision: 4           # coordinate decimals (4 is about 10 m)
        dissolve: [regions]    # group types merged into one polygon per group; true = all
        members_only: true     # states/counties layers keep only units in some group (default)
        base_layers: [states]  # write only these full layers (true = all, false = none)

Without a `manifest:` block, one layer is written per group type (regions.geojson,
meshregions.geojson, ...): member units as separate features, or one merged feature
per group if dissolved (--dissolve forces it for every type).
states/counties layers are written too, slimmed to name + group columns.

Client manifest (replaces the per-type layers): one file per group, plus layers.json (the Leaflet layer list).

      manifest:
        # file: layers.json     # optional (this is the default)
        layers:
          - {id: macro, label: Regions, type: regions, colorBy: feature, coverage: full}
          - {id: mesh,  label: Regional Meshes, type: meshregions, in: regions}
          - {id: state, label: State, type: states, in: regions, nameProperty: shapeISO}
          - {id: metro, label: Metro, type: metros, nameProperty: iata, allowOverlap: true}

  type          a group type (regions, meshregions, metros) or the ADM1 label (states)
  in            draw this layer from that group type's files instead of its own;
                groupBy is then set to the group column (e.g. meshregion)
  nameProperty  property the client reads; if it isn't a real column (e.g. iata)
                it is added to the layer's own files, set to the group name
  anything else (label, colorBy, optional, ...) is copied into the manifest unchanged.
  url/codes are generated. url entries are {url, name} for a layer's own files when it
  has no nameProperty, otherwise plain file names. Files for every group of a type
  are named <group>.geojson, so group names must be unique across types.

ADM1 members match on shapeISO (e.g. us-oh) or on name (Ohio), case-insensitive.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import IO, Annotated, Any, Literal, TypeAlias, cast

import click
import geopandas as gpd
import pandas as pd
import pycountry
from pydantic import ValidationError, AfterValidator, BaseModel, ConfigDict, Field, StrictStr, StringConstraints, TypeAdapter, model_validator
import requests
import shapely
import yaml

API = "https://www.geoboundaries.org/api/current/gbOpen/{iso}/{level}/"

EXTRA_CODES = {"XKX"}      # used by geoBoundaries, not in ISO 3166-1
GroupType = Literal["regions", "meshregions", "metros"]
GROUP_COLUMNS: dict[GroupType, str] = {
    "regions": "region",
    "meshregions": "meshregion",
    "metros": "metro",
}

def _iso3(code: str) -> str:
    code = code.upper()
    if code not in EXTRA_CODES and pycountry.countries.get(alpha_3=code) is None:
        raise ValueError(f"'{code}' is not an ISO 3166-1 alpha-3 country code")
    return code


Iso3 = Annotated[str, StringConstraints(min_length=3, max_length=3), AfterValidator(_iso3)]

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid") 

def _as_dict(x: object) -> dict[str, Any] | None:
    """Return x as a typed dict, or None if it isn't one."""
    if isinstance(x, dict):
        return cast("dict[str, Any]", x)
    return None

Names: TypeAlias = Annotated[list[StrictStr], Field(min_length=1)]
Members: TypeAlias = Names | dict[StrictStr, Names]

class Group(BaseModel):
    name: str
    members: dict[str, Members]

    @model_validator(mode="before")
    @classmethod
    def from_mapping(cls, v: object) -> object:
        d = _as_dict(v)
        if d is None or "name" in d:
            return v
        if len(d) != 1:
            raise ValueError(f"one group per list item, got {list(d)}")
        ((name, body),) = d.items()
        return {"name": name, "members": body}

    def adm1(self, labels: dict[str, str]) -> list[str]:
        """State codes/names, from every key that maps to ADM1."""
        out: list[str] = []
        for label, m in self.members.items():
            if labels.get(label) == "ADM1" and isinstance(m, list):
                out += m
        return out

    def adm2(self, labels: dict[str, str]) -> dict[str, list[str]]:
        """{parent state: county names}, from every key that maps to ADM2."""
        out: dict[str, list[str]] = {}
        for label, m in self.members.items():
            if labels.get(label) == "ADM2" and isinstance(m, dict):
                for parent, names in m.items():
                    out.setdefault(parent, []).extend(names)
        return out

class OutputConfig(Strict):
    simplified: bool = True
    simplify: float = 0.005
    precision: int = 4
    members_only: bool = True
    base_layers: bool | list[str] = True       # correct             # True | False | list of level labels
    dissolve: list[GroupType] = [] 

class Layer(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    id: str = Field(min_length=1)
    label: str | None = None                     # defaults to id
    type: str
    in_: str | None = Field(default=None, alias="in")
    nameProperty: str | None = None
    groupBy: str | None = None
    colorBy: Literal["feature", "group"] | None = None   # adjust to what your client accepts
    highlight: bool | None = None
    coverage: Literal["full", "partial"] | None = None
    optional: bool | None = None
    allowOverlap: bool | None = None

class Manifest(Strict):
    version: int = 1
    file: str = "layers.json"
    layers: Annotated[list[Layer], Field(min_length=1)]
    base_url: str = ""

    def prefixed(self, name: str) -> str:
        return f"{self.base_url.rstrip('/')}/{name}" if self.base_url else name

    def write(self, out_dir: Path, iso: str, cfg: "Country",
            states: gpd.GeoDataFrame | None, counties: gpd.GeoDataFrame | None,
            used: dict[str, bool]) -> list[str]:
        """Write one file per group for every group type the manifest draws from,
        one file per ADM1 unit a state layer draws from, plus the manifest itself.
        Returns the list of file names written."""
        labels = cfg.labels()

        file_types: set[str] = set()                 # group types whose files the manifest uses
        name_props: dict[str, str] = {}
        for layer in self.layers:
            ft = layer.in_ or (layer.type if layer.type in GROUP_COLUMNS else None)
            if ft:
                file_types.add(ft)
                if not layer.in_ and layer.nameProperty:
                    name_props[ft] = layer.nameProperty

        referenced: list[GroupType] = [t for t in GROUP_COLUMNS
                                    if any(t in (layer.type, layer.in_) for layer in self.layers)]

        files: dict[str, dict[str, str]] = {}
        frames: dict[str, dict[str, gpd.GeoDataFrame]] = {}
        taken: dict[str, tuple[str, str]] = {}
        written: list[str] = []

        for t in referenced:
            fmap, fr = write_group_files(out_dir, t, cfg.groups(t), labels, states, counties,
                                        used, name_props.get(t), taken, iso,
                                        write=t in file_types,
                                        dissolve=t in cfg.output.dissolve)
            files[t], frames[t] = fmap, fr
            written += list(fmap.values())

        built = self.build(cfg, files, frames, states if used["ADM1"] else None)
        (out_dir / self.file).write_text(json.dumps(built, indent=2))
        return written + [self.file]

    def build(self, cfg: "Country", files: dict[str, dict[str, str]],
              frames: dict[str, dict[str, gpd.GeoDataFrame]],
              all_states: gpd.GeoDataFrame | None) -> dict[str, Any]:
        """Build the client manifest (layers.json). Layer keys: type (group type or ADM1
        label), in (draw from that type's files), id, label; every other key is copied through."""
        layers_out: list[dict[str, Any]] = []
        for layer in self.layers:
            typ = layer.type
            col = next((c for t, c in GROUP_COLUMNS.items() if t == typ), None)   # None: not a group type
            is_group = col is not None
            src = layer.in_

            passthrough = layer.model_dump(
                exclude={"id", "label", "type", "in_", "nameProperty", "groupBy"},
                exclude_none=True,
            )
            entry: dict[str, Any] = {"id": layer.id, "label": layer.label or layer.id,
                                     **passthrough}

            file_type = src or (typ if is_group else None)
            if file_type:
                fmap = files.get(file_type, {})
                if src is None and not layer.nameProperty:
                    entry["url"] = [{"url": self.prefixed(f), "name": n} for n, f in fmap.items()]
                else:
                    entry["url"] = [self.prefixed(f) for f in fmap.values()]
            elif not is_group:
                # ADM1-label layer with no `in:`: the single base file written by run_country
                entry["url"] = [self.prefixed(f"{cfg.label_for('ADM1')}.geojson")]
            if layer.groupBy or (src and col):
                entry["groupBy"] = layer.groupBy or col
            if layer.nameProperty:
                entry["nameProperty"] = layer.nameProperty

            if is_group:
                present = frames.get(typ, {})
                entry["codes"] = [g.name for g in cfg.groups(typ) if g.name in present]
            else:
                codes: list[str] = []
                if file_type:
                    for gdf in frames.get(file_type, {}).values():
                        adm1 = gdf[gdf["level"] == "ADM1"]
                        codes += [c for c in adm1["shapeISO"].astype(str) if c]
                elif all_states is not None:
                    codes += [str(c).lower() for c in all_states["shapeISO"].fillna("") if c]
                entry["codes"] = list(dict.fromkeys(codes))
            layers_out.append(entry)
        return {"version": self.version, "layers": layers_out}
    
    @model_validator(mode="after")
    def unique_ids(self) -> "Manifest":
        ids = [layer.id for layer in self.layers]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate layer ids: {dupes}")
        return self

class Country(Strict):
    levels: Annotated[dict[str, Literal["ADM1", "ADM2"]], Field(min_length=1)]
    output: OutputConfig = OutputConfig()
    manifest: Manifest 
    regions: list[Group] = []
    meshregions: list[Group] = []
    metros: list[Group] = []

    @model_validator(mode="after")
    def check(self) -> "Country":
        errors: list[str] = []
        labels = self.labels()
        adm1_labels = {k for k, v in labels.items() if v == "ADM1"}

        if "ADM2" in self.levels.values() and "ADM1" not in self.levels.values():
            errors.append("levels: an ADM2 label needs an ADM1 label too (its parent)")

        seen: dict[str, str] = {}
        for gtype in GROUP_COLUMNS:
            for g in self.groups(gtype):
                where = f"{gtype}/{g.name}"
                if g.name in seen:
                    errors.append(f"{where}: name already used in {seen[g.name]}")
                seen[g.name] = gtype
                for label, members in g.members.items():
                    level = labels.get(label)
                    if level is None:
                        errors.append(f"{where}: unknown level '{label}'; known: {sorted(labels)}")
                    elif level == "ADM1" and not isinstance(members, list):
                        errors.append(f"{where}/{label}: expected a list of {label}")
                    elif level == "ADM2" and (not isinstance(members, dict) or not members):
                        errors.append(f"{where}/{label}: expected {{parent: [names]}}")

        if not seen:
            errors.append("define at least one group in regions, meshregions or metros")

        if isinstance(self.output.base_layers, list):
            for b in self.output.base_layers:
                if b not in labels:
                    errors.append(f"output.base_layers: unknown level '{b}'; known: {sorted(labels)}")

        for layer in self.manifest.layers:
            if layer.type not in {*GROUP_COLUMNS, *adm1_labels}:
                errors.append(f"manifest layer '{layer.id}': type '{layer.type}' must be one of "
                              f"{sorted(GROUP_COLUMNS)} or an ADM1 label from levels")
            if layer.in_ is not None:
                if layer.in_ not in GROUP_COLUMNS:
                    errors.append(f"manifest layer '{layer.id}': in '{layer.in_}' is not a group type")
                elif layer.in_ in self.output.dissolve:
                    errors.append(f"manifest layer '{layer.id}': draws from {layer.in_} files, but "
                                  f"output.dissolve merges them into one shape per group; use its own "
                                  f"files (drop `in:`) or remove {layer.in_} from dissolve")
            if layer.type in adm1_labels and layer.in_ is None and not base_layer_on(
                    self.output.base_layers, "ADM1", labels):
                errors.append(f"manifest layer '{layer.id}': needs output.base_layers to include {layer.type}")

        if errors:
            raise ValueError("\n".join(errors))
        return self

    def label_for(self, level: str) -> str:
        """First label the YAML gave this level (names the output file)."""
        for label, lvl in self.levels.items():
            if lvl == level:
                return label
        raise ValueError(f"levels: no label defined for {level}")

    def groups(self, key: str) -> list[Group]:
        return {"regions": self.regions, "meshregions": self.meshregions, "metros": self.metros}[key]

    def labels(self) -> dict[str, str]:
        """label -> 'ADM1'/'ADM2', including the literal ADM1/ADM2."""
        return {"ADM1": "ADM1", "ADM2": "ADM2", **self.levels}
    
    def levels_used(self) -> dict[str, bool]:
        lab = self.labels()
        used = {"ADM1": False, "ADM2": False}
        for key in {"regions", "meshregions", "metros"}:
            for g in self.groups(key):
                for label in g.members:
                    used[lab[label]] = True
        return used

   
    def adm1_tokens(self) -> set[str]:
        """Every ADM1 code/name the groups mention: listed states plus parents of listed counties."""
        lab = self.labels()
        tokens: set[str] = set()
        for key in GROUP_COLUMNS:
            for g in self.groups(key):
                tokens.update(g.adm1(lab))
                tokens.update(g.adm2(lab))        # a dict iterates as its parent keys
        return tokens

Doc = TypeAdapter(dict[Iso3, Country])

def group_list(cfg: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """cfg[key] must be a list of {group-name: members} mappings; absent/None -> []."""
    raw: Any = cfg.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise click.ClickException(f"'{key}' must be a list, got {type(raw).__name__}")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(cast("list[Any]", raw)):
        if not isinstance(item, dict):
            raise click.ClickException(
                f"'{key}'[{i}] must be a mapping of group name to members, got {type(item).__name__}")
        out.append(cast("dict[str, Any]", item))
    return out

def warn(msg: str) -> None:
    click.echo(f"WARN: {msg}", err=True)


def norm(name: object) -> str:
    """Normalize a place name for matching."""
    n = str(name).strip().casefold()
    for suffix in (" county", " parish", " borough", " census area"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n.replace(".", "").replace("'", "")

# --------------------------------------------------------------------------
# geoBoundaries access
# --------------------------------------------------------------------------
def get_meta(iso: str, level: str) -> dict[str, Any]:
    r = requests.get(API.format(iso=iso, level=level), timeout=60)
    r.raise_for_status()
    meta: dict[str, Any] = r.json()
    return meta


def layer_url(meta: dict[str, Any], simplified: bool) -> str:
    """geoBoundaries publishes a pre-simplified GeoJSON next to the full one."""
    if simplified and meta.get("simplifiedGeometryGeoJSON"):
        return str(meta["simplifiedGeometryGeoJSON"])
    return str(meta["gjDownloadURL"])

def get_layer(iso: str, level: str, simplified: bool = True) -> gpd.GeoDataFrame:
    meta = requests.get(
        f"https://www.geoboundaries.org/api/current/gbOpen/{iso}/{level}/", timeout=30
    ).json()
    url = meta["simplifiedGeometryGeoJSON" if simplified else "gjDownloadURL"]
    return gpd.read_file(url) # pyright: ignore[reportUnknownMemberType]

states = get_layer("USA", "ADM1")
print(len(states))        


def attach_state_to_counties(counties: gpd.GeoDataFrame, states: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """ADM2 has no parent attribute, so spatial-join a point inside each ADM2
    unit to the ADM1 polygons. Adds `state` (name) and `state_iso` columns."""
    pts = counties.copy()
    pts["geometry"] = counties.geometry.representative_point()
    joined = gpd.sjoin(
        pts,
        states[["shapeName", "shapeISO", "geometry"]].rename(
            columns={"shapeName": "state", "shapeISO": "state_iso"}),
        how="left", predicate="within",
    )
    joined = joined[~joined.index.duplicated(keep="first")]
    counties = counties.copy()
    counties["state"] = joined["state"]
    counties["state_iso"] = joined["state_iso"]
    return counties


def members_only(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Keep only units that were assigned to at least one group."""
    cols = [c for c in GROUP_COLUMNS.values() if c in gdf.columns]
    return gdf[gdf[cols].notna().any(axis=1)] if cols else gdf


def _is_clean_coverage(geoms: Any) -> bool:
    """True if the polygons tile without overlaps or gaps along shared edges.
    GEOS only simplifies shared borders together when this holds."""
    try:
        return bool(shapely.coverage_is_valid(list(geoms)).all()) # pyright: ignore[reportUnknownMemberType]
    except Exception:  # shapely/GEOS too old to check
        return False


def simplify_layer(gdf: gpd.GeoDataFrame, tol: float, precision: int | None) -> gpd.GeoDataFrame:
    """Simplify, round coordinates, and drop anything that collapsed to nothing.

    If the polygons form a clean coverage, shared borders are simplified
    together (no gaps/overlaps). If not, each shape is simplified on its own,
    which still shrinks the file but can leave hairline gaps along borders."""
    gdf = gdf.copy()
    if tol > 0:
        if _is_clean_coverage(gdf.geometry):
            gdf["geometry"] = gdf.geometry.simplify_coverage(tol)
        else:
            warn("layer is not a clean polygon coverage (shared borders differ slightly); "
                 "simplifying each shape separately, so borders may not line up exactly")
            gdf["geometry"] = gdf.geometry.simplify(tol, preserve_topology=True)
    if precision is not None:
        gdf["geometry"] = gdf.geometry.set_precision(10 ** -precision)
    return gdf[~(gdf.geometry.isna() | gdf.geometry.is_empty)]


def base_layer_on(raw: bool | list[str], lvl: str, labels: dict[str, str]) -> bool:
    """output.base_layers: true/false, or a list of level labels / ADM codes."""
    if isinstance(raw, bool):
        return raw
    return lvl in {labels.get(x) for x in raw}


def write_layer(gdf: gpd.GeoDataFrame, path: Path, keep: list[str]) -> str:
    cols: list[str] = []
    for c in dict.fromkeys(keep):
        if c not in gdf.columns:
            continue
        s = gdf[c]
        if s.isna().all() or s.astype(str).str.strip().eq("").all():
            continue
        cols.append(c)
    gdf[cols + ["geometry"]].to_file(path, driver="GeoJSON") #pyright: ignore[reportUnknownMemberType]
    return path.name


# --------------------------------------------------------------------------
# Per-group files and the client manifest
# --------------------------------------------------------------------------
def group_members(states: gpd.GeoDataFrame | None, counties: gpd.GeoDataFrame | None, used: dict[str, bool],
                  g: Group, labels: dict[str, str]) -> gpd.GeoDataFrame | None:
    """All member units of one group (ADM1 and/or ADM2 rows) in one GeoDataFrame.
    Built from the group's own definition, so groups may overlap (e.g. metros)."""
    parts: list[gpd.GeoDataFrame] = []

    adm1 = g.adm1(labels)
    if used["ADM1"] and adm1 and states is not None:
        code = states["shapeISO"].fillna("").astype(str).str.lower()
        name_key = states["shapeName"].map(norm)
        mask = pd.Series(False, index=states.index)
        iso_val = code.copy()
        for w in adm1:
            m = _match_unit(code, name_key, w)
            mask |= m
            iso_val[m & (iso_val == "")] = w.lower()   # no shapeISO: fall back to the YAML token
        sub = states[mask].copy()
        sub["shapeISO"] = iso_val[mask]
        sub["level"] = "ADM1"
        parts.append(sub)

    adm2 = g.adm2(labels)
    if used["ADM2"] and adm2 and counties is not None:
        iso = counties["parent_iso"].fillna("").astype(str).str.lower()
        parent_name = counties["parent"].map(lambda v: norm(v) if isinstance(v, str) else "")
        key = counties["shapeName"].map(norm)
        mask = pd.Series(False, index=counties.index)
        for parent, names in adm2.items():
            for name in names:
                mask |= _match_adm2(iso, parent_name, key, parent, name)
        sub = counties[mask].copy()
        sub["parent_iso"] = iso[mask]
        sub["level"] = "ADM2"
        parts.append(sub)

    parts = [p for p in parts if not p.empty]
    if not parts:
        return None
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)


def safe_name(name: object) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(name))


def write_group_files(out_dir: Path, ftype: GroupType, groups: list[Group], labels: dict[str, str],
                      states: gpd.GeoDataFrame | None, counties: gpd.GeoDataFrame | None,
                      used: dict[str, bool], name_prop: str | None,
                      taken: dict[str, tuple[str, str]], iso: str,
                      write: bool = True, dissolve: bool = False
                      ) -> tuple[dict[str, str], dict[str, gpd.GeoDataFrame]]:
    """One file per group of `ftype` (if write). Returns ({group: filename}, {group: frame})."""
    fmap: dict[str, str] = {}
    frames: dict[str, gpd.GeoDataFrame] = {}
    for g in groups:
        gdf = group_members(states, counties, used, g, labels)
        if gdf is None:
            warn(f"{iso}: {ftype} '{g.name}' has no matching units; no file written")
            continue
        if not write:
            frames[g.name] = gdf
            continue
        fname = f"{safe_name(g.name)}.geojson"
        if fname in taken and taken[fname] != (ftype, g.name):
            raise click.ClickException(
                f"{iso}: file name {fname} would be used by both {taken[fname][0]} "
                f"'{taken[fname][1]}' and {ftype} '{g.name}'; rename one group")
        taken[fname] = (ftype, g.name)
        col = GROUP_COLUMNS[ftype]
        out = gdf.copy()
        out[col] = g.name                    # this file is for exactly this group (groups may overlap)
        if name_prop and name_prop not in out.columns:
            out[name_prop] = g.name

        keep: list[str | None]
        if dissolve:                         # one merged shape for the whole group
            props = [col, *([name_prop] if name_prop else [])]
            out = out[[*props, "geometry"]].dissolve(by=col).reset_index()  # pyright: ignore[reportUnknownMemberType]
            keep = [*props]
        else:
            keep = ["shapeName", "shapeISO", "parent_iso", name_prop, *GROUP_COLUMNS.values()]
            if out["level"].nunique() > 1:
                keep.append("level")
        write_layer(out, out_dir / fname, [k for k in keep if k])
        fmap[g.name], frames[g.name] = fname, out
    return fmap, frames

def _match_adm2(iso: Any, parent_name: Any, key: Any, parent: str, name: str) -> Any:
    in_parent = (iso == parent.lower()) | (parent_name == norm(parent))
    return in_parent & (key == norm(name))

def _match_unit(code: Any, name_key: Any, w: str) -> Any:
    return (code == w.lower()) | (name_key == norm(w))

def assign_adm1_groups(units: gpd.GeoDataFrame, groups: list[Group], col: str,
                       labels: dict[str, str]) -> gpd.GeoDataFrame:
    """Tag each ADM1 unit with the name of the group that lists it, in column `col`."""
    units = units.copy()
    units[col] = None
    code = units["shapeISO"].fillna("").astype(str).str.lower()
    name_key = units["shapeName"].map(norm)
    for g in groups:
        wanted = g.adm1(labels)
        if not wanted:
            continue
        mask = pd.Series(False, index=units.index)
        for w in wanted:
            m = _match_unit(code, name_key, w)
            if not m.any():
                warn(f"no ADM1 match for '{w}' ({col} {g.name})")
            mask |= m
        clash = units.loc[mask & units[col].notna(), "shapeName"]
        if not clash.empty:
            warn(f"{', '.join(clash)} already in a {col}; overwriting with {g.name}")
        units.loc[mask, col] = g.name
    return units

def assign_adm2_groups(units: gpd.GeoDataFrame, groups: list[Group], col: str,
                       labels: dict[str, str]) -> gpd.GeoDataFrame:
    """Tag each ADM2 unit with the group that lists it under its parent, in column `col`."""
    units = units.copy()
    units[col] = None
    iso = units["parent_iso"].fillna("").astype(str).str.lower()
    parent_name = units["parent"].map(lambda s: norm(s) if isinstance(s, str) else "")
    key = units["shapeName"].map(norm)
    for g in groups:
        for parent, names in g.adm2(labels).items():
            for name in names:
                mask = _match_adm2(iso, parent_name, key, parent, name)
                if not mask.any():
                    warn(f"no ADM2 match for {name}, {parent} ({col} {g.name})")
                    continue
                clash = units.loc[mask & units[col].notna(), col]
                if not clash.empty:
                    warn(f"{name}, {parent} already in {clash.iloc[0]}; overwriting with {g.name}")
                units.loc[mask, col] = g.name
    return units


def read_stamp(stamp: Path) -> dict[str, Any]:
    if not stamp.exists():
        return {}
    try:
        data: Any = json.loads(stamp.read_text())
    except ValueError:
        return {}
    return cast("dict[str, Any]", data) if isinstance(data, dict) else {}

def fetch_boundaries(meta: dict[str, Any], iso: str, level: str, cache_dir: Path,
                     simplified: bool = True) -> gpd.GeoDataFrame:
    """Download a geoBoundaries layer (cached per upstream URL)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    url = layer_url(meta, simplified)
    version = hashlib.sha1(url.encode()).hexdigest()[:10]
    cached = cache_dir / f"{iso}_{level}_{version}.geojson"
    if not cached.exists():
        click.echo(f"Downloading {iso} {level}: {url}")
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        cached.write_bytes(r.content)
    gdf = gpd.read_file(cached).to_crs(4326) #pyright: ignore[reportUnknownMemberType]
    if "shapeISO" not in gdf.columns:
        gdf["shapeISO"] = None
    return gdf

def attach_parent(units: gpd.GeoDataFrame, parents: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    pts = units.copy()
    pts["geometry"] = units.geometry.representative_point()
    joined = gpd.sjoin(
        pts,
        parents[["shapeName", "shapeISO", "geometry"]].rename(
            columns={"shapeName": "parent", "shapeISO": "parent_iso"}),
        how="left", predicate="within",
    )
    joined = joined[~joined.index.duplicated(keep="first")]
    units = units.copy()
    units["parent"] = joined["parent"]
    units["parent_iso"] = joined["parent_iso"   ]
    return units


# --------------------------------------------------------------------------
# One country
# --------------------------------------------------------------------------
def run_country(iso: str, country: Country, out_dir: Path, cache_dir: Path, force: bool) -> None:
    labels = country.labels()
    used = country.levels_used()
    out = country.output
    n1, n2 = country.label_for("ADM1"), country.label_for("ADM2")
    need = {"ADM1": used["ADM1"] or used["ADM2"], "ADM2": used["ADM2"]}
    if not any(need.values()):
        click.echo(f"{iso}: no group references any admin level; nothing to do")
        return

    meta = {lvl: get_meta(iso, lvl) for lvl, on in need.items() if on}
    fingerprint: dict[str, Any] = {
        "yaml": hashlib.sha256(country.model_dump_json(by_alias=True).encode()).hexdigest(),
        **{lvl: layer_url(m, out.simplified) for lvl, m in meta.items()},
    }
    stamp = out_dir / ".fingerprint.json"

    prev = read_stamp(stamp)
    if not force and prev:
        if prev.get("fingerprint") == fingerprint and all((out_dir / f).exists() for f in prev.get("files", [])):
            click.echo(f"{iso}: up to date; skipping (use --force to rerun)")
            return


    layers = {lvl: fetch_boundaries(m, iso, lvl, cache_dir, out.simplified) for lvl, m in meta.items()}
    adm1: gpd.GeoDataFrame | None = layers.get("ADM1")
    adm2: gpd.GeoDataFrame | None = layers.get("ADM2")
    if adm1 is not None and adm2 is not None:
        adm2 = attach_parent(adm2, adm1)

    for key, col in GROUP_COLUMNS.items():
        groups = country.groups(key)
        if used["ADM1"] and adm1 is not None:
            adm1 = assign_adm1_groups(adm1, groups, col, labels)
        if used["ADM2"] and adm2 is not None:
            adm2 = assign_adm2_groups(adm2, groups, col, labels)

    if out.members_only:
        if adm1 is not None:
            adm1 = adm1[adm1_in_use(adm1, country)]
        if adm2 is not None:
            adm2 = members_only(adm2)

    if adm1 is not None:
        adm1 = adm1.copy()
        adm1["shapeISO"] = adm1["shapeISO"].fillna("").astype(str).str.lower()

    # Simplify once, here, so every output below shares identical edges.
    if adm1 is not None:
        adm1 = simplify_layer(adm1, out.simplify, out.precision)
    if adm2 is not None:
        adm2 = simplify_layer(adm2, out.simplify, out.precision)

    out_dir.mkdir(parents=True, exist_ok=True)
    cols = list(GROUP_COLUMNS.values())                    # ["region", "meshregion", "metro"]
    files: list[str] = []
    if used["ADM1"] and adm1 is not None and base_layer_on(out.base_layers, "ADM1", labels):
        files.append(write_layer(adm1, out_dir / f"{n1}.geojson", ["shapeName", "shapeISO", *cols]))
    if used["ADM2"] and adm2 is not None and base_layer_on(out.base_layers, "ADM2", labels):
        files.append(write_layer(adm2, out_dir / f"{n2}.geojson",
                                ["shapeName", "parent", "parent_iso", *cols]))

    if adm1 is not None:
        adm1 = simplify_layer(members_only(adm1) if out.members_only else adm1,
                              out.simplify, out.precision)
    if adm2 is not None:
        adm2 = simplify_layer(members_only(adm2) if out.members_only else adm2,
                              out.simplify, out.precision)

    out_dir.mkdir(parents=True, exist_ok=True)

    files: list[str] = []
    if used["ADM1"] and adm1 is not None and base_layer_on(out.base_layers, "ADM1", labels):
        files.append(write_layer(adm1, out_dir / f"{n1}.geojson", ["shapeName", "shapeISO", *cols]))
    if used["ADM2"] and adm2 is not None and base_layer_on(out.base_layers, "ADM2", labels):
        files.append(write_layer(adm2, out_dir / f"{n2}.geojson",
                                 ["shapeName", "state", "state_iso", *cols]))

    if country.manifest:
        # Leaflet layer-list mode: one file per group + layers.json
        files += country.manifest.write(out_dir, iso, country, adm1, adm2, used)

    remove_stale(out_dir, prev, files, iso)
    stamp.write_text(json.dumps({"fingerprint": fingerprint, "files": files}, indent=2))
    sizes = ", ".join(f"{f} {(out_dir / f).stat().st_size / 1024:.0f} KB" for f in files)
    click.echo(f"{iso}: done - {sizes}")

def load_doc(yaml_file: IO[str]) -> dict[str, Country]:
    try:
        return Doc.validate_python(yaml.safe_load(yaml_file) or {})
    except ValidationError as e:
        lines = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise click.ClickException("invalid YAML:\n  " + "\n  ".join(lines)) from e

def adm1_in_use(adm1: gpd.GeoDataFrame, cfg: Country) -> pd.Series:
    code = adm1["shapeISO"].fillna("").astype(str).str.lower()
    name_key = adm1["shapeName"].map(norm)
    mask = pd.Series(False, index=adm1.index)
    for tok in cfg.adm1_tokens():
        mask |= _match_unit(code, name_key, tok)
    return mask

def remove_stale(out_dir: Path, prev: dict[str, Any], files: list[str], iso: str) -> None:
    """Delete outputs an earlier run recorded that this run no longer produces."""
    stale = [f for f in prev.get("files", []) if f not in files and (out_dir / f).exists()]
    for f in stale:
        (out_dir / f).unlink()
    if stale:
        click.echo(f"{iso}: removed stale files from an earlier run: {', '.join(stale)}")
# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
@click.command()
@click.argument("yaml_file", type=click.File("r"))
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path),
              default="out", show_default=True, help="Output directory for GeoJSON files")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path),
              default=".geob_cache", show_default=True, help="Cache directory for downloaded boundaries")
@click.option("--dissolve", is_flag=True,
              help="Dissolve every breakdown layer (overrides output.dissolve in the YAML)")
@click.option("--countries", "-c", multiple=True,
              help="Only run these ISO3 codes (default: every country in the YAML)")
@click.option("--force", is_flag=True, help="Rerun even if nothing changed")
def cli(yaml_file: IO[str], out_dir: Path, cache_dir: Path, dissolve: bool,
        countries: tuple[str, ...], force: bool) -> None:
    doc = load_doc(yaml_file)
    wanted = [c.upper() for c in countries] or list(doc)
    missing = [c for c in wanted if c not in doc]
    if missing:
        raise click.ClickException(f"not in the YAML: {', '.join(missing)}")

    for iso in wanted:
        try:
            run_country(iso, doc[iso], out_dir, cache_dir, force)
        except (requests.RequestException, click.ClickException) as e:
            warn(f"{iso}: {e}; skipping")


if __name__ == "__main__":
    cli()
