from contextlib import contextmanager

from .util.pretty import metric, sci_parse


class FreqCapper:
    """Mixin for managing named frequency constraints.

    The effective frequency is the minimum of all active constraints.
    Subclasses override freq_update() to apply the frequency to hardware.

    Expects the target class to provide self.logger (e.g. Node).

    Place FreqCapper *before* Node in the MRO when mixing both, so the
    cooperative ``option_set`` here is reached before Node's terminal
    ValueError raise.
    """

    def __init__(self):
        self.__constraints = {}
        self.__freq = None

    def option_set(self, key, value):
        """Generic node option support: ``fmax=<sci>`` registers a
        ``"user"`` cap so command-line ``proto(fmax=1M)`` paths just
        work on any FreqCapper-mixing interface."""
        if key == "fmax":
            self.freq_cap("user", sci_parse(value))
            return
        super().option_set(key, value)

    @property
    def freq(self):
        return self.__freq

    def freq_cap(self, key, freq=None):
        """Add or remove a named frequency constraint.

        If freq is None, removes the constraint named key.
        Returns the new effective frequency.
        """
        if freq is None:
            self.__constraints.pop(key, None)
        else:
            self.__constraints[key] = freq
        self.__recalculate()
        return self.__freq

    @contextmanager
    def freq_capped(self, key, freq):
        """Temporarily add a frequency constraint for a with-block."""
        self.freq_cap(key, freq)
        try:
            yield self.__freq
        finally:
            self.freq_cap(key)

    def freq_cap_min(self, collection):
        """Cap frequency from each child's max_freq attribute."""
        for c in collection:
            max_freq = getattr(c, "max_freq", None)
            if max_freq is not None:
                self.freq_cap(c, max_freq)

    def __recalculate(self):
        caps = [(f, k) for k, f in self.__constraints.items() if f is not None]
        if caps:
            caps.sort(key=lambda x: x[0])
            freq, reason = caps[0]
        else:
            freq, reason = None, ""

        if freq == self.__freq:
            return

        self.logger.trace("Frequency cap: %s (constraint: %s)",
                          metric(freq, "Hz") if freq is not None else "none",
                          reason)
        freq = self.freq_update(freq)
        self.__freq = freq
        if freq is not None:
            self.logger.note("Frequency: %s", metric(freq, "Hz"))
        else:
            self.logger.note("Frequency: unconstrained")

    def freq_update(self, freq):
        """Hook for subclasses to apply frequency to hardware.

        Returns the actual achieved frequency.
        """
        return freq
