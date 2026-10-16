#!/usr/bin/env python3
"""
Quick sanity check of geob_assign.py output: prints a per-layer summary and
draws every layer into one PNG.

Usage:
    python geob_check.py out/USA
    python geob_check.py out/USA --png check.png

Requires: pip install geopandas matplotlib
"""
from pathlib import Path
from typing import Any, TypeAlias

import click
import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

GROUP_COLS = ("region", "meshregion", "metro")
GeoDF: TypeAlias = Any

@click.command()
@click.argument("layer_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--png", "png_path", type=click.Path(path_type=Path), default="check.png",
              show_default=True)
def cli(layer_dir: Path, png_path: Path):
    files = sorted(layer_dir.glob("*.geojson"))
    if not files:
        raise click.ClickException(f"no .geojson files in {layer_dir}")

    data: dict[str, GeoDF]  = {f.stem: gpd.read_file(f) for f in files} # pyright: ignore[reportUnknownMemberType]
    problems = 0

    for name, gdf in data.items():
        kb = (layer_dir / f"{name}.geojson").stat().st_size / 1024
        invalid = int((~gdf.geometry.is_valid).sum())
        empty = int(gdf.geometry.is_empty.sum())
        verts = int(gdf.geometry.count_coordinates().sum())
        b = gdf.total_bounds
        click.echo(f"\n{name}: {len(gdf)} features, {kb:,.0f} KB, {verts:,} vertices")
        click.echo(f"  bounds lon {b[0]:.2f}..{b[2]:.2f}, lat {b[1]:.2f}..{b[3]:.2f}")
        if invalid or empty:
            problems += 1
            click.echo(f"  PROBLEM: {invalid} invalid, {empty} empty geometries")
        for col in GROUP_COLS:
            if col in gdf.columns:
                counts = gdf[col].value_counts(dropna=False)
                shown = ", ".join(f"{k if k == k and k is not None else '(none)'}={v}"
                                  for k, v in counts.items())
                click.echo(f"  {col}: {shown}")
        if "shapeName" in gdf.columns and gdf["shapeName"].duplicated().any():
            dups = sorted(gdf.loc[gdf["shapeName"].duplicated(keep=False), "shapeName"].unique())
            click.echo(f"  note: repeated names (fine for counties in different states): {dups[:8]}")

    # one panel per layer, same extent so they are comparable
    names = list(data)
    cols = min(3, len(names))
    rows = -(-len(names) // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(5.5 * cols, 4.5 * rows), squeeze=False)
    allb = gpd.GeoSeries([g.geometry.union_all() for g in data.values()]).total_bounds
    for ax, name in zip(axes.flat, names):
        gdf = data[name]
        col = next((c for c in GROUP_COLS if c in gdf.columns and gdf[c].notna().any()), None)
        if col:
            gdf.plot(ax=ax, column=col, categorical=True, legend=True, edgecolor="black",
                     linewidth=0.3, legend_kwds={"fontsize": 7, "loc": "lower left"},
                     missing_kwds={"color": "lightgrey"})
        else:
            gdf.plot(ax=ax, edgecolor="black", linewidth=0.3)
        ax.set_title(name)
        ax.set_xlim(allb[0], allb[2])
        ax.set_ylim(allb[1], allb[3])
        ax.set_aspect(1 / max(0.2, abs(__import__("math").cos(__import__("math").radians((allb[1] + allb[3]) / 2)))))
        ax.tick_params(labelsize=6)
    for ax in list(axes.flat)[len(names):]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(png_path, dpi=110) # pyright: ignore[reportUnknownMemberType]
    click.echo(f"\nWrote {png_path}" + ("" if not problems else f"  ({problems} layer(s) with problems)"))


if __name__ == "__main__":
    cli()