import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import receipt


class ReceiptTest(unittest.TestCase):
    def test_total_adds_each_price(self):
        self.assertEqual(receipt.total("coffee 3.50\nbagel 2.25\n"), 575)

    def test_total_skips_blank_lines_and_comments(self):
        self.assertEqual(receipt.total("# lunch\n\nsoup 4.00\n"), 400)

    def test_main_reads_a_file_and_prints_the_total(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "receipt.txt"
            path.write_text("tea 1.25\ncake 3.00\n", encoding="utf-8")
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                self.assertEqual(receipt.main([str(path)]), 0)
        self.assertEqual(said.getvalue(), "Total: $4.25\n")


if __name__ == "__main__":
    unittest.main()
