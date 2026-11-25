"""Tâche 16 : des bulles qui défilent à la molette (plan : docs/superpowers/plans/2026-11-22-tache-16-bulles-defilantes.md)."""

import unittest
from contextlib import closing

from samdario import scene as scene
from samdario import scene_scroll as scroll
from tests.test_state import Base


class ReaderTest(unittest.TestCase):
    def test_wheel_events_are_read_and_the_rest_is_dropped(self):
        reader = scroll.Reader()
        data = b"Bonjour\x1b[<0;5;5M\x1b[<0;5;5m\x1b[<64;12;3M\x1b[A\x1b[<65;90;4M\x1b[<35;7;7M"
        self.assertEqual(reader.feed(data), [scroll.Wheel(-1, 12), scroll.Wheel(1, 90)])

    def test_event_split_across_two_reads_is_kept(self):
        reader = scroll.Reader()
        self.assertEqual(reader.feed(b"\x1b[<65;1"), [])
        self.assertEqual(reader.feed(b"0;5M"), [scroll.Wheel(1, 10)])


class ScrollTest(unittest.TestCase):
    LIMITS = {"claude": 100, "codex": 100}

    def test_wheel_over_each_half_moves_its_own_bubble(self):
        state = scroll.Scroll({"claude": 0, "codex": 0}, {})
        state = scroll.apply(state, scroll.Wheel(1, 20), 110, self.LIMITS)
        self.assertEqual(state.offsets, {"claude": scroll.STEP, "codex": 0})
        state = scroll.apply(state, scroll.Wheel(1, 90), 110, self.LIMITS)
        self.assertEqual(state.offsets, {"claude": scroll.STEP, "codex": scroll.STEP})

    def test_extra_wheel_steps_do_not_accumulate(self):
        limits = {"claude": 16, "codex": 0}  # 20 lignes, 4 visibles : la dernière position vaut 16
        state = scroll.Scroll({"claude": 0, "codex": 0}, {})
        for _ in range(16):
            state = scroll.apply(state, scroll.Wheel(1, 10), 110, limits)
        self.assertEqual(state.offsets["claude"], 16)
        state = scroll.apply(state, scroll.Wheel(-1, 10), 110, limits)
        self.assertEqual(state.offsets["claude"], 13)
        for _ in range(20):
            state = scroll.apply(state, scroll.Wheel(-1, 10), 110, limits)
        self.assertEqual(state.offsets["claude"], 0)
        self.assertEqual(scroll.apply(state, scroll.Wheel(1, 10), 110, limits).offsets["claude"], 3)

    def test_resize_that_shows_everything_then_back(self):
        state = scroll.Scroll({"claude": 12, "codex": 7}, {})
        bigger = scroll.fit(state, {"claude": 0, "codex": 40})  # tout le texte de Claude est visible
        self.assertEqual(bigger.offsets, {"claude": 0, "codex": 7})
        back = scroll.fit(bigger, {"claude": 16, "codex": 40})
        self.assertEqual(back.offsets, {"claude": 0, "codex": 7})
        self.assertEqual(scroll.fit(scroll.Scroll({"claude": 30, "codex": 7}, {}), {"claude": 16, "codex": 40}).offsets,
                         {"claude": 16, "codex": 7})

    def test_new_reply_resets_its_bubble_only(self):
        state = scroll.Scroll({"claude": 9, "codex": 6}, {"claude": 4, "codex": 5})
        followed = scroll.follow(state, {"claude": 7, "codex": 5})
        self.assertEqual(followed.offsets, {"claude": 0, "codex": 6})
        self.assertEqual(followed.ids, {"claude": 7, "codex": 5})

    def test_cache_keeps_lines_by_reply_and_width(self):
        cache = scroll.Cache()
        calls = []

        def wrap(text, width):
            calls.append(width)
            return text.split()

        first = cache.lines("claude", 3, "a b c", 20, wrap)
        self.assertEqual(cache.lines("claude", 3, "a b c", 20, wrap), first)
        cache.lines("claude", 3, "a b c", 30, wrap)
        self.assertEqual(calls, [20, 30])


class IdentityTest(Base):
    def test_identical_new_reply_resets_only_its_bubble(self):
        self.opening()
        long_text = "\n".join(f"ligne {n}" for n in range(80))
        first = self.turn("claude", "Question 1", long_text)
        with closing(scene.read_only(self.state)) as db:
            data = scene.scene_data(db)
        state = scroll.follow(scroll.Scroll({"claude": 0, "codex": 0}, {}), data.ids)
        state = scroll.Scroll({"claude": 20, "codex": 5}, state.ids)
        second = self.turn("claude", "Question 2", long_text)  # même texte, nouvel identifiant
        self.assertNotEqual(first, second)
        with closing(scene.read_only(self.state)) as db:
            again = scene.scene_data(db)
        self.assertEqual(again.texts["claude"], data.texts["claude"])
        self.assertEqual(scroll.follow(state, again.ids).offsets, {"claude": 0, "codex": 5})
        moved = again._replace(activity={"claude": "rédige sa réponse"})
        self.assertEqual(scroll.follow(scroll.follow(state, again.ids), moved.ids).offsets, {"claude": 0, "codex": 5})


if __name__ == "__main__":
    unittest.main()
