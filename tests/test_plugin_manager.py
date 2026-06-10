"""Tests for httpie.plugins.manager – plugin directory context manager and
installed-plugin loading logic.

These tests cover the following scenarios that previously caused bugs:

1. Nested ``_load_directories`` contexts corrupting ``sys.path``.
2. Duplicate directories in the site-dir iterable raising ``ValueError``.
3. Directories that were already in ``sys.path`` before entering the context
   being erroneously removed on exit.
4. ``load_installed_plugins`` being called multiple times, resulting in
   duplicate plugin registrations.
5. Duplicate entry points (same group+name) causing double registration.
6. Bad plugins producing useful warning messages.
"""
import sys
import warnings
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from httpie.plugins.manager import (
    PluginManager,
    _load_directories,
    enable_plugins,
)
from httpie.plugins.base import BasePlugin, AuthPlugin


# ---------------------------------------------------------------------------
# _load_directories / enable_plugins – sys.path isolation
# ---------------------------------------------------------------------------

class TestLoadDirectories:
    """Unit tests for the ``_load_directories`` context manager."""

    def test_dirs_added_and_removed(self, tmp_path):
        """Dirs are added to sys.path inside the context and removed after."""
        dir_a = tmp_path / 'a'
        dir_a.mkdir()
        original = sys.path.copy()

        with _load_directories([dir_a]):
            assert str(dir_a) in sys.path

        assert str(dir_a) not in sys.path
        assert sys.path == original

    def test_pre_existing_dir_preserved(self, tmp_path):
        """A dir already in sys.path before entering the context must still
        be present after exiting."""
        dir_a = tmp_path / 'a'
        dir_a.mkdir()
        dir_str = str(dir_a)

        # Simulate that dir_a is already on sys.path before we enter.
        with patch.object(sys, 'path', sys.path + [dir_str]):
            original = sys.path.copy()
            assert dir_str in original

            with _load_directories([dir_a]):
                # Should appear exactly once (not duplicated).
                assert sys.path.count(dir_str) == 1

            # Must still be present after exit because it was there before.
            assert dir_str in sys.path
            assert sys.path == original

    def test_nested_contexts(self, tmp_path):
        """Nested contexts with overlapping dirs must not corrupt sys.path."""
        dir_a = tmp_path / 'a'
        dir_a.mkdir()
        dir_b = tmp_path / 'b'
        dir_b.mkdir()

        original = sys.path.copy()

        with _load_directories([dir_a]):
            assert str(dir_a) in sys.path

            with _load_directories([dir_a, dir_b]):
                assert str(dir_a) in sys.path
                assert str(dir_b) in sys.path

            # After inner context exits, dir_a should still be present
            # (outer context added it), dir_b should be gone.
            assert str(dir_a) in sys.path
            assert str(dir_b) not in sys.path

        # After outer context exits, both should be gone.
        assert sys.path == original

    def test_duplicate_dirs_no_error(self, tmp_path):
        """Passing the same dir multiple times must not raise ValueError."""
        dir_a = tmp_path / 'a'
        dir_a.mkdir()

        original = sys.path.copy()

        # Same dir passed three times – should not raise.
        with _load_directories([dir_a, dir_a, dir_a]):
            assert sys.path.count(str(dir_a)) == 1

        assert sys.path == original

    def test_empty_dirs(self):
        """An empty iterable should be a no-op."""
        original = sys.path.copy()
        with _load_directories([]):
            assert sys.path == original
        assert sys.path == original

    def test_enable_plugins_none(self):
        """``enable_plugins(None)`` should be a no-op nullcontext."""
        original = sys.path.copy()
        with enable_plugins(None):
            assert sys.path == original
        assert sys.path == original

    def test_sys_path_restored_on_exception(self, tmp_path):
        """sys.path must be restored even if the body raises."""
        dir_a = tmp_path / 'a'
        dir_a.mkdir()
        original = sys.path.copy()

        with pytest.raises(RuntimeError):
            with _load_directories([dir_a]):
                assert str(dir_a) in sys.path
                raise RuntimeError('boom')

        assert sys.path == original


# ---------------------------------------------------------------------------
# PluginManager.load_installed_plugins – deduplication & warnings
# ---------------------------------------------------------------------------

class FakeAuthPlugin(AuthPlugin):
    """A minimal concrete auth plugin for testing."""
    name = 'fake'
    auth_type = 'fake'
    auth_require = False


class TestLoadInstalledPlugins:
    """Tests for ``PluginManager.load_installed_plugins``."""

    def _make_entry_point(self, group, name, plugin_cls, dist_name='fake-pkg'):
        """Create a mock entry point that loads ``plugin_cls``."""
        ep = MagicMock()
        ep.group = group
        ep.name = name
        ep.value = f'{plugin_cls.__module__}:{plugin_cls.__name__}'
        ep.load.return_value = plugin_cls
        # dist attribute for get_dist_name
        dist = MagicMock()
        dist.name = dist_name
        ep.dist = dist
        return ep

    def test_idempotent_loading(self):
        """Calling load_installed_plugins twice must not duplicate plugins."""
        ep = self._make_entry_point(
            'httpie.plugins.auth.v1', 'fake', FakeAuthPlugin
        )

        mgr = PluginManager()

        with patch.object(mgr, 'iter_entry_points', return_value=iter([ep])):
            mgr.load_installed_plugins()
            assert mgr.count(FakeAuthPlugin) == 1

            # Reset the mock iterator for the second call.
            # (iter_entry_points is a generator, so we need a fresh one)

        with patch.object(mgr, 'iter_entry_points', return_value=iter([ep])):
            mgr.load_installed_plugins()
            # Still only one instance – idempotent.
            assert mgr.count(FakeAuthPlugin) == 1

    def test_duplicate_entry_points_deduplicated(self):
        """Two entry points with the same (group, name) must result in a
        single registration."""
        ep1 = self._make_entry_point(
            'httpie.plugins.auth.v1', 'fake', FakeAuthPlugin, dist_name='pkg-a'
        )
        ep2 = self._make_entry_point(
            'httpie.plugins.auth.v1', 'fake', FakeAuthPlugin, dist_name='pkg-b'
        )

        mgr = PluginManager()

        # iter_entry_points already deduplicates, so test through it.
        with patch(
            'httpie.plugins.manager.importlib_metadata'
        ) as mock_meta:
            mock_eps = MagicMock()
            # select() returns both duplicate entry points
            mock_eps.select.return_value = [ep1, ep2]
            mock_meta.entry_points.return_value = mock_eps
            mock_meta.EntryPoint = MagicMock

            mgr.load_installed_plugins()

        assert mgr.count(FakeAuthPlugin) == 1

    def test_bad_plugin_warning_includes_entry_point_info(self):
        """A failing entry point must produce a warning that includes the
        group and entry point name, even when dist name is unavailable."""
        ep = self._make_entry_point(
            'httpie.plugins.auth.v1', 'bad_ep', FakeAuthPlugin,
            dist_name='bad-pkg'
        )
        ep.load.side_effect = ImportError('missing dependency')

        mgr = PluginManager()

        with patch.object(mgr, 'iter_entry_points', return_value=iter([ep])):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                mgr.load_installed_plugins()

        assert len(caught) == 1
        msg = str(caught[0].message)
        # The warning should mention the package name and group.
        assert 'bad-pkg' in msg
        assert 'httpie.plugins.auth.v1' in msg
        assert 'missing dependency' in msg

    def test_bad_plugin_warning_falls_back_to_ep_name(self):
        """When dist name is None, the warning should fall back to the
        entry point name."""
        ep = self._make_entry_point(
            'httpie.plugins.auth.v1', 'orphan_ep', FakeAuthPlugin,
        )
        # Make get_dist_name return None by removing dist attribute
        del ep.dist
        ep.pattern = MagicMock()
        ep.pattern.match.return_value = None
        ep.load.side_effect = RuntimeError('broken')

        mgr = PluginManager()

        with patch.object(mgr, 'iter_entry_points', return_value=iter([ep])):
            with patch(
                'httpie.plugins.manager.get_dist_name', return_value=None
            ):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    mgr.load_installed_plugins()

        assert len(caught) == 1
        msg = str(caught[0].message)
        # Falls back to entry point name.
        assert 'orphan_ep' in msg
        assert 'httpie.plugins.auth.v1' in msg

    def test_different_plugins_all_registered(self):
        """Distinct plugin classes must all be registered."""
        class PluginA(AuthPlugin):
            name = 'a'
            auth_type = 'a'
            auth_require = False

        class PluginB(AuthPlugin):
            name = 'b'
            auth_type = 'b'
            auth_require = False

        ep_a = self._make_entry_point(
            'httpie.plugins.auth.v1', 'a', PluginA, dist_name='pkg-a'
        )
        ep_b = self._make_entry_point(
            'httpie.plugins.auth.v1', 'b', PluginB, dist_name='pkg-b'
        )

        mgr = PluginManager()

        with patch.object(
            mgr, 'iter_entry_points', return_value=iter([ep_a, ep_b])
        ):
            mgr.load_installed_plugins()

        assert PluginA in mgr
        assert PluginB in mgr
        assert mgr.count(PluginA) == 1
        assert mgr.count(PluginB) == 1


# ---------------------------------------------------------------------------
# iter_entry_points – deduplication
# ---------------------------------------------------------------------------

class TestIterEntryPoints:
    """Tests for ``PluginManager.iter_entry_points``."""

    def test_deduplicates_by_group_and_name(self):
        """Entry points with the same (group, name) are yielded only once."""
        ep1 = MagicMock()
        ep1.group = 'httpie.plugins.auth.v1'
        ep1.name = 'test'

        ep2 = MagicMock()
        ep2.group = 'httpie.plugins.auth.v1'
        ep2.name = 'test'

        ep3 = MagicMock()
        ep3.group = 'httpie.plugins.auth.v1'
        ep3.name = 'other'

        mgr = PluginManager()

        with patch(
            'httpie.plugins.manager.importlib_metadata'
        ) as mock_meta:
            mock_eps = MagicMock()
            # select returns duplicates for 'test' group, and 'other'
            mock_eps.select.return_value = [ep1, ep2, ep3]
            mock_meta.entry_points.return_value = mock_eps

            result = list(mgr.iter_entry_points())

        # ep1 and ep2 are duplicates; only one should appear.
        names = [ep.name for ep in result]
        assert names.count('test') == 1
        assert names.count('other') == 1

    def test_different_groups_same_name_not_deduplicated(self):
        """Entry points with the same name but different groups are distinct."""
        ep1 = MagicMock()
        ep1.group = 'httpie.plugins.auth.v1'
        ep1.name = 'test'

        ep2 = MagicMock()
        ep2.group = 'httpie.plugins.formatter.v1'
        ep2.name = 'test'

        mgr = PluginManager()

        with patch(
            'httpie.plugins.manager.importlib_metadata'
        ) as mock_meta:
            mock_eps = MagicMock()

            def fake_select(group):
                if group == 'httpie.plugins.auth.v1':
                    return [ep1]
                elif group == 'httpie.plugins.formatter.v1':
                    return [ep2]
                return []

            mock_eps.select.side_effect = fake_select
            mock_meta.entry_points.return_value = mock_eps

            result = list(mgr.iter_entry_points())

        assert len(result) == 2
