"""Kommandozeile (Prototyp ohne GUI).

    python -m pc2sonos rooms
    python -m pc2sonos apps
    python -m pc2sonos play --rooms "Wohnzimmer,Kueche" --app chrome
    python -m pc2sonos play --rooms Wohnzimmer --system
"""
import argparse
import sys
import time

from . import capture, sonos
from .streamer import STREAM_PATH, Streamer, local_ip_for


def _hosts(a):
    return a.sonos.split(",") if a.sonos else None


def cmd_rooms(a):
    zones = sonos.discover(a.ip, hosts=_hosts(a))
    if not zones:
        print("Keine Sonos gefunden (gleiches Netz? Firewall?).")
        return 1
    for z in zones:
        coord = z.group.coordinator.player_name if z.group else "?"
        print(f"{z.player_name:25} {z.ip_address:16} Gruppe: {coord}")
    return 0


def cmd_apps(a):
    for name, pid in capture.list_audio_apps():
        print(f"{name:30} PID {pid}")
    return 0


def cmd_play(a):
    zones = sonos.discover(a.ip, hosts=_hosts(a))
    rooms = sonos.by_names(zones, a.rooms.split(","))
    pid = None
    if not a.system:
        pid = capture.find_app_pid(a.app)
        if pid is None:
            print(f"Programm '{a.app}' nicht gefunden.")
            return 1
    ip = a.ip or local_ip_for(rooms[0].ip_address)

    st = Streamer(port=a.port)
    cap = capture.Capture(st.feed, pid=pid)
    st.start()
    cap.start()
    uri = f"http://{ip}:{a.port}{STREAM_PATH}"
    src = "ganzer PC-Sound" if pid is None else f"{a.app} (PID {pid})"
    print(f"Quelle: {src}\nStream: {uri}")
    coord = sonos.play(rooms, uri)
    if a.volume is not None:
        sonos.set_volume(rooms, a.volume)
    print(f"Spielt auf: {', '.join(r.player_name for r in rooms)}  – Strg+C zum Beenden")
    try:
        while True:
            time.sleep(1)
            sys.stdout.write(f"\rPegel {'#' * int(st.level * 30):30}  Clients {st.client_count}  ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nStoppe ...")
    finally:
        sonos.stop(coord)
        cap.stop()
        st.stop()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="pc2sonos")
    p.add_argument("--ip", help="lokale IP/Netzwerkkarte erzwingen")
    p.add_argument("--sonos", help="Sonos-IP(s), kommagetrennt – fuer andere VLANs")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rooms", help="Sonos-Raeume anzeigen").set_defaults(fn=cmd_rooms)
    sub.add_parser("apps", help="Programme mit Audio anzeigen").set_defaults(fn=cmd_apps)
    pp = sub.add_parser("play", help="streamen")
    pp.add_argument("--rooms", required=True, help="kommagetrennt, erster = Koordinator")
    g = pp.add_mutually_exclusive_group(required=True)
    g.add_argument("--app", help="Programmname, z. B. chrome oder spotify")
    g.add_argument("--system", action="store_true", help="ganzer PC-Sound")
    pp.add_argument("--port", type=int, default=8090)
    pp.add_argument("--volume", type=int)
    pp.set_defaults(fn=cmd_play)
    a = p.parse_args(argv)
    return a.fn(a)
