"""Velocita', tempo stimato e formattazione per le barre di avanzamento."""
from __future__ import annotations


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "--"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class Speedometer:
    """Velocita' media mobile esponenziale, per chiave (di solito il file in corso).

    Si azzera da sola quando cambia la chiave o i byte tornano indietro (nuovo tentativo).
    """

    def __init__(self, smoothing: float = 0.3, min_interval: float = 0.25) -> None:
        self.smoothing, self.min_interval = smoothing, min_interval
        self._key: str | None = None
        self._t = 0.0
        self._done = 0
        self.speed = 0.0          # byte al secondo

    def update(self, key: str, done: int, now: float) -> float:
        if key != self._key or done < self._done:
            self._key, self._t, self._done, self.speed = key, now, done, 0.0
            return 0.0
        dt = now - self._t
        if dt >= self.min_interval:
            inst = (done - self._done) / dt
            self.speed = inst if self.speed == 0 else (
                self.smoothing * inst + (1 - self.smoothing) * self.speed)
            self._t, self._done = now, done
        return self.speed

    def eta(self, done: int, total: int) -> float | None:
        if total <= 0 or self.speed <= 0:
            return None
        return max(total - done, 0) / self.speed


def describe(label: str, done: int, total: int, speed: float, eta: float | None) -> str:
    """Riga di testo per la barra del file in corso."""
    if total > 0:
        txt = f"{label}  {human_bytes(done)} / {human_bytes(total)}"
    else:
        txt = f"{label}  {human_bytes(done)}"
    if speed > 0:
        txt += f"  ·  {human_bytes(speed)}/s  ·  ancora {human_eta(eta)}"
    return txt
