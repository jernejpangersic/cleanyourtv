"""tools/bench.py must not wipe the accessibility list, and must not warm the cold start.

Two problems, both found on a TCL BeyondTV2 (Android TV 9) while measuring the launcher:

1. `am force-stop` removes that package's services from `enabled_accessibility_services`, and they
   do not come back when the launcher starts again (#503 reported it on a TCL 55C715). The old
   `restore_accessibility` wrote each service on its own with
   `settings put secure enabled_accessibility_services <one service>`, which **replaces the whole
   list**: the launcher's other services and any other app's were wiped.

2. It restored the list *before* the cold-start measurement. Writing that setting makes the system
   bind the service, which starts the launcher's process — so the "cold start" measured a warm one.

3. It wrote the merged list back unquoted, so the TV's shell expanded `$`: an inner-class service
   such as `com.x/.Foo$Service`, ours or another app's, became `com.x/.Foo`.

The TV is a dict here: the point is the commands the tool would send.

Run from the repository root:
    python -m unittest tests.test_bench_accessibility -v
"""
import unittest
from unittest import mock

from tools import bench

OURS = "com.gathin.tvhome/.KeyRemapService"
OTHER = "com.exemplo.outroapp/.Servico"
INNER = "com.exemplo.outroapp/.Leitor$Servico"


def sh_word(text):
    """One word as `sh` reads it: quotes removed, unquoted and double-quoted `$name` expanded (to "")."""
    out, i, quote = [], 0, None
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                out.append(c)
        elif c == "\\" and i + 1 < len(text):
            i += 1
            out.append(text[i])
        elif c in "'\"" and quote is None:
            quote = c
        elif c == '"' and quote == '"':
            quote = None
        elif c == "$" and i + 1 < len(text) and (text[i + 1].isalnum() or text[i + 1] == "_"):
            i += 1
            while i + 1 < len(text) and (text[i + 1].isalnum() or text[i + 1] == "_"):
                i += 1
        else:
            out.append(c)
        i += 1
    return "".join(out)


class FakeTV:
    """Answers the two settings commands the tool uses, and keeps the value in memory."""

    def __init__(self, setting=""):
        self.setting = setting
        self.commands = []

    def shell(self, command, read_timeout_s=None):
        self.commands.append(command)
        if command.startswith("settings get secure enabled_accessibility_services"):
            return self.setting
        if command.startswith("settings put secure enabled_accessibility_services "):
            self.setting = sh_word(command.split(" ", 4)[4])
            return ""
        if command.startswith("am force-stop "):
            # This is what the TV does: the stopped package's services leave the list.
            package = command.split(" ", 2)[2]
            self.setting = ":".join(
                part for part in self.setting.split(":") if part and not part.startswith(package + "/"))
            return ""
        return ""

    def package_of(self, component):
        return component.split("/")[0]


class RestoreAccessibilityTest(unittest.TestCase):
    def test_another_apps_service_is_kept(self):
        tv = FakeTV("com.outro.novo/.Svc")
        bench.restore_accessibility(tv, "com.gathin.tvhome", [OURS])
        self.assertEqual(tv.setting, f"com.outro.novo/.Svc:{OURS}")

    def test_does_not_add_the_same_service_twice(self):
        tv = FakeTV(OURS)
        bench.restore_accessibility(tv, "com.gathin.tvhome", [OURS])
        self.assertEqual(tv.setting, OURS)
        self.assertEqual(len(tv.commands), 1, "nothing to write means no command at all")

    def test_short_and_long_component_forms_are_the_same_component(self):
        """The TV rewrites `pkg/pkg.Classe` as `pkg/.Classe`; restoring must not duplicate it."""
        tv = FakeTV("com.gathin.tvhome/com.gathin.tvhome.KeyRemapService")
        bench.restore_accessibility(tv, "com.gathin.tvhome", [OURS])
        self.assertEqual(tv.setting.count("KeyRemapService"), 1, tv.setting)

    def test_inner_class_service_survives_the_shell(self):
        """`$` in another app's service must reach `settings` as typed, not be expanded by `sh`."""
        tv = FakeTV(INNER)
        bench.restore_accessibility(tv, "com.gathin.tvhome", [OURS])
        self.assertEqual(tv.setting, f"{INNER}:{OURS}")

    def test_null_setting_is_treated_as_empty(self):
        tv = FakeTV("null")
        bench.restore_accessibility(tv, "com.gathin.tvhome", [OURS])
        self.assertEqual(tv.setting, OURS)


class ColdStartOrderTest(unittest.TestCase):
    def test_services_are_restored_after_the_measurement(self):
        """A `settings put` starts the process; doing it first would measure a warm start."""
        tv = FakeTV(OURS)
        order = []

        original_shell = tv.shell
        def spy(command, read_timeout_s=None):
            if command.startswith("settings put secure enabled_accessibility_services"):
                order.append("restore")
            elif command.startswith("am start -W ") and command.endswith("-n com.gathin.tvhome/.MainActivity"):
                order.append("measure")
            return original_shell(command, read_timeout_s)
        tv.shell = spy

        # `am start -W` has to answer something with a WaitTime.
        def shell_with_start(command, read_timeout_s=None):
            out = spy(command, read_timeout_s)
            if command.startswith("am start -W"):
                return "WaitTime: 420"
            return out
        tv.shell = shell_with_start

        with mock.patch.object(bench, "other_app", lambda _tv: "com.android.tv.settings/.MainSettings"):
            bench.cold_start(tv, "com.gathin.tvhome/.MainActivity", 1)

        self.assertIn("measure", order, order)
        self.assertIn("restore", order, order)
        self.assertLess(order.index("measure"), order.index("restore"),
                        f"restore happened before the measurement: {order}")


if __name__ == "__main__":
    unittest.main()
