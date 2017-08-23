import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import traceback
import zlib

from dmoj import sysinfo
from dmoj.judgeenv import get_supported_problems, get_runtime_versions

try:
    import ssl
except ImportError:
    ssl = None

logger = logging.getLogger(__name__)
timer = time.clock if os.name == 'nt' else time.time


class JudgeAuthenticationFailed(Exception):
    pass


class PacketManager(object):
    SIZE_PACK = struct.Struct('!I')

    def __init__(self, host, port, judge, name, key, secure=False, no_cert_check=False, cert_store=None):
        self.host = host
        self.port = port
        self.judge = judge
        self.name = name
        self.key = key

        if secure and ssl:
            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
            self.ssl_context.options |= ssl.OP_NO_SSLv2
            self.ssl_context.options |= ssl.OP_NO_SSLv3

            if not no_cert_check:
                self.ssl_context.verify_mode = ssl.CERT_REQUIRED
                self.ssl_context.check_hostname = True

            if cert_store is None:
                self.ssl_context.load_default_certs()
            else:
                self.ssl_context.load_verify_locations(cafile=cert_store)
        else:
            self.ssl_context = None

        self.secure = secure
        self.no_cert_check = no_cert_check
        self.cert_store = cert_store

        self._lock = threading.RLock()
        self._batch = 0
        # Exponential backoff: starting at 4 seconds.
        # Certainly hope it won't stack overflow, since it will take days if not years.
        self.fallback = 4

        self.conn = None
        self._do_reconnect()

    def _connect(self):
        problems = get_supported_problems()
        versions = get_runtime_versions()

        self.conn = socket.create_connection((self.host, self.port))
        self.conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        if self.ssl_context:
            self.conn = self.ssl_context.wrap_socket(self.conn, server_hostname=self.host)

        self.input = self.conn.makefile('r')
        self.output = self.conn.makefile('w', 0)
        self.handshake(problems, versions, self.name, self.key)

    def _reconnect(self):
        if self.fallback > 65536:
            # Return 0 to avoid supervisor restart.
            raise SystemExit(0)
        print>> sys.stderr
        print>> sys.stderr, 'SOCKET ERROR: Disconnected! Reconnecting in %d seconds.' % self.fallback
        if self.conn is not None:
            self.conn.close()
        time.sleep(self.fallback)
        self.fallback *= 1.5
        self._do_reconnect()

    def _do_reconnect(self):
        try:
            self._connect()
        except JudgeAuthenticationFailed:
            self._reconnect()
        except socket.error:
            traceback.print_exc()
            self._reconnect()

    def __del__(self):
        self.conn.shutdown(socket.SHUT_RDWR)

    def _read_async(self):
        try:
            while True:
                self._receive_packet(self._read_single())
        except KeyboardInterrupt:
            pass
        except Exception:  # connection reset by peer
            traceback.print_exc()
            raise SystemExit(1)

    def _read_single(self):
        try:
            data = self.input.read(PacketManager.SIZE_PACK.size)
        except socket.error:
            self._reconnect()
            return self._read_single()
        if not data:
            self._reconnect()
            return self._read_single()
        size = PacketManager.SIZE_PACK.unpack(data)[0]
        try:
            packet = self.input.read(size).decode('zlib')
        except zlib.error:
            self._reconnect()
            return self._read_single()
        else:
            return json.loads(packet)

    def run(self):
        self._read_async()

    def run_async(self):
        threading.Thread(target=self._read_async).start()

    def _send_packet(self, packet, rewrite=True):
        if rewrite and 'submission-id' in packet and self.judge.get_process_type() != 'submission':
            packet['%s-id' % self.judge.get_process_type()] = packet['submission-id']
            del packet['submission-id']

        for k, v in packet.items():
            if isinstance(v, str):
                # Make sure we don't have any garbage utf-8 from e.g. weird compilers
                # *cough* fpc *cough* that could cause this routine to crash
                packet[k] = v.decode('utf-8', 'replace')

        raw = json.dumps(packet).encode('zlib')
        with self._lock:
            self.output.write(PacketManager.SIZE_PACK.pack(len(raw)))
            self.output.write(raw)

    def _receive_packet(self, packet):
        name = packet['name']
        if name == 'ping':
            self.ping_packet(packet['when'])
        elif name == 'get-current-submission':
            self.current_submission_packet()
        elif name == 'submission-request':
            self.submission_acknowledged_packet(packet['submission-id'])
            self.judge.begin_grading(
                packet['submission-id'],
                packet['problem-id'],
                packet['language'],
                packet['source'],
                float(packet['time-limit']),
                int(packet['memory-limit']),
                packet['short-circuit'],
                packet['pretests-only']
            )
            self._batch = 0
            logger.info('Accept submission: %d: executor: %s, code: %s',
                        packet['submission-id'], packet['language'], packet['problem-id'])
        elif name == 'invocation-request':
            self.invocation_acknowledged_packet(packet['invocation-id'])
            self.judge.custom_invocation(
                packet['invocation-id'],
                packet['language'],
                packet['source'],
                float(packet['time-limit']),
                int(packet['memory-limit']),
                packet['input-data']
            )
            logger.info('Accept invocation: %d: executor: %s', packet['invocation-id'], packet['language'])
        elif name == 'terminate-submission':
            self.judge.terminate_grading()
        else:
            print 'ERROR: unknown packet %s, payload %s' % (name, packet)

    def handshake(self, problems, runtimes, id, key):
        self._send_packet({'name': 'handshake',
                           'problems': problems,
                           'executors': runtimes,
                           'id': id,
                           'key': key})
        try:
            data = self.input.read(PacketManager.SIZE_PACK.size)
            size = PacketManager.SIZE_PACK.unpack(data)[0]
            packet = self.input.read(size).decode('zlib')
            resp = json.loads(packet)
        except Exception:
            traceback.print_exc()
            raise JudgeAuthenticationFailed()
        else:
            if resp['name'] != 'handshake-success':
                raise JudgeAuthenticationFailed()

    def invocation_begin_packet(self):
        logger.info('Begin invoking: %d', self.judge.current_submission)
        self._send_packet({'name': 'invocation-begin',
                           'invocation-id': self.judge.current_submission})

    def invocation_end_packet(self, result):
        logger.info('End invoking: %d', self.judge.current_submission)
        self.fallback = 4
        self._send_packet({'name': 'invocation-end',
                           'output': result.proc_output,
                           'status': result.status_flag,
                           'time': result.execution_time,
                           'memory': result.max_memory,
                           'feedback': result.feedback,
                           'invocation-id': self.judge.current_submission})

    def supported_problems_packet(self, problems):
        logger.info('Update problems')
        self._send_packet({'name': 'supported-problems',
                           'problems': problems})

    def test_case_status_packet(self, position, result):
        self._send_packet({'name': 'test-case-status',
                           'submission-id': self.judge.current_submission,
                           'position': position,
                           'status': result.result_flag,
                           'time': result.execution_time,
                           'points': result.points,
                           'total-points': result.total_points,
                           'memory': result.max_memory,
                           'output': result.output,
                           'feedback': result.feedback})

    def compile_error_packet(self, log):
        logger.info('Compile error: %d', self.judge.current_submission)
        self.fallback = 4
        self._send_packet({'name': 'compile-error',
                           'submission-id': self.judge.current_submission,
                           'log': log})

    def compile_message_packet(self, log):
        logger.info('Compile message: %d', self.judge.current_submission)
        self._send_packet({'name': 'compile-message',
                           'submission-id': self.judge.current_submission,
                           'log': log})

    def internal_error_packet(self, message):
        logger.info('Internal error: %d', self.judge.current_submission)
        self._send_packet({'name': 'internal-error',
                           'submission-id': self.judge.current_submission,
                           'message': message})

    def begin_grading_packet(self, is_pretested):
        logger.info('Begin grading: %d', self.judge.current_submission)
        self._send_packet({'name': 'grading-begin',
                           'submission-id': self.judge.current_submission,
                           'pretested': is_pretested})

    def grading_end_packet(self):
        logger.info('End grading: %d', self.judge.current_submission)
        self.fallback = 4
        self._send_packet({'name': 'grading-end',
                           'submission-id': self.judge.current_submission})

    def batch_begin_packet(self):
        self._batch += 1
        logger.info('Enter batch number %d: %d', self._batch, self.judge.current_submission)
        self._send_packet({'name': 'batch-begin',
                           'submission-id': self.judge.current_submission})

    def batch_end_packet(self):
        logger.info('Exit batch number %d: %d', self._batch, self.judge.current_submission)
        self._send_packet({'name': 'batch-end',
                           'submission-id': self.judge.current_submission})

    def current_submission_packet(self):
        logger.info('Current submission query: %d', self.judge.current_submission)
        self._send_packet({'name': 'current-submission-id',
                           'submission-id': self.judge.current_submission})

    def submission_terminated_packet(self):
        logger.info('Submission aborted: %d', self.judge.current_submission)
        self._send_packet({'name': 'submission-terminated',
                           'submission-id': self.judge.current_submission})

    def ping_packet(self, when):
        data = {'name': 'ping-response',
                'when': when,
                'time': time.time()}
        for fn in sysinfo.report_callbacks:
            key, value = fn()
            data[key] = value
        self._send_packet(data)

    def submission_acknowledged_packet(self, sub_id):
        self._send_packet({'name': 'submission-acknowledged',
                           'submission-id': sub_id}, rewrite=False)

    def invocation_acknowledged_packet(self, sub_id):
        self._send_packet({'name': 'submission-acknowledged',
                           'invocation-id': sub_id}, rewrite=False)
