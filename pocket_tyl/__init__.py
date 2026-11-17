"""Pocket Option · Tyl (user, 21 Oct 2026): "Camino 1" on Tyl trader - Grupo VIP's signals. Since 22 Oct 2026 the short
martingale in the same signal: J2 2.00 → J3 4.40, one shared book with a single recovery level (J2 4.00 → J3 8.80), off
for the day at +20 USD, safety brake at -40 USD (it replaced the separate books 12 → 26.40 → 42 USD and +50 USD).

Its own process (`python -m pocket_tyl`) and its own database (%LOCALAPPDATA%\\PocketTyl\\pocket_tyl.sqlite). Nothing
here imports the gold bot (`signalbridge`) or touches MetaTrader 5; SignalBridge's dashboard only READS this database."""
