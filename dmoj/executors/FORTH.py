from dmoj.executors.mixins import ScriptDirectoryMixin
from .base_executor import ScriptExecutor


class Executor(ScriptDirectoryMixin, ScriptExecutor):
    name = 'FORTH'
    command = 'gforth'
    command_paths = ['gforth']
    ext = '.fs'
    test_program = '''\
: HELLO  ( -- ) ." echo: Hello, World!" CR ;

HELLO
'''

    def get_cmdline(self):
        return [self.get_command(), self._code, '-e', 'bye']
