# The piwheels project
#   Copyright (c) 2017 Ben Nuttall <https://github.com/bennuttall>
#   Copyright (c) 2017 Dave Jones <dave@waveform.org.uk>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""
Defines the classes which use ``pip`` to build wheels.

.. autoclass:: PiWheelsPackage
    :members:

.. autoclass:: PiWheelsBuilder
    :members:
"""

import os
import re
import zipfile
import hashlib
import resource
import tempfile
import warnings
import email.parser
from pathlib import Path
from datetime import datetime, timedelta
from subprocess import Popen, DEVNULL, PIPE, TimeoutExpired
from collections import defaultdict

try:
    import apt
except ImportError:
    apt = None

from ..systemd import get_systemd


class PiWheelsPackage:
    """
    Records the state of a build artifact, i.e. a wheel package. The filename
    is deconstructed into the fields specified by :pep:`425`.

    :param pathlib.Path path:
        The path to the wheel on the local filesystem.
    """
    apt_cache = None

    def __init__(self, path):
        self.systemd = get_systemd()
        self.wheel_file = path
        self._filesize = path.stat().st_size
        self._filehash = None
        self._metadata = None
        self._dependencies = None
        self._parts = list(path.stem.split('-'))
        # Fix up retired tags (noabi->none)
        if self._parts[-2] == 'noabi':
            self._parts[-2] = 'none'

    def as_message(self):
        """
        Return the state as a list suitable for use in the ``BUILT`` message
        of :program:`piw-slave`.
        """
        return (
            self.filename,
            self.filesize,
            self.filehash,
            self.package_tag,
            self.package_version_tag,
            self.py_version_tag,
            self.abi_tag,
            self.platform_tag,
            self.dependencies
        )

    @property
    def filename(self):
        """
        Return the filename of the wheel as a simple string (with no path
        components).
        """
        return self.wheel_file.name

    @property
    def filesize(self):
        """
        Return the size of the wheel in bytes.
        """
        return self._filesize

    @property
    def filehash(self):
        """
        Return an SHA256 digest of the wheel's contents.
        """
        if self._filehash is None:
            s = hashlib.sha256()
            with self.wheel_file.open('rb') as f:
                while True:
                    buf = f.read(65536)
                    if buf:
                        s.update(buf)
                    else:
                        break
            self._filehash = s.hexdigest().lower()
        return self._filehash

    @property
    def package_tag(self):
        """
        Return the package part of the wheel's filename (the first "-"
        separated element).
        """
        return self._parts[0]

    @property
    def package_version_tag(self):
        """
        Return the version part of the wheel's filename (the second "-"
        separated element).
        """
        return self._parts[1]

    @property
    def platform_tag(self):
        """
        Return the platform part of the wheel's filename (the last "-"
        separated element).
        """
        return self._parts[-1]

    @property
    def abi_tag(self):
        """
        Return the ABI part of the wheel's filename (the penultimate "-"
        separated element).
        """
        return self._parts[-2]

    @property
    def py_version_tag(self):
        """
        Return the python version part of the wheel's filename (third from last
        "-" separated element).
        """
        return self._parts[-3]

    @property
    def build_tag(self):
        """
        Return the optional build part of the wheel's filename (the third "-"
        separated element when 6 elements exist in total).
        """
        return self._parts[2] if len(self._parts) == 6 else None

    def open(self):
        """
        Open the wheel in binary mode and return the open file object.
        """
        return self.wheel_file.open('rb')

    @property
    def metadata(self):
        """
        Return the contents of the :file:`METADATA` file inside the wheel.
        """
        if self._metadata is None:
            with zipfile.ZipFile(self.open()) as wheel:
                filename = (
                    '{self.package_tag}-'
                    '{self.package_version_tag}.dist-info/'
                    'METADATA'.format(self=self)
                )
                with wheel.open(filename) as metadata:
                    parser = email.parser.BytesParser()
                    self._metadata = parser.parse(metadata)
        return self._metadata

    def _calculate_apt_dependencies(self):
        if PiWheelsPackage.apt_cache is None:
            PiWheelsPackage.apt_cache = apt.cache.Cache()
        cache = PiWheelsPackage.apt_cache
        find_re = re.compile(r'^\s*(.*)\s=>\s(/.*)\s\(0x[0-9a-fA-F]+\)$')
        deps = defaultdict(set)
        libs = set()
        with tempfile.TemporaryDirectory() as tempdir:
            with zipfile.ZipFile(self.open()) as wheel:
                for info in wheel.infolist():
                    if info.filename.endswith('.so') or '.so.' in info.filename:
                        with wheel.open(info) as testfile:
                            is_elf = testfile.read(4) == b'\x7FELF'
                        if is_elf:
                            libs.add(wheel.extract(info, path=tempdir))
            for lib in libs:
                p = Popen(['ldd', lib], stdout=PIPE, stderr=DEVNULL)
                try:
                    out, errs = p.communicate(timeout=10)
                except TimeoutExpired:
                    p.kill()
                    out, errs = p.communicate()
                finally:
                    out = out.decode('ascii', 'replace')
                    for line in out.splitlines():
                        match = find_re.search(line)
                        if match is not None:
                            try:
                                lib_path = str(Path(match.group(2)).resolve())
                            except FileNotFoundError:
                                continue
                            providers = {
                                pkg.name for pkg in cache
                                if pkg.installed is not None
                                and lib_path in pkg.installed_files}
                            assert len(providers) <= 1
                            try:
                                deps['apt'].add(providers.pop())
                            except KeyError:
                                deps[''].add(lib_path)
                            self.systemd.watchdog_ping()
        return {tool: sorted(deps) for tool, deps in deps.items()}

    @property
    def dependencies(self):
        if self._dependencies is None:
            if apt is None:
                warnings.warn(
                    Warning('Cannot import apt module; unable to calculate '
                            'apt dependencies'))
                self._dependencies = {}
            else:
                self._dependencies = self._calculate_apt_dependencies()
        return self._dependencies

    def transfer(self, queue, slave_id):
        """
        Transfer the wheel via the specified *queue*. This is the client side
        implementation of the :class:`.file_juggler.FileJuggler` protocol.
        """
        with self.open() as f:
            timeout = 0
            while True:
                if not queue.poll(timeout):
                    # Initially, send HELLO immediately; in subsequent loops if
                    # we hear nothing from the server for 5 seconds then it's
                    # dropped a *lot* of packets; prod the master with HELLO
                    queue.send_multipart(
                        [b'HELLO', str(slave_id).encode('ascii')]
                    )
                    timeout = 5
                    # Transfers are generally very fast but if we wind up
                    # having to restart there's a possibility we'll miss the
                    # watchdog timer, so ping it each time the poll fails
                    self.systemd.watchdog_ping()
                else:
                    req, *args = queue.recv_multipart()
                    if req == b'DONE':
                        return
                    elif req == b'FETCH':
                        offset, size = args
                        f.seek(int(offset))
                        queue.send_multipart([b'CHUNK', offset, f.read(int(size))])


class PiWheelsBuilder:
    """
    Class responsible for building wheels for a given *version* of a *package*.

    :param str package:
        The name of the package to attempt to build wheels for.

    :param str version:
        The version of the package to attempt to build.
    """
    def __init__(self, package, version):
        self.systemd = get_systemd()
        self.wheel_dir = None
        self.package = package
        self.version = version
        self.duration = None
        self.output = ''
        self.files = []
        self.status = False

    def as_message(self):
        """
        Return the state as a list suitable for use in the ``BUILT`` message
        of :program:`piw-slave`.
        """
        return [
            self.package, self.version, self.status, self.duration,
            self.output, [pkg.as_message() for pkg in self.files]
        ]

    def build(self, timeout=timedelta(minutes=5),
              pypi_index='https://pypi.python.org/simple'):
        """
        Attempt to build the package within the specified *timeout*.

        :param float timeout:
            The number of seconds to wait for ``pip`` to finish before raising
            :exc:`subprocess.TimeoutExpired`.

        :param str pypi_index:
            The URL of the :pep:`503` compliant repository from which to fetch
            packages for building.
        """
        self.wheel_dir = tempfile.TemporaryDirectory()
        with tempfile.NamedTemporaryFile('w+', dir=self.wheel_dir.name,
                                         suffix='.log',
                                         encoding='utf-8') as log_file:
            env = os.environ.copy()
            # Force git to fail if it needs to prompt for anything (a
            # disturbing minority of packages try to run git clone during their
            # setup.py)
            env['GIT_ALLOW_PROTOCOL'] = 'file'
            args = [
                'pip3', 'wheel',
                '--index-url={}'.format(pypi_index),
                '--wheel-dir={}'.format(self.wheel_dir.name),
                '--log={}'.format(log_file.name),
                '--no-deps',                    # don't build dependencies
                '--no-cache-dir',               # disable the cache directory
                '--exists-action=w',            # wipe existing paths
                '--disable-pip-version-check',  # don't check for new pip
                '{}=={}'.format(self.package, self.version),
            ]
            # Limit the data segment of this process (and all children) to 1Gb
            # in size. This doesn't guarantee that stuff can't grow until it
            # crashes (multiple children can violate the limit together while
            # obeying it individually), but it should reduce the incidence of
            # huge C++ compiles killing the build slaves
            resource.setrlimit(resource.RLIMIT_DATA, (1024**3, 1024**3))
            start = datetime.utcnow()
            try:
                proc = Popen(
                    args,
                    stdin=DEVNULL,     # ensure stdin is /dev/null; this causes
                                       # anything stupid enough to use input()
                                       # in its setup.py to fail immediately
                    stdout=DEVNULL,    # also ignore all output
                    stderr=DEVNULL,
                    env=env
                )
                # If the build times out attempt to kill it with SIGTERM; if
                # that hasn't worked after 10 seconds, resort to SIGKILL.
                # Builds frequently exceed the watchdog timeout (2 minutes) so
                # ping every 60 seconds
                while True:
                    self.systemd.watchdog_ping()
                    try:
                        proc.wait(10)
                    except TimeoutExpired:
                        if datetime.utcnow() - start > timeout:
                            proc.terminate()
                            try:
                                proc.wait(10)
                            except TimeoutExpired:
                                proc.kill()
                            raise
                    else:
                        break
            except Exception as exc:
                error = exc
            else:
                error = None
            self.duration = datetime.utcnow() - start
            self.status = proc.returncode == 0
            if error is not None:
                log_file.seek(0, os.SEEK_END)
                log_file.write('\n' + str(error))
            log_file.seek(0)
            self.output = log_file.read()

            if self.status:
                for path in Path(self.wheel_dir.name).glob('*.whl'):
                    self.files.append(PiWheelsPackage(path))
            return self.status

    def clean(self):
        """
        Remove the temporary build directory and all its contents.
        """
        if self.wheel_dir is not None:
            self.wheel_dir.cleanup()
            self.wheel_dir = None
