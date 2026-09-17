from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
from pathlib import Path

from .converter import EXTENSIONS, convert
from .model import Activity, ConversionResult, write_csv


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def print_preview(result: ConversionResult, *, limit: int = 8) -> None:
    counts = Counter(activity.activity_type for activity in result.activities)
    print(f"Actividades: {len(result.activities)}")
    print("Tipos: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    for key, value in result.checks.items():
        if key != "activityCounts":
            print(f"Control {key}: {value}")
    for warning in result.warnings:
        print(f"AVISO: {warning}")
    print(f"Vista previa (primeras {min(limit, len(result.activities))}):")
    for activity in result.activities[:limit]:
        target = activity.symbol or "cash"
        print(
            f"  {activity.date} | {activity.activity_type:10} | "
            f"{activity.amount} {activity.currency} | {target}"
        )


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    counter = 1
    while candidate.exists():
        candidate = directory / f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
        counter += 1
    return candidate


def _move(source: Path, root: Path, relative_parent: Path) -> Path:
    destination = _unique_path(root / relative_parent, source.name)
    shutil.move(str(source), destination)
    return destination


class WatchService:
    def __init__(self) -> None:
        self.config_path = Path(os.environ.get("CONFIG_PATH", "/app/config.json"))
        self.inbox = Path(os.environ.get("INBOX_DIR", "/inbox"))
        self.outbox = Path(os.environ.get("OUTBOX_DIR", "/outbox"))
        self.processed = Path(os.environ.get("PROCESSED_DIR", "/processed"))
        self.failed = Path(os.environ.get("FAILED_DIR", "/failed"))
        self.state_path = Path(os.environ.get("STATE_DIR", "/state")) / "emitted.json"
        self.dry_run = _boolean("DRY_RUN", True)
        self.seed_state_only = _boolean("SEED_STATE_ONLY", False)
        self.interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

    def load_config(self) -> dict:
        config = _load_json(self.config_path, None)
        if not isinstance(config, dict) or not isinstance(config.get("accounts"), dict):
            raise ValueError(f"Configuración inválida: {self.config_path}")
        return config

    def files(self) -> list[Path]:
        if not self.inbox.exists():
            return []
        return sorted(path for path in self.inbox.rglob("*") if path.is_file())

    def account(self, path: Path, config: dict) -> tuple[str, dict, Path]:
        relative = path.relative_to(self.inbox)
        if len(relative.parts) < 3:
            raise ValueError("El archivo debe estar en /inbox/<proveedor>/<cuenta>/")
        key = "/".join(relative.parts[:2])
        account = config["accounts"].get(key)
        if account is None:
            raise ValueError(f"No existe configuración para la cuenta {key!r}")
        return key, account, Path(*relative.parts[:2])

    def process(self, path: Path, config: dict) -> None:
        key, account, relative_parent = self.account(path, config)
        parser = account.get("parser")
        allowed = EXTENSIONS.get(parser)
        if not allowed or path.suffix.lower() not in allowed:
            raise ValueError(f"El archivo {path.name} no es válido para {parser!r}")

        print(f"\n[{key}] {path.name}")
        result = convert(path, account)
        state = _load_json(self.state_path, {"accounts": {}})
        emitted = set(state.setdefault("accounts", {}).setdefault(key, []))
        fresh = [activity for activity in result.activities if activity.identifier not in emitted]
        preview = ConversionResult(fresh, checks={**result.checks, "previouslyEmitted": len(result.activities) - len(fresh)})
        print_preview(preview)
        if self.dry_run:
            print("DRY_RUN=true: no se ha escrito ni movido ningún archivo.")
            return

        if self.seed_state_only:
            print("SEED_STATE_ONLY=true: se registra el histórico sin generar CSV.")
        elif fresh:
            output_name = f"{path.stem}-wealthfolio.csv"
            output = _unique_path(self.outbox / relative_parent, output_name)
            write_csv(output, fresh)
            print(f"CSV generado: {output}")
        else:
            print("No hay actividades nuevas; no se genera un CSV vacío.")

        state["accounts"][key] = sorted(emitted | {item.identifier for item in result.activities})
        _save_json(self.state_path, state)
        destination = _move(path, self.processed, relative_parent)
        print(f"Original archivado: {destination}")

    def run_once(self) -> int:
        config = self.load_config()
        failures = 0
        for path in self.files():
            try:
                self.process(path, config)
            except Exception as error:
                failures += 1
                print(f"ERROR {path}: {error}")
                if not self.dry_run:
                    try:
                        relative = path.relative_to(self.inbox)
                        parent = Path(*relative.parts[:2]) if len(relative.parts) >= 2 else Path()
                        destination = _move(path, self.failed, parent)
                        print(f"Original movido a failed: {destination}")
                    except Exception as move_error:
                        print(f"ERROR al mover a failed: {move_error}")
        return failures

    def run(self, once: bool = False) -> int:
        print(
            f"Wealthfolio Importer | inbox={self.inbox} | "
            f"dry_run={str(self.dry_run).lower()} | "
            f"seed_state_only={str(self.seed_state_only).lower()}"
        )
        while True:
            failures = self.run_once()
            if once:
                return 1 if failures else 0
            time.sleep(self.interval)
