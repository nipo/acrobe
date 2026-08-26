"""Target discovery orchestration.

`TargetDiscovery.run(root)` walks the tree under `root`, attempts
every `@Target.register` entry against every non-Target Node it
finds, and parents the resulting Targets flat under `root`.

The walk is a fixed-point loop: a freshly spawned Target may expose
new component children (e.g. a puppet-driven QSPI master that
contains a SpiFlash beneath), and those become candidates for the
next pass. The loop terminates when a pass produces no new Target.

Dedup state survives across runs of the same `TargetDiscovery`
instance:

  * `claimed` — component nodes already consumed by a Target.
  * `attempted` — `(component, explorer.func)` pairs already
    tried (whether the explorer succeeded or declined).

Components in `claimed` are skipped in subsequent rounds. Pairs in
`attempted` are not retried — a `NoMatch` / `NotImplementedError`
from an explorer is final for that pairing.
"""

from ..db import NoMatch
from .target import Target


class TargetDiscovery:
    def __init__(self):
        self.claimed = set()
        self.attempted = set()
        self.spawned = []

    async def run(self, root):
        """Run discovery to fixed point under `root`.

        Returns the list of Targets spawned during *this* call
        (not the cumulative list across all calls on this instance).
        """
        spawned_now = []
        while True:
            new = await self.__pass(root)
            if not new:
                break
            spawned_now.extend(new)
            self.spawned.extend(new)
        return spawned_now

    async def __pass(self, root):
        """One walk over the tree. Returns the Targets spawned in
        this pass."""
        spawned = []
        candidates = root.children_find(
            lambda n: not isinstance(n, Target), include_self=False)
        for component in candidates:
            if component in self.claimed:
                continue
            for explorer in Target.explorers:
                if not any(isinstance(component, t)
                           for t in explorer.component_types):
                    continue
                pair = (component, explorer.func)
                if pair in self.attempted:
                    continue
                self.attempted.add(pair)
                target = await self.__try_spawn(explorer.func, component)
                if target is None:
                    continue
                target.claim(component)
                self.claimed.update(target.claimed_components())
                root.child_add(target)
                spawned.append(target)
                break
        return spawned

    @staticmethod
    async def __try_spawn(func, component):
        """Invoke an explorer, swallowing `NoMatch` /
        `NotImplementedError` as declination signals.

        Returns the Target instance, or None if the explorer declined.
        Other exceptions propagate.
        """
        try:
            result = func(component)
        except (NoMatch, NotImplementedError):
            return None
        if hasattr(result, "__await__"):
            try:
                result = await result
            except (NoMatch, NotImplementedError):
                return None
        return result
