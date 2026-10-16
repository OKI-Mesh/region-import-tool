# region-import-tool

Reads a YAML file that says which states belong to which regions, which counties belong to which metros, and so on, then:

1. downloads only the [geoBoundaries](https://www.geoboundaries.org/) layers the YAML needs,
2. tags every unit with its groups,
3. writes simplified GeoJSON files plus a `layers.json` that tells a Leaflet map what to draw.

It skips the work when neither your YAML nor the upstream data has changed.

## Install

Python 3.10+, managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync              # runtime + dev dependencies (pytest, pyright, ...)
uv sync --no-dev     # runtime only
```

## Quick start

```bash
uv run region-import-tool groupings.example.yaml --out-dir out
uv run region-import-tool groupings.example.yaml --out-dir out -c USA   # one country
```

Run `uv run region-import-tool --help` for the full option list. Running again with nothing changed prints `USA: up to date; skipping`. It still makes one small metadata request per layer to check for a new upstream version, but downloads nothing; `--force` rebuilds anyway.

## The YAML

One block per ISO3 country code (validated against ISO 3166-1, plus `XKX` for Kosovo, which geoBoundaries uses). The whole file is validated up front, so a typo is reported with its exact path before anything is downloaded. See `groupings.example.yaml` for a complete, commented file.

```yaml
USA:
  levels:                       # required
    states: ADM1
    counties: ADM2
  regions:                      # group types: regions, meshregions, metros
    - us-midwest:
        states: [us-in, us-oh, us-mi]
  metros:
    - cvg:
        counties:               # ADM2 members are keyed by their parent
          us-oh: [Hamilton, Butler]
          us-ky: [Boone, Kenton]
  output:   { ... }             # optional
  manifest: { ... }             # required
```

### Levels

`levels:` is required and maps *your* names for the admin levels to geoBoundaries' `ADM1` / `ADM2`. Those names become the keys inside groups and the name of the full-layer file, so a country with provinces can say `provinces: ADM1`. Several labels may point at the same level (the first names the file). The literal keys `ADM1` and `ADM2` always work. Only ADM1 and ADM2 are supported, and an ADM2 label needs an ADM1 label too (it is the parent).

### Groups

There are three group types: `regions`, `meshregions` and `metros`. Each is optional, but at least one group must exist. Each unit gets one value per type.

- **ADM1 members** (`states:`) are a list, matched on `shapeISO` (`us-oh`) or on name (`Ohio`), case-insensitively. Use ISO 3166-2 codes with the two-letter country prefix (`ca-on`, not `can-on`).
- **ADM2 members** (`counties:`) are a mapping of parent to names. `Butler` under `us-oh` never matches Butler County, KY. A trailing "County", "Parish", "Borough" or "Census Area" is ignored when matching.
- A group can list both states and counties.
- Group names must be unique across all types, because they become file names.
- A name that matches nothing prints `WARN: no ADM1 match for ...` / `no ADM2 match for ...` and the run continues. **Read the warnings**: that is how typos show up.

Which layers are downloaded follows from the groups: ADM1 only if something lists states or counties, ADM2 only if some group lists counties. geoBoundaries ADM2 has no parent column, so each county's state is found by a spatial join.

### Output options

```yaml
  output:
    simplified: true
    simplify: 0.005
    precision: 4
    members_only: true
    base_layers: [states]
    dissolve: [metros]
```

| Option | Default | Effect |
|---|---|---|
| `simplified` | `true` | Download geoBoundaries' pre-simplified GeoJSON instead of the full-detail one. |
| `simplify` | `0.005` | Further simplification in degrees (~500 m). Raise it (`0.01`, `0.02`) if files are still too big. `0` turns it off. |
| `precision` | `4` | Coordinate decimals (~10 m). |
| `members_only` | `true` | Base layers contain only units that belong to some group (states also kept if they are the parent of a listed county). |
| `base_layers` | `true` | Full layers to write: `true`, `false`, or a list of level labels. Group files do not depend on them. |
| `dissolve` | `[]` | Group types whose files are merged into **one shape per group** instead of one feature per member unit. Values: `regions`, `meshregions`, `metros`. |

A dissolved type cannot be used as the `in:` source of another layer, since the member units are gone; this is rejected at load time.

Everything is simplified once up front, so all output files share identical edges. If the polygons do not form a clean coverage the tool says so and simplifies each shape separately, which can leave hairline gaps.

### `layers.json` (the `manifest:` block)

The manifest is required. The tool writes **one file per group** (`us-midwest.geojson`, `cvg.geojson`, ...), a single base file for the state layer, and `layers.json`.

```yaml
  manifest:
    # file: layers.json
    base_url: /data/usa
    layers:
      - {id: macro, label: Regions, type: regions, colorBy: feature}
      - {id: mesh,  label: Regional Meshes, type: meshregions, in: regions}
      - {id: state, label: State, type: states, nameProperty: shapeISO}
      - {id: metro, label: Metro, type: metros, nameProperty: iata, optional: true}
```

| Key | Meaning |
|---|---|
| `id`, `label` | `id` must be unique. `label` defaults to the id. |
| `type` | A group type, or your ADM1 label (`states`). |
| `in` | Draw this layer from that group type's files instead of its own. The mesh layer above reuses the region files and groups features by their `meshregion` property (`groupBy` is set for you). |
| `nameProperty` | Property the client reads for a feature's name. If it is not a real column (like `iata`) it is added to the layer's own files, set to the group name. |
| `groupBy`, `colorBy`, `highlight`, `coverage`, `optional`, `allowOverlap` | Copied through to `layers.json`. Any other key is copied too. |
| `base_url` (on `manifest`) | Prefix added to every `url` in `layers.json`. Files on disk keep plain names. Slashes are normalised. |

`url` and `codes` are generated. `codes` are the group names that actually matched units (for the state layer, the lowercase state codes in its file). A layer's `url` entries are `{url, name}` objects when it has no `nameProperty`, and plain strings otherwise. A state layer without `in:` points at the single base file (`states.geojson`), so the client loads one file and highlights by property rather than loading one file per state.

What the files contain:

- **Group files**: the member units, with `shapeName`, `shapeISO` (lowercased, to match `codes`), `parent_iso` for counties, and the group columns (`region`, `meshregion`, `metro`). Columns that are entirely empty are left out.
- **Dissolved group files**: one feature with the group column and the `nameProperty`.
- Groups are built from their own definition, so they can overlap (Butler County can be in both `cvg` and `day`).
- Two groups whose names map to the same file name stop the run, naming both.

### Output layout

```
out/
  USA/
    states.geojson       # base layer, members only
    us-midwest.geojson   # one per group the manifest uses
    cvg.geojson
    layers.json
```

## Development

```bash
uv run pytest tests          # no network needed
uv run pyright
```

Tests build a small fake geography (five state boxes, seven counties, two of them named "Butler County") and never touch the network.

## Attribution

Boundary data comes from geoBoundaries (gbOpen, CC BY 4.0). Please cite:

> Runfola, D. et al. (2020) geoBoundaries: A global database of political administrative boundaries. *PLoS ONE* 15(4): e0231866. https://doi.org/10.1371/journal.pone.0231866

Use the attribution text you already have in your README if it differs.