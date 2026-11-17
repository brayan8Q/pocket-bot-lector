"""The 15/3-second strategy's learning part (user, 23 Oct 2026: "que sea inteligente, que entienda las velas, la tendencia
y todo, y que no pare de operar"). Every second, for each pair, it reads the last two minutes of prices as 1-s samples and
5-s candles and turns them into votes (+1 up, -1 down, 0 nothing):

  tendencia corta   5-s average over the 20-s one        tendencia larga  15-s average over the 60-s one
  impulso 5 s       price now vs 5 s ago                 impulso 15 s     price now vs 15 s ago
  vela              colour of the last 5-s candle        racha            three candles of one colour → the other way
  mechas            lower wick longer than upper → up    RSI 14           above 70 → down, below 30 → up
  bandas            beyond 2 deviations of 20 s → back   ruptura          above the 30-s high → up, below the low → down

An online logistic regression weighs the votes: each sample is resolved `seconds` later (up or down; same price is
skipped) and nudges the weights toward what worked, so the mix follows the market of the moment (a vote that keeps
failing gets a negative weight, i.e. it is read the other way). It always gives a direction. No I/O here."""
from __future__ import annotations

import math
import statistics

FEATURES = ("sesgo", "tendencia corta", "tendencia larga", "impulso 5 s", "impulso 15 s", "vela", "racha", "mechas",
            "RSI 14", "bandas", "ruptura")


def sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def ema_last(values, n: int) -> float:
    a, e = 2 / (n + 1), values[0]
    for v in values:
        e = a * v + (1 - a) * e
    return e


def candles(g: list[float], size: int = 5, count: int = 6) -> list[tuple[float, float, float, float]]:
    """The last `count` candles of `size` seconds (open, high, low, close) from 1-s prices, oldest first."""
    out = []
    for k in range(count, 0, -1):
        chunk = g[len(g) - k * size - 1: len(g) - (k - 1) * size]
        if len(chunk) < 2:
            continue
        out.append((chunk[0], max(chunk), min(chunk), chunk[-1]))
    return out


def rsi(g: list[float], n: int = 14) -> float | None:
    d = [g[i] - g[i - 1] for i in range(len(g) - n, len(g))]
    up = sum(x for x in d if x > 0)
    down = -sum(x for x in d if x < 0)
    if up + down == 0:
        return None
    return 100 * up / (up + down)


def features(g: list[float]) -> list[float] | None:
    """The votes for these 1-s prices (oldest first, at least 61), bias first."""
    if len(g) < 61:
        return None
    x = [1.0]
    x.append(sign(ema_last(g[-40:], 5) - ema_last(g[-40:], 20)))
    x.append(sign(ema_last(g[-120:], 15) - ema_last(g[-120:], 60)))
    x.append(sign(g[-1] - g[-6]))
    x.append(sign(g[-1] - g[-16]))
    cs = candles(g)
    last = cs[-1]
    x.append(sign(last[3] - last[0]))
    colours = [sign(c[3] - c[0]) for c in cs[-3:]]
    x.append(-colours[-1] if len(colours) == 3 and colours[0] == colours[1] == colours[2] != 0 else 0)
    upper = last[1] - max(last[0], last[3])
    lower = min(last[0], last[3]) - last[2]
    x.append(sign(lower - upper))
    r = rsi(g)
    x.append(-1 if r is not None and r > 70 else 1 if r is not None and r < 30 else 0)
    window = g[-20:]
    mean, dev = statistics.fmean(window), statistics.pstdev(window)
    x.append(-1 if dev and g[-1] > mean + 2 * dev else 1 if dev and g[-1] < mean - 2 * dev else 0)
    high, low = max(g[-31:-1]), min(g[-31:-1])
    x.append(1 if g[-1] > high else -1 if g[-1] < low else 0)
    return [float(v) for v in x]


class Model:
    """Online logistic regression: P(up) = σ(w·x); one gradient step per resolved sample."""

    def __init__(self, weights=None, rate: float = 0.03, l2: float = 0.0005):
        self.w = list(weights) if weights and len(weights) == len(FEATURES) else [0.0] * len(FEATURES)
        self.rate, self.l2 = rate, l2
        self.seen = 0

    def prob_up(self, x: list[float]) -> float:
        z = sum(a * b for a, b in zip(self.w, x))
        z = max(-30.0, min(30.0, z))
        return 1 / (1 + math.exp(-z))

    def direction(self, x: list[float]) -> tuple[str, float]:
        """ARRIBA / ABAJO (never undecided: a tie leans on the short trend, then up) and the model's P(up)."""
        p = self.prob_up(x)
        if abs(p - 0.5) < 1e-9:
            return ("ABAJO" if x[1] < 0 else "ARRIBA"), p
        return ("ARRIBA" if p > 0.5 else "ABAJO"), p

    def learn(self, x: list[float], up: bool) -> None:
        err = (1.0 if up else 0.0) - self.prob_up(x)
        self.w = [w + self.rate * (err * xi - self.l2 * w) for w, xi in zip(self.w, x)]
        self.seen += 1

    def explain(self, top: int = 4) -> str:
        """The votes that weigh most now (a negative weight = read the other way)."""
        pairs = sorted(zip(FEATURES[1:], self.w[1:]), key=lambda p: -abs(p[1]))[:top]
        return ", ".join(f"{name} {'+' if w >= 0 else '−'}{abs(w):.2f}" for name, w in pairs)
