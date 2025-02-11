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


import os
import pickle
from unittest import mock
from threading import Thread
from subprocess import DEVNULL
from itertools import chain, cycle

import pytest

from conftest import find_message
from piwheels import __version__, protocols, transport
from piwheels.slave import PiWheelsSlave, MasterTimeout


@pytest.fixture()
def mock_slave_driver(request, zmq_context, tmpdir):
    queue = zmq_context.socket(
        transport.ROUTER, protocol=protocols.slave_driver)
    queue.hwm = 1
    queue.bind('ipc://' + str(tmpdir.join('slave-driver-queue')))
    yield queue
    queue.close()


@pytest.fixture()
def mock_file_juggler(request, zmq_context, tmpdir):
    queue = zmq_context.socket(
        transport.DEALER, protocol=protocols.file_juggler)
    queue.hwm = 1
    queue.bind('ipc://' + str(tmpdir.join('file-juggler-queue')))
    yield queue
    queue.close()


@pytest.fixture()
def mock_signal(request):
    with mock.patch('signal.signal') as signal:
        yield signal


@pytest.fixture()
def slave_thread(request, mock_context, mock_systemd, mock_signal, tmpdir):
    main = PiWheelsSlave()
    slave_thread = Thread(daemon=True, target=main, args=([],))
    yield slave_thread


def test_help(capsys):
    main = PiWheelsSlave()
    with pytest.raises(SystemExit):
        main(['--help'])
    out, err = capsys.readouterr()
    assert out.startswith('usage:')
    assert '--master' in out


def test_version(capsys):
    main = PiWheelsSlave()
    with pytest.raises(SystemExit):
        main(['--version'])
    out, err = capsys.readouterr()
    assert out.strip() == __version__


def test_no_root(caplog):
    main = PiWheelsSlave()
    with mock.patch('os.geteuid') as geteuid:
        geteuid.return_value = 0
        assert main([]) != 0
    assert find_message(caplog.records, message='Slave must not be run as root')


def test_system_exit(mock_systemd, slave_thread, mock_slave_driver):
    with mock.patch('piwheels.slave.PiWheelsSlave.main_loop') as main_loop:
        main_loop.side_effect = SystemExit(1)
        slave_thread.start()
        assert mock_systemd._ready.wait(10)
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BYE'
        slave_thread.join(10)
        assert not slave_thread.is_alive()


def test_bye_exit(mock_systemd, slave_thread, mock_slave_driver):
    slave_thread.start()
    assert mock_systemd._ready.wait(10)
    addr, msg, data = mock_slave_driver.recv_addr_msg()
    assert msg == 'HELLO'
    mock_slave_driver.send_addr_msg(addr, 'DIE')
    addr, msg, data = mock_slave_driver.recv_addr_msg()
    assert msg == 'BYE'
    slave_thread.join(10)
    assert not slave_thread.is_alive()


def test_connection_timeout(mock_systemd, slave_thread, mock_slave_driver, caplog):
    with mock.patch('piwheels.slave.time') as time_mock:
        time_mock.side_effect = chain([1.0, 401.0, 402.0], cycle([403.0]))
        slave_thread.start()
        assert mock_systemd._ready.wait(10)
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'HELLO'
        # Allow timeout (time_mock takes care of faking this)
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'HELLO'
        mock_slave_driver.send_addr_msg(addr, 'DIE')
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BYE'
        slave_thread.join(10)
        assert not slave_thread.is_alive()
    assert find_message(caplog.records, message='Timed out waiting for master')


def test_bad_message_exit(mock_systemd, slave_thread, mock_slave_driver):
    slave_thread.start()
    assert mock_systemd._ready.wait(10)
    addr, msg, data = mock_slave_driver.recv_addr_msg()
    assert msg == 'HELLO'
    mock_slave_driver.send_multipart([addr, b'', b'FOO'])
    addr, msg, data = mock_slave_driver.recv_addr_msg()
    assert msg == 'BYE'
    slave_thread.join(10)
    assert not slave_thread.is_alive()


def test_hello(mock_systemd, slave_thread, mock_slave_driver):
    slave_thread.start()
    assert mock_systemd._ready.wait(10)
    addr, msg, data = mock_slave_driver.recv_addr_msg()
    assert msg == 'HELLO'
    mock_slave_driver.send_addr_msg(addr, 'ACK', [1, 'https://pypi.org/pypi'])
    addr, msg, data = mock_slave_driver.recv_addr_msg()
    assert msg == 'IDLE'
    mock_slave_driver.send_addr_msg(addr, 'DIE')
    addr, msg, data = mock_slave_driver.recv_addr_msg()
    assert msg == 'BYE'
    slave_thread.join(10)
    assert not slave_thread.is_alive()


def test_sleep(mock_systemd, slave_thread, mock_slave_driver):
    with mock.patch('piwheels.slave.randint', return_value=0):
        slave_thread.start()
        assert mock_systemd._ready.wait(10)
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'HELLO'
        mock_slave_driver.send_addr_msg(addr, 'ACK', [1, 'https://pypi.org/pypi'])
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'IDLE'
        mock_slave_driver.send_addr_msg(addr, 'SLEEP')
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'IDLE'
        mock_slave_driver.send_addr_msg(addr, 'DIE')
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BYE'
        slave_thread.join(10)
        assert not slave_thread.is_alive()


def test_slave_build_failed(mock_systemd, slave_thread, mock_slave_driver, caplog):
    with mock.patch('piwheels.slave.builder.Popen') as popen_mock:
        popen_mock().returncode = 1
        slave_thread.start()
        assert mock_systemd._ready.wait(10)
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'HELLO'
        mock_slave_driver.send_addr_msg(addr, 'ACK', [1, 'https://pypi.org/pypi'])
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'IDLE'
        mock_slave_driver.send_addr_msg(addr, 'BUILD', ['foo', '1.0'])
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BUILT'
        assert popen_mock.call_args == mock.call([
            'pip3', 'wheel', '--index-url=https://pypi.org/pypi',
            mock.ANY, mock.ANY, '--no-deps', '--no-cache-dir',
            '--exists-action=w', '--disable-pip-version-check',
            'foo==1.0'],
            stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, env=mock.ANY
        )
        mock_slave_driver.send_addr_msg(addr, 'DIE')
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BYE'
        slave_thread.join(10)
        assert not slave_thread.is_alive()
    assert find_message(caplog.records, message='Build failed')


def test_connection_timeout_with_build(mock_systemd, slave_thread, mock_slave_driver, caplog):
    with mock.patch('piwheels.slave.builder.Popen') as popen_mock, \
            mock.patch('piwheels.slave.time') as time_mock:
        time_mock.side_effect = cycle([1.0])
        slave_thread.start()
        assert mock_systemd._ready.wait(10)
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'HELLO'
        mock_slave_driver.send_addr_msg(addr, 'ACK', [1, 'https://pypi.org/pypi'])
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'IDLE'
        mock_slave_driver.send_addr_msg(addr, 'BUILD', ['foo', '1.0'])
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BUILT'
        time_mock.side_effect = chain([400.0], cycle([800.0]))
        # Allow timeout (time_mock takes care of faking this)
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'HELLO'
        mock_slave_driver.send_addr_msg(addr, 'DIE')
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BYE'
        slave_thread.join(10)
        assert not slave_thread.is_alive()
    assert find_message(caplog.records, message='Build failed')
    assert find_message(caplog.records, message='Timed out waiting for master')


def test_slave_build_send_done(mock_systemd, slave_thread, mock_slave_driver, tmpdir, caplog):
    with mock.patch('piwheels.slave.builder.Popen') as popen_mock, \
            mock.patch('piwheels.slave.builder.PiWheelsPackage._calculate_apt_dependencies') as apt_mock, \
            mock.patch('piwheels.slave.builder.PiWheelsPackage.transfer') as transfer_mock, \
            mock.patch('piwheels.slave.builder.tempfile.TemporaryDirectory') as tmpdir_mock:
        popen_mock().returncode = 0
        apt_mock.return_value = {}
        tmpdir_mock().name = str(tmpdir)
        tmpdir.join('foo-0.1-cp34-cp34m-linux_armv7l.whl').ensure()
        slave_thread.start()
        assert mock_systemd._ready.wait(10)
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'HELLO'
        mock_slave_driver.send_addr_msg(addr, 'ACK', [1, 'https://pypi.org/pypi'])
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'IDLE'
        mock_slave_driver.send_addr_msg(addr, 'BUILD', ['foo', '1.0'])
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BUILT'
        assert popen_mock.call_args == mock.call([
            'pip3', 'wheel', '--index-url=https://pypi.org/pypi',
            mock.ANY, mock.ANY, '--no-deps', '--no-cache-dir',
            '--exists-action=w', '--disable-pip-version-check',
            'foo==1.0'],
            stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, env=mock.ANY
        )
        mock_slave_driver.send_addr_msg(addr, 'SEND', 'foo-0.1-cp34-cp34m-linux_armv7l.whl')
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'SENT'
        mock_slave_driver.send_addr_msg(addr, 'DONE')
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'IDLE'
        mock_slave_driver.send_addr_msg(addr, 'DIE')
        addr, msg, data = mock_slave_driver.recv_addr_msg()
        assert msg == 'BYE'
        slave_thread.join(10)
        assert not slave_thread.is_alive()
    assert find_message(caplog.records, message='Build succeeded')
    assert find_message(caplog.records,
                        message='Sending foo-0.1-cp34-cp34m-linux_armv7l.whl '
                        'to master on localhost')
    assert find_message(caplog.records, message='Removing temporary build directories')
