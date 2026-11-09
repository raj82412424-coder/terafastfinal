import unittest

from utils.formatting import bar, eta_label, human, progress_line, safe_name, short


class FormattingTest(unittest.TestCase):
    def test_human(self):
        self.assertEqual(human(0), "0 B")
        self.assertEqual(human(1024), "1.0 KB")
        self.assertEqual(human(1024 * 1024), "1.0 MB")
        self.assertEqual(human(1536), "1.5 KB")

    def test_bar(self):
        self.assertEqual(bar(0, 100), "[░░░░░░░░░░░░] 0%")
        self.assertEqual(bar(50, 100), "[██████░░░░░░] 50%")
        self.assertEqual(bar(100, 100), "[████████████] 100%")
        self.assertEqual(bar(0, 0), "[░░░░░░░░░░░░] 0%")

    def test_eta(self):
        self.assertEqual(eta_label(100, 100, 10), "ETA: done")
        self.assertEqual(eta_label(0, 0, 10), "ETA: --")
        self.assertIn("ETA:", eta_label(0, 100, 10))

    def test_progress_line(self):
        line = progress_line(50, 100, 1024 * 1024)
        self.assertIn("50%", line)
        self.assertIn("1.0 MB/s", line)
        self.assertIn("ETA:", line)

    def test_safe_name(self):
        self.assertEqual(safe_name("a/b:c.mp4"), "a_b_c.mp4")
        self.assertTrue(safe_name("").startswith("file_"))
        self.assertLessEqual(len(safe_name("x" * 500).encode("utf-8")), 120)

    def test_short(self):
        self.assertEqual(short("hello", 10), "hello")
        self.assertTrue(short("x" * 50, 10).endswith("…"))


if __name__ == "__main__":
    unittest.main()
