# PC2Sonos

Spielt den Ton **eines Programms** (z. B. Browser, Spotify) oder den **ganzen PC-Sound** auf
**Sonos**-Lautsprechern und **AirPlay**-Geräten ab – auf Wunsch **synchron** mit den PC-Lautsprechern.
Kein VB-Cable, kein virtueller Treiber, keine Umstellung des Standard-Ausgabegeräts.

**Download:** fertige `PC2Sonos.exe` (Windows 10 2004+ / 11) unter [Releases](../../releases).

## Funktionen

- Aufnahme **pro Programm** (WASAPI Process Loopback) oder des gesamten PC-Sounds
- **Sonos**: mehrere Räume als synchrone Gruppe, Räume im laufenden Betrieb zu-/abschalten
- **AirPlay** (Apple TV, HomePod, AirPlay-2-Lautsprecher) über `pyatv`, inkl. PIN-Kopplung
- Findet Sonos auch in anderen VLANs (mDNS-Reflector, Netzwerk-Scan, feste IP; gefundene IPs werden gemerkt)
- **Am PC synchron mitspielen**: PC-Ton wird um die Sonos-Latenz verzögert
  - 🎤 **Kalibrieren** per Mikrofon (kurze Pieptöne)
  - **Automatisch nachführen** über ein unhörbares Rauschsignal (siehe unten)
- Tray-Betrieb, Autostart mit `PC2Sonos.exe --tray`

## Nutzung

`PC2Sonos.exe` starten → Quelle wählen → Lautsprecher anhaken (erster Sonos-Raum = Gruppenkoordinator) → **Start**.
Fenster schließen = läuft im Tray weiter.

- **Firewall**: Beim ersten Start „Private Netzwerke“ erlauben – die Sonos holen den Stream vom PC (TCP 8090).
  Liegen die Sonos in einem anderen VLAN, muss der Router Verbindungen von dort zu diesem Port zulassen.
- **Latenz**: Sonos puffert Streams mehrere Sekunden – nicht reduzierbar. Deshalb gibt es die Synchron-Funktion.
- **Am PC synchron mitspielen** (Windows, Programm-Modus): Das Programm wird während des Streamens auf ein
  ungenutztes Ausgabegerät (z. B. „Digitale Ausgabe“) umgeleitet; PC2Sonos spielt den Ton verzögert auf dem
  gewählten Gerät. Beim Stopp wird die Umleitung zurückgesetzt.
- **Automatisch nachführen**: In Sonos- und PC-Zweig wird je ein eigenes Rauschsignal (2–8 kHz, ~30 dB unter
  der Musik) gemischt. Das Mikrofon wird im WASAPI-Raw-Modus geöffnet (umgeht Windows-Rauschunterdrückung nur
  für PC2Sonos). Alle 10 s wird über 30 s in 3-s-Teilfenstern per GCC-PHAT korreliert; korrigiert wird erst,
  wenn zwei Messungen auf ±30 ms übereinstimmen. Kleine Korrekturen gleiten unhörbar.

### Kommandozeile (aus dem Quellcode)

```
python run.py rooms [--sonos 10.0.0.5]
python run.py apps
python run.py play --rooms "Wohnzimmer,Küche" --app chrome
python run.py play --rooms Wohnzimmer --system --volume 30
```

`tools\mic_fx.py status|on|off` zeigt/schaltet die Windows-Audioverbesserungen eines Mikrofons (für PC2Sonos nicht nötig).

## Technik

| Teil | Umsetzung |
|---|---|
| Aufnahme Windows | WASAPI Process Loopback. App: INCLUDE Prozessbaum; System: EXCLUDE eigener Prozess |
| Aufnahme Linux | PulseAudio/PipeWire: `parec` auf Monitor; App-Modus über eigenen Null-Sink (ungetestet) |
| Aufnahme macOS | noch nicht umgesetzt (geplant: Core Audio Process Tap, macOS 14.4+) |
| Stream | 44,1 kHz/16 bit → MP3 320 kbit/s (`lameenc`) → `http://<PC>:8090/stream.mp3`, Stille wird aufgefüllt |
| Sonos | `soco`: Gruppieren, `x-rincon-mp3radio://`, Lautstärke |
| Sonos-Suche | gemerkte IPs → mDNS (QM-Fragen, VLAN-Reflector-tauglich) → SSDP → Netzwerk-Scan Port 1400 |
| AirPlay | `pyatv` (RAOP); Zugangsdaten in `~/.pc2sonos/airplay.json`; mehrere Geräte parallel, nicht sample-genau synchron |
| Pro-App-Umleitung | interne Windows-Schnittstelle `IAudioPolicyConfigFactory` (wie der Lautstärkemixer) |
| Synchronisation | verzögerte Ausgabe (`sounddevice`), Kalibrierung per Piep-Differenzmessung, Auto-Sync per Spread-Spectrum-Wasserzeichen |
| GUI | PySide6 + Tray, Einstellungen per QSettings |

## Bauen

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name PC2Sonos ^
  --hidden-import pycaw.pycaw --hidden-import pc2sonos.netscan --hidden-import pc2sonos.routing_win ^
  --hidden-import pc2sonos.autosync --hidden-import pc2sonos.mic_raw ^
  --collect-submodules pyatv --collect-data pyatv --collect-submodules zeroconf ^
  --collect-binaries sounddevice --collect-data _sounddevice_data run.py
```

Hinweis: Projektpfad kurz halten (pip scheitert unter Windows an Pfaden > 260 Zeichen).

## Lizenz

[GPL-3.0](LICENSE). Kein Bezug zu Sonos Inc. oder Apple Inc.; „Sonos“ und „AirPlay“ sind Marken der jeweiligen Inhaber.
Nutzung auf eigene Verantwortung.
