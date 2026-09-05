import unittest

import widget


class WidgetTests(unittest.TestCase):
    def test_health(self):
        self.assertEqual(widget.health(), {"status": "ok"})

    def test_list(self):
        self.assertEqual(widget.list_widgets(), ["anvil", "bobbin", "cog"])


if __name__ == "__main__":
    unittest.main()
