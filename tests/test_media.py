"""Tests for safe media playback (Batch 20).

Every launch is faked: no real media player is started and no real movie
library is touched. Temporary directories stand in for the approved
folders. These tests prove the security properties end to end through the
guarded executor:

* media tools are registered only when tools **and** media are enabled;
* discovery stays inside the approved roots and skips symlinks;
* a single exact/unique match may launch (after confirmation);
* several plausible matches never auto-play - an explicit position is
  required, and only against a current list;
* denied approval never launches;
* the chosen path is re-validated immediately before opening.
"""

import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import core.media as media_module
from app import _build_registry, main
from config import MediaConfig, ToolsConfig
from core.builtin_tools import build_builtin_registry
from core.confirmation import ConfirmationRegistry
from core.executor import ExecutionStatus, ToolExecutor
from core.media import (
    VIDEO_EXTENSIONS,
    MediaDiscovery,
    SubprocessMediaLauncher,
    _strip_media_location,
)
from core.media_tools import (
    TOOL_MEDIA_CANCEL,
    TOOL_MEDIA_PLAY,
    TOOL_MEDIA_SELECT,
    MediaSession,
    build_media_specs,
)
from core.permissions import (
    ApprovalOutcome,
    ApprovalResult,
    PermissionPolicy,
)
from core.router import ToolCommandRouter
from core.tools import ToolRegistry


# ----------------------------------------------------------------------
# Fakes and helpers
# ----------------------------------------------------------------------
class FakeOpener:
    """Records opened paths; never starts a process."""

    def __init__(self, error=None):
        self.opened = []
        self.error = error

    def __call__(self, path):
        if self.error is not None:
            raise self.error
        self.opened.append(path)


class EchoProvider:
    """A correct approval provider: echoes the request token back."""

    def __init__(self, outcome=ApprovalOutcome.APPROVED):
        self.outcome = outcome
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return ApprovalResult(self.outcome, "ok", request.token)


class FakeListener:
    """Stand-in for voice.listener.Listener with no audio hardware."""

    def __init__(self, texts=None):
        self.texts = list(texts or [])
        self.closed = False

    def transcribe(self):
        return self.texts.pop(0) if self.texts else "exit"

    def close(self):
        self.closed = True


class FakeSpeaker:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)

    def close(self):
        pass


def _make_tree(root, names):
    for name in names:
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("video", encoding="utf-8")


def _cleanup_dir(path):
    shutil.rmtree(path, ignore_errors=True)


def _media_tools_map():
    return {
        "play": TOOL_MEDIA_PLAY,
        "select": TOOL_MEDIA_SELECT,
        "cancel": TOOL_MEDIA_CANCEL,
    }


class MediaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def discovery(self, names, *, ext=VIDEO_EXTENSIONS):
        _make_tree(self.root, names)
        return MediaDiscovery([self.root], extensions=ext)

    def media_executor(
        self, names, *, provider=None, opener_error=None
    ):
        """Return a context with executor, session, opener, discovery."""
        discovery = self.discovery(names)
        session = MediaSession()
        opener = FakeOpener(opener_error)
        registry = ToolRegistry()
        for spec in build_media_specs(
            discovery, session, SubprocessMediaLauncher(opener)
        ):
            registry.register(spec)
        confirmations = ConfirmationRegistry(ttl=60.0)
        executor = ToolExecutor(
            registry,
            PermissionPolicy(),
            provider if provider is not None else EchoProvider(),
            confirmations=confirmations,
        )
        return types.SimpleNamespace(
            executor=executor,
            session=session,
            opener=opener,
            discovery=discovery,
        )


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------
class TestDiscovery(MediaTestCase):
    def test_exact_match_is_found(self):
        discovery = self.discovery(["Project Hail Mary.mkv"])
        results = discovery.find_candidates("Project Hail Mary")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_exact)

    def test_case_insensitive(self):
        discovery = self.discovery(["Interstellar.mp4"])
        results = discovery.find_candidates("interstellar")
        self.assertEqual(len(results), 1)

    def test_separators_are_normalized(self):
        discovery = self.discovery(["The_Matrix-Reloaded.2021.mkv"])
        for query in ("The Matrix Reloaded 2021", "the.matrix.reloaded.2021"):
            self.assertEqual(len(discovery.find_candidates(query)), 1, query)

    def test_unsupported_extension_is_ignored(self):
        discovery = self.discovery(["notes.txt", "song.mp3", "clip.mp4"])
        self.assertEqual(discovery.find_candidates("notes"), [])
        self.assertEqual(discovery.find_candidates("song"), [])
        self.assertEqual(len(discovery.find_candidates("clip")), 1)

    def test_no_and_empty_match_return_empty(self):
        discovery = self.discovery(["Interstellar.mp4"])
        self.assertEqual(discovery.find_candidates("nothing here"), [])
        self.assertEqual(discovery.find_candidates("   "), [])

    def test_missing_root_is_skipped(self):
        discovery = MediaDiscovery([self.root / "does-not-exist"])
        self.assertEqual(discovery.find_candidates("anything"), [])

    def test_ranking_exact_before_partial(self):
        discovery = self.discovery(
            ["Interstellar Trailer.mp4", "Interstellar.mp4"]
        )
        results = discovery.find_candidates("Interstellar")
        self.assertTrue(results[0].is_exact)
        self.assertTrue(results[0].display_name.endswith("Interstellar.mp4"))
        self.assertFalse(results[1].is_exact)

    def test_multiple_partial_matches(self):
        discovery = self.discovery(
            ["Interstellar.mp4", "Interstellar Trailer.mp4", "Interstellar Extra.mp4"]
        )
        results = discovery.find_candidates("Interstellar")
        self.assertEqual(len(results), 3)

    def test_bounded_scan(self):
        _make_tree(self.root, ["A.mp4", "A B.mp4", "A C.mp4"])
        discovery = MediaDiscovery([self.root], max_scan=1)
        self.assertLessEqual(len(discovery.find_candidates("A")), 1)

    def test_missing_root_is_skipped_safely(self):
        discovery = MediaDiscovery([self.root / "nope"])
        self.assertEqual(discovery.find_candidates("anything"), [])

    def test_strip_location_tail(self):
        self.assertEqual(
            _strip_media_location("Project Hail Mary from drive E."),
            "Project Hail Mary",
        )
        self.assertEqual(
            _strip_media_location("Interstellar on the disk D"),
            "Interstellar",
        )

    def test_query_with_location_tail_still_matches(self):
        discovery = self.discovery(["Project Hail Mary.mkv"])
        self.assertEqual(
            len(discovery.find_candidates("Project Hail Mary from drive E.")),
            1,
        )

    def test_containment_rejects_paths_outside_roots(self):
        discovery = self.discovery(["Interstellar.mp4"])
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_dir, outside)
        (outside / "Escape.mp4").write_text("x", encoding="utf-8")
        self.assertFalse(discovery.is_within_roots(outside / "Escape.mp4"))
        self.assertTrue(discovery.is_within_roots(self.root / "Interstellar.mp4"))

    def test_symlinked_file_is_skipped(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_dir, outside)
        outside_file = outside / "Escape.mp4"
        outside_file.write_text("x", encoding="utf-8")
        try:
            os.symlink(outside_file, self.root / "Escape.mp4")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted on this host")
        discovery = MediaDiscovery([self.root])
        self.assertEqual(discovery.find_candidates("Escape"), [])

    def test_symlinked_directory_is_not_traversed(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_dir, outside)
        (outside / "Escape.mp4").write_text("x", encoding="utf-8")
        try:
            os.symlink(outside, self.root / "sub", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted on this host")
        discovery = MediaDiscovery([self.root])
        self.assertEqual(discovery.find_candidates("Escape"), [])


# ----------------------------------------------------------------------
# Session
# ----------------------------------------------------------------------
class TestSession(MediaTestCase):
    def test_empty_session(self):
        session = MediaSession()
        self.assertFalse(session.active)
        self.assertIsNone(session.resolve(0))

    def test_offer_then_resolve(self):
        matches = self.discovery(["A.mp4", "A B.mp4"]).find_candidates("A")
        session = MediaSession()
        session.offer(matches)
        self.assertTrue(session.active)
        self.assertEqual(session.resolve(0).display_name, matches[0].display_name)

    def test_out_of_range_negative_and_bool(self):
        session = MediaSession()
        session.offer(self.discovery(["A.mp4"]).find_candidates("A"))
        self.assertIsNone(session.resolve(5))
        self.assertIsNone(session.resolve(-1))
        self.assertIsNone(session.resolve(True))

    def test_clear(self):
        session = MediaSession()
        session.offer(self.discovery(["A.mp4"]).find_candidates("A"))
        session.clear()
        self.assertFalse(session.active)

    def test_offer_is_bounded(self):
        session = MediaSession(max_candidates=2)
        matches = self.discovery(["A.mp4", "A B.mp4", "A C.mp4"]).find_candidates(
            "A"
        )
        session.offer(matches)
        self.assertEqual(len(session.candidates()), 2)


# ----------------------------------------------------------------------
# Launcher
# ----------------------------------------------------------------------
class TestLauncher(MediaTestCase):
    def test_opener_receives_the_exact_path(self):
        opener = FakeOpener()
        launcher = SubprocessMediaLauncher(opener)
        target = self.root / "Movie.mp4"
        target.write_text("x", encoding="utf-8")
        launcher.launch(target)
        self.assertEqual(opener.opened, [str(target)])

    def test_launcher_error_is_wrapped(self):
        from core.media import MediaError

        launcher = SubprocessMediaLauncher(FakeOpener(OSError("boom")))
        with self.assertRaises(MediaError):
            launcher.launch(self.root / "Movie.mp4")

    def test_source_uses_no_shell_and_no_subprocess(self):
        source = Path(media_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)


# ----------------------------------------------------------------------
# Tools through the guarded executor
# ----------------------------------------------------------------------
class TestMediaTools(MediaTestCase):
    def test_unique_match_launches_once(self):
        ctx = self.media_executor(["Project Hail Mary.mkv"])
        result = ctx.executor.execute(
            TOOL_MEDIA_PLAY, {"query": "Project Hail Mary"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(ctx.opener.opened), 1)

    def test_multiple_matches_do_not_launch(self):
        ctx = self.media_executor(
            ["Interstellar Trailer.mp4", "Interstellar Extra.mp4"]
        )
        result = ctx.executor.execute(TOOL_MEDIA_PLAY, {"query": "Interstellar"})
        self.assertTrue(result.ok)
        self.assertEqual(ctx.opener.opened, [])
        self.assertTrue(ctx.session.active)
        self.assertIn("several matches", result.value["text"].lower())

    def test_exact_match_preferred_over_partial(self):
        ctx = self.media_executor(
            ["Interstellar.mp4", "Interstellar Trailer.mp4"]
        )
        ctx.executor.execute(TOOL_MEDIA_PLAY, {"query": "Interstellar"})
        self.assertEqual(len(ctx.opener.opened), 1)
        self.assertTrue(ctx.opener.opened[0].endswith("Interstellar.mp4"))

    def test_no_match_reports_cleanly(self):
        ctx = self.media_executor(["Interstellar.mp4"])
        result = ctx.executor.execute(TOOL_MEDIA_PLAY, {"query": "Nonexistent"})
        self.assertTrue(result.ok)
        self.assertIn("couldn't find", result.value["text"])
        self.assertEqual(ctx.opener.opened, [])
        self.assertFalse(ctx.session.active)

    def test_select_launches_after_multi_result(self):
        ctx = self.media_executor(
            ["Interstellar Trailer.mp4", "Interstellar Extra.mp4"]
        )
        ctx.executor.execute(TOOL_MEDIA_PLAY, {"query": "Interstellar"})
        result = ctx.executor.execute(TOOL_MEDIA_SELECT, {"index": 1})
        self.assertTrue(result.ok)
        self.assertEqual(len(ctx.opener.opened), 1)

    def test_select_without_active_list_does_not_launch(self):
        ctx = self.media_executor(
            ["Interstellar Trailer.mp4", "Interstellar Extra.mp4"]
        )
        result = ctx.executor.execute(TOOL_MEDIA_SELECT, {"index": 0})
        self.assertTrue(result.ok)
        self.assertIn("no current list", result.value["text"].lower())
        self.assertEqual(ctx.opener.opened, [])

    def test_select_out_of_range_does_not_launch(self):
        ctx = self.media_executor(
            ["Interstellar Trailer.mp4", "Interstellar Extra.mp4"]
        )
        ctx.executor.execute(TOOL_MEDIA_PLAY, {"query": "Interstellar"})
        result = ctx.executor.execute(TOOL_MEDIA_SELECT, {"index": 99})
        self.assertTrue(result.ok)
        self.assertEqual(ctx.opener.opened, [])

    def test_select_is_single_use(self):
        ctx = self.media_executor(
            ["Interstellar Trailer.mp4", "Interstellar Extra.mp4"]
        )
        ctx.executor.execute(TOOL_MEDIA_PLAY, {"query": "Interstellar"})
        ctx.executor.execute(TOOL_MEDIA_SELECT, {"index": 0})
        self.assertFalse(ctx.session.active)
        again = ctx.executor.execute(TOOL_MEDIA_SELECT, {"index": 0})
        self.assertIn("no current list", again.value["text"].lower())
        self.assertEqual(len(ctx.opener.opened), 1)

    def test_denied_approval_never_launches(self):
        ctx = self.media_executor(
            ["Project Hail Mary.mkv"],
            provider=EchoProvider(ApprovalOutcome.DENIED),
        )
        result = ctx.executor.execute(
            TOOL_MEDIA_PLAY, {"query": "Project Hail Mary"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, ExecutionStatus.DENIED)
        self.assertEqual(ctx.opener.opened, [])

    def test_invalid_query_is_rejected(self):
        ctx = self.media_executor(["Project Hail Mary.mkv"])
        result = ctx.executor.execute(TOOL_MEDIA_PLAY, {"query": ""})
        self.assertEqual(result.status, ExecutionStatus.INVALID)
        self.assertEqual(ctx.opener.opened, [])

    def test_invalid_index_is_rejected(self):
        ctx = self.media_executor(["Project Hail Mary.mkv"])
        result = ctx.executor.execute(TOOL_MEDIA_SELECT, {"index": "two"})
        self.assertEqual(result.status, ExecutionStatus.INVALID)
        self.assertEqual(ctx.opener.opened, [])

    def test_cancel_clears_the_session(self):
        ctx = self.media_executor(
            ["Interstellar Trailer.mp4", "Interstellar Extra.mp4"]
        )
        ctx.executor.execute(TOOL_MEDIA_PLAY, {"query": "Interstellar"})
        self.assertTrue(ctx.session.active)
        result = ctx.executor.execute(TOOL_MEDIA_CANCEL, {})
        self.assertTrue(result.ok)
        self.assertFalse(ctx.session.active)
        self.assertEqual(ctx.opener.opened, [])

    def test_launcher_error_is_reported(self):
        ctx = self.media_executor(
            ["Project Hail Mary.mkv"], opener_error=OSError("boom")
        )
        result = ctx.executor.execute(
            TOOL_MEDIA_PLAY, {"query": "Project Hail Mary"}
        )
        self.assertTrue(result.ok)
        self.assertIn("could not open", result.value["text"])

    def test_file_removed_before_launch_is_reported(self):
        ctx = self.media_executor(["Project Hail Mary.mkv"])
        (self.root / "Project Hail Mary.mkv").unlink()
        result = ctx.executor.execute(
            TOOL_MEDIA_PLAY, {"query": "Project Hail Mary"}
        )
        self.assertTrue(result.ok)
        self.assertIn("couldn't find", result.value["text"])
        self.assertEqual(ctx.opener.opened, [])

    def test_selection_outside_roots_is_refused(self):
        # Forge a session entry pointing outside the approved root and
        # confirm the guard re-validates containment before launching.
        from core.media import MediaMatch

        ctx = self.media_executor(["Interstellar.mp4"])
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_dir, outside)
        escape = outside / "Escape.mp4"
        escape.write_text("x", encoding="utf-8")
        ctx.session.offer(
            [MediaMatch(escape, "Escape.mp4", 1.0, True)]
        )
        result = ctx.executor.execute(TOOL_MEDIA_SELECT, {"index": 0})
        self.assertTrue(result.ok)
        self.assertIn("outside the approved folders", result.value["text"])
        self.assertEqual(ctx.opener.opened, [])


# ----------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------
class TestRouterMedia(MediaTestCase):
    def _router(self, *, media=True, research=False):
        registry = ToolRegistry()
        for spec in build_media_specs(
            MediaDiscovery([]), MediaSession(), SubprocessMediaLauncher()
        ):
            registry.register(spec)
        available = list(registry.names())
        if research:
            available.append("web_research")
        return ToolCommandRouter(
            (),
            available_tools=available,
            media_tools=_media_tools_map() if media else None,
            research_tool="web_research" if research else None,
        )

    def test_play_phrase_carries_title(self):
        command = self._router().route("play Project Hail Mary")
        self.assertEqual(command.tool_name, TOOL_MEDIA_PLAY)
        self.assertEqual(command.arguments["query"], "Project Hail Mary")

    def test_find_the_movie_phrase(self):
        command = self._router().route("find the movie Interstellar")
        self.assertEqual(command.tool_name, TOOL_MEDIA_PLAY)
        self.assertEqual(command.arguments["query"], "Interstellar")

    def test_selection_phrases(self):
        router = self._router()
        for text, index in [
            ("play the first one", 0),
            ("play the second one", 1),
            ("select the third", 2),
            ("the second one", 1),
            ("play number 3", 2),
        ]:
            command = router.route(text)
            self.assertEqual(command.tool_name, TOOL_MEDIA_SELECT, text)
            self.assertEqual(command.arguments["index"], index, text)

    def test_title_with_a_number_is_not_a_selection(self):
        command = self._router().route("play the 2 towers")
        self.assertEqual(command.tool_name, TOOL_MEDIA_PLAY)
        self.assertEqual(command.arguments["query"], "the 2 towers")

    def test_cancel_phrases(self):
        router = self._router()
        for text in ("cancel", "cancel that", "stop searching", "never mind"):
            command = router.route(text)
            self.assertEqual(command.tool_name, TOOL_MEDIA_CANCEL, text)

    def test_media_phrases_are_inert_without_media_tools(self):
        router = self._router(media=False)
        command = router.route("play Interstellar")
        self.assertNotEqual(command.tool_name, TOOL_MEDIA_PLAY)

    def test_research_question_is_not_hijacked(self):
        router = self._router(media=True, research=True)
        command = router.route("who directed Interstellar")
        self.assertEqual(command.tool_name, "web_research")

    def test_action_play_still_routes_to_media_not_research(self):
        router = self._router(media=True, research=True)
        command = router.route("play Interstellar")
        self.assertEqual(command.tool_name, TOOL_MEDIA_PLAY)

    def test_memory_save_phrase_never_becomes_playback(self):
        registry = ToolRegistry()
        for spec in build_media_specs(
            MediaDiscovery([]), MediaSession(), SubprocessMediaLauncher()
        ):
            registry.register(spec)
        router = ToolCommandRouter(
            (),
            available_tools=[*list(registry.names()), "memory_save"],
            media_tools=_media_tools_map(),
            memory_tools={"save": "memory_save"},
        )
        command = router.route("remember that Interstellar is my favourite")
        self.assertEqual(command.tool_name, "memory_save")
        self.assertNotEqual(command.tool_name, TOOL_MEDIA_PLAY)


# ----------------------------------------------------------------------
# Registry wiring
# ----------------------------------------------------------------------
class TestRegistryWiring(MediaTestCase):
    def _registry(self, *, tools=True, media=True, discovery=True, session=True):
        return _build_registry(
            ToolsConfig(enabled=tools),
            None,
            media_config=MediaConfig(enabled=media, roots=(str(self.root),)),
            media_discovery=MediaDiscovery([self.root]) if discovery else None,
            media_session=MediaSession() if session else None,
            media_launcher=SubprocessMediaLauncher(FakeOpener()),
        )

    def test_media_registered_when_enabled(self):
        names = self._registry().names()
        self.assertIn(TOOL_MEDIA_PLAY, names)
        self.assertIn(TOOL_MEDIA_SELECT, names)
        self.assertIn(TOOL_MEDIA_CANCEL, names)

    def test_media_not_registered_when_disabled(self):
        names = self._registry(media=False).names()
        self.assertNotIn(TOOL_MEDIA_PLAY, names)
        self.assertNotIn(TOOL_MEDIA_SELECT, names)
        self.assertNotIn(TOOL_MEDIA_CANCEL, names)

    def test_media_not_registered_without_tools(self):
        self.assertEqual(self._registry(tools=False).names(), ())

    def test_media_not_registered_without_discovery(self):
        self.assertNotIn(TOOL_MEDIA_PLAY, self._registry(discovery=False).names())

    def test_media_not_registered_without_session(self):
        self.assertNotIn(TOOL_MEDIA_PLAY, self._registry(session=False).names())


# ----------------------------------------------------------------------
# main() opt-in behaviour (no audio hardware touched)
# ----------------------------------------------------------------------
class TestMainOptIn(MediaTestCase):
    def _run(self, argv, texts):
        output = []

        def listener_factory(config):
            return FakeListener(texts)

        code = main(
            argv,
            listener_factory=listener_factory,
            speaker_factory=lambda config: FakeSpeaker(),
            output=output.append,
            media_discovery=MediaDiscovery([self.root]),
            media_session=MediaSession(),
            media_launcher=SubprocessMediaLauncher(FakeOpener()),
        )
        return code, output

    def test_media_requires_tools(self):
        _code, output = self._run(["--media"], ["play Interstellar", "exit"])
        self.assertIn(
            "Media playback requires --tools", "\n".join(output)
        )

    def test_help_documents_media_flag(self):
        output = []
        code = main(["--help"], output=output.append)
        self.assertEqual(code, 0)
        self.assertIn("--media", "\n".join(output))

    def test_media_off_by_default(self):
        _code, output = self._run(["--tools"], ["play Interstellar", "exit"])
        joined = "\n".join(output)
        self.assertNotIn("Playing", joined)

    def test_media_requires_roots(self):
        output = []

        def listener_factory(config):
            return FakeListener(["exit"])

        result = main(
            ["--tools", "--media"],
            listener_factory=listener_factory,
            speaker_factory=lambda config: FakeSpeaker(),
            output=output.append,
            media_config=MediaConfig(enabled=True, roots=()),
        )
        self.assertEqual(result, 0)
        self.assertIn(
            "at least one approved folder", "\n".join(output)
        )

    def test_unrelated_command_invalidates_pending_selection(self):
        # End-to-end through the real loop: a search that lists several
        # matches, then an unrelated tool command, must drop the pending
        # list so a later positional choice cannot launch anything.
        _make_tree(
            self.root, ["Interstellar Trailer.mp4", "Interstellar Extra.mp4"]
        )
        discovery = MediaDiscovery([self.root])
        session = MediaSession()
        opener = FakeOpener()
        registry = build_builtin_registry(None)
        for spec in build_media_specs(
            discovery, session, SubprocessMediaLauncher(opener)
        ):
            registry.register(spec)
        executor = ToolExecutor(
            registry,
            PermissionPolicy(),
            EchoProvider(),
            confirmations=ConfirmationRegistry(ttl=60.0),
        )
        router = ToolCommandRouter(
            (),
            available_tools=registry.names(),
            media_tools=_media_tools_map(),
        )
        output = []
        code = main(
            ["--tools", "--media"],
            router=router,
            executor=executor,
            listener_factory=lambda config: FakeListener(
                [
                    "play Interstellar",
                    "what time is it",
                    "play the second one",
                    "exit",
                ]
            ),
            speaker_factory=lambda config: FakeSpeaker(),
            output=output.append,
            media_config=MediaConfig(enabled=True, roots=(str(self.root),)),
            media_discovery=discovery,
            media_session=session,
            media_launcher=SubprocessMediaLauncher(opener),
        )
        joined = "\n".join(output).lower()
        self.assertEqual(code, 0)
        self.assertIn("several matches", joined)
        self.assertIn("no current list", joined)
        self.assertFalse(session.active)
        self.assertEqual(opener.opened, [])


if __name__ == "__main__":
    unittest.main()
