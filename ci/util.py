# Copyright (c) 2019-2020 SAP SE or an SAP affiliate company. All rights reserved. This file is
# licensed under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import os
import pathlib
import shutil
import sys
import yaml
import yamllint
import yamllint.config

import termcolor

from urllib.parse import urlunparse


class Failure(RuntimeError, ValueError):
    pass


class LintingError(Failure):
    pass


class LintingResult:
    def __init__(self, problems):
        problems_dict = collections.defaultdict(list)
        for p in problems:
            problems_dict[self._normalise_problem_level(p)].append(p)
        self.problems_dict = problems_dict

    def _normalise_problem_level(self, p):
        # Yamllint uses a dict with mappings for both int->level and level->int. Consistently use
        # the int representation
        if isinstance(p.level, str):
            return yamllint.linter.PROBLEM_LEVELS[p.level]
        else:
            return p.level

    def problems(self):
        return self.problems_dict.items()

    def has_problems(self):
        return bool(self.problems_dict)

    def max_level(self):
        if self.problems:
            max(self.problems_dict.keys())
        return 0

    def __str__(self):
        if not self.has_problems():
            return "No linting problems found"

        return '\n'.join(s for s in [
            f'[{yamllint.linter.PROBLEM_LEVELS[l]}] {p}'
            for l in self.problems_dict.keys()
            for p in self.problems_dict[l]
        ])


def _set_cli(is_cli: bool):
    ctx().args._cli = is_cli
    global Failure
    if is_cli:
        class Failure(SystemExit):
            pass
    else:
        class Failure(RuntimeError):
            pass


def existing_file(path):
    if isinstance(path, pathlib.Path):
        is_file = path.is_file()
    else:
        is_file = os.path.isfile(path)
    if not is_file:
        fail('not an existing file: ' + str(path))
    return path


def existing_dir(path: str):
    if isinstance(path, pathlib.Path):
        is_dir = path.is_dir()
    else:
        is_dir = os.path.isdir(path)
    if not is_dir:
        fail('not an existing directory: ' + str(path))
    return path


def check_type(instance, type):
    if not isinstance(instance, type):
        fail('{i} is not an instance of {t}'.format(i=instance, t=type))
    return instance


def gardener_cicd_libs_version():
    p = os.path.abspath(os.path.realpath(__file__))
    versionfile_file_path = os.path.join(os.path.dirname(p), 'version')
    with open(versionfile_file_path, 'rt') as f:
        return f.readline()


class CliHint(object):
    def __init__(self, typehint=str, *args, **kwargs):
        self.argparse_args = dict(*args, **kwargs)
        self.typehint = typehint


class CliHints(object):
    '''
    predefined cli hint instances
    '''
    @staticmethod
    def existing_file(help: str='an existing file', **kwargs):
        return CliHint(type=existing_file, help=help, **kwargs)

    @staticmethod
    def yaml_file(help: str='an existing YAML file', **kwargs):
        return CliHint(type=parse_yaml_file, help=help, **kwargs)

    @staticmethod
    def existing_dir(help: str='an existing directory', **kwargs):
        return CliHint(type=existing_dir, help=help, **kwargs)

    @staticmethod
    def non_empty_string(help:str = 'a non-empty string', **kwargs):
        return CliHint(type=not_empty, help=help, **kwargs)


def ctx():
    # late import because the ctx module is altered after all existing modules have
    # already been imported
    import ctx
    return ctx


def _quiet():
    return ctx().args and ctx().args.quiet


def _verbose():
    return ctx().args and ctx().args.verbose


def _cli():
    return bool(ctx().args and hasattr(ctx().args, '._cli') and ctx().args._cli)


def _print(msg, colour, outfh=sys.stdout):
    if not msg:
        return
    if not outfh.isatty():
        outfh.write(msg + '\n')
    else:
        outfh.write(termcolor.colored(msg, colour) + '\n')

    outfh.flush()


def error(msg=None):
    if _quiet():
        return
    if msg:
        _print('ERROR: ' + str(msg), colour='red', outfh=sys.stderr)


def fail(msg=None):
    if msg:
        _print('ERROR: ' + str(msg), colour='red', outfh=sys.stderr)
    raise RuntimeError()


def info(msg:str):
    if _quiet():
        return
    if msg:
        _print('INFO: ' + str(msg), colour='cyan', outfh=sys.stdout)


def warning(msg:str):
    if _quiet():
        return
    if msg:
        _print('WARNING: ' + str(msg), colour='yellow', outfh=sys.stderr)


def verbose(msg:str):
    if not _verbose():
        return
    if msg:
        _print('VERBOSE: ' + msg, colour=None, outfh=sys.stdout)


def success(msg:str):
    if msg:
        _print('SUCCESS: ' + msg, colour='green', outfh=sys.stdout)


def not_empty(value):
    if not value or len(value) == 0:
        fail('passed value must not be empty')
    return value


def not_none(value):
    if value is None:
        fail('passed value must not be None')
    return value


def none(value):
    if value is not None:
        fail('value must be None')
    return value


def is_yaml_file(path):
    with open(path) as f:
        try:
            if yaml.load(f, Loader=yaml.SafeLoader):
                return True
        except Exception:
            if not _quiet():
                warning('an error occurred whilst trying to parse {f}'.format(f=path))
            raise
    return False


def load_yaml(stream, lint=False, linter_config=None, *args, **kwargs):
    '''
    Parses YAML from the given stream in a (by default) safe manner. The given stream and any
    *args and **kwargs are passed to `yaml.load`, by default using yaml.SafeLoader.

    In addition to using SafeLoader, a mitigation against YAML Bombs (Billion Laughs Attack) is
    applied (by limiting amount of allowed elements)

    @raises ValueError if YAML Bomb was (heuristically) detected.
    '''
    if lint:
        # redefine stream, as yamllint will read() this, resulting in stream being empty
        # when parsing later
        stream = stream.read()
        linter_config = yamllint.config.YamlLintConfig(yaml.dump(not_none(linter_config)))
        linting_result = LintingResult(yamllint.linter.run(input=stream, conf=linter_config))
        _print_linting_findings(linting_result)

    if not 'Loader' in kwargs:
        kwargs['Loader'] = yaml.SafeLoader

    parsed = yaml.load(stream, *args, **kwargs)
    _count_elements(parsed)
    return parsed


def parse_yaml_file(path, lint=False, max_elements_count=100000):
    if lint:
        lint_yaml_file(path)

    with open(path) as f:
        parsed = yaml.load(f, Loader=yaml.SafeLoader)
        # mitigate yaml bomb
        _count_elements(parsed)
        return parsed


def _count_elements(value, count=0, max_elements_count=100000):
    '''
    recursively counts elements contained in the given value. Before each recursion step,
    the amount of encountered elements is checked against a maximum allowed elements count.
    If said threshold is exceeded, recursion is aborted and a `ValueError` is raised.

    This function is intended to be used as a mitigation against "Billion laughs attack"
    (https://en.wikipedia.org/wiki/Billion_laughs_attack).

    @param value: typically a dict or a list. Other types will yield a count of 1
    '''
    if count > max_elements_count:
        raise ValueError('dict too large')

    if not isinstance(value, dict):
        if isinstance(value, list):
            leng = 0
            for e in value:
                leng += _count_elements(e, count=count+leng)
            return leng
        else:
            return 1

    leng = 0

    for value in value.values():
        leng += _count_elements(value, count=count+leng)

    return leng


def lint_yaml_file(path, linter_config: dict={'extends': 'relaxed'}):
    existing_file(path)
    info(f'linting YAML file: {path}')

    with open(path) as f:
        linting_result = _lint_yaml(f.read(), linter_config)

    if not linting_result.has_problems():
        return

    _print_linting_findings(linting_result)

    if linting_result.max_level() >= yamllint.linter.PROBLEM_LEVELS['error']:
        raise LintingError('Found some Errors while linting. See above.')


def _print_linting_findings(linting_result: LintingResult):
    for level, problems in linting_result.problems():
        if level < yamllint.linter.PROBLEM_LEVELS['error']:
            for p in problems:
                warning(p)
        else:
            for p in problems:
                error(p)


def _lint_yaml(input, config):
    cfg = yamllint.config.YamlLintConfig(yaml.dump(config))
    return LintingResult(yamllint.linter.run(input=input, conf=cfg))


def lint_yaml(input, config={'extends': 'relaxed'}):

    linting_result = _lint_yaml(input=input, config=config)

    if not linting_result.has_problems():
        return

    if linting_result.max_level() <= yamllint.linter.PROBLEM_LEVELS['warning']:
        warning(f'Found some problems while linting:\n{linting_result}')
    else:
        raise LintingError(f'Found some errors while linting. \n{linting_result}')


def random_str(prefix=None, length=12):
    import random
    import string
    if prefix:
        length -= len(prefix)
    else:
        prefix = ''
    return prefix + ''.join(random.choice(string.ascii_lowercase) for _ in range(length))


def create_url_from_attributes(
    netloc: str,
    scheme='https',
    path='',
    params='',
    query='',
    fragment=''
):
    return urlunparse((scheme, netloc, path, params, query, fragment))


def check_env(name: str):
    '''
    returns: the specified environment variable's value.
    raises: ci.util.Failure if no environment variable with the given name is defined
    '''
    not_none(name)
    if name in os.environ:
        return os.environ[name]
    fail('env var {n} must be set'.format(n=name))


def _running_on_ci():
    '''
    heuristically determines whether or not the caller is running inside a central
    CI/CD job.
    '''
    return 'CC_ROOT_DIR' in os.environ


def current_config_set_name():
    if not _running_on_ci():
        raise RuntimeError('must only be called within CI/CD')
    return check_env('CONCOURSE_CURRENT_CFG')


def _root_dir():
    if not _running_on_ci():
        raise RuntimeError('must only be called within CI/CD')
    return check_env('CC_ROOT_DIR')


def urljoin(*parts):
    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    last = parts[-1]
    middle = parts[1:-1]

    first = first.rstrip('/')
    middle = list(map(lambda s: s.strip('/'), middle))
    last = last.lstrip('/')

    return '/'.join([first] + middle + [last])


def which(cmd_name: str) -> str:
    '''
    wrapper around shutil.which that calls ci.util.fail if the requested executable is not
    found in the PATH.
    '''
    cmd_path = shutil.which(cmd_name)
    if not cmd_path:
        fail("{cmd} not found in PATH".format(cmd=cmd_name))
    return cmd_path


def merge_dicts(base: dict, other: dict, list_semantics='merge'):
    '''
    merges copies of the given dict instances and returns the merge result.
    The arguments remain unmodified. However, it must be possible to copy them
    using `copy.deepcopy`.

    Merging is done using the `deepmerge` module. In case of merge conflicts, values from
    `other` overwrite values from `base`.

    By default, different from the original implementation, a merge will be applied to
    lists. This results in deduplication retaining element order. The elements from `other` are
    appended to those from `base`.

    '''
    not_none(base)
    not_none(other)

    from deepmerge import Merger

    if list_semantics == 'merge':
        # monkey-patch merge-strategy for lists
        list_merge_strategy = Merger.PROVIDED_TYPE_STRATEGIES[list]
        list_merge_strategy.strategy_merge = lambda c, p, base, other: \
            list(base) + [e for e in other if e not in base]

        strategy_cfg = [(list, ['merge']), (dict, ['merge'])]
        merger = Merger(strategy_cfg, ['override'], ['override'])
    elif list_semantics is None:
        strategy_cfg = [(dict, ['merge'])]
        merger = Merger(strategy_cfg, ['override'], ['override'])
    else:
        raise NotImplementedError

    from copy import deepcopy
    # copy dicts, so they remain unmodified
    return merger.merge(deepcopy(base), deepcopy(other))


class FluentIterable(object):
    ''' a fluent object stream processing chain builder inspired by guava's FluentIterable

    Example:
        result = FluentIterable(items=(1,2,3))
            .filter(lambda e: e < 2)
            .map(lambda e: e * 2)
            .as_generator()

    '''

    def __init__(self, items):
        def starter():
            yield from items
        self.ops = [starter]

    def filter(self, filter_func):
        last_op = self.ops[-1]

        def f():
            yield from filter(filter_func, last_op())

        self.ops.append(f)
        return self

    def map(self, map_func):
        last_op = self.ops[-1]

        def m():
            yield from map(map_func, last_op())

        self.ops.append(m)
        return self

    def as_generator(self):
        return self.ops[-1]()

    def as_list(self):
        return list(self.as_generator())
