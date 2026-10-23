import pytest

from acrobe.bitstring import BitString
from acrobe.protocol import jtag
from acrobe.protocol.jtag_encoder import JtagOpEncoder


class TapFsm:
    """IEEE 1149.1 TAP state machine, counting TCKs spent in each
    state."""

    NEXT = {
        "TLR": ("RTI", "TLR"),
        "RTI": ("RTI", "SELDR"),
        "SELDR": ("CAPDR", "SELIR"),
        "CAPDR": ("SHDR", "EX1DR"),
        "SHDR": ("SHDR", "EX1DR"),
        "EX1DR": ("PAUSEDR", "UPDR"),
        "PAUSEDR": ("PAUSEDR", "EX2DR"),
        "EX2DR": ("SHDR", "UPDR"),
        "UPDR": ("RTI", "SELDR"),
        "SELIR": ("CAPIR", "TLR"),
        "CAPIR": ("SHIR", "EX1IR"),
        "SHIR": ("SHIR", "EX1IR"),
        "EX1IR": ("PAUSEIR", "UPIR"),
        "PAUSEIR": ("PAUSEIR", "EX2IR"),
        "EX2IR": ("SHIR", "UPIR"),
        "UPIR": ("RTI", "SELDR"),
    }

    def __init__(self):
        self.state = "TLR"
        self.visits = []

    def clock(self, tms: BitString):
        for i in range(len(tms)):
            self.state = self.NEXT[self.state][int(tms[i])]
            self.visits.append(self.state)

    def count(self, state):
        return self.visits.count(state)


def run(ops):
    tms, tdi, _ = JtagOpEncoder().encode(ops)
    assert len(tms) == len(tdi)
    fsm = TapFsm()
    fsm.clock(tms)
    return fsm


def dr(width, post_run=0):
    return [jtag.CaptureDr(), jtag.Shift(BitString(0, width), post_run=post_run)]


def test_back_to_back_dr_shifts_skip_idle():
    fsm = run([jtag.Reset()] + dr(8) + dr(8) + dr(8))
    assert fsm.count("RTI") == 1
    assert fsm.count("UPDR") == 2
    assert fsm.state == "PAUSEDR"


def test_post_run_idles_in_rti():
    fsm = run([jtag.Reset()] + dr(8, post_run=3) + dr(8))
    assert fsm.count("RTI") == 1 + 3
    assert fsm.count("UPDR") == 1


def test_post_run_on_last_shift():
    fsm = run([jtag.Reset()] + dr(8, post_run=2))
    assert fsm.state == "RTI"
    assert fsm.visits[-2:] == ["RTI", "RTI"]


def test_shift_bits_reach_shift_state():
    fsm = run([jtag.Reset()] + dr(5) + [jtag.CaptureIr(),
                                        jtag.Shift(BitString(0, 4))] + dr(3))
    assert fsm.count("SHDR") == 5 + 3
    assert fsm.count("SHIR") == 4


def test_post_run_inside_continued_shift_rejected():
    with pytest.raises(ValueError):
        JtagOpEncoder().encode([
            jtag.CaptureDr(),
            jtag.Shift(BitString(0, 4), post_run=1),
            jtag.Shift(BitString(0, 4))])


def test_run_counts_rti_tcks():
    fsm = run([jtag.Reset()] + dr(4) + [jtag.Run(5)])
    assert fsm.count("RTI") == 1 + 5
    assert fsm.state == "RTI"
    fsm = run([jtag.Reset(), jtag.Run(2)])
    assert fsm.count("RTI") == 2
