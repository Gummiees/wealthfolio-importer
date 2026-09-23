from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
from pathlib import Path

from .converter import EXTENSIONS, convert
from .mcp import WealthfolioMcpClient, mcp_field_warnings
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
        if activity.target_account:
            target = f"{target} -> {activity.target_account}"
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
        self.auto_import = _boolean("AUTO_IMPORT", False)
        self.mcp_url = os.environ.get(
            "WEALTHFOLIO_MCP_URL", "http://wealthfolio:8088/mcp"
        )
        self.mcp_token_file = Path(
            os.environ.get(
                "WEALTHFOLIO_MCP_TOKEN_FILE", "/run/secrets/wealthfolio_mcp_token"
            )
        )
        self.mcp_batch_size = int(os.environ.get("MCP_IMPORT_BATCH_SIZE", "500"))
        self.interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

    def load_config(self) -> dict:
        config = _load_json(self.config_path, None)
        if not isinstance(config, dict) or not isinstance(config.get("accounts"), dict):
            raise ValueError(f"Configuración inválida: {self.config_path}")
        return config

    def apply_state_resets(self, config: dict) -> None:
        """Reset one configured account exactly once per reset token."""
        state = _load_json(self.state_path, {"accounts": {}})
        accounts = state.setdefault("accounts", {})
        applied = state.setdefault("resetTokens", {})
        changed = False
        for key, account in config["accounts"].items():
            token = str(account.get("stateResetToken", "")).strip()
            if token and applied.get(key) != token:
                cleared = len(accounts.pop(key, []))
                replay_name = str(account.get("replayProcessedFile", "")).strip()
                if replay_name:
                    source = self.processed / Path(*key.split("/")) / replay_name
                    if not source.is_file():
                        raise ValueError(f"No existe el histórico configurado para repetir: {source}")
                    destination = _unique_path(self.inbox / Path(*key.split("/")), replay_name)
                    shutil.copy2(source, destination)
                    print(f"Histórico reencolado para {key}: {destination}")
                applied[key] = token
                changed = True
                print(f"Estado reiniciado para {key}: {cleared} identificador(es) eliminados.")
        if changed:
            _save_json(self.state_path, state)

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
        preview = ConversionResult(
            fresh,
            checks={
                **result.checks,
                "previouslyEmitted": len(result.activities) - len(fresh),
            },
            warnings=list(result.warnings),
        )
        if self.auto_import:
            preview.warnings.extend(mcp_field_warnings(fresh))
        print_preview(preview)

        if self.seed_state_only and self.dry_run:
            raise ValueError("SEED_STATE_ONLY=true requiere DRY_RUN=false")

        if self.seed_state_only:
            print(
                "SEED_STATE_ONLY=true: se registra el histórico sin generar CSV "
                "ni llamar a MCP."
            )
        elif self.auto_import and fresh:
            groups: dict[str, list[Activity]] = {}
            for activity in fresh:
                target_key = activity.target_account or key
                groups.setdefault(target_key, []).append(activity)
            client = WealthfolioMcpClient.from_environment(
                self.mcp_url, self.mcp_token_file
            )
            totals = {"imported": 0, "skipped": 0, "duplicates": 0}
            run_ids: list[str] = []
            for target_key, activities in groups.items():
                target_config = config["accounts"].get(target_key)
                if target_config is None:
                    raise ValueError(
                        f"La cuenta destino {target_key!r} no existe en config.json"
                    )
                account_id = str(target_config.get("wealthfolioAccountId", ""))
                if not account_id.strip():
                    raise ValueError(f"Falta wealthfolioAccountId para {target_key}")
                imported = client.import_activities(
                    activities,
                    account_id,
                    commit=not self.dry_run,
                    batch_size=self.mcp_batch_size,
                )
                totals["imported"] += imported.imported
                totals["skipped"] += imported.skipped
                totals["duplicates"] += imported.duplicates
                run_ids.extend(imported.import_run_ids)
                print(
                    f"MCP {target_key}: actividades={len(activities)}, "
                    f"importadas={imported.imported}, omitidas={imported.skipped}, "
                    f"duplicados={imported.duplicates}"
                )
            if self.dry_run:
                print(
                    "Preview MCP correcto: "
                    f"{len(fresh)} actividad(es), {totals['duplicates']} duplicado(s)."
                )
            else:
                print(
                    "Importación MCP completada: "
                    f"importadas={totals['imported']}, omitidas={totals['skipped']}, "
                    f"duplicados={totals['duplicates']}, runs={','.join(run_ids)}"
                )

        if self.dry_run:
            print("DRY_RUN=true: no se ha escrito ni movido ningún archivo.")
            return

        if self.seed_state_only:
            pass
        elif self.auto_import:
            if not fresh:
                print("No hay actividades nuevas; no se llama a MCP.")
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
        self.apply_state_resets(config)
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
            f"seed_state_only={str(self.seed_state_only).lower()} | "
            f"auto_import={str(self.auto_import).lower()}"
        )
        while True:
            failures = self.run_once()
            if once:
                return 1 if failures else 0
            time.sleep(self.interval)
