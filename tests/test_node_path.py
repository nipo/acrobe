"""Tests for acrobe.node.Path utilities."""

import os
from pathlib import Path as PyPath

import pytest

from acrobe.node import Node, Path


class TestStringHelpers:
    def test_parts_empty(self):
        assert Path.parts("") == ()

    def test_parts_root(self):
        assert Path.parts("/") == ()

    def test_parts_single(self):
        assert Path.parts("a") == ("a",)

    def test_parts_multi(self):
        assert Path.parts("a/b/c") == ("a", "b", "c")

    def test_parts_absolute(self):
        assert Path.parts("/a/b") == ("a", "b")

    def test_parts_double_slash(self):
        assert Path.parts("a//b") == ("a", "b")

    def test_parent_of_empty(self):
        assert Path.parent_of("") is None

    def test_parent_of_single(self):
        assert Path.parent_of("a") is None

    def test_parent_of_multi(self):
        assert Path.parent_of("a/b/c") == "a/b"

    def test_parent_of_abs_root(self):
        assert Path.parent_of("/") is None

    def test_parent_of_abs_one_segment(self):
        assert Path.parent_of("/a") == "/"

    def test_parent_of_abs_multi(self):
        assert Path.parent_of("/a/b") == "/a"

    def test_descendant_self(self):
        assert Path.is_descendant_or_self("a/b", "a/b") is True

    def test_descendant_child(self):
        assert Path.is_descendant_or_self("a/b/c", "a/b") is True

    def test_descendant_deeper(self):
        assert Path.is_descendant_or_self("a/b/c/d", "a/b") is True

    def test_descendant_unrelated(self):
        assert Path.is_descendant_or_self("a/c", "a/b") is False

    def test_descendant_prefix_not_segment(self):
        # "a/bb" is not a descendant of "a/b" (segment boundary
        # respected).
        assert Path.is_descendant_or_self("a/bb", "a/b") is False

    def test_descendant_empty_ancestor(self):
        # Empty ancestor is the universal root — matches anything.
        assert Path.is_descendant_or_self("a/b", "") is True

    def test_descendant_trailing_slash_tolerant(self):
        assert Path.is_descendant_or_self("a/b/c", "a/b/") is True


class TestCanonicalizeFs:
    def test_existing_file_no_symlinks(self, tmp_path: PyPath):
        f = tmp_path / "firmware.elf"
        f.write_bytes(b"")
        result = Path.canonicalize_fs(f)
        assert result == os.path.realpath(f)

    def test_nonexistent_file_under_real_dir(self, tmp_path: PyPath):
        # File doesn't exist yet; parent does.
        f = tmp_path / "not-yet.elf"
        result = Path.canonicalize_fs(f)
        # Parent should be resolved to its real path; the leaf
        # appended literally.
        assert result == os.path.join(os.path.realpath(tmp_path), "not-yet.elf")

    def test_symlinked_dir(self, tmp_path: PyPath):
        real = tmp_path / "real"
        real.mkdir()
        (real / "fw.elf").write_bytes(b"")
        link = tmp_path / "link"
        link.symlink_to(real)

        result = Path.canonicalize_fs(link / "fw.elf")
        assert result == os.path.realpath(real / "fw.elf")

    def test_symlinked_dir_file_absent(self, tmp_path: PyPath):
        # Symlink target exists, file inside it doesn't.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        result = Path.canonicalize_fs(link / "future.elf")
        assert result == os.path.join(os.path.realpath(real), "future.elf")

    def test_symlink_in_middle_of_path(self, tmp_path: PyPath):
        # a/b/d/foo where d -> ../c, foo absent.
        a = tmp_path / "a"
        b = a / "b"
        c = a / "b" / "c"
        c.mkdir(parents=True)
        d = b / "d"
        d.symlink_to(PyPath("c"))

        result = Path.canonicalize_fs(d / "foo")
        assert result == os.path.join(os.path.realpath(c), "foo")

    def test_completely_absent_path(self, tmp_path: PyPath):
        # Nothing along the trailing path exists; everything
        # appended literally after the real prefix.
        result = Path.canonicalize_fs(tmp_path / "x" / "y" / "z")
        expected = os.path.join(
            os.path.realpath(tmp_path), "x", "y", "z")
        assert result == expected

    def test_accepts_str_and_pathlib(self, tmp_path: PyPath):
        s = str(tmp_path / "f")
        p = tmp_path / "f"
        assert Path.canonicalize_fs(s) == Path.canonicalize_fs(p)


class TestCanonicalizeHw:
    def __make_tree(self):
        root = Node("HwRoot")
        proby = Node("proby-9")
        jtag = Node("jtag")
        chain = Node("chain")
        root.child_add(proby)
        proby.child_add(jtag)
        jtag.child_add(chain)
        return root

    def test_exact_path(self):
        root = self.__make_tree()
        assert (Path.canonicalize_hw(root, "proby-9/jtag")
                == "HwRoot/proby-9/jtag")

    def test_substring_shorthand(self):
        # `child_lookup` resolves a case-insensitive unique
        # substring match.
        root = self.__make_tree()
        assert (Path.canonicalize_hw(root, "proby/jtag")
                == "HwRoot/proby-9/jtag")

    def test_redundant_root_prefix(self):
        root = self.__make_tree()
        assert (Path.canonicalize_hw(root, "HwRoot/proby-9")
                == "HwRoot/proby-9")

    def test_unknown_tail(self):
        # `usb-cdc` doesn't exist; appended literally.
        root = self.__make_tree()
        assert (Path.canonicalize_hw(root, "proby-9/usb-cdc")
                == "HwRoot/proby-9/usb-cdc")

    def test_unknown_tail_with_deeper_segments(self):
        root = self.__make_tree()
        assert (Path.canonicalize_hw(root, "proby-9/usb-cdc/tty")
                == "HwRoot/proby-9/usb-cdc/tty")

    def test_empty_path_is_root(self):
        root = self.__make_tree()
        assert Path.canonicalize_hw(root, "") == "HwRoot"

    def test_root_only(self):
        root = self.__make_tree()
        assert Path.canonicalize_hw(root, "HwRoot") == "HwRoot"

    def test_canonicalises_segment_in_middle(self):
        # Substring in middle still resolves.
        root = self.__make_tree()
        assert (Path.canonicalize_hw(root, "proby-9/jt/chain")
                == "HwRoot/proby-9/jtag/chain")
