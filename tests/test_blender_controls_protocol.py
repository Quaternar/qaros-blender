# SPDX-License-Identifier: MIT
"""The controls panel's protocol (qaros_anari/protocol.py), with plain Python: which page messages
are acted on, how edits coalesce between QAR's library thread and Blender's UI thread, and the
state message."""
import importlib.util
import json
from pathlib import Path
import threading
import unittest

path = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari" / "protocol.py"
spec = importlib.util.spec_from_file_location("protocol", path)
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)

USER = "00" * 16


def message(**fields):
    return json.dumps(fields)


class ParseTests(unittest.TestCase):
    def test_a_set_of_a_vector_component(self):
        edit = protocol.parse(message(t="set", key="location", index=2, value=1.5, final=True), USER)
        self.assertEqual(edit, protocol.Set("location", 2, 1.5, True, USER))

    def test_a_scalar_ignores_its_index_and_final_defaults_off(self):
        edit = protocol.parse(message(t="set", key="roughness", index=7, value=0.25), USER)
        self.assertEqual(edit, protocol.Set("roughness", 0, 0.25, False, USER))

    def test_values_are_held_to_their_limits(self):
        self.assertEqual(protocol.parse(message(t="set", key="scale", index=0, value=0), USER).value, 1.0e-3)
        self.assertEqual(protocol.parse(message(t="set", key="metallic", value=4), USER).value, 1.0)
        self.assertEqual(protocol.parse(message(t="set", key="color", index=1, value=-2), USER).value, 0.0)

    def test_hello_and_calls(self):
        self.assertEqual(protocol.parse(message(t="hello"), USER), protocol.Hello(USER))
        self.assertEqual(protocol.parse(message(t="call", id=3, name="undo"), USER), protocol.Call(3, "undo", USER))

    def test_a_tool_names_one_of_the_tools(self):
        self.assertEqual(protocol.parse(message(t="tool", value="SELECT"), USER), protocol.ToolSet("SELECT", USER))
        for value in ("select", "AUTO", None, 1):
            with self.subTest(value=value), self.assertRaises(protocol.Refused):
                protocol.parse(message(t="tool", value=value), USER)

    def test_anything_else_is_refused(self):
        refused = [
            "not json",
            "[1, 2]",
            message(t="exec", code="import os"),
            message(t="call", id=1, name="quit"),
            message(t="call", id="1", name="undo"),
            message(t="call", id=True, name="undo"),
            message(t="set", key="name", value=1),
            message(t="set", key="location", index=3, value=1),
            message(t="set", key="location", index=True, value=1),
            message(t="set", key="location", index=0, value="1"),
            message(t="set", key="location", index=0, value=True),
            '{"t": "set", "key": "location", "index": 0, "value": NaN}',
            '{"t": "set", "key": "location", "index": 0, "value": Infinity}',
            message(t="set", key="location", index=0, value=1, final="yes"),
            "x" * (protocol.MAX_MESSAGE_BYTES + 1),
        ]
        for text in refused:
            with self.subTest(text=text[:60]):
                with self.assertRaises(protocol.Refused):
                    protocol.parse(text, USER)


class EditQueueTests(unittest.TestCase):
    def test_a_drag_coalesces_to_its_newest_value_and_stays_final(self):
        queue = protocol.EditQueue()
        queue.put(protocol.Set("location", 0, 1.0, False, USER))
        queue.put(protocol.Set("location", 0, 2.0, True, USER))
        queue.put(protocol.Set("location", 0, 3.0, False, USER))
        sets, others = queue.drain()
        self.assertEqual(sets, [protocol.Set("location", 0, 3.0, True, USER)])
        self.assertEqual(others, [])

    def test_components_and_keys_stay_apart_in_first_moved_order(self):
        queue = protocol.EditQueue()
        queue.put(protocol.Set("scale", 1, 2.0, False, USER))
        queue.put(protocol.Set("location", 0, 1.0, False, USER))
        queue.put(protocol.Set("scale", 0, 3.0, False, USER))
        sets, _ = queue.drain()
        self.assertEqual([(s.key, s.index) for s in sets], [("scale", 1), ("location", 0), ("scale", 0)])

    def test_calls_keep_their_order_and_a_drain_empties_the_queue(self):
        queue = protocol.EditQueue()
        queue.put(protocol.Call(1, "undo", USER))
        queue.put(protocol.Hello(USER))
        queue.put(protocol.Call(2, "redo", USER))
        _, others = queue.drain()
        self.assertEqual(others, [protocol.Call(1, "undo", USER), protocol.Hello(USER), protocol.Call(2, "redo", USER)])
        self.assertEqual(queue.drain(), ([], []))

    def test_puts_from_another_thread_are_all_drained(self):
        queue = protocol.EditQueue()

        def fill(start):
            for i in range(500):
                queue.put(protocol.Call(start + i, "undo", USER))

        threads = [threading.Thread(target=fill, args=(n * 1000,)) for n in range(4)]
        for thread in threads:
            thread.start()
        drained = []
        while any(thread.is_alive() for thread in threads) or not drained or len(drained) < 2000:
            drained.extend(queue.drain()[1])
            if not any(thread.is_alive() for thread in threads) and len(drained) >= 2000:
                break
        for thread in threads:
            thread.join()
        drained.extend(queue.drain()[1])
        self.assertEqual(len(drained), 2000)


class PropTests(unittest.TestCase):
    LOCATION = {"id": "objects:Cube:location", "type": "float", "length": 3, "index": -1, "readonly": False,
                "min": None, "max": None}
    SCALE_Y = {"id": "objects:Cube:scale", "type": "float", "length": 3, "index": 1, "readonly": False,
               "min": -1.0e6, "max": 1.0e6}
    COUNT = {"id": "scenes:Scene:frame_current", "type": "int", "length": 0, "index": -1, "readonly": False,
             "min": 0, "max": 1000}
    MODE = {"id": "objects:Cube:rotation_mode", "type": "enum", "length": 0, "index": -1, "readonly": False,
            "items": [["XYZ", "XYZ Euler"], ["QUATERNION", "Quaternion (WXYZ)"]]}
    HIDE = {"id": "objects:Cube:hide_render", "type": "boolean", "length": 0, "index": -1, "readonly": False}

    def test_parse_keeps_the_raw_value_for_the_ui_thread_to_check(self):
        edit = protocol.parse(message(t="prop", id="objects:Cube:location", index=1, value=2, final=True), USER)
        self.assertEqual(edit, protocol.PropSet("objects:Cube:location", 1, 2, True, USER))

    def test_parse_refuses_malformed_props(self):
        for fields in (dict(id=3, value=1), dict(id="", value=1), dict(id="x" * 513, value=1),
                       dict(id="a", index=64, value=1), dict(id="a", value=[1]), dict(id="a", value=None)):
            with self.subTest(fields=fields), self.assertRaises(protocol.Refused):
                protocol.parse(message(t="prop", **fields), USER)

    def test_numbers_are_finite_held_to_limits_and_rounded_for_ints(self):
        self.assertEqual(protocol.coerce_prop(self.LOCATION, 2, 1.5), 1.5)
        self.assertEqual(protocol.coerce_prop(self.COUNT, -1, 2000), 1000)
        self.assertEqual(protocol.coerce_prop(self.COUNT, -1, 3.6), 4)
        for bad in ("1", True, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(protocol.Refused):
                protocol.coerce_prop(self.LOCATION, 0, bad)

    def test_a_vector_needs_a_component_unless_the_field_is_one(self):
        with self.assertRaises(protocol.Refused):
            protocol.coerce_prop(self.LOCATION, -1, 1.0)
        with self.assertRaises(protocol.Refused):
            protocol.coerce_prop(self.LOCATION, 3, 1.0)
        self.assertEqual(protocol.coerce_prop(self.SCALE_Y, -1, 2.0), 2.0)

    def test_enums_and_booleans_take_only_their_own_values(self):
        self.assertEqual(protocol.coerce_prop(self.MODE, -1, "QUATERNION"), "QUATERNION")
        self.assertTrue(protocol.coerce_prop(self.HIDE, -1, True))
        for field, bad in ((self.MODE, "SIDEWAYS"), (self.MODE, 1), (self.HIDE, 1), (self.HIDE, "true")):
            with self.subTest(bad=bad), self.assertRaises(protocol.Refused):
                protocol.coerce_prop(field, -1, bad)

    NAME = {"id": "objects:Cube:name", "type": "string", "length": 0, "index": -1, "readonly": False, "maxlen": 9}
    PARENT = {"id": "objects:Cube:parent", "type": "pointer", "length": 0, "index": -1, "readonly": False,
              "maxlen": 255, "collection": "objects", "choices": ["Camera", "Light"], "more": False}

    def test_text_is_held_to_its_length_and_refuses_control_characters(self):
        self.assertEqual(protocol.coerce_prop(self.NAME, -1, "Kocka"), "Kocka")
        self.assertEqual(protocol.coerce_prop(self.NAME, -1, "Kocka ✓"), "Kocka ✓", "9 UTF-8 bytes")
        for bad in ("Kocka ✓✓", "a\nb", "a\x7fb", 3, None, True):
            with self.subTest(bad=bad), self.assertRaises(protocol.Refused):
                protocol.coerce_prop(self.NAME, -1, bad)

    def test_a_link_takes_one_of_its_choices_or_none(self):
        self.assertEqual(protocol.coerce_prop(self.PARENT, -1, "Camera"), "Camera")
        self.assertEqual(protocol.coerce_prop(self.PARENT, -1, ""), "")
        with self.assertRaises(protocol.Refused):
            protocol.coerce_prop(self.PARENT, -1, "Nothing")
        cut_short = dict(self.PARENT, more=True)
        self.assertEqual(protocol.coerce_prop(cut_short, -1, "Nothing"), "Nothing", "the UI thread looks it up")

    def test_read_only_fields_are_refused(self):
        with self.assertRaises(protocol.Refused):
            protocol.coerce_prop(dict(self.HIDE, readonly=True), -1, True)

    def test_prop_edits_coalesce_per_field_and_component(self):
        queue = protocol.EditQueue()
        queue.put(protocol.PropSet("objects:Cube:location", 0, 1.0, True, USER))
        queue.put(protocol.PropSet("objects:Cube:location", 0, 2.0, False, USER))
        queue.put(protocol.PropSet("objects:Cube:location", 1, 3.0, False, USER))
        queue.put(protocol.Set("location", 0, 4.0, False, USER))
        sets, _ = queue.drain()
        self.assertEqual(sets, [
            protocol.PropSet("objects:Cube:location", 0, 2.0, True, USER),
            protocol.PropSet("objects:Cube:location", 1, 3.0, False, USER),
            protocol.Set("location", 0, 4.0, False, USER),
        ])


class SettingsTests(unittest.TestCase):
    def test_a_hidden_tab_refuses_its_edits_and_calls(self):
        tabs = ("material", "panels")
        self.assertFalse(protocol.shown(protocol.Set("location", 0, 1.0, False, USER), tabs))
        self.assertTrue(protocol.shown(protocol.Set("roughness", 0, 0.5, False, USER), tabs))
        self.assertFalse(protocol.shown(protocol.Call(1, "play", USER), tabs), "Play is on the Frame tab")
        self.assertTrue(protocol.shown(protocol.Call(2, "undo", USER), tabs), "undo is in the header")
        self.assertTrue(protocol.shown(protocol.PropSet("objects:Cube:name", -1, "A", True, USER), tabs))
        self.assertFalse(protocol.shown(protocol.PropSet("objects:Cube:name", -1, "A", True, USER), ("object",)))
        self.assertTrue(protocol.shown(protocol.Hello(USER), ()))
        self.assertTrue(protocol.shown(protocol.ToolSet("SELECT", USER), ("tools",)))
        self.assertFalse(protocol.shown(protocol.ToolSet("SELECT", USER), tabs), "the Tools tab is off")

    def test_the_tools_tab_comes_first(self):
        self.assertEqual(protocol.TABS[0], "tools")

    def test_the_panel_list_keeps_its_order_and_toggles_at_the_end(self):
        self.assertEqual(protocol.panel_names(" A, B,,A ,C"), ("A", "B", "C"))
        self.assertEqual(protocol.toggled_panel("A,B,C", "B"), "A,C")
        self.assertEqual(protocol.toggled_panel("A,C", "B"), "A,C,B")
        self.assertEqual(protocol.toggled_panel("", "A"), "A")

    def test_panels_that_do_not_fit_one_message_are_left_out_from_the_end(self):
        def panel(name):
            return {"name": name, "title": name, "items": [{"kind": "note", "text": "x" * 300}]}

        panels = [panel(f"P{i}") for i in range(10)]
        text, kept = protocol.panels_message(panels)
        self.assertEqual(kept, panels, "small enough: all of them")
        text, kept = protocol.panels_message(panels, limit=2000)
        self.assertLessEqual(len(text.encode("utf-8")), 2000)
        self.assertEqual(kept, panels[: len(kept)])
        self.assertLess(len(kept), 10)
        self.assertIn(f"{10 - len(kept)} panel(s) left out", text)


class MessageTests(unittest.TestCase):
    def test_state_rounds_floats_so_an_unchanged_value_posts_the_same_text(self):
        a = protocol.state_message({"object": {"location": [1.000001, 2.0, 3.0]}})
        b = protocol.state_message({"object": {"location": [1.0000004, 2.0, 3.0]}})
        self.assertEqual(a, b)
        self.assertEqual(json.loads(a), {"t": "state", "key": "scene", "value": {"object": {"location": [1.0, 2.0, 3.0]}}})

    def test_result_carries_the_call_id(self):
        call = protocol.Call(7, "undo", USER)
        self.assertEqual(json.loads(protocol.result_message(call, True)), {"t": "result", "id": 7, "ok": True})
        self.assertEqual(
            json.loads(protocol.result_message(call, False, "no window")),
            {"t": "result", "id": 7, "ok": False, "error": "no window"},
        )


if __name__ == "__main__":
    unittest.main()
