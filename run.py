"""Einstieg fuer PyInstaller und direktes Starten: GUI ohne Argumente, sonst CLI."""
import sys

from pc2sonos.cli import main as cli_main
from pc2sonos.gui import main as gui_main

if __name__ == "__main__":
    if any(a in {"rooms", "apps", "play", "-h", "--help"} for a in sys.argv[1:]):
        sys.exit(cli_main())
    sys.exit(gui_main())
