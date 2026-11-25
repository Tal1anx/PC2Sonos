import sys

CLI_COMMANDS = {"rooms", "apps", "play", "-h", "--help"}

if any(a in CLI_COMMANDS for a in sys.argv[1:]):
    from pc2sonos.cli import main
else:
    from pc2sonos.gui import main

sys.exit(main())
