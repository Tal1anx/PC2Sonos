"""Sonos-Suche per direktem Netzwerk-Scan (ohne Multicast/SSDP).

Hintergrund: Viele Router/Access Points leiten Multicast-Suchanfragen (SSDP)
nicht zwischen LAN-Kabel und WLAN weiter, oder die Windows-Firewall blockiert
die Antworten. Dann findet soco.discover() nichts, obwohl die Lautsprecher im
selben Netz erreichbar sind.

Dieses Modul fragt stattdessen jede Adresse im lokalen /24-Netz direkt auf
Port 1400 (Sonos-HTTP-Port) ab. Dauer: ca. 1-3 Sekunden.

Verwendung:
    from pc2sonos.netscan import discover_speakers
    speakers = discover_speakers()   # -> Liste von soco.SoCo, alle Räume
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

log = logging.getLogger(__name__)

SONOS_PORT = 1400
DESCRIPTION_PATH = "/xml/device_description.xml"
_UPNP_NS = "{urn:schemas-upnp-org:device-1-0}"


@dataclass(frozen=True)
class FoundDevice:
    ip: str
    room: str
    model: str


# --------------------------------------------------------------------------
# Lokale Netze ermitteln
# --------------------------------------------------------------------------

def _primary_ipv4() -> str | None:
    """IP der Schnittstelle mit Standardroute (es wird nichts gesendet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def local_ipv4_addresses() -> list[str]:
    """Alle privaten IPv4-Adressen dieses Rechners, Hauptadresse zuerst."""
    ips: list[str] = []
    primary = _primary_ipv4()
    if primary:
        ips.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    result = []
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        # Loopback und APIPA (169.254.x.x) ignorieren
        if addr.is_private and not addr.is_loopback and not addr.is_link_local:
            result.append(ip)
    return result


def candidate_networks(extra_ips: list[str] | None = None) -> list[ipaddress.IPv4Network]:
    """/24-Netze aller lokalen Adressen (plus optional bekannter Geräte-IPs)."""
    nets: list[ipaddress.IPv4Network] = []
    for ip in local_ipv4_addresses() + list(extra_ips or []):
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        if net not in nets:
            nets.append(net)
    return nets


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe(ip: str, timeout: float = 1.5) -> FoundDevice | None:
    """Prüft, ob unter `ip` ein Sonos-Gerät antwortet."""
    url = f"http://{ip}:{SONOS_PORT}{DESCRIPTION_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read(200_000)
    except Exception:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    dev = root.find(f"{_UPNP_NS}device")
    if dev is None:
        return None
    manufacturer = (dev.findtext(f"{_UPNP_NS}manufacturer") or "")
    if "sonos" not in manufacturer.lower():
        return None
    room = dev.findtext(f"{_UPNP_NS}roomName") or ip
    model = dev.findtext(f"{_UPNP_NS}modelName") or ""
    return FoundDevice(ip=ip, room=room, model=model)


def scan(networks: list[ipaddress.IPv4Network] | None = None,
         connect_timeout: float = 0.4,
         workers: int = 128) -> list[FoundDevice]:
    """Sucht alle Sonos-Geräte in den angegebenen (oder lokalen) /24-Netzen."""
    networks = networks or candidate_networks()
    own = set(local_ipv4_addresses())
    hosts = [str(h) for net in networks for h in net.hosts() if str(h) not in own]
    if not hosts:
        return []
    log.info("Netzwerk-Scan: %d Adressen in %s", len(hosts),
             ", ".join(map(str, networks)))

    def check(ip: str) -> FoundDevice | None:
        if not _port_open(ip, SONOS_PORT, connect_timeout):
            return None
        return probe(ip)

    with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as pool:
        found = [d for d in pool.map(check, hosts) if d is not None]
    found.sort(key=lambda d: (d.room.lower(), d.ip))
    log.info("Netzwerk-Scan: %d Sonos-Gerät(e) gefunden", len(found))
    return found


# --------------------------------------------------------------------------
# Integration mit soco
# --------------------------------------------------------------------------

def discover_speakers(ssdp_timeout: float = 3.0,
                      manual_ip: str | None = None) -> list:
    """Findet alle Sonos-Räume. Reihenfolge:

    1. manuell eingetragene IP (falls angegeben)
    2. normale Sonos-Suche per Multicast (soco.discover)
    3. direkter Netzwerk-Scan auf Port 1400

    Ein gefundenes Gerät reicht: über dessen Topologie liefert soco alle
    Räume des Systems. Rückgabe: Liste von soco.SoCo, nach Raumname sortiert.
    """
    import soco

    def expand(seed) -> list:
        try:
            zones = seed.all_zones or {seed}
        except Exception as exc:  # Gerät antwortet nicht vollständig
            log.warning("Topologie von %s nicht lesbar: %s", seed.ip_address, exc)
            zones = {seed}
        return sorted(zones, key=lambda z: z.player_name.lower())

    if manual_ip:
        try:
            dev = soco.SoCo(manual_ip.strip())
            _ = dev.player_name  # Erreichbarkeit prüfen
            return expand(dev)
        except Exception as exc:
            log.warning("Manuelle IP %s nicht erreichbar: %s", manual_ip, exc)

    try:
        zones = soco.discover(timeout=ssdp_timeout)
    except Exception as exc:
        log.warning("Multicast-Suche fehlgeschlagen: %s", exc)
        zones = None
    if zones:
        return sorted(zones, key=lambda z: z.player_name.lower())

    log.info("Multicast-Suche ohne Ergebnis, starte Netzwerk-Scan")
    extra = [manual_ip.strip()] if manual_ip else None
    for dev in scan(candidate_networks(extra)):
        try:
            return expand(soco.SoCo(dev.ip))
        except Exception as exc:
            log.warning("Gerät %s (%s) nicht nutzbar: %s", dev.ip, dev.room, exc)
    return []


if __name__ == "__main__":  # Schnelltest: python -m pc2sonos.netscan
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Lokale Adressen:", local_ipv4_addresses())
    for d in scan():
        print(f"  {d.ip:15}  {d.room:20}  {d.model}")
