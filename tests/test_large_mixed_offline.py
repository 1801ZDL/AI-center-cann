"""Real subprocess output bounds for the independent raw-image audit helper."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from large_mixed_offline_check import Detached,MIB


class MetadataOutputTests(unittest.TestCase):
    def operation(self,program):
        native=subprocess.run
        def redirect(command,**kwargs):
            self.assertEqual(command[0],'/usr/sbin/debugfs')
            self.assertNotIn('capture_output',kwargs)
            self.assertEqual(kwargs['pass_fds'],(self.image.fd,))
            # Replace only debugfs with a controlled writer. The real child,
            # held FD, file descriptors, deadline and kernel limit still run.
            return native([sys.executable,'-I','-c',program],**kwargs)
        with patch('large_mixed_offline_check.subprocess.run',side_effect=redirect):
            return self.image.operation('cat','/results/report.json')

    def setUp(self):
        self.backing=tempfile.TemporaryFile()
        self.image=Detached.__new__(Detached);self.image.fd=self.backing.fileno()

    def tearDown(self):self.backing.close()

    def test_valid_record_and_standard_banner_pass(self):
        result=self.operation("import os;os.write(2,b'debugfs 1.47.0 (5-Feb-2023)\\n');os.write(1,b'{\"passed\":true}')")
        self.assertEqual(result,b'{"passed":true}')

    def test_single_short_write_cannot_hide_truncation(self):
        program='import os;n=os.write(1,b"x"*'+str(3*MIB)+');assert n<'+str(3*MIB)
        with self.assertRaisesRegex(AssertionError,'Unexpectedly large metadata'):
            self.operation(program)

    def test_repeated_writer_is_stopped_by_kernel_limit(self):
        program='import os,signal;signal.signal(signal.SIGXFSZ,signal.SIG_DFL)\nwhile True:os.write(1,b"x"*65536)'
        with self.assertRaises(subprocess.CalledProcessError) as caught:self.operation(program)
        self.assertEqual(caught.exception.returncode,-signal.SIGXFSZ)

    def test_diagnostic_flood_is_bounded_too(self):
        program='import os;os.write(2,b"x"*'+str(3*MIB)+')'
        with self.assertRaisesRegex(AssertionError,'Unexpectedly large metadata'):
            self.operation(program)

    def test_filesystem_warning_is_rejected(self):
        with self.assertRaisesRegex(AssertionError,'error or warning'):
            self.operation("import os;os.write(2,b'debugfs 1.47.0\\nfilesystem warning\\n');os.write(1,b'{}')")


if __name__=='__main__':unittest.main()
