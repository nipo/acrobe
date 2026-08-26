"""FreqCapper constraint bookkeeping, including re-application for
interfaces whose clock hardware only comes up in start()."""

from acrobe.freq_capper import FreqCapper


class _Logger:
    def trace(self, *args):
        pass

    def note(self, *args):
        pass


class LateClock(FreqCapper):
    """Clock that only exists once ``live`` is set — the shape of a
    transactor codec built after base_freq is read in start()."""

    def __init__(self):
        FreqCapper.__init__(self)
        self.logger = _Logger()
        self.applied = []
        self.live = False

    def freq_update(self, freq):
        if not self.live:
            return 0.0
        self.applied.append(freq)
        return freq


class TestConstraints:
    def test_min_constraint_wins(self):
        clock = LateClock()
        clock.live = True
        clock.freq_cap("a", 2e6)
        clock.freq_cap("b", 1e6)
        assert clock.freq == 1e6
        clock.freq_cap("b")
        assert clock.freq == 2e6

    def test_fmax_option_parses_sci(self):
        clock = LateClock()
        clock.live = True
        clock.option_set("fmax", "1M")
        assert clock.freq == 1e6

    def test_other_options_ignored(self):
        clock = LateClock()
        clock.option_set("pinout", "inverted")
        assert clock.freq is None


class TestReapply:
    def test_pre_live_cap_lands_on_reapply(self):
        clock = LateClock()
        clock.option_set("fmax", "1M")
        assert clock.freq == 0.0
        assert clock.applied == []

        clock.live = True
        assert clock.freq_reapply() == 1e6
        assert clock.applied == [1e6]
        assert clock.freq == 1e6

    def test_reapply_without_constraint_passes_none(self):
        clock = LateClock()
        clock.live = True
        clock.freq_reapply()
        assert clock.applied == [None]

    def test_cap_change_after_reapply_still_tracks(self):
        clock = LateClock()
        clock.option_set("fmax", "2M")
        clock.live = True
        clock.freq_reapply()
        clock.freq_cap("user", 1e6)
        assert clock.applied == [2e6, 1e6]
