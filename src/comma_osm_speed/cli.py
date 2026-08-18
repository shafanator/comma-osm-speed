"""Command-line interface for comma-osm-speed."""
from __future__ import annotations

import datetime as dt
import logging
import sys
import warnings
from pathlib import Path

# Silence the noisy urllib3-on-LibreSSL warning that macOS system Python emits.
warnings.filterwarnings("ignore", message=".*OpenSSL 1.1.1.*", category=Warning)
try:
    import urllib3
    urllib3.disable_warnings()
except Exception:  # pragma: no cover
    pass

import click  # noqa: E402

from .config import Config
from .osm_submitter import write_osc
from .pipeline import run_analysis, write_report
from .trends import (
    build_histories,
    load_ignore_list,
    write_recurring_csv,
    write_summary_csv,
)
from .web import serve as serve_review

log = logging.getLogger("comma_osm_speed")


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _parse_date(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


@click.group()
@click.option("-v", "--verbose", count=True, help="Repeat for more verbosity (-v info, -vv debug).")
@click.option("--env-file", type=click.Path(path_type=Path), default=None, help="Path to .env file.")
@click.pass_context
def main(ctx: click.Context, verbose: int, env_file: Path | None) -> None:
    """Compare Comma 3X driving data against OSM maxspeed tags."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config"] = Config.from_env(env_file)


@main.command()
@click.option(
    "--gps-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=None,
    help="Directory of pre-extracted route .json files (alternative offline input).",
)
@click.option("--start", default=None, help="Start date for Comma Connect fetch (ISO 8601, UTC).")
@click.option("--end", default=None, help="End date for Comma Connect fetch (ISO 8601, UTC).")
@click.option(
    "--ignore-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Text file of way_ids to drop (one per line, # for comments).",
)
@click.option(
    "--threshold-mph",
    type=float,
    default=10.0,
    show_default=True,
    help="Flag a way if |observed p85 - current OSM maxspeed| >= this.",
)
@click.option(
    "--default-units",
    type=click.Choice(["mph", "kmh"]),
    default="mph",
    show_default=True,
    help="Country default for bare maxspeed values (US uses mph).",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=Path("output"),
    show_default=True,
)
@click.pass_context
def analyze(
    ctx: click.Context,
    gps_dir: Path | None,
    start: str | None,
    end: str | None,
    ignore_file: Path | None,
    threshold_mph: float,
    default_units: str,
    output: Path,
) -> None:
    """Map-match trips and write candidate edits to ./output/.

    Two data sources:
      --start / --end       Fetch your routes from Comma Connect (default)
      --gps-dir PATH        Read pre-extracted GPS .json files (offline input)
    """
    cfg: Config = ctx.obj["config"]
    cfg.threshold_mph = threshold_mph
    cfg.output_dir = output

    if gps_dir is not None:
        if start or end:
            raise click.UsageError("Pass either --gps-dir or --start/--end, not both.")
        candidates = run_analysis(
            cfg,
            local_gps_dir=gps_dir,
            default_units=default_units,
        )
    else:
        if not (start and end):
            raise click.UsageError(
                "Need either --gps-dir PATH, or both --start and --end."
            )
        start_dt = _parse_date(start)
        end_dt = _parse_date(end)
        if end_dt <= start_dt:
            raise click.UsageError("--end must be after --start")
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        candidates = run_analysis(
            cfg,
            start_unix_ms=start_ms,
            end_unix_ms=end_ms,
            default_units=default_units,
        )

    # Apply ignore list before writing reports.
    ignore_ids = load_ignore_list(ignore_file) if ignore_file else set()
    if ignore_ids:
        before = len(candidates)
        candidates = [c for c in candidates if c.way_id not in ignore_ids]
        click.echo(f"Dropped {before - len(candidates)} candidates per ignore list ({len(ignore_ids)} way_ids).")

    paths = write_report(candidates, output)
    osc_path = output / "candidates.osc"
    write_osc(candidates, str(osc_path), units=default_units)

    missing_count = sum(1 for c in candidates if c.current_maxspeed_mph is None)
    mismatched_count = len(candidates) - missing_count

    click.echo(f"\nFound {len(candidates)} candidate edits ({missing_count} missing maxspeed, {mismatched_count} mismatched).")
    click.echo(f"  Missing maxspeed:    {paths['missing']}")
    click.echo(f"  Mismatched maxspeed: {paths['mismatched']}")
    click.echo(f"  JSON (all):          {paths['json']}")
    click.echo(f"  OSC (for JOSM):      {osc_path}")
    if candidates:
        click.echo("\nBoth CSVs are sorted by sample_count (highest evidence first).")
        click.echo("'proposed_maxspeed_mph' is INFERRED (observed p85 rounded to the nearest 5,")
        click.echo("flagged against the highway type) — see 'proposal_reason'. The raw value is")
        click.echo("in 'observed_p85_mph'. Always verify the posted sign before tagging.")


@main.command()
@click.option(
    "--history-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    required=True,
    help="Root directory containing YYYY-MM-DD subfolders of past runs.",
)
@click.option(
    "--ignore-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Text file of way_ids to omit from trend reports.",
)
@click.option(
    "--min-weeks",
    type=int,
    default=2,
    show_default=True,
    help="Minimum runs a way_id must appear in to be considered 'recurring'.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write trend CSVs. Defaults to the latest run's folder.",
)
def trends(history_dir: Path, ignore_file: Path | None, min_weeks: int, output: Path | None) -> None:
    """Analyze candidate-edit history across runs.

    Reads every YYYY-MM-DD/candidates.json under --history-dir and emits:

      recurring_candidates.csv  way_ids that appeared in >= --min-weeks runs
      trend_summary.csv         per-run candidate counts
    """
    ignore_ids = load_ignore_list(ignore_file)
    histories = build_histories(history_dir)

    if output is None:
        # Default to the most recent run dir.
        run_dirs = sorted([p for p in history_dir.iterdir() if p.is_dir()], key=lambda p: p.name)
        if not run_dirs:
            raise click.UsageError("History dir has no run subfolders.")
        output = run_dirs[-1]
    output.mkdir(parents=True, exist_ok=True)

    rec_path = output / "recurring_candidates.csv"
    sum_path = output / "trend_summary.csv"
    rec_count = write_recurring_csv(histories, rec_path, min_weeks=min_weeks, ignore_ids=ignore_ids)
    sum_count = write_summary_csv(history_dir, sum_path)

    click.echo(f"\n{rec_count} recurring way_ids (>= {min_weeks} runs)")
    click.echo(f"  recurring:  {rec_path}")
    click.echo(f"  summary:    {sum_path}  ({sum_count} runs in history)")


@main.command()
@click.option(
    "--candidates",
    "candidates_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Path to a candidates.json file (from `analyze` or a history/ run).",
)
@click.option(
    "--ignore-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("ignored_ways.txt"),
    show_default=True,
    help="Path to the persistent ignore list. Created on first ignore.",
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=8765, show_default=True)
@click.option("--no-browser", is_flag=True, help="Don't auto-open a browser window.")
def review(candidates_path: Path, ignore_file: Path, host: str, port: int, no_browser: bool) -> None:
    """Open a local web UI to step through candidates and mark ones to ignore.

    Ignore decisions are persisted to --ignore-file (default ignored_ways.txt),
    which is the same list `analyze` and `trends` honor — so once you ignore a
    way here, it's filtered out of all future runs automatically.
    """
    click.echo(f"Serving review UI for {candidates_path}")
    click.echo(f"Ignore list: {ignore_file}")
    click.echo(f"URL: http://{host}:{port}/  (Ctrl-C to stop)")
    serve_review(
        candidates_path=candidates_path,
        ignore_path=ignore_file,
        host=host,
        port=port,
        open_browser=not no_browser,
    )


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
