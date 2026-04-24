import pytest
from acrobe.component import Component
from acrobe.component.fpga import SramFpga, JtagSramFpga
from acrobe.db import Db, NoMatch
from acrobe.protocol.jtag import JtagInterface, Chain


# --- Db.acall tests ---

class TestAcall:
    @pytest.mark.asyncio
    async def test_sync_handler(self):
        db = Db("test")

        @db.register("x")
        def handler(a, b):
            return a + b

        assert await db.acall("x", 1, 2) == 3

    @pytest.mark.asyncio
    async def test_async_handler(self):
        db = Db("test")

        @db.register("x")
        async def handler(val):
            return val * 2

        assert await db.acall("x", 5) == 10

    @pytest.mark.asyncio
    async def test_skip_on_nomatch(self):
        db = Db("test")

        @db.register("x")
        def h1():
            raise NoMatch("inner", "a")

        @db.register("x")
        def h2():
            return "fallback"

        assert await db.acall("x") == "fallback"

    @pytest.mark.asyncio
    async def test_async_skip_on_nomatch(self):
        db = Db("test")

        @db.register("x")
        async def h1():
            raise NoMatch("inner", "a")

        @db.register("x")
        async def h2():
            return "async fallback"

        assert await db.acall("x") == "async fallback"

    @pytest.mark.asyncio
    async def test_all_fail(self):
        db = Db("test")

        @db.register("x")
        def h1():
            raise NoMatch("inner", "a")

        with pytest.raises(NoMatch):
            await db.acall("x")

    @pytest.mark.asyncio
    async def test_nomatch_id(self):
        db = Db("test")
        with pytest.raises(NoMatch):
            await db.acall("missing")


# --- MRO child_spawn tests ---

class TestChildSpawnMro:
    @pytest.mark.asyncio
    async def test_base_raises_nomatch(self):
        c = Component("root")
        with pytest.raises(NoMatch):
            await c._child_spawn_mro("anything")

    @pytest.mark.asyncio
    async def test_single_override(self):
        class Parent(Component):
            async def child_spawn(self, name):
                if name == "x":
                    return Component("x")
                raise NoMatch("child", name)

        p = Parent("p")
        child = await p._child_spawn_mro("x")
        assert child.name == "x"

    @pytest.mark.asyncio
    async def test_mro_fallback(self):
        """Second class in MRO handles name that first doesn't."""
        class Base(Component):
            async def child_spawn(self, name):
                if name == "base":
                    return Component("from-base")
                raise NoMatch("child", name)

        class Sub(Base):
            async def child_spawn(self, name):
                if name == "sub":
                    return Component("from-sub")
                raise NoMatch("child", name)

        s = Sub("s")
        # Sub handles "sub"
        child = await s._child_spawn_mro("sub")
        assert child.name == "from-sub"
        # Base handles "base" via MRO fallback
        child = await s._child_spawn_mro("base")
        assert child.name == "from-base"

    @pytest.mark.asyncio
    async def test_mro_all_miss(self):
        class Base(Component):
            async def child_spawn(self, name):
                raise NoMatch("child", name)

        class Sub(Base):
            async def child_spawn(self, name):
                raise NoMatch("child", name)

        s = Sub("s")
        with pytest.raises(NoMatch):
            await s._child_spawn_mro("nope")

    @pytest.mark.asyncio
    async def test_child_summon_uses_mro(self):
        """child_summon dispatches through MRO walking."""
        class Base(Component):
            async def child_spawn(self, name):
                if name == "x":
                    return Component("x")
                raise NoMatch("child", name)

        b = Base("b")
        child = await b.child_summon("x")
        assert child.name == "x"
        assert child in b.children

    @pytest.mark.asyncio
    async def test_class_without_child_spawn_skipped(self):
        """A mixin without child_spawn doesn't break MRO walking."""
        class Mixin:
            pass

        class Base(Component):
            async def child_spawn(self, name):
                if name == "x":
                    return Component("x")
                raise NoMatch("child", name)

        class Sub(Mixin, Base):
            pass

        s = Sub("s")
        child = await s._child_spawn_mro("x")
        assert child.name == "x"


# --- SramFpga per-class application_db tests ---

class TestSramFpgaApplicationDb:
    def test_each_subclass_gets_own_db(self):
        class FpgaA(SramFpga):
            pass

        class FpgaB(SramFpga):
            pass

        assert FpgaA.application_db is not FpgaB.application_db
        assert FpgaA.application_db is not SramFpga.application_db

    def test_sub_subclass_gets_own_db(self):
        class FpgaA(SramFpga):
            pass

        class FpgaA1(FpgaA):
            pass

        assert FpgaA1.application_db is not FpgaA.application_db

    def test_db_names_include_class(self):
        class MyFpga(SramFpga):
            pass

        assert "MyFpga" in MyFpga.application_db.name

    @pytest.mark.asyncio
    async def test_child_spawn_finds_parent_db(self):
        """Registration on parent's db is found via MRO walk in child_spawn."""
        class FpgaBase(SramFpga):
            pass

        class FpgaChild(FpgaBase):
            pass

        @FpgaBase.application_db.register("app")
        def _make_app(fpga):
            return Component("test-app")

        fpga = FpgaChild.__new__(FpgaChild)
        Component.__init__(fpga, "test-fpga")
        child = await fpga._child_spawn_mro("app")
        assert child.name == "test-app"

    @pytest.mark.asyncio
    async def test_child_spawn_child_db_takes_precedence(self):
        """Registration on child's db is tried before parent's."""
        class FpgaBase(SramFpga):
            pass

        class FpgaChild(FpgaBase):
            pass

        @FpgaBase.application_db.register("app")
        def _make_base(fpga):
            return Component("from-base")

        @FpgaChild.application_db.register("app")
        def _make_child(fpga):
            return Component("from-child")

        fpga = FpgaChild.__new__(FpgaChild)
        Component.__init__(fpga, "test-fpga")
        child = await fpga._child_spawn_mro("app")
        assert child.name == "from-child"

    @pytest.mark.asyncio
    async def test_child_spawn_nomatch(self):
        class FpgaX(SramFpga):
            pass

        fpga = FpgaX.__new__(FpgaX)
        Component.__init__(fpga, "test-fpga")
        with pytest.raises(NoMatch):
            await fpga._child_spawn_mro("nonexistent")

    @pytest.mark.asyncio
    async def test_jtag_sram_fpga_inherits(self):
        """JtagSramFpga subclasses also get per-class db."""
        class MyJtagFpga(JtagSramFpga):
            pass

        assert MyJtagFpga.application_db is not JtagSramFpga.application_db
        assert MyJtagFpga.application_db is not SramFpga.application_db


# --- GowinFpga Db-based registration ---

class TestGowinDbRegistration:
    def test_gowin_has_spi_registered(self):
        from acrobe.component.gowin.gw1n import GowinFpga
        handlers = GowinFpga.application_db.get("spi")
        assert len(handlers) == 1

    def test_gw5a_inherits_gowin_spi(self):
        """Gw5a (subclass of GowinFpga) doesn't have its own spi,
        but GowinFpga.application_db does, found via MRO walk."""
        from acrobe.component.gowin.gw1n import Gw5a, GowinFpga
        # Gw5a has its own empty db
        with pytest.raises(NoMatch):
            Gw5a.application_db.get("spi")
        # But GowinFpga's db has it
        assert GowinFpga.application_db.get("spi")


# --- child_summon starts components ---

class TestChildSummonStart:
    @pytest.mark.asyncio
    async def test_spawned_child_started(self):
        """child_summon starts spawned components."""
        class Parent(Component):
            async def child_spawn(self, name):
                if name == "x":
                    return Component("x")
                raise NoMatch("child", name)

        p = Parent("p")
        child = await p.child_summon("x")
        assert child.started

    @pytest.mark.asyncio
    async def test_existing_child_started(self):
        """child_summon starts existing children when navigating through."""
        started = []

        class Tracked(Component):
            async def start(self):
                started.append(self.name)

        root = Component("root")
        child = Tracked("child")
        grandchild = Tracked("grandchild")
        root.child_add(child)
        child.child_add(grandchild)

        result = await root.child_summon("child", "grandchild")
        assert result is grandchild
        assert "child" in started
        assert "grandchild" in started

    @pytest.mark.asyncio
    async def test_leaf_started(self):
        """child_summon starts even the leaf component."""
        started = []

        class Tracked(Component):
            async def start(self):
                started.append(self.name)

        root = Component("root")
        child = Tracked("child")
        root.child_add(child)

        await root.child_summon("child")
        assert "child" in started
        assert child.started

    @pytest.mark.asyncio
    async def test_already_started_not_restarted(self):
        """child_summon doesn't re-start already started components."""
        start_count = 0

        class Tracked(Component):
            async def start(self):
                nonlocal start_count
                start_count += 1

        root = Component("root")
        child = Tracked("child")
        root.child_add(child)

        await child.start()
        child._started = True
        assert start_count == 1

        await root.child_summon("child")
        assert start_count == 1  # not called again

    @pytest.mark.asyncio
    async def test_start_populates_children(self):
        """A component's start() can populate children, which are then navigable."""
        class Discoverer(Component):
            async def start(self):
                self.child_add(Component("discovered"))

        root = Component("root")
        disc = Discoverer("disc")
        root.child_add(disc)

        result = await root.child_summon("disc", "discovered")
        assert result.name == "discovered"


# --- JtagInterface ---

class TestJtagInterface:
    async def test_spawns_chain_child(self):
        """Chain is spawned on demand via child_spawn, not auto-created."""
        iface = JtagInterface(name="jtag-pt")
        assert len(iface.children) == 0
        chain = await iface.child_spawn("chain")
        assert isinstance(chain, Chain)
        assert chain.name == "chain"


# --- start_tree idempotent ---

class TestStartTreeIdempotent:
    @pytest.mark.asyncio
    async def test_start_tree_skips_started(self):
        """start_tree doesn't re-call start() on already-started components."""
        start_count = 0

        class Tracked(Component):
            async def start(self):
                nonlocal start_count
                start_count += 1

        root = Tracked("root")
        await root.start()
        root._started = True
        assert start_count == 1

        await root.start_tree()
        assert start_count == 1  # not called again


# --- spi.Target.child_db tests ---

class TestSpiTargetChildDb:
    def test_flash_registered(self):
        from acrobe.protocol.spi import Target
        handlers = Target.child_db.get("flash")
        assert len(handlers) == 1

    def test_nomatch_for_unknown(self):
        from acrobe.protocol.spi import Target
        with pytest.raises(NoMatch):
            Target.child_db.get("unknown")
