"""Asistente adaptativo seguro para operar exclusivamente en DEMO/DRY_RUN.
No ejecuta operaciones ni promete rentabilidad. Guarda estadísticas locales y
usa un modelo ML opcional únicamente cuando existe suficiente historial.
"""
from __future__ import annotations

import json
import math
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


class AdaptiveAssistant:
    def __init__(self, path: str = "adaptive_history.jsonl", min_history: int = 40) -> None:
        self.path = Path(path)
        self.min_history = max(20, int(min_history))
        self.lock = threading.RLock()
        self.rows: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        row = json.loads(line)
                        if isinstance(row, dict):
                            self.rows.append(row)
        except (OSError, ValueError, TypeError):
            self.rows = []

    @staticmethod
    def _features(signal: Any, candles: Iterable[dict[str, Any]]) -> dict[str, float]:
        candles = list(candles)
        last = candles[-1] if candles else {}
        def num(key: str, default: float = 0.0) -> float:
            try:
                value = float(last.get(key, default))
                return value if math.isfinite(value) else default
            except (TypeError, ValueError):
                return default
        return {
            "score": float(getattr(signal, "score", 0) or 0),
            "entry_price": float(getattr(signal, "entry_price", 0) or 0),
            "open": num("open"),
            "high": num("high"),
            "low": num("low"),
            "close": num("close"),
            "range": max(0.0, num("high") - num("low")),
        }

    def evaluate(self, asset: str, signal: Any, candles: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Returns an advisory decision. It can only approve an existing signal."""
        action = getattr(signal, "action", "none")
        score = float(getattr(signal, "score", 0) or 0)
        if action not in {"call", "put"}:
            return {"approved": False, "reason": "Sin dirección válida", "adaptive_score": 0.0}

        with self.lock:
            relevant = [r for r in self.rows if r.get("asset") == asset and r.get("action") == action and r.get("result") in {"WIN", "LOSS"}]
        wins = sum(r.get("result") == "WIN" for r in relevant)
        total = len(relevant)
        win_rate = (wins / total) if total else None

        # No se permite que el historial bloquee señales antes de contar con muestra mínima.
        if total >= self.min_history and win_rate is not None and win_rate < 0.50:
            return {"approved": False, "reason": f"Filtro adaptativo: tasa histórica baja ({win_rate:.1%})", "adaptive_score": score, "sample": total, "win_rate": win_rate}

        return {"approved": True, "reason": "Aprobada por filtros técnicos y adaptativos", "adaptive_score": score, "sample": total, "win_rate": win_rate}

    def record_signal(self, asset: str, signal: Any, candles: Iterable[dict[str, Any]]) -> None:
        row = {
            "type": "signal",
            "asset": asset,
            "action": getattr(signal, "action", "none"),
            "score": getattr(signal, "score", 0),
            "candle_time": getattr(signal, "candle_time", None),
            "features": self._features(signal, candles),
        }
        self._append(row)

    def record_result(self, asset: str, action: str, candle_time: Any, result: str, entry: float, exit_price: float) -> None:
        self._append({
            "type": "result", "asset": asset, "action": action,
            "candle_time": candle_time, "result": result,
            "entry": entry, "exit": exit_price,
        })

    def _append(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.rows.append(row)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            except OSError:
                # L'échec du journal ne doit jamais interrompre le bot.
                pass

    def summary(self) -> dict[str, Any]:
        with self.lock:
            results = [r for r in self.rows if r.get("type") == "result"]
        wins = sum(r.get("result") == "WIN" for r in results)
        losses = sum(r.get("result") == "LOSS" for r in results)
        return {"samples": len(results), "wins": wins, "losses": losses, "win_rate": (wins / len(results)) if results else None}
