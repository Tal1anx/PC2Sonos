"""Sonos-Steuerung ueber soco: Suche, Gruppieren, Abspielen, Lautstaerke."""
import json
import os
import threading

import soco


def discover(interface_addr=None, timeout=5, hosts=None):
    """Alle Sonos-Raeume finden.

    Reihenfolge: feste IP(s) aus `hosts` -> Multicast-Suche -> direkter Netzwerk-Scan
    auf Port 1400 (siehe netscan). Eine erreichbare IP reicht fuer alle Raeume.
    """
    from . import netscan

    zones = []
    manual = [x.strip() for x in hosts or [] if x.strip()]
    for h in manual:
        zones = netscan.discover_speakers(ssdp_timeout=min(timeout, 3), manual_ip=h)
        if zones:
            break
    if not zones:
        # Zuletzt bekannte Sonos direkt fragen (schnell, braucht kein Multicast)
        for ip in _load_known():
            if netscan.probe(ip, timeout=1.0):
                zones = netscan.discover_speakers(manual_ip=ip)
                if zones:
                    break
    if not zones:
        # mDNS: klappt auch, wenn der Router Sonos aus einem anderen VLAN per
        # mDNS-Reflector weiterreicht (so findet sie auch das iPhone).
        ip = mdns_find_sonos(timeout=15)
        if ip:
            zones = netscan.discover_speakers(manual_ip=ip)
    if not zones and not manual:
        zones = netscan.discover_speakers(ssdp_timeout=min(timeout, 3))
    if zones:
        _save_known([z.ip_address for z in zones])
    if not zones and interface_addr:
        # Multicast gezielt ueber eine bestimmte Netzwerkkarte versuchen
        zones = soco.discover(timeout=timeout, interface_addr=interface_addr) or []
    # nur sichtbare Raeume (keine Sub/Surround-Teile)
    zones = {z for z in zones if getattr(z, "is_visible", True)}
    return sorted(zones, key=lambda z: z.player_name.lower())


MDNS_TYPES = ["_sonos._tcp.local.", "_airplay._tcp.local.", "_raop._tcp.local.", "_spotify-connect._tcp.local."]
KNOWN_FILE = os.path.join(os.path.expanduser("~"), ".pc2sonos", "sonos_hosts.json")


def _load_known():
    try:
        with open(KNOWN_FILE, encoding="utf-8") as f:
            return list(json.load(f))
    except (OSError, ValueError):
        return []


def _save_known(ips):
    try:
        os.makedirs(os.path.dirname(KNOWN_FILE), exist_ok=True)
        with open(KNOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(set(ips)), f)
    except OSError:
        pass


def mdns_find_sonos(timeout=15):
    """Lauscht per mDNS auf AirPlay/Sonos-Dienste und prueft jede gemeldete IP auf
    Port 1400. Gibt die erste Sonos-IP zurueck, sobald eine antwortet (oder None)."""
    from concurrent.futures import ThreadPoolExecutor

    from zeroconf import DNSQuestionType, IPVersion, ServiceBrowser, Zeroconf

    from . import netscan

    # Nur Multicast-Fragen (QM): Unicast-Antworten (QU) reicht ein mDNS-Reflector
    # zwischen VLANs nicht weiter – dann kaemen nur lokale Geraete zurueck.
    QM = DNSQuestionType.QM
    seen, found = set(), threading.Event()
    result = []
    pool = ThreadPoolExecutor(8)

    def check(ip):
        if not found.is_set() and netscan.probe(ip, timeout=1.5):
            result.append(ip)
            found.set()

    class L:
        def add_service(self, zc, t, name):
            info = zc.get_service_info(t, name, timeout=3000, question_type=QM)
            for ip in (info.parsed_addresses(IPVersion.V4Only) if info else []):
                if ip not in seen:
                    seen.add(ip)
                    pool.submit(check, ip)

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        browsers = [ServiceBrowser(zc, t, L(), question_type=QM) for t in MDNS_TYPES]  # noqa: F841
        found.wait(timeout)
    finally:
        zc.close()
        pool.shutdown(wait=False, cancel_futures=True)
    return result[0] if result else None


def by_names(zones, names):
    wanted = [n.strip().lower() for n in names if n.strip()]
    found = {z.player_name.lower(): z for z in zones}
    missing = [n for n in wanted if n not in found]
    if missing:
        raise ValueError(f"Nicht gefunden: {', '.join(missing)}. Vorhanden: {', '.join(z.player_name for z in zones)}")
    return [found[n] for n in wanted]


def group(rooms):
    """Erster Raum wird Koordinator, die anderen werden dazugenommen."""
    coord = rooms[0]
    if coord.group and coord.group.coordinator != coord:
        coord.unjoin()
    for r in rooms[1:]:
        if r.group is None or r.group.coordinator != coord:
            r.join(coord)
    return coord


def play(rooms, uri, title="PC"):
    coord = group(rooms)
    # force_radio -> x-rincon-mp3radio:// (endloser Stream, kein Seek)
    coord.play_uri(uri, title=title, force_radio=True)
    return coord


def stop(coord):
    try:
        coord.stop()
    except Exception:  # noqa: BLE001
        pass


def set_volume(rooms, volume):
    for r in rooms:
        r.volume = int(volume)
