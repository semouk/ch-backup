"""
util module defines various auxiliary functions
"""
import collections
import glob
import grp
import os
import pwd
import re
import shutil
import string
import time
from dataclasses import fields as data_fields
from datetime import datetime, timedelta, timezone
from functools import partial
from inspect import currentframe
from itertools import islice
from typing import (BinaryIO, Callable, Iterable, Iterator, List, Tuple, Type, TypeVar, Union)

import humanfriendly
import tenacity

from ch_backup import logging
from ch_backup.exceptions import ClickhouseBackupError

T = TypeVar('T')

LOCAL_TZ = timezone(timedelta(seconds=-1 * (time.altzone if time.daylight else time.timezone)))
_ALLOWED_NAME_CHARS = set(['_'] + list(string.ascii_letters) + list(string.digits))
_HEX_UPPERCASE_TABLE = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'A', 'B', 'C', 'D', 'E', 'F']


def chown_dir_contents(user: str, group: str, dir_path: str, need_recursion: bool = False) -> None:
    """
    Recursively change directory user/group
    """
    if need_recursion:
        for path, dirs, files in os.walk(dir_path):
            for directory in dirs:
                shutil.chown(os.path.join(path, directory), user, group)
            for file in files:
                shutil.chown(os.path.join(path, file), user, group)
    else:
        for path in os.listdir(dir_path):
            shutil.chown(os.path.join(dir_path, path), user, group)


def list_dir_files(dir_path: str) -> List[str]:
    """
    Returns paths of all files of directory (recursively), relative to its path
    """
    return [file[len(dir_path) + 1:] for file in filter(os.path.isfile, glob.iglob(dir_path + '/**', recursive=True))]


def setup_environment(config: dict) -> None:
    """
    Set environment variables
    """
    try:
        env_value = ':'.join(config['ca_bundle'])
        os.environ['REQUESTS_CA_BUNDLE'] = env_value
    except KeyError:
        pass


def demote_group(new_group: str) -> None:
    """
    Perform group change
    """
    new_gid = grp.getgrnam(new_group).gr_gid
    os.setgid(new_gid)


def demote_user(new_user: str) -> None:
    """
    Perform user change
    """
    new_uid = pwd.getpwnam(new_user).pw_uid
    os.setuid(new_uid)


def escape(s: str) -> str:
    """
    Escaping special character '`'
    """
    return r'\`'.join(s.split('`'))


def demote_user_group(new_user: str, new_group: str) -> None:
    """
    Perform user and group change
    """
    demote_group(new_group)
    demote_user(new_user)


def drop_privileges(config: dict) -> bool:
    """
    Demote user/group if needed
    """

    try:
        if config['drop_privileges']:
            demote_user_group(config['user'], config['group'])
            return True
    except KeyError:
        pass

    return False


def strip_query(query_text: str) -> str:
    """
    Remove query without newlines and duplicate whitespaces.
    """
    return re.sub(r'\s{2,}', ' ', query_text.replace('\n', ' ')).strip()


def now() -> datetime:
    """
    Return local datetime with timezone information.
    """
    return datetime.now(LOCAL_TZ)


def utcnow() -> datetime:
    """
    Return UTC datetime with timezone information.
    """
    return datetime.now(timezone.utc)


def wait_for(func: Callable[[], bool],
             timeout_s: float,
             interval_s: float = 1.,
             on_wait_begin: Callable = None,
             on_wait_end: Callable = None,
             on_interval_begin: Callable = None,
             on_interval_end: Callable = None) -> None:
    """
    Waits for function to return True in time.
    """
    if on_wait_begin is not None:
        on_wait_begin()

    time_left = timeout_s
    while time_left > 0 and func():
        if on_interval_begin is not None:
            on_interval_begin()

        time.sleep(interval_s)
        time_left -= interval_s

        if on_interval_end is not None:
            on_interval_end()

    if on_wait_end is not None:
        on_wait_end()


def retry(exception_types: Union[type, tuple] = Exception,
          max_attempts: int = 5,
          max_interval: float = 5,
          retry_if: tenacity.retry_base = tenacity.retry_always) -> Callable:
    """
    Function decorator that retries wrapped function on failures.
    """
    def _log_retry(retry_state):
        logging.debug("Retrying %s.%s in %.2fs, attempt: %s, reason: %r", retry_state.fn.__module__,
                      retry_state.fn.__qualname__, retry_state.next_action.sleep, retry_state.attempt_number,
                      retry_state.outcome.exception())

    return tenacity.retry(retry=tenacity.retry_all(tenacity.retry_if_exception_type(exception_types), retry_if),
                          wait=tenacity.wait_random_exponential(multiplier=0.5, max=max_interval),
                          stop=tenacity.stop_after_attempt(max_attempts),
                          reraise=True,
                          before_sleep=_log_retry)


def get_table_zookeeper_paths(tables: Iterable) -> Iterable[Tuple]:
    """
    Parse ZooKeeper path from create statement.
    """
    result = []
    for table in tables:
        match = re.search(R"""Replicated\S{0,20}MergeTree\(\'(?P<zk_path>[^']+)\',""", table.create_statement)
        if not match:
            raise ClickhouseBackupError(f'Couldn`t parse create statement for zk path: "{table}')
        result.append((table, match.group('zk_path')))
    return result


def get_database_zookeeper_paths(databases: Iterable[str]) -> Iterable[str]:
    """
    Parse ZooKeeper path from create statement.
    """
    result = []
    for db_sql in databases:
        match = re.search(R"""Replicated\(\'(?P<zk_path>[^']+)\', '(?P<shard>[^']+)', '(?P<replica>[^']+)'""", db_sql)
        if not match:
            continue
        result.append(f'{match.group("zk_path")}/replicas/{match.group("shard")}|{match.group("replica")}')
    return result


def compare_schema(schema_a: str, schema_b: str) -> bool:
    """
    Normalize table schema for comparison.
    `... ENGINE = Distributed('aaa', bbb, ccc, xxx) ...` may be in ver. before 19.16, 20.1
    `... ENGINE = Distributed('aaa', 'bbb', 'ccc', xxx) ...` in ver. 19.16+, 20.1+

    Also
    `ATTACH TABLE `db`.`table` UUID '...' ...` from file schema, multiline
    `CREATE TABLE db.table ` from sql request, single line
    """
    def _normalize(schema: str) -> str:
        res = re.sub(r"ENGINE = Distributed\('([^']+)', ('?)(\w+)\2, ('?)(\w+)\4(, .*)?\)",
                     r"ENGINE = Distributed('\1', '\3', '\5'\6)", schema).lower()
        res = re.sub(r"^attach table `?([^`\.]+)`?\.\`?([^`\.]+)\` (uuid '[^']+')?", r"create table \1.\2", res)
        res = re.sub(r"\s+", " ", res)
        res = re.sub(r"\( +", "(", res)
        res = re.sub(r" +\)", ")", res)
        res = re.sub(r" $", "", res)
        return res

    return _normalize(schema_a) == _normalize(schema_b)


def format_size(value: int) -> str:
    """
    Format a value in bytes to human-friendly representation.
    """
    return humanfriendly.format_size(value, binary=True)


def escape_metadata_file_name(name: str) -> str:
    """
    Escape object name to use for metadata file.
    Should be equal to https://github.com/ClickHouse/ClickHouse/blob/master/src/Common/escapeForFileName.cpp#L8
    """
    result = bytearray(b'')
    name_b = name.encode('utf-8')
    for c in name_b:
        if chr(c) in _ALLOWED_NAME_CHARS:
            result.append(c)
        else:
            result.extend(f'%{_HEX_UPPERCASE_TABLE[int(c / 16)]}{_HEX_UPPERCASE_TABLE[c % 16]}'.encode('utf-8'))
    return result.decode('utf-8')


def chunked(iterable: Iterable, n: int) -> Iterator[list]:
    """
    Chunkify iterable into lists of length n. The last chunk may be shorter.

    Based on https://docs.python.org/3/library/itertools.html#itertools-recipes

    >>> list(chunked('ABCDEFG', 3))
    [['A', 'B', 'C'], ['D', 'E', 'F'], ['G']]
    """
    if n < 1:
        raise ValueError('n must be at least one')
    it = iter(iterable)
    while True:
        chunk = list(islice(it, n))
        if not chunk:
            break
        yield chunk


def read_by_chunks(file: BinaryIO, chunk_size: int) -> Iterator[bytes]:
    """
    Read and yield file-like object by chunks.
    """
    for chunk in iter(partial(file.read, chunk_size), b''):
        yield chunk


def current_func_name() -> str:
    """
    Return the current function name.

    Current function is a function calling func_name()
    """
    return currentframe().f_back.f_code.co_name  # type: ignore[union-attr]


def exhaust_iterator(iterator: Iterator) -> None:
    """
    Read all elements from iterator until it stops.
    """
    collections.deque(iterator, maxlen=0)


def dataclass_from_dict(type_: Type[T], data: dict) -> T:
    """
    Create dataclass instance from dictionary.

    Function ignores extra keys and is not recursive.
    """
    class_fields = {f.name for f in data_fields(type_)}
    return type_(**{k: v for k, v in data.items() if k in class_fields})  # type: ignore[call-arg]
