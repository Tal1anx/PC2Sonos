"""AirPlay-Ausgabe (Apple TV, HomePod, AirPlay-2-Lautsprecher) ueber pyatv/RAOP.

Jedes Geraet holt sich denselben lokalen MP3-Stream (http://127.0.0.1:8090/stream.mp3)
ueber pyatv und spielt ihn per AirPlay ab. Mehrere AirPlay-Geraete laufen parallel,
aber nicht sample-genau synchron (anders als eine Sonos-Gruppe).

Kopplung: Apple TV/HomePod verlangen oft eine einmalige PIN-Kopplung. Die
Zugangsdaten werden in ~/.pc2sonos/airplay.json gespeichert.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass

import pyatv
from pyatv.const import PairingRequirement, Protocol
from pyatv.interface import MediaMetadata

log = logging.getLogger(__name__)
CRED_FILE = os.path.join(os.path.expanduser("~"), ".pc2sonos", "airplay.json")


@dataclass
class AirPlayDevice:
    name: str
    address: str
    identifier: str
    model: str
    needs_pairing: bool
    config: object  # pyatv BaseConfig

    @property
    def paired(self) -> bool:
        return self.identifier in _load_creds()


def _load_creds() -> dict:
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cred(identifier: str, cred: str):
    creds = _load_creds()
    creds[identifier] = cred
    os.makedirs(os.path.dirname(CRED_FILE), exist_ok=True)
    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)


class AirPlay:
    """Steuert AirPlay-Geraete aus synchronem Code; intern eigener asyncio-Loop."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="airplay", daemon=True).start()
        self._active: dict[str, tuple[object, asyncio.Future]] = {}  # id -> (atv, stream-task)
        self._pairing = None

    def _run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # --- Suche ------------------------------------------------------------------
    def discover(self, hosts=None, timeout=5, exclude_ips=()) -> list[AirPlayDevice]:
        async def scan():
            kw = {"hosts": hosts} if hosts else {}
            return await pyatv.scan(self._loop, timeout=timeout, protocol=Protocol.RAOP, **kw)

        devs = []
        for c in self._run(scan(), timeout + 10):
            if str(c.address) in exclude_ips:
                continue  # z. B. Sonos – die laufen besser ueber die Sonos-Gruppe
            svc = c.get_service(Protocol.RAOP)
            if not svc:
                continue
            devs.append(AirPlayDevice(
                name=c.name, address=str(c.address), identifier=c.identifier,
                model=c.device_info.model_str,
                needs_pairing=svc.pairing == PairingRequirement.Mandatory,
                config=c))
        return sorted(devs, key=lambda d: d.name.lower())

    # --- Kopplung ---------------------------------------------------------------
    def pair_begin(self, dev: AirPlayDevice) -> bool:
        """Startet die Kopplung. True = Geraet zeigt eine PIN an, die eingegeben werden muss."""
        async def go():
            self._pairing = await pyatv.pair(dev.config, Protocol.RAOP, self._loop, name="PC2Sonos")
            await self._pairing.begin()
            return self._pairing.device_provides_pin
        return self._run(go())

    def pair_finish(self, dev: AirPlayDevice, pin: str | None):
        async def go():
            p = self._pairing
            try:
                if pin:
                    p.pin(int(pin))
                await p.finish()
                if not p.has_paired:
                    raise RuntimeError("Kopplung fehlgeschlagen (PIN falsch?)")
                _save_cred(dev.identifier, p.service.credentials)
            finally:
                await p.close()
                self._pairing = None
        self._run(go())

    # --- Abspielen --------------------------------------------------------------
    def start(self, devices: list[AirPlayDevice], url: str, volume: int, title="PC"):
        creds = _load_creds()

        async def one(dev):
            if dev.identifier in creds:
                dev.config.set_credentials(Protocol.RAOP, creds[dev.identifier])
            atv = await pyatv.connect(dev.config, self._loop)
            try:
                await atv.audio.set_volume(float(volume))
            except Exception as e:  # noqa: BLE001
                log.warning("Lautstaerke %s: %s", dev.name, e)
            task = asyncio.ensure_future(
                atv.stream.stream_file(url, metadata=MediaMetadata(title=title, artist="PC2Sonos")))
            await asyncio.sleep(1.5)  # fruehe Fehler (Kopplung, Verbindung) sofort melden
            if task.done() and task.exception():
                atv.close()
                raise task.exception()
            return atv, task

        async def go():
            res = await asyncio.gather(*(one(d) for d in devices), return_exceptions=True)
            errors = []
            for d, r in zip(devices, res):
                if isinstance(r, Exception):
                    errors.append(f"{d.name}: {r}")
                else:
                    self._active[d.identifier] = r
            return errors
        return self._run(go(), 60)

    def stop(self, identifiers=None):
        """Alle (oder nur die angegebenen) AirPlay-Geraete stoppen."""
        async def go():
            ids = list(self._active) if identifiers is None else [i for i in identifiers if i in self._active]
            for i in ids:
                atv, task = self._active.pop(i)
                task.cancel()
                try:
                    atv.close()
                except Exception:  # noqa: BLE001
                    pass
        self._run(go())

    def is_playing(self, identifier) -> bool:
        return identifier in self._active

    def set_volume(self, volume: int):
        async def go():
            for atv, _ in list(self._active.values()):
                try:
                    await atv.audio.set_volume(float(volume))
                except Exception as e:  # noqa: BLE001
                    log.warning("Lautstaerke: %s", e)
        self._run(go())

    @property
    def active(self) -> bool:
        return bool(self._active)
