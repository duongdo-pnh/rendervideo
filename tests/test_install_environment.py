import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from install import environment, preflight, prepare_musetalk, resolve_profile, verify_engines


class EnvironmentTests(unittest.TestCase):
    def test_requires_receipt_and_every_nonempty_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in environment.REQUIRED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'model')
            self.assertFalse(environment.ready(root))
            (root / '.environment-ready.json').write_text(json.dumps({'schema': 1, 'engines': ['latentsync', 'musetalk']}))
            self.assertTrue(environment.ready(root))
            for name in environment.REQUIRED:
                with self.subTest(name=name):
                    path = root / name
                    path.write_bytes(b'')
                    self.assertFalse(environment.ready(root))
                    path.write_bytes(b'model')
            (root / '.environment-ready.json').write_text('{broken')
            self.assertFalse(environment.ready(root))

    def test_unvalidated_or_unknown_gpu_produces_no_shell_assignments(self):
        for arch, expected in [('sm_86', 0), ('sm_89', 1), ('sm_120', 1), ('sm_99', 1)]:
            out = io.StringIO()
            with patch.object(resolve_profile, 'detect', return_value={'primary': {'sm_arch': arch, 'name': 'Test GPU'}}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(resolve_profile.main(), expected)
            if expected:
                self.assertEqual(out.getvalue(), '')

    def test_disk_space_rejected_before_downloads(self):
        with patch.object(preflight.shutil, 'disk_usage') as disk:
            disk.return_value.free = 59 * preflight.GIB
            with self.assertRaisesRegex(RuntimeError, 'Không đủ dung lượng'):
                preflight.check_space()
            disk.return_value.free = 100 * preflight.GIB
            preflight.check_space()

    def test_detects_nvidia_display_hardware_without_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / '0000:01:00.0'
            device.mkdir()
            (device / 'vendor').write_text('0x10de\n')
            (device / 'class').write_text('0x030000\n')
            self.assertTrue(preflight.has_nvidia(root))
            (device / 'class').write_text('0x040300\n')
            self.assertFalse(preflight.has_nvidia(root))

    def test_render_failure_does_not_write_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(verify_engines, 'ROOT', root), patch.object(environment, 'missing_files', return_value=[]), patch.object(verify_engines.os, 'chdir'), patch.object(verify_engines, 'run', side_effect=RuntimeError('backend import failed')):
                with self.assertRaisesRegex(RuntimeError, 'backend import failed'):
                    verify_engines.verify()
            self.assertFalse((root / '.environment-ready.json').exists())

    def test_both_render_outputs_must_have_audio_and_video(self):
        for valid in [True, False]:
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'logs').mkdir()
                good = json.dumps({'streams': [{'codec_type': 'video'}, {'codec_type': 'audio'}], 'format': {'duration': '1'}})
                bad = json.dumps({'streams': [{'codec_type': 'video'}], 'format': {'duration': '1'}})
                with patch.object(verify_engines, 'ROOT', root), patch.object(environment, 'missing_files', return_value=[]), patch.object(verify_engines.os, 'chdir'), patch.object(verify_engines, 'run'), patch.object(verify_engines.subprocess, 'check_output', side_effect=[good, good if valid else bad]):
                    if valid:
                        verify_engines.verify()
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'musetalk produced an invalid render'):
                            verify_engines.verify()
                self.assertEqual((root / '.environment-ready.json').exists(), valid)

    def test_existing_musetalk_revision_is_never_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'install').mkdir()
            (root / 'engines/MuseTalk').mkdir(parents=True)
            (root / 'install/musetalk-source.json').write_text(json.dumps({'commit': 'required', 'repository': 'unused'}))
            with patch.object(prepare_musetalk.subprocess, 'check_output', return_value='different'), patch.object(prepare_musetalk.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'differs'):
                    prepare_musetalk.prepare(root)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
