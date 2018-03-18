#!/usr/bin/env python

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
Defines the :class:`PiWheelsSlave` class. An instance of this is the
entry-point for the :program:`piw-slave` script.

.. autoclass:: PiWheelsSlave
    :members:

.. autofunction:: duration
"""

import os
import sys
import logging
from datetime import datetime
from time import sleep
from random import randint

import zmq
import dateutil.parser
from wheel import pep425tags

from .. import __version__, terminal, systemd
from .builder import PiWheelsBuilder


class PiWheelsSlave:
    """
    This is the main class for the :program:`piw-slave` script. It connects
    (over zmq sockets) to a master (see :program:`piw-master`) then loops
    around the slave protocol (see the :doc:`slaves` chapter). It retrieves
    source packages directly from `PyPI`_, attempts to build a wheel in a
    sandbox directory and, if successful, transmits the results to the master.

    .. _PyPI: https://pypi.python.org/
    """
    def __init__(self):
        self.logger = logging.getLogger('slave')
        self.config = None
        self.slave_id = None
        self.builder = None
        self.pypi_url = None

    def __call__(self, args=None):
        sys.excepthook = terminal.error_handler
        parser = terminal.configure_parser("""
The piw-slave script is intended to be run on a standalone machine to build
packages on behalf of the piw-master script. It is intended to be run as an
unprivileged user with a clean home-directory. Any build dependencies you wish
to use must already be installed. The script will run until it is explicitly
terminated, either by Ctrl+C, SIGTERM, or by the remote piw-master script.
""")
        parser.add_argument(
            '-m', '--master', env_var='PIW_MASTER', metavar='HOST',
            default='localhost',
            help="The IP address or hostname of the master server "
            "(default: %(default)s)")
        parser.add_argument(
            '-t', '--timeout', env_var='PIW_TIMEOUT', metavar='DURATION',
            default='3h', type=duration,
            help="The time to wait before assuming a build has failed; "
            "(default: %(default)s)")
        self.config = parser.parse_args(args)
        terminal.configure_logging(self.config.log_level,
                                   self.config.log_file)

        self.logger.info('PiWheels Slave version %s', __version__)
        if os.geteuid() == 0:
            self.logger.error('Slave must not be run as root')
            return 1
        ctx = zmq.Context.instance()
        queue = None
        try:
            while True:
                queue = ctx.socket(zmq.REQ)
                queue.hwm = 1
                queue.ipv6 = True
                queue.connect('tcp://{master}:5555'.format(
                    master=self.config.master))
                systemd.ready()
                request = ['HELLO', self.config.timeout,
                           pep425tags.get_impl_ver(),
                           pep425tags.get_abi_tag(),
                           pep425tags.get_platform()]
                while request is not None:
                    queue.send_pyobj(request)
                    if queue.poll(60000):
                        reply, *args = queue.recv_pyobj()
                        request = self.handle_reply(reply, *args)
                    else:
                        self.logger.warning('Timed out waiting for master')
                        if self.builder:
                            self.logger.warning('Discarding current build')
                            self.builder.clean()
                            self.builder = None
                        self.slave_id = None
                        queue.close(linger=0)
                        queue = None
                        request = None
                        self.logger.warning('Resetting connection')
                if queue is not None:
                    break
                systemd.reloading()
        finally:
            systemd.stopping()
            queue.send_pyobj(['BYE'])
            ctx.destroy(linger=1000)
            ctx.term()

    # A general note about the design of the slave: the build slave is
    # deliberately designed to be "brittle". In other words to fall over and
    # die loudly in the event anything happens to go wrong (other than utterly
    # expected failures like wheels occasionally failing to build and file
    # transfers occasionally needing a retry). Hence all the apparently silly
    # asserts littering the functions below.

    # This is in stark constrast to the master which is expected to stay up and
    # carry on running even if a build slave goes bat-shit crazy and starts
    # sending nonsense (in which case it should calmly ignore it and/or attempt
    # to kill said slave with a "BYE" message).

    def handle_reply(self, reply, *args):
        """
        Dispatch a message from the master to an appropriate handler method.
        """
        try:
            handler = {
                'HELLO': self.do_hello,
                'SLEEP': self.do_sleep,
                'BUILD': self.do_build,
                'SEND': self.do_send,
                'DONE': self.do_done,
                'BYE': self.do_bye,
            }[reply]
        except KeyError:
            assert False, 'Invalid message from master %r' % reply
        else:
            return handler(*args)

    def do_hello(self, new_id, pypi_url):
        """
        In response to our initial "HELLO" (detailing our various :pep:`425`
        tags), the master is expected to send "HELLO" back with an integer
        identifier and the URL of the PyPI repository to download from. We use
        the identifier in all future log messages for the ease of the
        administrator.

        We reply with "IDLE" to indicate we're ready to accept a build job.
        """
        assert self.slave_id is None, 'Duplicate hello'
        self.slave_id = int(new_id)
        self.pypi_url = pypi_url
        self.logger = logging.getLogger('slave-%d' % self.slave_id)
        self.logger.info('Connected to master')
        return ['IDLE']

    def do_sleep(self):
        """
        If, in response to an "IDLE" message we receive "SLEEP" this indicates
        the master has nothing for us to do currently. Sleep for a little while
        then try "IDLE" again.
        """
        assert self.slave_id is not None, 'Sleep before hello'
        self.logger.info('No available jobs; sleeping')
        sleep(randint(5, 15))
        return ['IDLE']

    def do_build(self, package, version):
        """
        Alternatively, in response to "IDLE", the master may send "BUILD"
        *package* *version*. We should then attempt to build the specified
        wheel and send back a "BUILT" message with a full report of the
        outcome.
        """
        assert self.slave_id is not None, 'Build before hello'
        assert not self.builder, 'Last build still exists'
        self.logger.warning('Building package %s version %s', package, version)
        self.builder = PiWheelsBuilder(package, version)
        if self.builder.build(self.config.timeout, self.pypi_url):
            self.logger.info('Build succeeded')
        else:
            self.logger.warning('Build failed')
        return ['BUILT'] + self.builder.as_message[2:]

    def do_send(self, filename):
        """
        If a build succeeds and generates files (detailed in a "BUILT"
        message), the master will reply with "SEND" *filename* indicating we
        should transfer the specified file (this is done on a separate socket
        with a different protocol; see :meth:`builder.PiWheelsPackage.transfer`
        for more details). Once the transfers concludes, reply to the master
        with "SENT".
        """
        assert self.slave_id is not None, 'Send before hello'
        assert self.builder, 'Send before build / after failed build'
        assert self.builder.status, 'Send after failed build'
        pkg = [f for f in self.builder.files if f.filename == filename][0]
        self.logger.info('Sending %s to master on localhost', pkg.filename)
        ctx = zmq.Context.instance()
        queue = ctx.socket(zmq.DEALER)
        queue.ipv6 = True
        queue.hwm = 10
        queue.connect('tcp://{master}:5556'.format(master=self.config.master))
        try:
            pkg.transfer(queue, self.slave_id)
        finally:
            queue.close()
        return ['SENT']

    def do_done(self):
        """
        After all files have been sent (and successfully verified), the master
        will reply with "DONE" indicating we can remove all associated build
        artifacts. We respond with "IDLE".
        """
        assert self.slave_id is not None, 'Done before hello'
        assert self.builder, 'Done before build'
        self.logger.info('Removing temporary build directories')
        self.builder.clean()
        self.builder = None
        return ['IDLE']

    def do_bye(self):
        """
        The master may respond with "BYE" at any time indicating we should
        immediately terminate (first cleaning up any extant build). We return
        ``None`` to tell the main loop to quit.
        """
        self.logger.warning('Master requested termination')
        if self.builder is not None:
            self.logger.info('Removing temporary build directories')
            self.builder.clean()
        return None


def duration(s):
    """
    Convert *s*, a string representing a duration, into a
    :class:`datetime.timedelta`.
    """
    return (
        dateutil.parser.parse(s, default=datetime(1, 1, 1)) -
        datetime(1, 1, 1)
    ).total_seconds()


main = PiWheelsSlave()  # pylint: disable=invalid-name

if __name__ == '__main__':
    main()
