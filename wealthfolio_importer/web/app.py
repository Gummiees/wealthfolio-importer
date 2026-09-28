from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..converter import EXTENSIONS


PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
app = FastAPI(title="Wealthfolio Importer")
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

# Fonditel is intentionally visible in the UI before its parser is implemented.
KNOWN_PROVIDERS = ("sabadell", "revolut", "xtb", "fonditel")


def _config_path() -> Path:
    return Path(os.environ.get("CONFIG_PATH", "/app/config.json"))


def _inbox_path() -> Path:
    return Path(os.environ.get("INBOX_DIR", "/inbox"))


def load_accounts() -> dict[str, dict]:
    """Load only valid account records; config.json remains the source of truth."""
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"No existe la configuración: {_config_path()}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"La configuración no es JSON válido: {error.msg}") from error
    accounts = raw.get("accounts")
    if not isinstance(accounts, dict):
        raise RuntimeError("La configuración debe contener un objeto 'accounts'.")
    return {
        key: value
        for key, value in accounts.items()
        if isinstance(key, str) and isinstance(value, dict) and value.get("parser") in EXTENSIONS
    }


def account_choices(accounts: dict[str, dict]) -> list[dict[str, object]]:
    choices = []
    for key, config in sorted(accounts.items()):
        provider, _, account = key.partition("/")
        if not provider or not account:
            continue
        extensions = sorted(EXTENSIONS[config["parser"]])
        choices.append({
            "key": key,
            "provider": provider,
            "account": account,
            "extensions": extensions,
            "accept": ",".join(extensions),
        })
    return choices


def _safe_filename(filename: str | None) -> str:
    candidate = Path(filename or "").name
    if not candidate or candidate in {".", ".."}:
        raise HTTPException(status_code=400, detail="Selecciona un archivo válido.")
    return re.sub(r"[^A-Za-z0-9._() -]", "_", candidate)


def _unique_destination(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    stem, suffix = candidate.stem, candidate.suffix
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        choices = account_choices(load_accounts())
    except RuntimeError as error:
        return templates.TemplateResponse(
            request, "index.html", {"accounts": [], "providers": [], "error": str(error)}
        )
    providers = [provider for provider in KNOWN_PROVIDERS if any(
        account["provider"] == provider for account in choices
    )]
    return templates.TemplateResponse(
        request, "index.html", {"accounts": choices, "providers": providers, "error": None}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, account_key: str = Form(...), statement: UploadFile = File(...)):
    try:
        accounts = load_accounts()
        config = accounts.get(account_key)
        if config is None:
            raise HTTPException(status_code=400, detail="La cuenta seleccionada no está configurada.")
        filename = _safe_filename(statement.filename)
        suffix = Path(filename).suffix.lower()
        allowed = EXTENSIONS[config["parser"]]
        if suffix not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"{account_key} solo acepta archivos {', '.join(sorted(allowed))}.",
            )
        destination = _unique_destination(_inbox_path() / Path(*account_key.split("/")), filename)
        temporary = destination.with_suffix(destination.suffix + ".uploading")
        with temporary.open("wb") as output:
            while chunk := await statement.read(1024 * 1024):
                output.write(chunk)
        temporary.replace(destination)
    finally:
        await statement.close()

    return templates.TemplateResponse(
        request, "upload_result.html", {"account_key": account_key, "filename": destination.name}
    )
