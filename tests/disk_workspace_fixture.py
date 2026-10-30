"""Owned, file-backed ext4 fixture. Never formats a host block device.

The fixed helper has no Docker socket, host namespaces or physical disk devices.
It keeps one autoclear loop FD open; Docker mounts that exact device in an owned
volume. No detach-all, host mknod, global mount propagation or loop resize.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import sys
import time

INFO = '=QQQQQIIII64s64s32sQQ'
GET_STATUS = 0x4c05


def helper(folder):
    folder = Path(folder)
    intent = json.loads((folder / 'intent.json').read_text())
    lifetime = intent.get('helper_seconds',720)
    assert type(lifetime) is int and 60<=lifetime<=10800
    image = folder / 'workspace.ext4'
    source = os.open(image, os.O_RDWR | os.O_NOFOLLOW)
    item = os.fstat(source)
    assert stat.S_ISREG(item.st_mode) and item.st_nlink == 1
    assert [item.st_dev, item.st_ino, item.st_size] == intent['identity']
    control = os.open('/dev/loop-control', os.O_RDWR)
    loop = None
    try:
        for _ in range(16):
            number = fcntl.ioctl(control, 0x4c82)
            device = Path('/dev/loop' + str(number))
            if not device.exists():
                os.mknod(device, stat.S_IFBLK | 0o600, os.makedev(7, number))
            info = device.stat()
            assert stat.S_ISBLK(info.st_mode) and info.st_rdev == os.makedev(7, number)
            candidate = os.open(device, os.O_RDWR | os.O_NOFOLLOW)
            try:
                # LOOP_CONFIGURE attaches and sets AUTOCLEAR atomically (Linux >=5.8).
                # Unsupported kernels fail closed, never fall back to a leaking SET_FD gap.
                initial = [0] * 9 + [b'', b'', b'', 0, 0]
                initial[8] = 4
                configuration = struct.pack('=II', source, 0) + struct.pack(INFO, *initial) + bytes(64)
                fcntl.ioctl(candidate, 0x4c0a, configuration)
            except OSError as error:
                os.close(candidate)
                if error.errno == errno.EBUSY:
                    continue
                raise
            loop = candidate
            break
        assert loop is not None, 'No unused loop device could be acquired; no existing device changed'
        details = list(struct.unpack(INFO, fcntl.ioctl(loop, GET_STATUS, bytes(struct.calcsize(INFO)))))
        assert details[:2] == [item.st_dev, item.st_ino] and details[3:5] == [0, 0]
        confirmed = struct.unpack(INFO, fcntl.ioctl(loop, GET_STATUS, bytes(struct.calcsize(INFO))))
        assert confirmed[:2] == (item.st_dev, item.st_ino) and confirmed[8] & 4
        result = {'ready': True, 'number': number, 'device': str(device),
                  'backing_identity': intent['identity'], 'autoclear': True, 'released': False}
        def save():
            temporary = folder / 'loop-state.new'
            temporary.write_text(json.dumps(result))
            temporary.chmod(0o644)  # Non-secret metadata, inside the caller's private fixture directory.
            os.replace(temporary, folder / 'loop-state.json')
        save()
        def stop(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        deadline = time.monotonic() + lifetime
        try:
            while time.monotonic() < deadline and not (folder / 'release-loop').exists():
                time.sleep(.2)
        finally:
            os.close(loop)
            loop = None
            # Do not detach a device that could have been reused by another caller.
            check = os.open(device, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                try:
                    actual = struct.unpack(INFO, fcntl.ioctl(check, GET_STATUS, bytes(struct.calcsize(INFO))))
                    result['released'] = actual[:2] != (item.st_dev, item.st_ino)
                except OSError as error:
                    if error.errno != errno.ENXIO:
                        raise
                    result['released'] = True
            finally:
                os.close(check)
            save()
    finally:
        if loop is not None:
            os.close(loop)
        os.close(control)
        os.close(source)


class OwnedDisk:
    def __init__(self, base, prefix, image, cpus, docker, folder_name='disk-workspace',image_mib=32,inodes=512,quota_type=None,helper_seconds=720):
        assert folder_name in {'disk-workspace','archive-pressure','central-payload','central-restore','quota-workspace','spool-workspace','cache-workspace','expand-work','expand-cache','expand-spool'}
        assert quota_type in {None,'user','group'}
        assert type(image_mib) is int and 32<=image_mib<=65536 and type(inodes) is int and 512<=inodes<=1000000
        assert type(helper_seconds) is int and 60<=helper_seconds<=10800
        self.folder = Path(base) / folder_name
        self.folder.mkdir(mode=0o700)
        self.name = prefix + '-disk-helper'
        self.prefix = prefix
        self.image = image
        self.cpus = cpus
        self.docker = docker
        self.started = False
        self.inodes=inodes
        self.quota_type=quota_type
        self.helper_seconds=helper_seconds
        self.report = {'image_bytes': image_mib * 1024**2, 'backing_store': 'regular file on existing disk',
                       'host_block_devices_formatted': False, 'host_device_nodes_created': False,
                       'host_mount_propagation_changed': False, 'loop_autoclear': True}

    def create(self):
        path = self.folder / 'workspace.ext4'
        mounts = []
        for row in Path('/proc/self/mountinfo').read_text().splitlines():
            left, right = row.split(' - ', 1)
            mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), left.split()[4]))
            if path.is_relative_to(mount):
                mounts.append((len(mount.parts), right.split()[0]))
        assert mounts and max(mounts)[1] == 'ext4', 'This disk fixture requires observed ext4 backing storage; no RAM/unknown fallback'
        self.report['observed_backing_filesystem'] = max(mounts)[1]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.posix_fallocate(fd, 0, self.report['image_bytes'])
            item = os.fstat(fd)
        finally:
            os.close(fd)
        identity = [item.st_dev, item.st_ino, item.st_size]
        intent = self.folder / 'intent.json'
        intent.write_text(json.dumps({'identity': identity,'helper_seconds':self.helper_seconds}))
        intent.chmod(0o600)
        subprocess.run(['/usr/sbin/mkfs.ext4', '-q', '-F', '-b', '4096', '-N', str(self.inodes), '-m', '0',
                        *(['-O','quota'] if self.quota_type else []),
                        '-E', 'lazy_itable_init=0,lazy_journal_init=0'+(',quotatype='+('usrquota' if self.quota_type=='user' else 'grpquota') if self.quota_type else ''), '-L', 'ascend-lab-demo', str(path)],
                       check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        assert [path.stat().st_dev, path.stat().st_ino, path.stat().st_size] == identity
        self.docker(['run', '--detach', '--name', self.name, '--label', 'io.ascend-lab.diskproof=' + self.prefix,
            '--network', 'none', '--read-only', '--security-opt', 'no-new-privileges:true', '--cap-drop', 'ALL',
            '--cap-add', 'SYS_ADMIN', '--cap-add', 'MKNOD', '--cap-add', 'DAC_OVERRIDE',
            '--device', '/dev/loop-control', '--device-cgroup-rule', 'b 7:* rwm',
            '--memory', '128m', '--memory-swap', '128m', '--pids-limit', '32', '--cpus', '0.25',
            '--cpuset-cpus', str(self.cpus[0]), '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1',
            '--mount', 'type=bind,src=' + str(Path(__file__).resolve()) + ',dst=/disk-helper.py,readonly',
            '--mount', 'type=bind,src=' + str(self.folder) + ',dst=/fixture',
            '--entrypoint', '/usr/bin/python3', self.image, '-I', '/disk-helper.py', '--helper', '/fixture'])
        self.started = True
        until = time.monotonic() + 15
        state = None
        while time.monotonic() < until:
            metadata = self.folder / 'loop-state.json'
            if metadata.exists():
                state = json.loads(metadata.read_text())
                break
            details = json.loads(self.docker(['inspect', self.name]).stdout)[0]
            if not details['State']['Running']:
                raise RuntimeError('Owned disk helper failed: ' + self.docker(['logs', self.name]).stdout[-1500:])
            time.sleep(.1)
        assert state and state['ready'] and state['autoclear'] and state['backing_identity'] == identity
        details = json.loads(self.docker(['inspect', self.name]).stdout)[0]
        assert details['Config']['Labels']['io.ascend-lab.diskproof'] == self.prefix
        self.report.update(backing_identity=identity, loop_number=state['number'], helper_container=self.name)
        # Device node exists only in the pinned helper namespace; no host /dev mutation.
        return '/proc/' + str(details['State']['Pid']) + '/root' + state['device']

    def cleanup(self):
        if not self.started:
            # docker run can publish a Created container and then fail in OCI
            # setup before returning its id (or before Python was executed).
            # Reconcile only the exact intended helper, image and bind source.
            result=self.docker(['inspect',self.name],check=False)
            if result.returncode:
                if 'No such object' in result.stderr or 'No such container' in result.stderr:return
                raise RuntimeError('Cannot inspect intended disk helper; preserve its fixture for reconciliation')
            details=json.loads(result.stdout)[0]
            assert details['Config']['Labels'].get('io.ascend-lab.diskproof')==self.prefix
            assert details['Config']['Image']==self.image
            assert any(m['Type']=='bind' and m['Source']==str(self.folder) and m['Destination']=='/fixture' for m in details['Mounts'])
            state=details['State']
            if state['Status']=='created' and not state['Running'] and state['Pid']==0 and state['StartedAt'].startswith('0001-'):
                assert not (self.folder/'loop-state.json').exists(), 'Unexpected device publication from an unstarted helper'
                (self.folder/'helper-create-failure.json').write_text(json.dumps({'container':self.name,'state':state,'never_started':True}))
                self.docker(['rm',self.name]);self.report.update(loop_association_released=True,helper_removed=True,loop_never_attached=True)
                return
            self.started=True
        (self.folder / 'release-loop').touch(mode=0o600)
        self.docker(['wait', self.name], timeout=20)
        state_path = self.folder / 'loop-state.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            assert state['released'] and state['backing_identity'] == self.report.get('backing_identity', state['backing_identity']), 'Keep the fixture: loop association is still in use'
        else:
            # No device was published to Docker; failed helper exit closes any
            # atomic-AUTOCLEAR descriptor. Preserve the original failure logs.
            result = self.docker(['logs', self.name], check=False)
            (self.folder / 'helper-failure.log').write_text(result.stdout + result.stderr)
        details = json.loads(self.docker(['inspect', self.name]).stdout)[0]
        assert details['Config']['Labels']['io.ascend-lab.diskproof'] == self.prefix
        self.docker(['rm', self.name])
        self.report['loop_association_released'] = bool(state_path.exists())
        self.report['helper_removed'] = True
        self.started = False


if __name__ == '__main__':
    assert len(sys.argv) == 3 and sys.argv[1] == '--helper'
    helper(sys.argv[2])
