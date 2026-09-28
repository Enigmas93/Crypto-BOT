#!/usr/bin/env python
"""Keeps an HTTPS ngrok tunnel open to the local dashboard (port 8000) so the
PWA on the phone (hosted on Vercel) can reach this PC.

Refuses to start without AEGIS_API_TOKEN: the tunnel makes the API reachable
from the internet, and the token is what stops anyone else from changing
BingX keys or switching to the live account.

When the public URL changes (free ngrok accounts keep one stable dev domain,
but it can still change), a Telegram message carries a link that opens the
PWA with the new server already filled in.

Usage (from backend/, venv active):
    python scripts/run_tunnel.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.logging_utils import configure_logging, get_logger, log_event  # noqa: E402
from aegis.notifications.telegram import TelegramNotifier  # noqa: E402

_LOG = get_logger("scripts.run_tunnel")
_LAST_URL_FILE = Path(__file__).resolve().parents[1] / "logs" / "tunnel_url.txt"
_AGENT_API = "http://127.0.0.1:4040/api/tunnels"


async def _public_url(client: httpx.AsyncClient) -> str | None:
    try:
        data = (await client.get(_AGENT_API)).json()
    except (httpx.HTTPError, ValueError):
        return None
    return next((t["public_url"] for t in data.get("tunnels", []) if t.get("public_url", "").startswith("https://")),
                None)


async def _main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.aegis_api_token:
        log_event(_LOG, "refusing_to_run", level=40,
                  message="AEGIS_API_TOKEN is empty - never expose the dashboard API without it.")
        await asyncio.sleep(300)  # don't let the supervisor spin
        return 1
    ngrok = shutil.which("ngrok")
    if ngrok is None:
        log_event(_LOG, "ngrok_not_found", level=40, message="install ngrok and run `ngrok config add-authtoken`")
        await asyncio.sleep(300)
        return 1

    args = [ngrok, "http", "8000", "--log", "false"]
    if settings.ngrok_domain:
        args += ["--url", settings.ngrok_domain]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            url = None
            for _ in range(30):
                url = await _public_url(client)
                if url or proc.returncode is not None:
                    break
                await asyncio.sleep(1)
            if url is None:
                log_event(_LOG, "tunnel_failed_to_start", level=40, exit_code=proc.returncode)
                return 1
            previous = _LAST_URL_FILE.read_text().strip() if _LAST_URL_FILE.exists() else ""
            _LAST_URL_FILE.parent.mkdir(exist_ok=True)
            _LAST_URL_FILE.write_text(url)
            pwa = settings.pwa_url.rstrip("/")
            log_event(_LOG, "tunnel_ready", public_url=url, pwa_link=f"{pwa}/#server={url}" if pwa else None)
            if url != previous:
                await notifier.send(
                    "📱 Aegis Quant acessível pelo celular\n"
                    + (f"Abrir o app: {pwa}/#server={url}\n" if pwa else "")
                    + f"Servidor: {url}\n(o token de acesso continua o mesmo)"
                )
            while proc.returncode is None:
                await asyncio.sleep(30)
                if await _public_url(client) is None and proc.returncode is None:
                    log_event(_LOG, "tunnel_lost", level=30)
                    break
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        await notifier.aclose()
    return 1  # exiting at all means the tunnel is down - let the supervisor restart it


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        pass
