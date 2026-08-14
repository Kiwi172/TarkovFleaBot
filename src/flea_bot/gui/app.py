"""Tkinter dashboard: live earnings, spend cap, and run controls.

Tkinter rather than Qt because it's in the standard library — no extra 150 MB
in the bundle, no extra DLLs for antivirus to be suspicious of, and it survives
PyInstaller with no hooks. It's not beautiful; ttk theming gets it respectable.

The window owns the main thread and never blocks: all bot work happens on
:class:`~flea_bot.gui.controller.BotController`'s worker, and the UI refreshes
from a 100 ms timer that drains the event queue.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from flea_bot.config import Config, get_config
from flea_bot.gui.controller import BotController, BotState, EventType
from flea_bot.ledger import LedgerSnapshot
from flea_bot.logging_setup import get_logger

log = get_logger("gui")

POLL_MS = 100
MAX_LOG_LINES = 500

# Colour-blind-safe: blue/amber rather than green/red, since a
# profit-vs-loss indicator is exactly where red/green fails people.
COLOURS = {
    "profit": "#0072B2",
    "loss": "#D55E00",
    "neutral": "#666666",
    "warn": "#E69F00",
    "bg": "#F5F5F5",
}


# The rouble sign (U+20BD) is missing from the default Tk font on most Linux
# desktops and renders as tofu. Terminals handle it fine, so the CLI keeps it;
# the GUI spells it out instead.
CURRENCY = "RUB"


def fmt(value: int | None) -> str:
    """Roubles with thousands separators."""
    return "—" if value is None else f"{value:,}"


class FleaBotApp(ttk.Frame):
    """The main window."""

    def __init__(self, master: tk.Tk, config: Config) -> None:
        super().__init__(master, padding=12)
        self.config_obj = config
        self.controller = BotController(config)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_counters()
        self._build_controls()
        self._build_log()

        self.rowconfigure(2, weight=1)
        self._refresh_ledger(None)
        self.after(POLL_MS, self._poll)
        master.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _build_counters(self) -> None:
        box = ttk.LabelFrame(self, text="This session", padding=10)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for i in range(4):
            box.columnconfigure(i, weight=1)

        self.var_net = tk.StringVar(value="0")
        self.var_spent = tk.StringVar(value="0")
        self.var_earned = tk.StringVar(value="0")
        self.var_remaining = tk.StringVar(value="—")
        self.var_rate = tk.StringVar(value="—")
        self.var_trades = tk.StringVar(value="0 buys · 0 sales")

        # Net profit is the headline — bigger, and recoloured by sign.
        ttk.Label(box, text="NET PROFIT", foreground=COLOURS["neutral"]).grid(
            row=0, column=0, sticky="w"
        )
        self.lbl_net = ttk.Label(box, textvariable=self.var_net, font=("TkDefaultFont", 22, "bold"))
        self.lbl_net.grid(row=1, column=0, sticky="w")

        for col, (title, var) in enumerate(
            [("SPENT", self.var_spent), ("EARNED", self.var_earned),
             ("BUDGET LEFT", self.var_remaining)],
            start=1,
        ):
            ttk.Label(box, text=title, foreground=COLOURS["neutral"]).grid(
                row=0, column=col, sticky="w"
            )
            ttk.Label(box, textvariable=var, font=("TkDefaultFont", 13)).grid(
                row=1, column=col, sticky="w"
            )

        self.progress = ttk.Progressbar(box, mode="determinate", maximum=100)
        self.progress.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 2))

        ttk.Label(box, textvariable=self.var_trades, foreground=COLOURS["neutral"]).grid(
            row=3, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(box, textvariable=self.var_rate, foreground=COLOURS["neutral"]).grid(
            row=3, column=2, columnspan=2, sticky="e"
        )

    def _build_controls(self) -> None:
        box = ttk.LabelFrame(self, text="Run", padding=10)
        box.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        box.columnconfigure(5, weight=1)

        ttk.Label(box, text=f"Max spend this session ({CURRENCY}):").grid(
            row=0, column=0, sticky="w"
        )
        self.var_budget = tk.StringVar(
            value=str(self.config_obj.safety.max_spend_per_session)
        )
        ttk.Entry(box, textvariable=self.var_budget, width=12).grid(
            row=0, column=1, sticky="w", padx=(6, 16)
        )

        ttk.Label(box, text="Trades:").grid(row=0, column=2, sticky="w")
        self.var_top = tk.StringVar(value=str(self.config_obj.thresholds.top_n))
        ttk.Spinbox(box, from_=1, to=200, textvariable=self.var_top, width=5).grid(
            row=0, column=3, sticky="w", padx=(6, 16)
        )

        # Dry-run defaults ON every launch, regardless of config: the safe
        # option should require a deliberate action to leave, every time.
        self.var_dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            box, text="Dry run (no clicks sent)", variable=self.var_dry,
            command=self._on_dry_toggle,
        ).grid(row=0, column=4, sticky="w")

        self.btn_start = ttk.Button(box, text="Start", command=self._on_start)
        self.btn_start.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        self.btn_pause = ttk.Button(box, text="Pause", command=self._on_pause, state="disabled")
        self.btn_pause.grid(row=1, column=1, sticky="ew", pady=(10, 0), padx=6)

        self.btn_stop = ttk.Button(box, text="STOP", command=self._on_stop, state="disabled")
        self.btn_stop.grid(row=1, column=2, sticky="ew", pady=(10, 0))

        self.var_status = tk.StringVar(value="Idle")
        ttk.Label(box, textvariable=self.var_status, font=("TkDefaultFont", 10, "bold")).grid(
            row=1, column=3, columnspan=3, sticky="e", pady=(10, 0)
        )

        kill = self.config_obj.safety.kill_hotkey.upper()
        pause = self.config_obj.safety.pause_hotkey.upper()
        ttk.Label(
            box,
            text=f"Global hotkeys: {pause} pause · {kill} kill (needs root on Linux)",
            foreground=COLOURS["neutral"],
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))

    def _build_log(self) -> None:
        box = ttk.LabelFrame(self, text="Activity", padding=6)
        box.grid(row=2, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self.log_text = tk.Text(box, height=12, wrap="word", state="disabled",
                                font=("TkFixedFont", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _parse_budget(self) -> int | None:
        """Read the budget box. Returns None for unlimited, raises on garbage."""
        raw = self.var_budget.get().strip().replace(",", "").replace("_", "")
        if raw in ("", "0"):
            return None
        value = int(raw)  # ValueError propagates to the caller
        if value < 0:
            raise ValueError("Budget cannot be negative")
        return value

    def _on_dry_toggle(self) -> None:
        if not self.var_dry.get():
            ok = messagebox.askyesno(
                "Leave dry run?",
                "Live mode sends real mouse clicks and key presses to whatever "
                "is on screen.\n\n"
                "Make sure SPT is focused and calibrated first.\n\nContinue?",
                icon="warning",
            )
            if not ok:
                self.var_dry.set(True)

    def _on_start(self) -> None:
        try:
            budget = self._parse_budget()
        except ValueError:
            messagebox.showerror(
                "Invalid budget",
                "Enter a whole number of roubles, or 0 for unlimited.",
            )
            return

        if budget is None and not self.var_dry.get():
            ok = messagebox.askyesno(
                "No spend limit",
                "You're about to run LIVE with no maximum spend.\n\n"
                "The bot can spend every rouble you have. Continue?",
                icon="warning",
            )
            if not ok:
                return

        try:
            top_n = int(self.var_top.get())
        except ValueError:
            top_n = self.config_obj.thresholds.top_n

        if self.controller.start(budget=budget, dry_run=self.var_dry.get(), top_n=top_n):
            self.btn_start.configure(state="disabled")
            self.btn_pause.configure(state="normal")
            self.btn_stop.configure(state="normal")
            self._append_log(
                f"Started ({'dry run' if self.var_dry.get() else 'LIVE'}), "
                f"budget {fmt(budget) if budget else 'unlimited'}"
            )

    def _on_pause(self) -> None:
        self.controller.pause()

    def _on_stop(self) -> None:
        self.controller.stop()
        self._append_log("Stop requested — halting at next checkpoint.")

    def _on_close(self) -> None:
        if self.controller.running:
            if not messagebox.askyesno(
                "Bot is running", "Stop the bot and quit?", icon="warning"
            ):
                return
            self.controller.stop()
            self.controller.join(timeout=5)
        self.master.destroy()

    # ------------------------------------------------------------------
    # refresh
    # ------------------------------------------------------------------
    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        # Trim from the top so a long run can't grow the widget without bound.
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_ledger(self, snap: LedgerSnapshot | None) -> None:
        if snap is None:
            self.var_net.set("0")
            self.var_spent.set("0")
            self.var_earned.set("0")
            self.var_remaining.set("—")
            self.progress["value"] = 0
            return

        self.var_net.set(f"{snap.net:+,}")
        self.lbl_net.configure(
            foreground=COLOURS["profit"] if snap.net >= 0 else COLOURS["loss"]
        )
        self.var_spent.set(fmt(snap.spent))
        self.var_earned.set(fmt(snap.earned))
        self.var_remaining.set(
            "unlimited" if snap.remaining is None else fmt(snap.remaining)
        )
        self.progress["value"] = snap.budget_used_fraction * 100
        self.var_trades.set(f"{snap.purchases} buys · {snap.sales} sales")
        rate = snap.profit_per_hour
        self.var_rate.set(f"{rate:+,.0f} {CURRENCY}/hr" if rate else "measuring...")

    def _poll(self) -> None:
        """Drain the event queue and repaint. Runs on the Tk thread."""
        try:
            for event in self.controller.drain():
                if event.type is EventType.LEDGER:
                    self._refresh_ledger(event.payload)
                elif event.type is EventType.STATUS:
                    self._on_status(event.payload)
                elif event.type is EventType.LOG:
                    self._append_log(event.message)
                elif event.type is EventType.TRADE:
                    self._append_log(f"  trade: {event.message}")
                elif event.type is EventType.FINISHED:
                    self._append_log(event.message)
                    self._reset_buttons()
                elif event.type is EventType.ERROR:
                    self._append_log(f"ERROR: {event.message}")
                    self._reset_buttons()
                    messagebox.showerror("Bot error", event.message)
        finally:
            # Rescheduled in `finally` so one bad event can't kill the UI loop.
            self.after(POLL_MS, self._poll)

    def _on_status(self, state: BotState) -> None:
        self.var_status.set(state.value.title())
        if state is BotState.PAUSED:
            self.btn_pause.configure(text="Resume")
        elif state is BotState.RUNNING:
            self.btn_pause.configure(text="Pause")

    def _reset_buttons(self) -> None:
        self.btn_start.configure(state="normal")
        self.btn_pause.configure(state="disabled", text="Pause")
        self.btn_stop.configure(state="disabled")


def launch(config: Config | None = None) -> None:
    """Open the dashboard. Blocks until the window is closed."""
    cfg = config or get_config()
    root = tk.Tk()
    root.title("flea-bot — SPT flea market assistant")
    root.geometry("820x620")
    root.minsize(720, 520)

    try:
        style = ttk.Style()
        # 'clam' is present on every platform and looks less dated than the
        # X11 default; fall back silently if a theme is missing.
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass

    FleaBotApp(root, cfg)
    root.mainloop()
