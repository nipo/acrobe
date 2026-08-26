import pytest
from acrobe.db import Db, NoMatch


class TestRegister:
    def test_register_decorator(self):
        db = Db("test")

        @db.register("foo")
        def handler():
            return "result"

        assert db.get("foo") == [handler]

    def test_register_multiple_ids(self):
        db = Db("test")

        @db.register("a", "b")
        def handler():
            pass

        assert db.get("a") == [handler]
        assert db.get("b") == [handler]

    def test_register_multiple_handlers(self):
        db = Db("test")

        @db.register("x")
        def h1():
            pass

        @db.register("x")
        def h2():
            pass

        assert db.get("x") == [h1, h2]


class TestDefault:
    def test_default_used(self):
        db = Db("test")

        @db.register_default
        def fallback():
            pass

        assert db.get("anything") == [fallback]

    def test_default_not_used_when_match(self):
        db = Db("test")

        @db.register_default
        def fallback():
            pass

        @db.register("x")
        def specific():
            pass

        assert db.get("x") == [specific]

    def test_no_default_raises(self):
        db = Db("test")

        @db.register("x")
        def handler():
            pass

        with pytest.raises(NoMatch):
            db.get("y", allow_default=False)


class TestGet:
    def test_nomatch_raises(self):
        db = Db("test")
        with pytest.raises(NoMatch) as exc_info:
            db.get("missing")
        assert "missing" in str(exc_info.value)
        assert "test" in str(exc_info.value)

    def test_custom_eq_func(self):
        # eq_func(key, id): key.startswith(id)
        db = Db("test", eq_func=lambda key, id: key.startswith(id))

        @db.register("hello_world")
        def handler():
            pass

        assert db.get("hello") == [handler]

        with pytest.raises(NoMatch):
            db.get("world")


class TestCall:
    def test_call_success(self):
        db = Db("test")

        @db.register("x")
        def handler(a, b):
            return a + b

        assert db.call("x", 1, 2) == 3

    def test_call_first_match_wins(self):
        db = Db("test")

        @db.register("x")
        def h1():
            return "first"

        @db.register("x")
        def h2():
            return "second"

        assert db.call("x") == "first"

    def test_call_skip_on_nomatch(self):
        db = Db("test")

        @db.register("x")
        def h1():
            raise NoMatch("inner", "whatever")

        @db.register("x")
        def h2():
            return "fallback"

        assert db.call("x") == "fallback"

    def test_call_all_fail(self):
        db = Db("test")

        @db.register("x")
        def h1():
            raise NoMatch("inner", "a")

        with pytest.raises(NoMatch):
            db.call("x")

    def test_call_with_default(self):
        db = Db("test")

        @db.register_default
        def fallback(val):
            return val * 2

        assert db.call("anything", 5) == 10

    def test_call_kwargs(self):
        db = Db("test")

        @db.register("x")
        def handler(a, b=10):
            return a + b

        assert db.call("x", 1, b=20) == 21
