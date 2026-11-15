# SPDX-License-Identifier: MIT
"""Which QAROS devices the add-on streams to (qaros_anari/targets.py), with plain Python."""
import importlib.util
import json
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari" / "targets.py"
spec = importlib.util.spec_from_file_location("targets", path)
targets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(targets)

A = "0123456789abcdef0123456789abcdef"
B = "fedcba9876543210fedcba9876543210"
C = "11111111222222223333333344444444"


def session(*items):
    return targets.parse_session_targets(json.dumps(items))


def device(peer_id, name, online=True, streaming=False):
    return {"id": peer_id, "name": name, "online": online, "selected": True, "streaming": streaming}


class ParseTests(unittest.TestCase):
    def test_the_device_list_is_read(self):
        listed = session(device(A, "Display Receiver 1", streaming=True), device(B, "Quest", online=False))
        self.assertEqual([t.peer_id for t in listed], [A, B])
        self.assertEqual(listed[0].name, "Display Receiver 1")
        self.assertTrue(listed[0].online and listed[0].streaming)
        self.assertFalse(listed[1].online)

    def test_no_answer_or_garbage_is_none_and_bad_entries_are_skipped(self):
        self.assertIsNone(targets.parse_session_targets(None))
        self.assertIsNone(targets.parse_session_targets("not json"))
        self.assertIsNone(targets.parse_session_targets("{}"))
        self.assertEqual(targets.parse_session_targets("[]"), [])
        listed = targets.parse_session_targets(json.dumps([{"name": "no id"}, 3, device(A.upper(), "x")]))
        self.assertEqual([t.peer_id for t in listed], [A], "ids are compared in lower case")


class ChoiceTests(unittest.TestCase):
    def test_the_default_streams_to_every_device_and_sets_nothing(self):
        choice = targets.TargetChoice.from_scene(True, "")
        self.assertTrue(choice.stream_to_all)
        self.assertIsNone(choice.parameter(), "qar.targets stays unset: every device, as before")
        self.assertTrue(choice.is_ticked(A))

    def test_the_scene_text_round_trips_and_damage_is_survived(self):
        choice = targets.TargetChoice(False, {B: "Quest", A: "Display Receiver 1"})
        again = targets.TargetChoice.from_scene(False, choice.selected_text())
        self.assertEqual(again.selected, choice.selected)
        self.assertEqual(again.parameter(), f"{A},{B}")
        self.assertEqual(targets.TargetChoice.from_scene(False, "{broken").selected, {})
        self.assertEqual(targets.TargetChoice.from_scene(False, "[1, 2]").selected, {})

    def test_no_device_selected_streams_to_nobody(self):
        self.assertEqual(targets.TargetChoice(False, {}).parameter(), "")

    def test_unticking_all_keeps_every_connected_device(self):
        listed = session(device(A, "R1"), device(B, "R2"), device(C, "Gone", online=False))
        choice = targets.set_all(targets.TargetChoice(), False, listed)
        self.assertFalse(choice.stream_to_all)
        self.assertEqual(choice.selected, {A: "R1", B: "R2"}, "nothing changes until one is unticked")

    def test_unticking_all_keeps_an_earlier_selection(self):
        earlier = targets.TargetChoice(True, {C: "Quest"})
        choice = targets.set_all(earlier, False, session(device(A, "R1")))
        self.assertEqual(choice.selected, {C: "Quest"})
        self.assertTrue(targets.set_all(choice, True, None).stream_to_all)

    def test_unticking_one_device_while_all_is_on_keeps_the_others(self):
        listed = session(device(A, "R1"), device(B, "R2"))
        choice = targets.set_target(targets.TargetChoice(), B, False, listed)
        self.assertFalse(choice.stream_to_all)
        self.assertEqual(choice.parameter(), A)

    def test_ticking_and_unticking_a_device(self):
        listed = session(device(A, "R1"), device(B, "R2"))
        choice = targets.set_target(targets.TargetChoice(False, {}), B, True, listed)
        self.assertEqual(choice.selected, {B: "R2"})
        self.assertEqual(targets.set_target(choice, B, False, listed).parameter(), "")

    def test_names_are_remembered_for_a_device_that_leaves(self):
        choice = targets.TargetChoice(False, {A: ""})
        named = targets.remember_names(choice, session(device(A, "Display Receiver 1")))
        self.assertEqual(named.selected[A], "Display Receiver 1")
        self.assertIs(targets.remember_names(named, session(device(A, "Display Receiver 1"))), named)


class RowTests(unittest.TestCase):
    def test_all_lists_the_connected_devices_ticked(self):
        listed = session(device(B, "Receiver 2"), device(A, "Receiver 1", streaming=True), device(C, "Gone", online=False))
        shown = targets.rows(targets.TargetChoice(), listed)
        self.assertEqual([r.label for r in shown], ["Receiver 1", "Receiver 2"])
        self.assertTrue(all(r.ticked for r in shown))
        self.assertTrue(shown[0].streaming)

    def test_a_chosen_device_that_left_stays_listed_after_the_connected_ones(self):
        listed = session(device(A, "Receiver 1"), device(C, "Quest", online=False))
        choice = targets.TargetChoice(False, {C: "", B: "Old headset"})
        shown = targets.rows(choice, listed)
        self.assertEqual(
            [(r.label, r.ticked, r.online) for r in shown],
            [("Receiver 1", False, True), ("Old headset (not connected)", True, False), ("Quest (not connected)", True, False)],
        )

    def test_an_unnamed_device_shows_its_short_id(self):
        shown = targets.rows(targets.TargetChoice(), session(device(A, "")))
        self.assertEqual(shown[0].label, "Device 01234567")

    def test_before_streaming_only_the_saved_choice_is_listed(self):
        self.assertEqual(targets.rows(targets.TargetChoice(), None), [])
        shown = targets.rows(targets.TargetChoice(False, {A: "Receiver 1"}), None)
        self.assertEqual([r.label for r in shown], ["Receiver 1 (not connected)"])


class SummaryTests(unittest.TestCase):
    def test_summaries(self):
        listed = session(device(A, "R1"), device(B, "R2"))
        self.assertEqual(targets.summary(targets.TargetChoice(), listed), "All devices")
        self.assertEqual(targets.summary(targets.TargetChoice(False, {}), listed), "No device: nobody receives the stream")
        self.assertEqual(targets.summary(targets.TargetChoice(False, {A: "", C: ""}), listed), "2 selected devices, 1 connected")
        self.assertEqual(targets.summary(targets.TargetChoice(False, {A: ""}), None), "1 selected device")

    def test_more_devices_than_users_is_warned(self):
        listed = session(device(A, "R1"), device(B, "R2"), device(C, "R3"))
        self.assertEqual(
            targets.over_limit(targets.TargetChoice(), listed, 2), "Only 2 of 3 devices get the stream (Preferences: Users)"
        )
        self.assertIsNone(targets.over_limit(targets.TargetChoice(False, {A: ""}), listed, 2))
        self.assertIsNone(targets.over_limit(targets.TargetChoice(), None, 2))


if __name__ == "__main__":
    unittest.main()
