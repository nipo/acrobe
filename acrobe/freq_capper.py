from contextlib import contextmanager


class FreqCapper:
    """Mixin for managing named frequency constraints.

    The effective frequency is the minimum of all active constraints.
    Subclasses override freq_update() to apply the frequency to hardware.

    Expects the target class to provide self.logger (e.g. Component).
    """

    def __init__(self):
        self.__constraints = {}
        self.__freq = None

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

        self.logger.trace("Frequency cap: %s (constraint: %s)", freq, reason)
        freq = self.freq_update(freq)
        self.__freq = freq
        if freq is not None:
            self.logger.note("Frequency: %g Hz", freq)
        else:
            self.logger.note("Frequency: unconstrained")

    def freq_update(self, freq):
        """Hook for subclasses to apply frequency to hardware.

        Returns the actual achieved frequency.
        """
        return freq
