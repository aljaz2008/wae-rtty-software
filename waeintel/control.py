"""Station control: QSY the radio through N1MM, and (optionally) turn the rotator."""
from __future__ import annotations

import logging
import socket
import time

log = logging.getLogger("control")


class Control:
    def __init__(self, cfg, state):
        c = cfg.get("control", {})
        self.state = state
        self.qsy_on_click = c.get("qsy_on_click", True)
        self.n1mm_addr = (c.get("n1mm_host", "127.0.0.1"), int(c.get("n1mm_port", 13064)))
        self.offset_khz = float(c.get("qsy_offset_khz", 0.0))
        self.radio_nr = c.get("radio_nr", "active")
        self.block_tx = c.get("block_while_transmitting", True)
        self.rotator = c.get("rotator", "none").lower()
        self.rot_addr = (c.get("rotator_host", "127.0.0.1"),
                         int(c.get("rotator_port", 12000 if self.rotator == "pstrotator" else 12040)))
        self.rot_name = c.get("rotator_name", "")
        self.rotate_on_click = c.get("rotate_on_click", False)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.last = ""

    def info(self):
        return {"qsy_on_click": self.qsy_on_click, "rotator": self.rotator,
                "rotate_on_click": self.rotate_on_click and self.rotator != "none", "last": self.last}

    def _radio(self):
        if self.radio_nr != "active":
            return int(self.radio_nr)
        return int(self.state.radio.get("nr") or 1)

    def qsy(self, khz: float):
        if self.block_tx and self.state.radio.get("tx"):
            self.last = "QSY blocked: you are transmitting"
            return False, self.last
        f = round(float(khz) + self.offset_khz, 3)
        if not (1800 <= f <= 30000):
            self.last = f"QSY refused: {f} kHz out of range"
            return False, self.last
        xml = ('<?xml version="1.0" encoding="utf-8"?><radio_setfrequency><app>WAE-Intel</app>'
               f'<radionr>{self._radio()}</radionr><frequency>{f:.3f}</frequency>'
               '<mousebutton>Left</mousebutton></radio_setfrequency>')
        self.sock.sendto(xml.encode(), self.n1mm_addr)
        self.last = f"QSY → {f:.1f} kHz (radio {self._radio()}) @ {time.strftime('%H:%M:%S', time.gmtime())}Z"
        log.info(self.last)
        return True, self.last

    def rotate(self, azimuth: float, band=None):
        if self.rotator == "none":
            return False, "no rotator configured"
        if self.block_tx and self.state.radio.get("tx"):
            self.last = "Rotor blocked: you are transmitting"
            return False, self.last
        az = int(round(azimuth)) % 360
        if self.rotator == "pstrotator":
            xml = f"<PST><AZIMUTH>{az}</AZIMUTH></PST>"
        elif self.rotator == "n1mmrotor":
            xml = (f"<N1MMRotor><rotor>{self.rot_name}</rotor><goazi>{az}.0</goazi><offset>0.0</offset>"
                   f"<bidirectional>0</bidirectional><freqband>{band or ''}</freqband></N1MMRotor>")
        else:
            return False, f"unknown rotator type {self.rotator}"
        self.sock.sendto(xml.encode(), self.rot_addr)
        msg = f"Rotor → {az}°"
        self.last = msg
        log.info(msg)
        return True, msg
