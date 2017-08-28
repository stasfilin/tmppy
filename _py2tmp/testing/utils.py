#!/usr/bin/env python3
#  Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import os
import tempfile
import unittest
import textwrap
import re
import sys
import itertools
import subprocess
from functools import wraps

import pytest
import py2tmp

from py2tmp_test_config import *

def pretty_print_command(command):
    return ' '.join('"' + x + '"' for x in command)

class CommandFailedException(Exception):
    def __init__(self, command, stdout, stderr, error_code):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.error_code = error_code

    def __str__(self):
        return textwrap.dedent('''\
        Ran command: {command}
        Exit code {error_code}
        Stdout:
        {stdout}

        Stderr:
        {stderr}
        ''').format(command=pretty_print_command(self.command), error_code=self.error_code, stdout=self.stdout, stderr=self.stderr)

def run_command(executable, args=[]):
    command = [executable] + args
    print('Executing command:', pretty_print_command(command))
    try:
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        (stdout, stderr) = p.communicate()
    except Exception as e:
        raise Exception("While executing: %s" % command)
    if p.returncode != 0:
        raise CommandFailedException(command, stdout, stderr, p.returncode)
    print('Execution successful.')
    print('stdout:')
    print(stdout)
    print('')
    print('stderr:')
    print(stderr)
    print('')
    return (stdout, stderr)

def run_compiled_executable(executable):
    run_command(executable)

class CompilationFailedException(Exception):
    def __init__(self, command, error_message):
        self.command = command
        self.error_message = error_message

    def __str__(self):
        return textwrap.dedent('''\
        Ran command: {command}
        Error message:
        {error_message}
        ''').format(command=pretty_print_command(self.command), error_message=self.error_message)

class PosixCompiler:
    def __init__(self):
        self.executable = CXX
        self.name = CXX_COMPILER_NAME

    def compile_discarding_output(self, source, include_dirs, args=[]):
        try:
            args = args + ['-c', source, '-o', os.path.devnull]
            self._compile(include_dirs, args=args)
        except CommandFailedException as e:
            raise CompilationFailedException(e.command, e.stderr)

    def compile_and_link(self, source, include_dirs, output_file_name, args=[]):
        self._compile(
            include_dirs,
            args = (
                [source]
                + args
                + ['-o', output_file_name]
            ))

    def _compile(self, include_dirs, args):
        include_flags = ['-I%s' % include_dir for include_dir in include_dirs]
        args = (
            ['-W', '-Wall', '-g0', '-Werror']
            + include_flags
            + args
        )
        run_command(self.executable, args)

class MsvcCompiler:
    def __init__(self):
        self.executable = CXX
        self.name = CXX_COMPILER_NAME

    def compile_discarding_output(self, source, include_dirs, args=[]):
        try:
            args = args + ['/c', source]
            self._compile(include_dirs, args = args)
        except CommandFailedException as e:
            # Note that we use stdout here, unlike above. MSVC reports compilation warnings and errors on stdout.
            raise CompilationFailedException(e.command, e.stdout)

    def compile_and_link(self, source, include_dirs, output_file_name, args=[]):
        self._compile(
            include_dirs,
            args = (
                [source]
                + args
                + ['/Fe' + output_file_name]
            ))

    def _compile(self, include_dirs, args):
        include_flags = ['-I%s' % include_dir for include_dir in include_dirs]
        args = (
            ['/nologo', '/FS', '/W4', '/D_SCL_SECURE_NO_WARNINGS', '/WX']
            + include_flags
            + args
        )
        run_command(self.executable, args)

if CXX_COMPILER_NAME == 'MSVC':
    compiler = MsvcCompiler()
    py2tmp_error_message_extraction_regex = 'error C2338: (.*)'
else:
    compiler = PosixCompiler()
    py2tmp_error_message_extraction_regex = 'static.assert(.*)'

_assert_helper = unittest.TestCase()

def _create_temporary_file(file_content, file_name_suffix=''):
    file_descriptor, file_name = tempfile.mkstemp(text=True, suffix=file_name_suffix)
    file = os.fdopen(file_descriptor, mode='w')
    file.write(file_content)
    file.close()
    return file_name

def _cap_to_lines(s, n):
    lines = s.splitlines()
    if len(lines) <= n:
        return s
    else:
        return '\n'.join(lines[0:n] + ['...'])

def _replace_using_test_params(s, test_params):
    for var_name, value in test_params.items():
        if isinstance(value, str):
            s = re.sub(r'\b%s\b' % var_name, value, s)
    return s

def _construct_final_source_code(source_code, test_params):
    source_code = textwrap.dedent(source_code)
    source_code = _replace_using_test_params(source_code, test_params)
    return source_code

def try_remove_temporary_file(filename):
    try:
        os.remove(filename)
    except:
        # When running tests on Windows using Appveyor, the remove command fails for temporary files sometimes.
        # This shouldn't cause the tests to fail, so we ignore the exception and go ahead.
        pass

def expect_cpp_code_compile_error_helper(
        check_error_fun,
        source_code,
        test_params={}):
    source_code = _construct_final_source_code(source_code, test_params)

    source_file_name = _create_temporary_file(source_code, file_name_suffix='.cpp')

    try:
        compiler.compile_discarding_output(
            source=source_file_name,
            include_dirs=[MPYL_INCLUDE_DIR],
            args=[])
        raise Exception('The test should have failed to compile, but it compiled successfully')
    except CompilationFailedException as e1:
        e = e1

    error_message = e.error_message
    error_message_lines = error_message.splitlines()
    # Different compilers output a different number of spaces when pretty-printing types.
    # When using libc++, sometimes std::foo identifiers are reported as std::__1::foo.
    normalized_error_message = error_message.replace(' ', '').replace('std::__1::', 'std::')
    normalized_error_message_lines = normalized_error_message.splitlines()
    error_message_head = _cap_to_lines(error_message, 40)

    check_error_fun(e, error_message_lines, error_message_head, normalized_error_message_lines)

    try_remove_temporary_file(source_file_name)

def expect_cpp_code_generic_compile_error(expected_error_regex, source_code, test_params={}):
    """
    Tests that the given source produces the expected error during compilation.

    :param expected_error_regex: A regex used to match the _py2tmp error type,
           e.g. 'NoBindingFoundForAbstractClassError<ScalerImpl>'.
           Any identifiers contained in the regex will be replaced using test_params (where a replacement is defined).
    :param source_code: The second part of the source code. Any identifiers will be replaced using test_params
           (where a replacement is defined). This will be dedented.
    :param test_params: A dict containing the definition of some identifiers. Each identifier in
           expected_error_regex and source_code will be replaced (textually) with its definition (if a definition
           was provided).
    """

    expected_error_regex = _replace_using_test_params(expected_error_regex, test_params)
    expected_error_regex = expected_error_regex.replace(' ', '')

    def check_error(e, error_message_lines, error_message_head, normalized_error_message_lines):
        for line in normalized_error_message_lines:
            if re.search(expected_error_regex, line):
                return
        raise Exception(textwrap.dedent('''\
            Expected error {expected_error} but the compiler output did not contain that.
            Compiler command line: {compiler_command}
            Error message was:
            {error_message}
            ''').format(expected_error = expected_error_regex, compiler_command=e.command, error_message = error_message_head))

    expect_cpp_code_compile_error_helper(check_error, source_code, test_params)


def expect_cpp_code_compile_error(
        expected_py2tmp_error_regex,
        expected_py2tmp_error_desc_regex,
        source_code,
        test_params={}):
    """
    Tests that the given source produces the expected error during compilation.

    :param expected_py2tmp_error_regex: A regex used to match the _py2tmp error type,
           e.g. 'NoBindingFoundForAbstractClassError<ScalerImpl>'.
           Any identifiers contained in the regex will be replaced using test_params (where a replacement is defined).
    :param expected_py2tmp_error_desc_regex: A regex used to match the _py2tmp error description,
           e.g. 'No explicit binding was found for C, and C is an abstract class'.
    :param source_code: The second part of the source code. Any identifiers will be replaced using test_params
           (where a replacement is defined). This will be dedented.
    :param test_params: A dict containing the definition of some identifiers. Each identifier in
           expected_py2tmp_error_regex and source_code will be replaced (textually) with its definition (if a definition
           was provided).
    :param ignore_deprecation_warnings: A boolean. If True, deprecation warnings will be ignored.
    """
    if '\n' in expected_py2tmp_error_regex:
        raise Exception('expected_py2tmp_error_regex should not contain newlines')
    if '\n' in expected_py2tmp_error_desc_regex:
        raise Exception('expected_py2tmp_error_desc_regex should not contain newlines')

    expected_py2tmp_error_regex = _replace_using_test_params(expected_py2tmp_error_regex, test_params)
    expected_py2tmp_error_regex = expected_py2tmp_error_regex.replace(' ', '')

    def check_error(e, error_message_lines, error_message_head, normalized_error_message_lines):
        for line_number, line in enumerate(normalized_error_message_lines):
            match = re.search('tmppy::impl::(.*Error<.*>)', line)
            if match:
                actual_py2tmp_error_line_number = line_number
                actual_py2tmp_error = match.groups()[0]
                if CXX_COMPILER_NAME == 'MSVC':
                    # MSVC errors are of the form:
                    #
                    # C:\Path\To\header\foo.h(59): note: see reference to class template instantiation 'tmppy::impl::MyError<X, Y>' being compiled
                    #         with
                    #         [
                    #              X=int,
                    #              Y=double
                    #         ]
                    #
                    # So we need to parse the following few lines and use them to replace the placeholder types in the tmppy error type.
                    try:
                        replacement_lines = []
                        if normalized_error_message_lines[line_number + 1].strip() == 'with':
                            for line in itertools.islice(normalized_error_message_lines, line_number + 3, None):
                                line = line.strip()
                                if line == ']':
                                    break
                                if line.endswith(','):
                                    line = line[:-1]
                                replacement_lines.append(line)
                        for replacement_line in replacement_lines:
                            match = re.search('([A-Za-z0-9_-]*)=(.*)', replacement_line)
                            if not match:
                                raise Exception('Failed to parse replacement line: %s' % replacement_line) from e
                            (type_variable, type_expression) = match.groups()
                            actual_py2tmp_error = re.sub(r'\b' + type_variable + r'\b', type_expression, actual_py2tmp_error)
                    except Exception:
                        raise Exception('Failed to parse MSVC template type arguments')
                break
        else:
            raise Exception(textwrap.dedent('''\
                Expected error {expected_error} but the compiler output did not contain user-facing _py2tmp errors.
                Compiler command line: {compiler_command}
                Error message was:
                {error_message}
                ''').format(expected_error = expected_py2tmp_error_regex, compiler_command = e.command, error_message = error_message_head))

        for line_number, line in enumerate(error_message_lines):
            match = re.search(py2tmp_error_message_extraction_regex, line)
            if match:
                actual_static_assert_error_line_number = line_number
                actual_static_assert_error = match.groups()[0]
                break
        else:
            raise Exception(textwrap.dedent('''\
                Expected error {expected_error} but the compiler output did not contain static_assert errors.
                Compiler command line: {compiler_command}
                Error message was:
                {error_message}
                ''').format(expected_error = expected_py2tmp_error_regex, compiler_command=e.command, error_message = error_message_head))

        try:
            regex_search_result = re.search(expected_py2tmp_error_regex, actual_py2tmp_error)
        except Exception as e:
            raise Exception('re.search() failed for regex \'%s\'' % expected_py2tmp_error_regex) from e
        if not regex_search_result:
            raise Exception(textwrap.dedent('''\
                The compilation failed as expected, but with a different error type.
                Expected _py2tmp error type:    {expected_py2tmp_error_regex}
                Error type was:               {actual_py2tmp_error}
                Expected static assert error: {expected_py2tmp_error_desc_regex}
                Static assert was:            {actual_static_assert_error}
                Error message was:
                {error_message}
                '''.format(
                expected_py2tmp_error_regex = expected_py2tmp_error_regex,
                actual_py2tmp_error = actual_py2tmp_error,
                expected_py2tmp_error_desc_regex = expected_py2tmp_error_desc_regex,
                actual_static_assert_error = actual_static_assert_error,
                error_message = error_message_head)))
        try:
            regex_search_result = re.search(expected_py2tmp_error_desc_regex, actual_static_assert_error)
        except Exception as e:
            raise Exception('re.search() failed for regex \'%s\'' % expected_py2tmp_error_desc_regex) from e
        if not regex_search_result:
            raise Exception(textwrap.dedent('''\
                The compilation failed as expected, but with a different error message.
                Expected _py2tmp error type:    {expected_py2tmp_error_regex}
                Error type was:               {actual_py2tmp_error}
                Expected static assert error: {expected_py2tmp_error_desc_regex}
                Static assert was:            {actual_static_assert_error}
                Error message:
                {error_message}
                '''.format(
                expected_py2tmp_error_regex = expected_py2tmp_error_regex,
                actual_py2tmp_error = actual_py2tmp_error,
                expected_py2tmp_error_desc_regex = expected_py2tmp_error_desc_regex,
                actual_static_assert_error = actual_static_assert_error,
                error_message = error_message_head)))

        # 6 is just a constant that works for both g++ (<=6.0.0 at least) and clang++ (<=4.0.0 at least).
        # It might need to be changed.
        if actual_py2tmp_error_line_number > 6 or actual_static_assert_error_line_number > 6:
            raise Exception(textwrap.dedent('''\
                The compilation failed with the expected message, but the error message contained too many lines before the relevant ones.
                The error type was reported on line {actual_py2tmp_error_line_number} of the message (should be <=6).
                The static assert was reported on line {actual_static_assert_error_line_number} of the message (should be <=6).
                Error message:
                {error_message}
                '''.format(
                actual_py2tmp_error_line_number = actual_py2tmp_error_line_number,
                actual_static_assert_error_line_number = actual_static_assert_error_line_number,
                error_message = error_message_head)))

        for line in error_message_lines[:max(actual_py2tmp_error_line_number, actual_static_assert_error_line_number)]:
            if re.search('tmppy::impl', line):
                raise Exception(
                    'The compilation failed with the expected message, but the error message contained some metaprogramming types in the output (besides Error). Error message:\n%s' + error_message_head)

    expect_cpp_code_compile_error_helper(check_error, source_code, test_params)

def expect_cpp_code_success(source_code, test_params={}):
    """
    Tests that the given source compiles and runs successfully.

    :param source_code: The second part of the source code. Any identifiers will be replaced using test_params
           (where a replacement is defined). This will be dedented.
    :param test_params: A dict containing the definition of some identifiers. Each identifier in
           source_code will be replaced (textually) with its definition (if a definition was provided).
    """
    source_code = _construct_final_source_code(source_code, test_params)

    if 'main(' not in source_code:
        source_code += textwrap.dedent('''
            int main() {
            }
            ''')

    source_file_name = _create_temporary_file(source_code, file_name_suffix='.cpp')
    executable_suffix = {'posix': '', 'nt': '.exe'}[os.name]
    output_file_name = _create_temporary_file('', executable_suffix)

    compiler.compile_and_link(
        source=source_file_name,
        include_dirs=[MPYL_INCLUDE_DIR],
        output_file_name=output_file_name,
        args=[])

    run_compiled_executable(output_file_name)

    # Note that we don't delete the temporary files if the test failed. This is intentional, keeping them around helps debugging the failure.
    try_remove_temporary_file(source_file_name)
    try_remove_temporary_file(output_file_name)

def _get_function_body(f):
    source_code, _ = inspect.getsourcelines(f)
    assert source_code[0].startswith('@'), source_code[0]
    assert source_code[1].endswith('():\n'), source_code[1]
    return textwrap.dedent(''.join(source_code[2:]))

def assert_compilation_succeeds(f):
    @wraps(f)
    def wrapper():
        source_code = _get_function_body(f)
        cpp_source = py2tmp.convert_to_cpp(source_code)
        expect_cpp_code_success(cpp_source)
    return wrapper

def assert_compilation_fails(expected_py2tmp_error_regex: str, expected_py2tmp_error_desc_regex: str):
    def eval(f):
        @wraps(f)
        def wrapper():
            source_code = _get_function_body(f)
            cpp_source = py2tmp.convert_to_cpp(source_code)
            expect_cpp_code_compile_error(
                expected_py2tmp_error_regex,
                expected_py2tmp_error_desc_regex,
                cpp_source)
        return wrapper
    return eval

def assert_compilation_fails_with_generic_error(expected_error_regex: str):
    def eval(f):
        @wraps(f)
        def wrapper():
            source_code = _get_function_body(f)
            cpp_source = py2tmp.convert_to_cpp(source_code)
            expect_cpp_code_generic_compile_error(
                expected_error_regex,
                cpp_source)
        return wrapper
    return eval

def assert_conversion_fails(f):
    @wraps(f)
    def wrapper():
        source_code = _get_function_body(f)
        actual_source_lines = []
        expected_error_regex = None
        expected_error_line = None
        for line_index, line in enumerate(source_code.splitlines()):
            error_regex_marker = ' # error: '
            if error_regex_marker in line:
                if expected_error_regex:
                    raise Exception('Multiple expected errors in the same test are not supported')
                [line, expected_error_regex] = line.split(error_regex_marker)
                expected_error_line = line_index
            actual_source_lines.append(line)

        if not expected_error_regex:
            raise Exception('assert_conversion_fails was used, but no expected error regex was found. ')

        try:
            py2tmp.convert_to_cpp('\n'.join(actual_source_lines))
            e = None
        except Exception as e1:
            e = e1

        if not e:
            raise Exception('Expected an exception, but the _py2tmp conversion completed successfully')

        if not re.match(expected_error_regex, e.args[0]):
            raise Exception('An exception was thrown, but it didn\'t match the expected error regex. The error message was: ' + e.args[0])
    return wrapper

# Note: this is not the main function of this file, it's meant to be used as main function from test_*.py files.
def main(file):
    code = pytest.main(args = sys.argv + [os.path.realpath(file)])
    exit(code)
