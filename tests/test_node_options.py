"""Tests for the path options grammar (Node._Node__parse_options).

Per docs/vfs-design.md D10:

- Names must contain balanced parens.
- Trailing (...) is options if its content parses as
  key=value pairs (every entry must contain '=').
- () (empty parens) is the explicit "no options, name has trailing
  parens" escape.
- Bare keys (without '=') are NOT supported (parse error).
- Quoted values via "..." with \\" and \\\\ escapes.
"""

import pytest

from acrobe.node import Node, _parse_kv_list, _NotKvList


parse = Node._Node__parse_options


class TestSimpleNames:
    def test_no_options(self):
        assert parse("foo") == ("foo", {})

    def test_with_dashes_dots(self):
        assert parse("file.bin") == ("file.bin", {})
        assert parse("proby-9") == ("proby-9", {})

    def test_empty_string(self):
        assert parse("") == ("", {})


class TestSimpleOptions:
    def test_single_option(self):
        assert parse("node(k=v)") == ("node", {"k": "v"})

    def test_multiple_options(self):
        assert parse("node(a=1,b=2)") == ("node", {"a": "1", "b": "2"})

    def test_hex_value(self):
        assert parse("region(offset=0x100)") == ("region", {"offset": "0x100"})

    def test_mime_type(self):
        assert parse("as(type=zip)") == ("as", {"type": "zip"})

    def test_mime_type_with_slash(self):
        assert parse("as(mime-type=application/zip)") == (
            "as", {"mime-type": "application/zip"})


class TestQuotedValues:
    def test_quoted_simple(self):
        assert parse('node(s="hello")') == ("node", {"s": "hello"})

    def test_quoted_with_comma(self):
        assert parse('node(label="hello, world")') == (
            "node", {"label": "hello, world"})

    def test_quoted_with_parens(self):
        assert parse('node(regex="(a=b)")') == ("node", {"regex": "(a=b)"})

    def test_quoted_with_space(self):
        assert parse('node(path="some dir/file.bin")') == (
            "node", {"path": "some dir/file.bin"})

    def test_quoted_escape_quote(self):
        assert parse('node(s="he said \\"hi\\"")') == (
            "node", {"s": 'he said "hi"'})

    def test_quoted_escape_backslash(self):
        assert parse('node(p="\\\\")') == ("node", {"p": "\\"})


class TestBalancedParensInName:
    def test_parens_in_name_no_options(self):
        # No '=' in content → not options → parens part of name
        assert parse("crazy(I like it)") == ("crazy(I like it)", {})

    def test_parens_in_name_with_explicit_escape(self):
        assert parse("crazy(I like it)()") == ("crazy(I like it)", {})

    def test_two_groups_last_is_options(self):
        # First (...) is part of name (no '='), last (...) is options
        assert parse("bin(a=1)(b=2)") == ("bin(a=1)", {"b": "2"})


class TestParseFailures:
    def test_bare_key_not_allowed(self):
        # "node(verbose)" — content has no '=' → not options → name
        assert parse("node(verbose)") == ("node(verbose)", {})

    def test_empty_key(self):
        # "(=v)" — empty key → parse fails → treated as name
        assert parse("node(=v)") == ("node(=v)", {})

    def test_unbalanced_open_paren(self):
        assert parse("test(foo") == ("test(foo", {})

    def test_unbalanced_close_paren(self):
        # Unmatched ')' → not options → whole as name
        assert parse("test)") == ("test)", {})

    def test_unterminated_quote(self):
        assert parse('node(k="abc)') == ('node(k="abc)', {})


class TestKvListDirect:
    def test_basic(self):
        assert _parse_kv_list("a=1,b=2") == {"a": "1", "b": "2"}

    def test_quoted_value_with_comma(self):
        assert _parse_kv_list('a="x,y",b=2') == {"a": "x,y", "b": "2"}

    def test_quoted_value_with_paren(self):
        assert _parse_kv_list('a="(x)"') == {"a": "(x)"}

    def test_empty_raises(self):
        with pytest.raises(_NotKvList):
            _parse_kv_list("")

    def test_no_eq_raises(self):
        with pytest.raises(_NotKvList):
            _parse_kv_list("verbose")

    def test_bare_value_with_whitespace_rejected(self):
        with pytest.raises(_NotKvList):
            _parse_kv_list("k=hello world")


class TestChildLookupExactBeforeSubstring:
    """Exact match must beat substring when names share a prefix
    (e.g. STAPL vars J2, J23, J24)."""

    def test_exact_match_wins(self):
        parent = Node("p")
        for n in ["J2", "J23", "J24", "J25"]:
            parent.child_add(Node(n))
        # "J2" must resolve to the exact node, not raise ambiguity.
        assert parent.child_lookup("J2").name == "J2"
        assert parent.child_lookup("J23").name == "J23"

    def test_substring_still_works_when_unique(self):
        parent = Node("p")
        parent.child_add(Node("longname"))
        parent.child_add(Node("other"))
        assert parent.child_lookup("long").name == "longname"

    def test_substring_ambiguity_returns_none(self):
        parent = Node("p")
        parent.child_add(Node("foo"))
        parent.child_add(Node("foobar"))
        # Substring "fo" matches both, no exact match → None.
        assert parent.child_lookup("fo") is None


class TestChildSummonOptions:
    """Integration: child_summon parses options and applies them
    via option_set."""

    @pytest.mark.asyncio
    async def test_options_passed_to_option_set(self):
        applied = {}

        class Root(Node):
            async def child_spawn(self, name):
                child = OptNode(name)
                return child

        class OptNode(Node):
            def option_set(self, key, value):
                applied[key] = value

        root = Root("root")
        await root.start_tree()
        await root.child_summon("foo(k1=v1,k2=v2)")
        assert applied == {"k1": "v1", "k2": "v2"}

    @pytest.mark.asyncio
    async def test_no_options_no_calls(self):
        applied = {}

        class OptNode(Node):
            def option_set(self, key, value):
                applied[key] = value

        class Root(Node):
            async def child_spawn(self, name):
                return OptNode(name)

        root = Root("root")
        await root.start_tree()
        await root.child_summon("foo")
        assert applied == {}
