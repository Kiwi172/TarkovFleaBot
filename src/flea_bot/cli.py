"""Command-line interface.

    flea-bot fetch                 # pull prices into the local database
    flea-bot rank                  # show the current top-N opportunities
    flea-bot stats "Bottle of water"
    flea-bot calibrate             # print cursor position, for window coords
    flea-bot snip --name X --region L,T,W,H
    flea-bot run --dry-run         # drive the FSM without touching the game
    flea-bot doctor                # check the environment
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import typer

from flea_bot import __version__
from flea_bot.config import Config, load_config, reset_config_cache
from flea_bot.logging_setup import get_logger, setup_logging

app = typer.Typer(
    add_completion=False,
    help="Flea market price analysis and UI automation for SPT (offline Tarkov).",
)
log = get_logger("cli")

# Populated by the top-level callback so subcommands share one config.
_state: dict[str, Config] = {}


def _config() -> Config:
    return _state["config"]


@app.callback()
def main(
    config_path: Path | None = typer.Option(
        None, "--config", "-c", help="Path to config.toml."
    ),
    dry_run: bool | None = typer.Option(
        None, "--dry-run/--live", help="Override [general].dry_run."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Load config and set up logging for every subcommand."""
    reset_config_cache()
    cfg = load_config(config_path)
    if dry_run is not None:
        cfg.general.dry_run = dry_run
    if verbose:
        cfg.logging.level = "DEBUG"
    setup_logging(cfg, force=True)
    _state["config"] = cfg

    log.debug("Config loaded from {}", cfg.source_path)
    if not cfg.general.dry_run:
        log.warning("LIVE MODE — input will be dispatched to the game.")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f"flea-bot {__version__}")


@app.command()
def fetch(
    store: bool = typer.Option(True, help="Write snapshots to the database."),
    limit: int | None = typer.Option(None, help="Only show this many rows."),
) -> None:
    """Fetch current prices from the API and record a snapshot."""
    from flea_bot.database.repository import PriceRepository
    from flea_bot.scraper.base import fetch_with_fallback

    cfg = _config()
    items, source = fetch_with_fallback(cfg)

    if not items:
        typer.secho("No items returned.", fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.secho(f"Source: {source} ({len(items)} items)", fg=typer.colors.CYAN)

    if store:
        repo = PriceRepository(cfg)
        # Tag the snapshot with its origin — SPT and live prices are different
        # economies and must not be averaged together in the history.
        count = repo.insert_snapshots(items, source=source)
        repo.dispose()
        typer.secho(f"Stored {count} snapshots in {cfg.db_path}", fg=typer.colors.GREEN)

    for item in items[: limit or 10]:
        typer.echo(f"  {item.item_name:<48} {item.price:>10,} ₽")


@app.command()
def rank(
    top: int | None = typer.Option(None, "--top", "-n", help="How many to show."),
    sort: str = typer.Option("score", help="score | margin | ratio | per_slot"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Fetch fresh prices instead of using the last snapshot."
    ),
    show_rejects: bool = typer.Option(False, help="Print the rejection breakdown."),
) -> None:
    """Show the most profitable flea-to-trader flips."""
    from flea_bot.database.repository import PriceRepository
    from flea_bot.decision.ranking import SortKey, rank_items
    from flea_bot.scraper.base import fetch_with_fallback
    from flea_bot.traders.prices import TraderPriceBook

    cfg = _config()
    repo = PriceRepository(cfg)

    items, source = fetch_with_fallback(cfg)
    typer.secho(f"Source: {source}", fg=typer.colors.CYAN)
    if refresh:
        repo.insert_snapshots(items, source=source)

    book = TraderPriceBook.load(config=cfg)
    margins = book.compute_margins(items)
    stats = repo.stats_for_all(source=source)
    result = rank_items(margins, stats=stats, config=cfg, top_n=top, sort_by=SortKey(sort))
    repo.dispose()

    if not result.items:
        typer.secho("Nothing passed the filters.", fg=typer.colors.YELLOW)
        typer.echo(f"Rejections: {result.rejection_summary()}")
        typer.echo("Loosen [thresholds] in your config to widen the net.")
        raise typer.Exit(0)

    header = f"{'#':<4}{'ITEM':<40}{'FLEA':>12}{'TRADER':>12}{'MARGIN':>12}{'/SLOT':>10}"
    typer.secho(header, bold=True)
    typer.echo("-" * len(header))
    for i, ranked in enumerate(result.items, 1):
        m = ranked.margin
        typer.echo(
            f"{i:<4}{m.item_name[:38]:<40}{m.flea_price:>12,}{m.trader_price:>12,}"
            f"{m.profit_margin:>12,}{m.profit_per_slot:>10,.0f}"
        )

    if show_rejects:
        typer.echo(f"\nRejections: {result.rejection_summary()}")


@app.command()
def stats(
    item: str = typer.Argument(..., help="Item name (or part of one)."),
    hours: int | None = typer.Option(None, help="Rolling window in hours."),
) -> None:
    """Rolling average and volatility for an item from local history."""
    from sqlalchemy import select

    from flea_bot.database.models import Item
    from flea_bot.database.repository import PriceRepository

    cfg = _config()
    repo = PriceRepository(cfg)

    with repo.session() as sess:
        matches = list(
            sess.scalars(select(Item).where(Item.name.ilike(f"%{item}%")).limit(10))
        )

    if not matches:
        typer.secho(f"No item matching {item!r} in the database.", fg=typer.colors.RED)
        typer.echo("Run `flea-bot fetch` first.")
        raise typer.Exit(1)

    for match in matches:
        s = repo.stats(match.id, window_hours=hours)
        if s is None:
            typer.echo(f"{match.name}: no snapshots in window")
            continue
        typer.secho(f"\n{s.item_name}", bold=True)
        typer.echo(f"  samples    {s.samples}  (window {s.window_hours}h)")
        typer.echo(f"  mean       {s.mean:,.0f} ₽")
        typer.echo(f"  latest     {s.latest:,} ₽")
        typer.echo(f"  range      {s.minimum:,} – {s.maximum:,} ₽")
        typer.echo(f"  stddev     {s.stddev:,.0f}")
        caveat = "" if s.is_reliable else "  (too few samples)"
        typer.echo(f"  volatility {s.volatility:.3f}{caveat}")

    repo.dispose()


@app.command()
def gui() -> None:
    """Open the dashboard: live earnings, spend cap, start/pause/stop."""
    cfg = _config()
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        typer.secho("tkinter is not available in this Python.", fg=typer.colors.RED)
        typer.echo("  Linux:   sudo apt install python3-tk")
        typer.echo("  Or use a uv-managed Python, which bundles it:")
        typer.echo("           uv python install 3.12 && uv sync")
        raise typer.Exit(1) from exc

    from flea_bot.gui import launch

    launch(cfg)


@app.command()
def setup(
    only: str | None = typer.Option(
        None, help="Comma-separated step keys to redo (default: all)."
    ),
    output: Path | None = typer.Option(None, help="Config path to write."),
) -> None:
    """Interactive calibration — capture UI regions and templates, write config.

    Run this once after install, and again after changing resolution or UI
    scale. It replaces only the [window] sections; your thresholds and price
    source settings are preserved.
    """
    from flea_bot.config import DEFAULT_CONFIG_PATH
    from flea_bot.setup_wizard import (
        STEPS,
        CalibrationWizard,
        WizardAborted,
        countdown,
        write_config,
    )

    cfg = _config()
    target = output or DEFAULT_CONFIG_PATH

    typer.secho("\nflea-bot calibration", bold=True)
    typer.echo("Point at each UI element in turn; the wizard reads your cursor.")
    typer.echo("You'll mark two corners per item. Ctrl-C aborts at any time.\n")

    keys = [k.strip() for k in only.split(",")] if only else None
    if keys:
        known = {s.key for s in STEPS}
        if unknown := set(keys) - known:
            typer.secho(f"Unknown step(s): {', '.join(sorted(unknown))}", fg=typer.colors.RED)
            typer.echo(f"Valid steps: {', '.join(s.key for s in STEPS)}")
            raise typer.Exit(1)

    def ask(message: str) -> str:
        return input(message)

    def say(message: str) -> None:
        typer.echo(message)

    wizard = CalibrationWizard(cfg, prompt=ask, notify=say)

    try:
        if keys is None:
            typer.echo("Make sure SPT is running and visible.")
            countdown(say, 5)
            window = wizard.detect_window()
        else:
            w = cfg.window
            window = (w.left, w.top, w.width, w.height)
            typer.echo(f"Reusing existing window bounds {list(window)}.")

        wizard.run(only=keys)
    except (KeyboardInterrupt, WizardAborted):
        typer.secho("\nCalibration aborted — nothing was written.", fg=typer.colors.YELLOW)
        raise typer.Exit(1) from None

    written = write_config(wizard, window, target)
    typer.secho(f"\nWrote {written}", fg=typer.colors.GREEN)
    typer.echo("Verify it with:  flea-bot doctor")
    typer.echo("Then try a safe run:  flea-bot run --dry-run")


@app.command()
def calibrate(seconds: int = typer.Option(30, help="How long to track the cursor.")) -> None:
    """Print the cursor position live, to fill in [window] coordinates."""
    try:
        from flea_bot.input.backends import get_backend

        backend = get_backend()
    except Exception as exc:
        typer.secho(f"No input backend for cursor tracking: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.echo(f"Tracking cursor for {seconds}s — hover over UI elements. Ctrl-C to stop.")
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            x, y = backend.position()
            sys.stdout.write(f"\r  x={x:<6} y={y:<6}")
            sys.stdout.flush()
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    sys.stdout.write("\n")


@app.command()
def snip(
    name: str = typer.Option(..., "--name", help="Template name (no extension)."),
    region: str = typer.Option(..., "--region", help="left,top,width,height"),
    delay: float = typer.Option(3.0, help="Seconds to wait before capturing."),
) -> None:
    """Capture a screen region and save it as a template image."""
    from flea_bot.config import PROJECT_ROOT
    from flea_bot.vision.capture import ScreenCapture

    try:
        left, top, width, height = (int(n) for n in region.split(","))
    except ValueError as exc:
        typer.secho("--region must be four integers: left,top,width,height", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.echo(f"Capturing in {delay:.0f}s — switch to the game window now.")
    time.sleep(delay)

    capture = ScreenCapture(_config())
    frame = capture.grab((left, top, width, height))
    target = PROJECT_ROOT / "assets" / "templates" / f"{name}.png"
    capture.save(frame, target)
    capture.close()
    typer.secho(f"Saved {target}", fg=typer.colors.GREEN)


@app.command()
def run(
    top: int | None = typer.Option(None, "--top", "-n", help="How many trades to queue."),
    max_trades: int | None = typer.Option(None, help="Stop after this many."),
) -> None:
    """Rank opportunities and drive the state machine through them."""
    from flea_bot.database.repository import PriceRepository
    from flea_bot.decision.ranking import rank_items
    from flea_bot.orchestrator.machine import FleaBotMachine
    from flea_bot.safety import RunGuard
    from flea_bot.scraper.base import fetch_with_fallback
    from flea_bot.traders.prices import TraderPriceBook

    cfg = _config()

    if not cfg.general.dry_run:
        typer.secho(
            "\n  LIVE MODE: this will send real clicks to whatever is on screen.",
            fg=typer.colors.RED,
            bold=True,
        )
        typer.echo(f"  Kill switch: {cfg.safety.kill_hotkey.upper()}  "
                   f"Pause: {cfg.safety.pause_hotkey.upper()}")
        if not typer.confirm("  Continue?", default=False):
            raise typer.Abort()

    items, source = fetch_with_fallback(cfg)
    typer.secho(f"Source: {source}", fg=typer.colors.CYAN)

    repo = PriceRepository(cfg)
    repo.insert_snapshots(items, source=source)
    book = TraderPriceBook.load(config=cfg)
    ranked = rank_items(
        book.compute_margins(items), stats=repo.stats_for_all(source=source), config=cfg, top_n=top
    )
    repo.dispose()

    if not ranked.items:
        typer.secho("Nothing profitable to do.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    with RunGuard(cfg) as guard:
        machine = FleaBotMachine(cfg, guard=guard)
        machine.queue_trades(ranked.items)
        context = machine.run(max_trades=max_trades)

    typer.echo(f"\nSummary: {context.summary()}")


@app.command()
def doctor() -> None:
    """Check that dependencies, templates and config are usable."""
    cfg = _config()
    ok = True

    typer.secho("Configuration", bold=True)
    typer.echo(f"  source     {cfg.source_path}")
    typer.echo(f"  dry_run    {cfg.general.dry_run}")
    typer.echo(f"  data_dir   {cfg.data_dir}")

    typer.secho("\nPrice source", bold=True)
    typer.echo(f"  configured {cfg.prices.source}")
    try:
        from flea_bot.scraper.spt import SPTDataSource

        install = SPTDataSource.find_install(cfg)
        if install is not None:
            typer.secho(f"  ok    local SPT database at {install}", fg=typer.colors.GREEN)
            typer.echo("        Trader eligibility will be resolved exactly.")
        else:
            typer.secho("  none  no local SPT install found", fg=typer.colors.YELLOW)
            typer.echo(
                "        Falling back to the mirror; trader payouts will be an\n"
                "        upper bound. Set [prices].spt_install_path for exact data."
            )
    except Exception as exc:
        typer.secho(f"  MISS  {exc}", fg=typer.colors.RED)

    typer.secho("\nDependencies", bold=True)
    for module, why in [
        ("httpx", "price fetching"),
        ("sqlalchemy", "price history"),
        ("cv2", "template matching"),
        ("mss", "screen capture"),
        ("pytesseract", "OCR"),
        ("transitions", "state machine"),
    ]:
        try:
            __import__(module)
            typer.secho(f"  ok    {module:<14} ({why})", fg=typer.colors.GREEN)
        except ImportError as exc:
            ok = False
            typer.secho(f"  MISS  {module:<14} ({why}): {exc}", fg=typer.colors.RED)

    typer.secho("\nTesseract binary", bold=True)
    try:
        import pytesseract

        if cfg.ocr.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = cfg.ocr.tesseract_cmd
        typer.secho(f"  ok    v{pytesseract.get_tesseract_version()}", fg=typer.colors.GREEN)
    except Exception as exc:
        ok = False
        typer.secho(f"  MISS  {exc}", fg=typer.colors.RED)
        typer.echo("        Install tesseract-ocr, or set [ocr].tesseract_cmd.")

    typer.secho("\nInput backend", bold=True)
    try:
        from flea_bot.input.backends import get_backend

        typer.secho(f"  ok    {get_backend(dry_run=cfg.general.dry_run).name}",
                    fg=typer.colors.GREEN)
    except Exception as exc:
        ok = False
        typer.secho(f"  MISS  {exc}", fg=typer.colors.RED)

    typer.secho("\nTemplates", bold=True)
    if not cfg.window.templates:
        typer.secho("  none configured in [window.templates]", fg=typer.colors.YELLOW)
    for name in sorted(cfg.window.templates):
        path = cfg.window.template(name)
        if path.is_file():
            typer.secho(f"  ok    {name:<20} {path.name}", fg=typer.colors.GREEN)
        else:
            ok = False
            typer.secho(f"  MISS  {name:<20} {path}", fg=typer.colors.YELLOW)
    if any(not cfg.window.template(n).is_file() for n in cfg.window.templates):
        typer.echo("        Capture these with: flea-bot snip --name <n> --region L,T,W,H")

    typer.secho("\nTrader reference", bold=True)
    ref = cfg.trader_reference_path()
    if ref.is_file():
        from flea_bot.traders.prices import TraderPriceBook

        book = TraderPriceBook.load(config=cfg)
        typer.secho(f"  ok    {len(book)} override(s) in {ref.name}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  MISS  {ref}", fg=typer.colors.YELLOW)

    typer.echo()
    if ok:
        typer.secho("All checks passed.", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho("Some checks failed — see above.", fg=typer.colors.YELLOW, bold=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
