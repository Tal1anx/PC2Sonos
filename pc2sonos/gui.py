"""PC → Sonos – Fenster + Tray."""
import sys
import threading

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QSpinBox, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSlider,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

from . import __version__, capture, localout, sonos
from .airplay import AirPlay
from .calibrate import Splitter, calibrate as run_calibration
from .streamer import STREAM_PATH, Streamer, local_ip_for

PORT = 8090
ACCENT = "#2f6fed"


def make_icon(active=False):
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(ACCENT if active else "#444"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(30)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "♪")
    p.end()
    return QIcon(pm)


class Bridge(QObject):
    rooms_found = Signal(list, list, str)
    calibrated = Signal(int)
    autosync_update = Signal(int, str)  # neuer Versatz (-1 = unveraendert), Statustext
    cal_failed = Signal(str)
    progress = Signal(str)
    started = Signal(str)
    failed = Signal(str)
    volume_read = Signal(int)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PC → Sonos")
        self.setWindowIcon(make_icon())
        self.setMinimumWidth(380)
        self.settings = QSettings("Bideon", "pc2sonos")
        self.bridge = Bridge()
        self.zones = []
        self.ap_devices = []
        self.airplay = AirPlay()
        self.streamer = None
        self.cap = None
        self.coord = None
        self.active_rooms = []
        self._quitting = False
        self.local = None      # LocalOutput (verzoegerte PC-Wiedergabe)
        self.autosync = None   # AutoSync (fortlaufende Synchronisation)
        self.splitter = None
        self._routed = None    # (pids, vorheriges Geraet) zum Zuruecksetzen

        self._build_ui()
        self._upd_sync()
        self._build_tray()
        self.bridge.rooms_found.connect(self._on_rooms)
        self.bridge.started.connect(self._on_started)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.volume_read.connect(self._on_volume_read)
        self.bridge.calibrated.connect(self._on_calibrated)
        self.bridge.autosync_update.connect(self._on_autosync)
        self.bridge.cal_failed.connect(self._on_cal_failed)
        self.bridge.progress.connect(self._status)

        self._vol_timer = QTimer(self, singleShot=True, interval=150, timeout=self._apply_volume)
        self._level_timer = QTimer(self, interval=100, timeout=self._update_level)
        self._route_timer = QTimer(self, interval=3000, timeout=self._enforce_routing)
        self._route_target = None  # (exe, dummy_id) solange synchron gestreamt wird

        self.refresh_apps()
        self.find_rooms()

    # --- UI ------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        src = QGroupBox("Quelle")
        sl = QVBoxLayout(src)
        self.rb_app = QRadioButton("Ein Programm")
        self.rb_sys = QRadioButton("Ganzer PC-Sound")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_app)
        grp.addButton(self.rb_sys)
        row = QHBoxLayout()
        self.cb_app = QComboBox()
        self.cb_app.setEditable(True)
        btn_ref = QPushButton("↻")
        btn_ref.setFixedWidth(32)
        btn_ref.setToolTip("Programme mit Audio neu laden")
        btn_ref.clicked.connect(self.refresh_apps)
        row.addWidget(self.cb_app, 1)
        row.addWidget(btn_ref)
        sl.addWidget(self.rb_app)
        sl.addLayout(row)
        sl.addWidget(self.rb_sys)
        (self.rb_sys if self.settings.value("mode") == "system" else self.rb_app).setChecked(True)
        self.rb_app.toggled.connect(lambda on: self.cb_app.setEnabled(on))
        self.cb_app.setEnabled(self.rb_app.isChecked())
        lay.addWidget(src)

        snb = QGroupBox("Lautsprecher (Sonos + AirPlay, mehrere möglich)")
        nl = QVBoxLayout(snb)
        row = QHBoxLayout()
        self.ed_host = QLineEdit(self.settings.value("sonos_host", ""))
        self.ed_host.setPlaceholderText("Lautsprecher-IP (nur nötig bei anderem Netz)")
        self.btn_find = QPushButton("Suchen")
        self.btn_find.clicked.connect(self.find_rooms)
        row.addWidget(self.ed_host, 1)
        row.addWidget(self.btn_find)
        nl.addLayout(row)
        self.lst_rooms = QListWidget()
        self.lst_rooms.setMinimumHeight(120)
        self.lst_rooms.setMaximumHeight(240)
        self.lst_rooms.setSelectionMode(QListWidget.NoSelection)
        self.lst_rooms.itemChanged.connect(self._on_room_toggled)
        # Klick irgendwo auf die Zeile schaltet den Haken um (nicht nur auf das Kaestchen)
        self._press_state = None
        self.lst_rooms.itemPressed.connect(lambda it: setattr(self, "_press_state", (it, it.checkState())))
        self.lst_rooms.itemClicked.connect(self._on_room_clicked)
        nl.addWidget(self.lst_rooms)
        lay.addWidget(snb)

        # --- Synchron am PC mitspielen ---
        syn = QGroupBox("Am PC synchron mitspielen")
        yl = QVBoxLayout(syn)
        self.chk_sync = QCheckBox("PC-Ton verzögern, damit er zu den Lautsprechern passt")
        self.chk_sync.setToolTip("Das Programm wird während des Streamens auf ein ungenutztes Ausgabegerät "
                                 "umgeleitet; PC2Sonos spielt den Ton verzögert auf dem gewählten Gerät.")
        self.chk_sync.setChecked(self.settings.value("sync", "false") == "true")
        yl.addWidget(self.chk_sync)
        self.chk_auto = QCheckBox("Automatisch nachführen (Mikrofon, unhörbares Rauschsignal)")
        self.chk_auto.setToolTip("Mischt ein sehr leises Rauschsignal unter die Musik und misst per Mikrofon "
                                 "laufend den Versatz (alle 10 s über 30 s). Korrekturen sind unhörbar.")
        self.chk_auto.setChecked(self.settings.value("sync_auto", "true") == "true")
        yl.addWidget(self.chk_auto)
        row = QHBoxLayout()
        row.addWidget(QLabel("Ausgabe"))
        self.cb_out = QComboBox()
        try:
            devs, default = localout.output_devices()
        except Exception:  # noqa: BLE001
            devs, default = [], None
        saved_out = self.settings.value("sync_out", "")
        for idx, name in devs:
            self.cb_out.addItem(name, idx)
        i = self.cb_out.findText(saved_out) if saved_out else self.cb_out.findData(default)
        if i >= 0:
            self.cb_out.setCurrentIndex(i)
        row.addWidget(self.cb_out, 1)
        yl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Versatz"))
        self.sp_delay = QSpinBox()
        self.sp_delay.setRange(0, 15000)
        self.sp_delay.setSingleStep(50)
        self.sp_delay.setSuffix(" ms")
        self.sp_delay.setValue(int(self.settings.value("sync_delay", 4000)))
        self.sp_delay.valueChanged.connect(self._on_delay)
        self.btn_cal = QPushButton("🎤 Kalibrieren")
        self.btn_cal.setToolTip("Spielt kurze Pieptöne und misst per Mikrofon den Versatz (läuft ~15–40 s)")
        self.btn_cal.clicked.connect(self.calibrate)
        row.addWidget(self.sp_delay, 1)
        row.addWidget(self.btn_cal)
        yl.addLayout(row)
        lay.addWidget(syn)
        syn.setVisible(sys.platform == "win32")

        def upd_sync():
            on = self.rb_app.isChecked()
            self.chk_sync.setEnabled(on)
            self.cb_out.setEnabled(on and self.chk_sync.isChecked())
            self.chk_auto.setEnabled(on and self.chk_sync.isChecked() and not self.streamer)
            self.btn_cal.setEnabled(bool(self.local))
        self._upd_sync = upd_sync
        self.rb_app.toggled.connect(upd_sync)
        self.chk_sync.toggled.connect(upd_sync)

        row = QHBoxLayout()
        row.addWidget(QLabel("Lautstärke"))
        self.sl_vol = QSlider(Qt.Horizontal)
        self.sl_vol.setRange(0, 100)
        self.sl_vol.setValue(int(self.settings.value("volume", 25)))
        self.lb_vol = QLabel(str(self.sl_vol.value()))
        self.lb_vol.setFixedWidth(28)
        self.sl_vol.valueChanged.connect(self._on_volume)
        row.addWidget(self.sl_vol, 1)
        row.addWidget(self.lb_vol)
        lay.addLayout(row)

        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setFixedHeight(6)
        lay.addWidget(self.level)

        self.btn_go = QPushButton("Start")
        self.btn_go.setMinimumHeight(38)
        self.btn_go.setStyleSheet(f"QPushButton{{background:{ACCENT};color:white;border-radius:6px;font-weight:600}}"
                                  "QPushButton:disabled{background:#999}")
        self.btn_go.clicked.connect(self.toggle)
        lay.addWidget(self.btn_go)

        self.lb_status = QLabel("Bereit")
        self.lb_status.setWordWrap(True)
        self.lb_status.setStyleSheet("color:gray")
        lay.addWidget(self.lb_status)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("PC → Sonos")
        m = QMenu()
        self.act_toggle = QAction("Start", self, triggered=self.toggle)
        m.addAction(self.act_toggle)
        m.addAction(QAction("Fenster zeigen", self, triggered=self._show))
        m.addSeparator()
        m.addAction(QAction("Beenden", self, triggered=self.quit))
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self._show() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self._quitting or not QSystemTrayIcon.isSystemTrayAvailable():
            self.stop()
            e.accept()
            return
        e.ignore()
        self.hide()
        if self.streamer:
            self.tray.showMessage("PC → Sonos", "Läuft im Hintergrund weiter.", make_icon(True), 2000)

    def quit(self):
        self._quitting = True
        self.stop()
        self._save()
        QApplication.quit()

    def _status(self, text):
        self.lb_status.setText(text)
        self.tray.setToolTip(f"PC → Sonos – {text}")

    def _save(self):
        s = self.settings
        s.setValue("mode", "system" if self.rb_sys.isChecked() else "app")
        s.setValue("app", self.cb_app.currentText())
        s.setValue("sonos_host", self.ed_host.text().strip())
        s.setValue("volume", self.sl_vol.value())
        s.setValue("rooms", self._checked_rooms_names())
        s.setValue("sync", "true" if self.chk_sync.isChecked() else "false")
        s.setValue("sync_out", self.cb_out.currentText())
        s.setValue("sync_delay", self.sp_delay.value())
        s.setValue("sync_auto", "true" if self.chk_auto.isChecked() else "false")

    # --- Programme / Räume -----------------------------------------------------
    def refresh_apps(self):
        current = self.cb_app.currentText() or self.settings.value("app", "")
        self.cb_app.clear()
        try:
            names = [n for n, _ in capture.list_audio_apps() if not n.lower().startswith("pc2sonos")]
        except Exception as e:  # noqa: BLE001
            names = []
            self._status(f"Programmliste nicht verfügbar: {e}")
        self.cb_app.addItems(names)
        if current:
            i = self.cb_app.findText(current, Qt.MatchFixedString)
            if i >= 0:
                self.cb_app.setCurrentIndex(i)
            else:
                self.cb_app.setEditText(current)

    def find_rooms(self):
        self.btn_find.setEnabled(False)
        self._status("Suche Sonos … (ggf. Netzwerk-Scan, einige Sekunden)")
        host = self.ed_host.text().strip()

        hosts = [h.strip() for h in host.split(",") if h.strip()] or None

        def work():
            errs, zones, aps = [], [], []
            try:
                zones = sonos.discover(hosts=hosts)
            except Exception as e:  # noqa: BLE001
                errs.append(f"Sonos: {e}")
            try:
                aps = self.airplay.discover(hosts=hosts, exclude_ips={z.ip_address for z in zones})
            except Exception as e:  # noqa: BLE001
                errs.append(f"AirPlay: {e}")
            self.bridge.rooms_found.emit(zones, aps, "; ".join(errs))

        threading.Thread(target=work, daemon=True).start()

    def _on_rooms(self, zones, aps, err):
        self.btn_find.setEnabled(True)
        self.zones, self.ap_devices = zones, aps
        saved = self.settings.value("rooms", []) or []
        if isinstance(saved, str):
            saved = [saved]
        saved = [s if ":" in s else f"sonos:{s}" for s in saved]
        if self.streamer:  # waehrend des Streamens: aktuelle Auswahl behalten
            saved = self._checked_rooms_names()
        self.lst_rooms.blockSignals(True)
        self.lst_rooms.clear()
        entries = [(f"sonos:{z.player_name}", f"{z.player_name}   · Sonos") for z in zones]
        for d in aps:
            lock = " 🔒" if d.needs_pairing and not d.paired else ""
            entries.append((f"airplay:{d.identifier}", f"{d.name}   · AirPlay ({d.model}){lock}"))
        for key, text in entries:
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, key)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if key in saved else Qt.Unchecked)
            self.lst_rooms.addItem(it)
        self.lst_rooms.blockSignals(False)
        # Hoehe an Anzahl anpassen, damit alle Raeume ohne Scrollen sichtbar sind
        row_h = self.lst_rooms.sizeHintForRow(0) if entries else 24
        self.lst_rooms.setFixedHeight(min(240, max(120, row_h * len(entries) + 8)))
        if not entries:
            self._status("Keine Lautsprecher gefunden. Anderes Netz/VLAN? Dann oben eine IP eintragen."
                         + (f" ({err})" if err else ""))
        else:
            self._status(f"{len(zones)} Sonos, {len(aps)} AirPlay gefunden" + (f" – {err}" if err else ""))

    def _checked_rooms_names(self):
        return [self.lst_rooms.item(i).data(Qt.UserRole) for i in range(self.lst_rooms.count())
                if self.lst_rooms.item(i).checkState() == Qt.Checked]

    def _pair_if_needed(self, devs):
        """Ungekoppelte Apple TV/HomePods koppeln. False = abgebrochen/fehlgeschlagen."""
        for d in devs:
            if not d.needs_pairing or d.paired:
                continue
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                shows_pin = self.airplay.pair_begin(d)
            except Exception as e:  # noqa: BLE001
                QApplication.restoreOverrideCursor()
                self._status(f"Kopplung mit {d.name} nicht möglich: {e}")
                return False
            QApplication.restoreOverrideCursor()
            pin = None
            if shows_pin:
                pin, ok = QInputDialog.getText(self, "AirPlay koppeln",
                                               f"PIN, die „{d.name}“ gerade anzeigt:")
                if not ok or not pin.strip():
                    try:
                        self.airplay.pair_finish(d, None)
                    except Exception:  # noqa: BLE001
                        pass
                    self._status("Kopplung abgebrochen")
                    return False
            try:
                self.airplay.pair_finish(d, pin.strip() if pin else None)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "AirPlay koppeln", f"{d.name}: {e}")
                return False
            self._status(f"{d.name} gekoppelt")
        return True

    # --- Start / Stop ----------------------------------------------------------
    def toggle(self):
        if self.streamer:
            self.stop()
        else:
            self.start()

    def start(self):
        keys = self._checked_rooms_names()
        if not keys:
            self._status("Bitte mindestens einen Lautsprecher anhaken.")
            self._show()
            return
        # Reihenfolge der Liste: erster angehakter Sonos-Raum = Koordinator
        rooms = [z for z in self.zones if f"sonos:{z.player_name}" in keys]
        rooms.sort(key=lambda z: keys.index(f"sonos:{z.player_name}"))
        aps = [d for d in self.ap_devices if f"airplay:{d.identifier}" in keys]
        if aps and not self._pair_if_needed(aps):
            return
        pid = None
        if self.rb_app.isChecked():
            app = self.cb_app.currentText().strip()
            pid = capture.find_app_pid(app) if app else None
            if pid is None:
                self._status(f"Programm „{app}“ nicht gefunden – läuft es?")
                return
            label = app
        else:
            label = "ganzer PC-Sound"
        sync = (sys.platform == "win32" and self.rb_app.isChecked() and self.chk_sync.isChecked()
                and self.cb_out.currentData() is not None)
        exe = self.cb_app.currentText().strip()
        if exe and not exe.lower().endswith(".exe"):
            exe += ".exe"
        out_idx, out_name, delay = self.cb_out.currentData(), self.cb_out.currentText(), self.sp_delay.value()
        auto = sync and self.chk_auto.isChecked()
        self._save()
        self.btn_go.setEnabled(False)
        self._status("Starte …")
        vol = self.sl_vol.value()

        def work():
            st = cap = local = None
            try:
                st = Streamer(port=PORT)
                st.start()
                if sync:
                    from . import routing_win
                    dummy_id, dummy_name = routing_win.pick_dummy_output(exclude_name=out_name)
                    if not dummy_id:
                        raise RuntimeError("Kein ungenutztes Ausgabegerät zum Umleiten gefunden")
                    pids = routing_win.app_pids(exe)
                    prev = routing_win.get_app_output_any(pids)
                    routing_win.set_app_output(pids, dummy_id)
                    self._routed = (pids, None if prev == dummy_id else prev)
                    self._route_target = (exe, dummy_id)
                    local = localout.LocalOutput(device=out_idx, delay_ms=delay)
                    local.start()
                splitter = Splitter(st.feed, local.feed if local else None)
                if auto:
                    splitter.enable_watermark(True)
                cap = capture.Capture(splitter.feed, pid=pid)
                cap.start()
                self.local, self.splitter = local, splitter
                if auto:
                    from .autosync import AutoSync
                    self.autosync = AutoSync(
                        splitter, local,
                        on_update=lambda new, msg: self.bridge.autosync_update.emit(-1 if new is None else new, msg))
                    self.autosync.start()
                coord, targets, warn = None, [], []
                if rooms:
                    ip = local_ip_for(rooms[0].ip_address)
                    coord = sonos.play(rooms, f"http://{ip}:{PORT}{STREAM_PATH}", title=f"PC – {label}")
                    sonos.set_volume(rooms, vol)
                    targets += [r.player_name for r in rooms]
                if aps:
                    errs = self.airplay.start(aps, f"http://127.0.0.1:{PORT}{STREAM_PATH}", vol,
                                              title=f"PC – {label}")
                    warn += errs
                    failed = {e.split(":")[0] for e in errs}
                    targets += [d.name for d in aps if d.name not in failed]
                if not targets:
                    raise RuntimeError("; ".join(warn) or "kein Lautsprecher erreichbar")
                self.streamer, self.cap, self.coord, self.active_rooms = st, cap, coord, list(rooms)
                self._title = f"PC – {label}"
                msg = f"Spielt: {label} → {', '.join(targets)}"
                if warn:
                    msg += "\nNicht erreichbar: " + "; ".join(warn)
                self.bridge.started.emit(msg)
            except Exception as e:  # noqa: BLE001
                try:
                    self.airplay.stop()
                except Exception:  # noqa: BLE001
                    pass
                if getattr(self, "autosync", None):
                    self.autosync.stop()
                    self.autosync = None
                if cap:
                    cap.stop()
                if local:
                    local.stop()
                self.local = self.splitter = None
                self._restore_routing()
                if st:
                    st.stop()
                self.bridge.failed.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_started(self, text):
        self.btn_go.setEnabled(True)
        self.btn_go.setText("Stop")
        self.act_toggle.setText("Stop")
        self.tray.setIcon(make_icon(True))
        self._level_timer.start()
        if self._route_target:
            self._route_timer.start()
        self._upd_sync()
        if self.local:
            if getattr(self, "autosync", None):
                text += f"\nPC synchron ({self.sp_delay.value()} ms) – Auto-Sync misst, erste Korrektur nach ~1 min."
            else:
                text += f"\nPC spielt synchron mit {self.sp_delay.value()} ms Versatz – bei Bedarf „Kalibrieren“."
        self._status(text)

    def _on_autosync(self, new, msg):
        if new >= 0:
            # Spinbox nachziehen, ohne einen Sprung auszuloesen (Auto-Sync gleitet selbst)
            self.sp_delay.blockSignals(True)
            self.sp_delay.setValue(new)
            self.sp_delay.blockSignals(False)
            self.settings.setValue("sync_delay", new)
        base = self.lb_status.text().split("\nAuto-Sync")[0]
        self._status(f"{base}\n{msg}")

    def _on_failed(self, err):
        self.btn_go.setEnabled(True)
        if "10048" in err or "Address already in use" in err:
            err = f"Port {PORT} ist belegt (läuft das Tool schon?)"
        self._status(f"Fehler: {err}")

    def stop(self):
        coord, cap, st = self.coord, self.cap, self.streamer
        self.coord = self.cap = self.streamer = None
        self.active_rooms = []
        if coord:
            threading.Thread(target=sonos.stop, args=(coord,), daemon=True).start()
        if self.airplay.active:
            threading.Thread(target=self.airplay.stop, daemon=True).start()
        if getattr(self, "autosync", None):
            self.autosync.stop()
            self.autosync = None
        if cap:
            cap.stop()
        if self.local:
            self.local.stop()
        self.local = self.splitter = None
        self._restore_routing()
        if st:
            st.stop()
        self._upd_sync()
        self._level_timer.stop()
        self.level.setValue(0)
        self.btn_go.setText("Start")
        self.act_toggle.setText("Start")
        self.tray.setIcon(make_icon())
        self._status("Gestoppt")

    # --- Räume im laufenden Betrieb ------------------------------------------------
    def _set_checked(self, item, on):
        self.lst_rooms.blockSignals(True)
        item.setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.lst_rooms.blockSignals(False)

    def _on_room_clicked(self, item):
        pressed = self._press_state
        self._press_state = None
        # Hat Qt den Haken schon selbst umgeschaltet (Klick aufs Kaestchen)? Dann nichts tun.
        if pressed and pressed[0] is item and pressed[1] != item.checkState():
            return
        item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def _on_room_toggled(self, item):
        if not self.streamer:
            return  # wird beim Start ausgewertet
        key = item.data(Qt.UserRole) or ""
        on = item.checkState() == Qt.Checked
        kind, _, ident = key.partition(":")
        vol = self.sl_vol.value()
        title = getattr(self, "_title", "PC")
        if not hasattr(self, "_toggle_lock"):
            self._toggle_lock = threading.Lock()

        if kind == "airplay":
            dev = next((d for d in self.ap_devices if d.identifier == ident), None)
            if not dev:
                return
            if on and not self._pair_if_needed([dev]):
                self._set_checked(item, False)
                return

            def work():
                with self._toggle_lock:
                    try:
                        if on:
                            errs = self.airplay.start([dev], f"http://127.0.0.1:{PORT}{STREAM_PATH}", vol, title=title)
                            msg = f"{dev.name}: {errs[0]}" if errs else f"{dev.name} spielt mit"
                        else:
                            self.airplay.stop([dev.identifier])
                            msg = f"{dev.name} entfernt"
                    except Exception as e:  # noqa: BLE001
                        msg = f"{dev.name}: {e}"
                    self.bridge.progress.emit(msg)

        elif kind == "sonos":
            zone = next((z for z in self.zones if z.player_name == ident), None)
            if not zone:
                return

            def work():
                with self._toggle_lock:
                    try:
                        rooms = [r for r in self.active_rooms if r != zone]
                        uri = f"http://{local_ip_for(zone.ip_address)}:{PORT}{STREAM_PATH}"
                        if on:
                            if self.coord:
                                zone.join(self.coord)  # spielt synchron in der Gruppe mit
                                zone.volume = vol
                            else:
                                self.coord = sonos.play([zone], uri, title=title)
                                zone.volume = vol
                            self.active_rooms = rooms + [zone]
                            msg = f"{zone.player_name} spielt mit"
                        else:
                            if zone == self.coord:
                                # Koordinator raus: Gruppe mit den uebrigen neu aufbauen
                                sonos.stop(zone)
                                zone.unjoin()
                                self.coord = sonos.play(rooms, uri, title=title) if rooms else None
                            else:
                                zone.unjoin()
                                zone.stop()
                            self.active_rooms = rooms
                            msg = f"{zone.player_name} entfernt"
                    except Exception as e:  # noqa: BLE001
                        msg = f"{zone.player_name}: {e}"
                    self.bridge.progress.emit(msg)
        else:
            return
        self.bridge.progress.emit("Ändere Lautsprecher …")
        threading.Thread(target=work, daemon=True).start()

    def _enforce_routing(self):
        """Haelt die Umleitung aufrecht: neue Audio-Prozesse des Programms oder eine
        Aenderung im Lautstaerkemixer wuerden sonst wieder direkt am PC spielen."""
        target = self._route_target
        if not target:
            return

        def work():
            from . import routing_win
            exe, dummy = target
            pids = routing_win.app_pids(exe)
            wrong = []
            for p in pids:
                try:
                    if routing_win.get_app_output(p) != dummy:
                        wrong.append(p)
                except OSError:
                    continue
            if wrong:
                routing_win.set_app_output(pids, dummy)
                if self._routed:
                    self._routed = (sorted(set(self._routed[0]) | set(pids)), self._routed[1])

        threading.Thread(target=work, daemon=True).start()

    def _restore_routing(self):
        self._route_target = None
        if hasattr(self, "_route_timer"):
            self._route_timer.stop()
        if not self._routed:
            return
        pids, prev = self._routed
        self._routed = None
        try:
            from . import routing_win
            routing_win.set_app_output(pids, prev)
        except Exception as e:  # noqa: BLE001
            self._status(f"Umleitung konnte nicht zurückgesetzt werden: {e}")

    # --- Synchronisation ---------------------------------------------------------
    def _on_delay(self, v):
        if self.local:
            self.local.set_delay(v)
        self.settings.setValue("sync_delay", v)

    def calibrate(self):
        if not (self.local and self.splitter):
            self._status("Kalibrieren geht nur, während mit „Am PC synchron mitspielen“ gestreamt wird.")
            return
        self.btn_cal.setEnabled(False)
        splitter, cur = self.splitter, self.local.delay_ms

        def work():
            try:
                new = run_calibration(splitter, cur, progress=self.bridge.progress.emit)
                self.bridge.calibrated.emit(new)
            except Exception as e:  # noqa: BLE001
                self.bridge.cal_failed.emit(str(e))

        self._status("Kalibriere – kurze Pieptöne, bitte ruhig halten …")
        threading.Thread(target=work, daemon=True).start()

    def _on_calibrated(self, ms):
        self.sp_delay.setValue(ms)
        self._upd_sync()
        self._status(f"Kalibriert: Versatz {ms} ms")

    def _on_cal_failed(self, err):
        self._upd_sync()
        self._status(f"Kalibrieren fehlgeschlagen: {err}")

    # --- Lautstärke / Pegel ------------------------------------------------------
    def _on_volume(self, v):
        self.lb_vol.setText(str(v))
        if self.streamer:
            self._vol_timer.start()

    def _apply_volume(self):
        rooms, v = list(self.active_rooms), self.sl_vol.value()

        def work():
            if rooms:
                sonos.set_volume(rooms, v)
            if self.airplay.active:
                self.airplay.set_volume(v)

        threading.Thread(target=work, daemon=True).start()

    def _on_volume_read(self, v):
        self.sl_vol.blockSignals(True)
        self.sl_vol.setValue(v)
        self.lb_vol.setText(str(v))
        self.sl_vol.blockSignals(False)

    def _update_level(self):
        if self.streamer:
            self.level.setValue(int(min(1.0, self.streamer.level) * 100))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PC → Sonos")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    w = MainWindow()
    if "--tray" not in sys.argv:
        w.show()
    return app.exec()
