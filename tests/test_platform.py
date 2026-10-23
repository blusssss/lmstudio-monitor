import ctypes
import io
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import platform_support as platform
from server import Monitor, Store


class PlatformTests(unittest.TestCase):
    def test_bionic_sdk_port_is_discovered_from_its_private_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            internal = home / '.lmstudio' / 'apps' / 'bionic' / '.internal'
            internal.mkdir(parents=True)
            (internal / 'http-server.json').write_text(json.dumps({'port':64488}),encoding='utf-8')
            with patch.object(Path,'home',return_value=home), patch.object(platform.urllib.request,'urlopen',return_value=io.BytesIO(b'{"lmstudio":true}')) as request:
                self.assertEqual(platform.lms_connection_args(),['--port','64488'])
                self.assertIn(':64488/',request.call_args.args[0])

    def test_invalid_or_stale_sdk_records_fall_back_to_cli_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            internal = home / '.lmstudio' / '.internal'
            internal.mkdir(parents=True)
            with patch.object(Path,'home',return_value=home), patch.object(platform.urllib.request,'urlopen',side_effect=OSError('offline')):
                for value in ({'port':12345},{'port':True},{'port':0},[],{'port':'1234'}):
                    (internal / 'http-server.json').write_text(json.dumps(value),encoding='utf-8')
                    self.assertEqual(platform.lms_connection_args(),[])

    def test_explicit_sdk_port_skips_discovery(self):
        with patch.object(Path,'home',side_effect=AssertionError('discovery called')):
            self.assertEqual(platform.lms_connection_args(5555),['--port','5555'])

    def test_bionic_cli_home_is_isolated_without_copying_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)
            bionic=home/'.lmstudio/apps/bionic'
            (bionic/'.internal').mkdir(parents=True)
            (bionic/'.internal/http-server.json').write_text('{"port":64488}',encoding='utf-8')
            original=os.environ.get('USERPROFILE')
            with patch.object(Path,'home',return_value=home), platform.lms_environment(['--port','64488']) as env:
                isolated=Path(env['USERPROFILE'])
                self.assertEqual((isolated/'.lmstudio-home-pointer').read_text(encoding='utf-8'),str(bionic))
                self.assertEqual(list(isolated.iterdir()),[isolated/'.lmstudio-home-pointer'])
                self.assertEqual(os.environ.get('USERPROFILE'),original)
            self.assertFalse(isolated.exists())

    def test_lms_override_and_default_install_with_spaces(self):
        with patch.dict(os.environ, {'LMS_BIN': r'C:\My Tools\lms.exe'}):
            self.assertEqual(platform.find_lms(), r'C:\My Tools\lms.exe')
        with tempfile.TemporaryDirectory(prefix='lm studio ') as folder:
            home = Path(folder)
            binary = home / '.lmstudio' / 'bin' / 'lms.exe'
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home', return_value=home):
                self.assertEqual(platform.find_lms(), str(binary))

    def test_lms_path_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home', return_value=Path.cwd()), patch.object(Path, 'is_file', return_value=False), patch.object(platform.shutil, 'which', return_value='found-lms'):
            self.assertEqual(platform.find_lms(), 'found-lms')

    def test_windows_structure_sizes_and_high_ticks(self):
        self.assertEqual(ctypes.sizeof(platform.MemoryStatus), 64)
        self.assertEqual(platform.FileTime(1, 2).ticks, (2 << 32) + 1)

    def test_cpu_delta_excludes_idle_and_first_sample_is_unknown(self):
        store = Store(':memory:')
        try:
            monitor = Monitor(types.SimpleNamespace(), store)
            self.assertIsNone(monitor.system_values(100, 40, 16000, 4000)['cpu'])
            self.assertEqual(monitor.system_values(200, 60, 16000, 5000)['cpu'], 80)
            self.assertIsNone(monitor.system_values(200, 60, 16000, 5000)['cpu'])
        finally:
            store.db.close()

    def test_monitor_reads_windows_cpu_and_memory(self):
        store = Store(':memory:')
        try:
            monitor = Monitor(types.SimpleNamespace(), store)
            with patch('server.windows_system',side_effect=[(100,40,16000,4000),(200,60,16000,5000)]):
                first=monitor.system_read()
                second=monitor.system_read()
            self.assertIsNone(first['cpu'])
            self.assertEqual(second,{'cpu':80,'memory_total':16000,'memory_used':5000})
        finally:
            store.db.close()

    def test_real_windows_resources(self):
        first = platform.windows_system()
        time.sleep(.05)
        second = platform.windows_system()
        self.assertGreater(second[0], first[0])
        self.assertGreaterEqual(second[1], first[1])
        self.assertGreater(second[2], 0)
        self.assertTrue(0 <= second[3] <= second[2])

    def test_wddm_unavailable_process_memory_does_not_remove_gpu(self):
        store = Store(':memory:')
        try:
            monitor = Monitor(types.SimpleNamespace(), store)
            results = [types.SimpleNamespace(stdout='0, GPU-1, NVIDIA Test, 12, N/A, 100, 8000, 50, 30, 1000, 9000\n'),
                       types.SimpleNamespace(returncode=0, stdout='GPU-1, 123, C:\\Apps\\lms.exe, [N/A]\n')]
            with patch('server.subprocess.run', side_effect=results):
                devices, processes = monitor.gpu_read()
            self.assertEqual(devices[0]['memory_total'], 8000)
            self.assertIsNone(devices[0]['memory_utilization'])
            self.assertIsNone(processes[0]['memory'])
        finally:
            store.db.close()
