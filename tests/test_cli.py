import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from hh_agent.__main__ import process_lock, save_env_values


class CliTests(unittest.TestCase):
    def test_init_is_private_and_does_not_overwrite_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            result=subprocess.run([sys.executable,"-m","hh_agent","--root",temp,"init"],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            path=root/".env"
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            path.write_text("CUSTOM=keep\n")
            subprocess.run([sys.executable,"-m","hh_agent","--root",temp,"init"],check=True,capture_output=True)
            self.assertEqual(path.read_text(),"CUSTOM=keep\n")

    def test_local_process_lock_prevents_second_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            with process_lock(Path(temp)):
                with self.assertRaises(RuntimeError):
                    with process_lock(Path(temp)):
                        self.fail("second worker acquired lock")

    def test_secrets_written_without_executing_or_interpolating_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/".env"
            save_env_values(path,{"LLM_API_KEY":"value'with${HOME}$(no)"})
            from dotenv import dotenv_values
            self.assertEqual(dotenv_values(path,interpolate=False)["LLM_API_KEY"],"value'with${HOME}$(no)")
            self.assertEqual(path.stat().st_mode&0o777,0o600)
