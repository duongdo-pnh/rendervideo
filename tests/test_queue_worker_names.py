import unittest

from queue_worker import _drive_safe_name


class DriveFilenameTests(unittest.TestCase):
    def test_special_characters_become_single_underscores(self):
        self.assertEqual(
            _drive_safe_name("POC Booster 250ml_500ml – Hasil 3X & Pelebat!.mp4"),
            "POC_Booster_250ml_500ml_Hasil_3X_Pelebat.mp4",
        )

    def test_vietnamese_marks_are_removed(self):
        self.assertEqual(
            _drive_safe_name("Sản phẩm mới: phục hồi lá.mp4"),
            "San_pham_moi_phuc_hoi_la.mp4",
        )

    def test_empty_name_has_safe_fallback(self):
        self.assertEqual(_drive_safe_name("!!!.mp4"), "video.mp4")


if __name__ == "__main__":
    unittest.main()
