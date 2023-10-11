#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import hashlib
import itertools
import logging
import logging.config
import os
from typing import Callable, Any, Iterable, Optional
import pickle as c_pickle
import shutil
import signal
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor as Executor
from contextlib import ExitStack
from functools import partial
from heapq import heapify, heappop, heapreplace
from operator import is_not
from pathlib import Path
from typing import List, Tuple, Literal

import cloudpickle as f_pickle
import lmdb

LOGGER = logging.getLogger(__name__)
PartyMeta = Tuple[Literal["guest", "host", "arbiter", "local"], str]


class FederationDataType(object):
    OBJECT = "obj"
    TABLE = "Table"
    SPLIT_OBJECT = "split_obj"


serialize = c_pickle.dumps
deserialize = c_pickle.loads

# default message max size in bytes = 1MB
DEFAULT_MESSAGE_MAX_SIZE = 1048576

if (STANDALONE_DATA_PATH := os.getenv("STANDALONE_DATA_PATH")) is not None:
    _data_dir = Path(STANDALONE_DATA_PATH)
    LOGGER.debug(f"env STANDALONE_DATA_PATH is set to {STANDALONE_DATA_PATH}, using {_data_dir} as data dir")
else:
    _data_dir = Path(
        os.path.abspath(
            os.path.join(
                os.path.dirname(os.path.realpath(__file__)), os.pardir, os.pardir, os.pardir, "__standalone_data__"
            )
        )
    )
    LOGGER.debug(f"env STANDALONE_DATA_PATH is not set, using {_data_dir} as data dir")


def _watch_thread_react_to_parent_die(ppid, logger_config):
    """
    this function is call when a process is created, and it will watch parent process and initialize loggers
    Args:
        ppid: parent process id
    """

    # watch parent process, if parent process is dead, then kill self
    # the trick is to use os.kill(ppid, 0) to check if parent process is alive periodically
    # and if parent process is dead, then kill self
    #
    # Note: this trick is modified from the answer by aaron: https://stackoverflow.com/a/71369760/14697733
    pid = os.getpid()

    def f():
        while True:
            try:
                os.kill(ppid, 0)
            except OSError:
                os.kill(pid, signal.SIGTERM)
            time.sleep(1)

    thread = threading.Thread(target=f, daemon=True)
    thread.start()

    # initialize loggers
    if logger_config is not None:
        logging.config.dictConfig(logger_config)
    # else:
    #     level = os.getenv("DEBUG_MODE_LOG_LEVEL", "DEBUG")
    #     try:
    #         import rich.logging
    #
    #         logging_class = "rich.logging.RichHandler"
    #         logging_formatters = {}
    #         handlers = {
    #             "console": {
    #                 "class": logging_class,
    #                 "level": level,
    #                 "filters": [],
    #             }
    #         }
    #     except ImportError:
    #         logging_class = "logging.StreamHandler"
    #         logging_formatters = {
    #             "console": {
    #                 "format": "[%(levelname)s][%(asctime)-8s][%(process)s][%(module)s.%(funcName)s][line:%(lineno)d]: %(message)s"
    #             }
    #         }
    #         handlers = {
    #             "console": {
    #                 "class": logging_class,
    #                 "level": level,
    #                 "formatter": "console",
    #             }
    #         }
    #     logging.config.dictConfig(dict(
    #         version=1,
    #         formatters=logging_formatters,
    #         handlers=handlers,
    #         filters={},
    #         loggers={},
    #         root=dict(handlers=["console"], level="DEBUG"),
    #         disable_existing_loggers=False,
    #     ))


# noinspection PyPep8Naming
class Table(object):
    def __init__(
        self,
        session: "Session",
        namespace: str,
        name: str,
        partitions,
        key_serdes_type: int,
        value_serdes_type: int,
        partitioner_type: int,
        need_cleanup=True,
    ):
        self._need_cleanup = need_cleanup
        self._namespace = namespace
        self._name = name
        self._partitions = partitions
        self._session = session
        self._key_serdes_type = key_serdes_type
        self._value_serdes_type = value_serdes_type
        self._partitioner_type = partitioner_type

    @property
    def key_serdes_type(self):
        return self._key_serdes_type

    @property
    def value_serdes_type(self):
        return self._value_serdes_type

    @property
    def partitioner_type(self):
        return self._partitioner_type

    @property
    def partitions(self):
        return self._partitions

    @property
    def name(self):
        return self._name

    @property
    def namespace(self):
        return self._namespace

    def __del__(self):
        if self._need_cleanup:
            try:
                self.destroy()
            except:
                pass

    def __str__(self):
        return f"<Table {self._namespace}|{self._name}|{self._partitions}|{self._need_cleanup}>"

    def __repr__(self):
        return self.__str__()

    def destroy(self):
        for p in range(self._partitions):
            with self._get_env_for_partition(p, write=True) as env:
                db = env.open_db()
                with env.begin(write=True) as txn:
                    txn.drop(db)
        _TableMetaManager.destroy_table(self._namespace, self._name)

    def take(self, n, **kwargs):
        if n <= 0:
            raise ValueError(f"{n} <= 0")
        return list(itertools.islice(self.collect(**kwargs), n))

    def count(self):
        cnt = 0
        for p in range(self._partitions):
            with self._get_env_for_partition(p) as env:
                cnt += env.stat()["entries"]
        return cnt

    # noinspection PyUnusedLocal
    def collect(self, **kwargs):
        iterators = []
        with ExitStack() as s:
            for p in range(self._partitions):
                env = s.enter_context(self._get_env_for_partition(p))
                txn = s.enter_context(env.begin())
                iterators.append(s.enter_context(txn.cursor()))

            # Merge sorted
            entries = []
            for _id, it in enumerate(iterators):
                if it.next():
                    key, value = it.item()
                    entries.append([key, value, _id, it])
            heapify(entries)
            while entries:
                key, value, _, it = entry = entries[0]
                yield key, value
                if it.next():
                    entry[0], entry[1] = it.item()
                    heapreplace(entries, entry)
                else:
                    _, _, _, it = heappop(entries)

    def reduce(self, func):
        return self._session.submit_reduce(
            func, num_partitions=self._partitions, name=self._name, namespace=self._namespace
        )

    def map_reduce_partitions_with_index(
        self,
        map_partition_op: Callable[[int, Iterable], Iterable],
        reduce_partition_op: Optional[Callable[[Any, Any], Any]],
        output_partitioner: Optional[Callable[[bytes, int], int]],
        shuffle,
        output_key_serdes_type,
        output_value_serdes_type,
        output_partitioner_type,
        need_cleanup=True,
        output_name=None,
        output_namespace=None,
    ):
        if not shuffle:
            # noinspection PyProtectedMember
            results = self._session._submit_map_reduce_partitions_with_index(
                _do_mrwi_no_shuffle,
                map_partition_op,
                reduce_partition_op,
                self._partitions,
                self._name,
                self._namespace,
                output_partitioner=output_partitioner,
                output_name=output_name,
                output_namespace=output_namespace,
            )
            result = results[0]
            # noinspection PyProtectedMember
            return _create_table(
                session=self._session,
                name=result.name,
                namespace=result.namespace,
                partitions=self._partitions,
                need_cleanup=need_cleanup,
                key_serdes_type=output_key_serdes_type,
                value_serdes_type=output_value_serdes_type,
                partitioner_type=output_partitioner_type,
            )
        if reduce_partition_op is None:
            # noinspection PyProtectedMember
            results = self._session._submit_map_reduce_partitions_with_index(
                _do_mrwi_shuffle_no_reduce,
                map_partition_op,
                reduce_partition_op,
                self._partitions,
                self._name,
                self._namespace,
                output_partitioner=output_partitioner,
                output_name=output_name,
                output_namespace=output_namespace,
            )
            result = results[0]
            # noinspection PyProtectedMember
            return _create_table(
                session=self._session,
                name=result.name,
                namespace=result.namespace,
                partitions=self._partitions,
                need_cleanup=need_cleanup,
                key_serdes_type=output_key_serdes_type,
                value_serdes_type=output_value_serdes_type,
                partitioner_type=output_partitioner_type,
            )

        # Step 1: do map and write intermediate results to cache table
        # noinspection PyProtectedMember
        intermediate = self._session._submit_map_reduce_partitions_with_index(
            _do_mrwi_map_and_shuffle_write,
            map_partition_op,
            None,
            self._partitions,
            self._name,
            self._namespace,
            output_partitioner=output_partitioner,
        )[0]
        # Step 2: do shuffle read and reduce
        # noinspection PyProtectedMember
        result = self._session._submit_map_reduce_partitions_with_index(
            _do_mrwi_shuffle_read_and_reduce,
            None,
            reduce_partition_op,
            self._partitions,
            intermediate.name,
            intermediate.namespace,
            output_name,
            output_namespace,
        )[0]
        output = _create_table(
            session=self._session,
            name=result.name,
            namespace=result.namespace,
            partitions=self._partitions,
            need_cleanup=need_cleanup,
            key_serdes_type=output_key_serdes_type,
            value_serdes_type=output_value_serdes_type,
            partitioner_type=output_partitioner_type,
        )

        # drop cache table
        for p in range(self._partitions):
            with _get_env(intermediate.namespace, intermediate.name, str(p), write=True) as env:
                db = env.open_db()
                with env.begin(write=True) as txn:
                    txn.drop(db)

        path = _data_dir.joinpath(intermediate.namespace, intermediate.name)
        shutil.rmtree(path, ignore_errors=True)
        return output

    def join(self, other: "Table", merge_op):
        return self._binary(
            other,
            merge_op,
            _do_join,
            need_cleanup=True,
            key_serdes_type=self._key_serdes_type,
            value_serdes_type=self._value_serdes_type,
            partitioner_type=self._partitioner_type,
        )

    def subtract_by_key(self, other: "Table"):
        return self._binary(
            other,
            None,
            _do_subtract_by_key,
            need_cleanup=True,
            key_serdes_type=self._key_serdes_type,
            value_serdes_type=self._value_serdes_type,
            partitioner_type=self._partitioner_type,
        )

    def union(self, other: "Table", merge_op=lambda v1, v2: v1):
        return self._binary(
            other,
            merge_op,
            _do_union,
            need_cleanup=True,
            key_serdes_type=self._key_serdes_type,
            value_serdes_type=self._value_serdes_type,
            partitioner_type=self._partitioner_type,
        )

    def _binary(
        self, other: "Table", func, do_func, need_cleanup, key_serdes_type, value_serdes_type, partitioner_type
    ):
        session_id = self._session.session_id
        left, right = self, other
        if left._partitions != right._partitions:
            if other.count() > self.count():
                left = left._repartition(partitions=right._partitions)
            else:
                right = right._repartition(partitions=left._partitions)

        # noinspection PyProtectedMember
        results = self._session._submit_binary(
            func,
            do_func,
            left._partitions,
            left._name,
            left._namespace,
            right._name,
            right._namespace,
        )
        result: _Operand = results[0]
        # noinspection PyProtectedMember
        return _create_table(
            session=self._session,
            name=result.name,
            namespace=result.namespace,
            partitions=left._partitions,
            need_cleanup=need_cleanup,
            key_serdes_type=key_serdes_type,
            value_serdes_type=value_serdes_type,
            partitioner_type=partitioner_type,
        )

    def save_as(self, name, namespace, partitions=None, need_cleanup=True):
        if partitions is not None and partitions != self._partitions:
            return self._repartition(partitions=partitions, need_cleanup=True).copy_as(name, namespace, need_cleanup)

        return self.copy_as(name, namespace, need_cleanup)

    def copy_as(self, name, namespace, need_cleanup=True):
        return self.map_reduce_partitions_with_index(
            map_partition_op=lambda i, x: x,
            reduce_partition_op=None,
            output_partitioner=None,
            shuffle=False,
            need_cleanup=need_cleanup,
            output_name=name,
            output_namespace=namespace,
            output_key_serdes_type=self._key_serdes_type,
            output_value_serdes_type=self._value_serdes_type,
            output_partitioner_type=self._partitioner_type,
        )

    def _repartition(self, partitions, name=None, namespace=None, need_cleanup=True):
        # TODO: optimize repartition
        if partitions == self._partitions:
            return self
        if name is None:
            name = str(uuid.uuid1())
        if namespace is None:
            namespace = self._namespace
        dup = _create_table(self._session, name, namespace, partitions, need_cleanup)
        dup.put_all(self.collect())
        return dup

    def _get_env_for_partition(self, p: int, write=False):
        return _get_env(self._namespace, self._name, str(p), write=write)

    def put(self, k_bytes, v_bytes, partitioner: Callable[[bytes, int], int] = None):
        p = partitioner(k_bytes, self._partitions)
        with self._get_env_for_partition(p, write=True) as env:
            with env.begin(write=True) as txn:
                return txn.put(k_bytes, v_bytes)

    def put_all(self, kv_list: Iterable[Tuple[bytes, bytes]], partitioner: Callable[[bytes, int], int]):
        txn_map = {}
        with ExitStack() as s:
            for p in range(self._partitions):
                env = s.enter_context(self._get_env_for_partition(p, write=True))
                txn_map[p] = env, env.begin(write=True)
            try:
                for k_bytes, v_bytes in kv_list:
                    p = partitioner(k_bytes, self._partitions)
                    if not txn_map[p][1].put(k_bytes, v_bytes):
                        break
            except Exception as e:
                LOGGER.exception(f"put_all fail. exception: {e}")
                for p, (env, txn) in txn_map.items():
                    txn.abort()
                raise e
            else:
                for p, (env, txn) in txn_map.items():
                    txn.commit()

    def get(self, k_bytes: bytes, partitioner: Callable[[bytes, int], int]):
        p = partitioner(k_bytes, self._partitions)
        with self._get_env_for_partition(p) as env:
            with env.begin(write=True) as txn:
                old_value_bytes = txn.get(k_bytes)
                return None if old_value_bytes is None else deserialize(old_value_bytes)

    def delete(self, k_bytes: bytes, partitioner: Callable[[bytes, int], int]):
        p = partitioner(k_bytes, self._partitions)
        with self._get_env_for_partition(p, write=True) as env:
            with env.begin(write=True) as txn:
                old_value_bytes = txn.get(k_bytes)
                if txn.delete(k_bytes):
                    return None if old_value_bytes is None else deserialize(old_value_bytes)
                return None


# noinspection PyMethodMayBeStatic
class Session(object):
    def __init__(self, session_id, max_workers=None, logger_config=None):
        self.session_id = session_id
        self._pool = Executor(
            max_workers=max_workers,
            initializer=_watch_thread_react_to_parent_die,
            initargs=(
                os.getpid(),
                logger_config,
            ),
        )

    def __getstate__(self):
        # session won't be pickled
        pass

    def load(self, name, namespace):
        return _load_table(session=self, name=name, namespace=namespace)

    def create_table(
        self,
        name,
        namespace,
        partitions,
        need_cleanup,
        error_if_exist,
        key_serdes_type,
        value_serdes_type,
        partitioner_type,
    ):
        return _create_table(
            session=self,
            name=name,
            namespace=namespace,
            partitions=partitions,
            need_cleanup=need_cleanup,
            error_if_exist=error_if_exist,
            key_serdes_type=key_serdes_type,
            value_serdes_type=value_serdes_type,
            partitioner_type=partitioner_type,
        )

    # noinspection PyUnusedLocal
    def parallelize(
        self,
        data: Iterable,
        partition: int,
        partitioner: Callable[[bytes], int],
        key_serdes_type,
        value_serdes_type,
        partitioner_type,
    ):
        table = _create_table(
            session=self,
            name=str(uuid.uuid1()),
            namespace=self.session_id,
            partitions=partition,
            need_cleanup=True,
            key_serdes_type=key_serdes_type,
            value_serdes_type=value_serdes_type,
            partitioner_type=partitioner_type,
        )
        table.put_all(data, partitioner=partitioner)
        return table

    def cleanup(self, name, namespace):
        if not _data_dir.is_dir():
            LOGGER.error(f"illegal data dir: {_data_dir}")
            return

        namespace_dir = _data_dir.joinpath(namespace)

        if not namespace_dir.is_dir():
            return

        if name == "*":
            shutil.rmtree(namespace_dir, True)
            return

        for table in namespace_dir.glob(name):
            shutil.rmtree(table, True)

    def stop(self):
        self.cleanup(name="*", namespace=self.session_id)
        self._pool.shutdown()

    def kill(self):
        self.cleanup(name="*", namespace=self.session_id)
        self._pool.shutdown()

    def submit_reduce(self, func, num_partitions, name, namespace):
        futures = []
        for p in range(num_partitions):
            futures.append(
                self._pool.submit(
                    _do_reduce,
                    _ReduceProcess(p, _TaskInputInfo(namespace, name, num_partitions), _ReduceFunctorInfo(func)),
                )
            )
        rs = [r.result() for r in futures]
        rs = [r for r in filter(partial(is_not, None), rs)]
        if len(rs) <= 0:
            return None
        rtn = rs[0]
        for r in rs[1:]:
            rtn = func(rtn, r)
        return rtn

    def _submit_map_reduce_partitions_with_index(
        self,
        _do_func,
        mapper,
        reducer,
        num_partitions,
        input_name,
        input_namespace,
        output_name=None,
        output_namespace=None,
        output_partitioner=None,
    ):
        input_info = _TaskInputInfo(input_namespace, input_name, num_partitions)
        output_info = _TaskOutputInfo(
            namespace=output_namespace if output_namespace is not None else self.session_id,
            name=output_name if output_name is not None else str(uuid.uuid1()),
            num_partitions=num_partitions,
            partitioner=output_partitioner,
        )
        futures = []
        for p in range(num_partitions):
            futures.append(
                self._pool.submit(
                    _do_func,
                    _MapReduceProcess(
                        partition_id=p,
                        input_info=input_info,
                        output_info=output_info,
                        operator_info=_MapReduceFunctorInfo(mapper=mapper, reducer=reducer),
                    ),
                )
            )
        results = [r.result() for r in futures]
        return results

    def _submit_binary(self, func, do_func, partitions, name, namespace, other_name, other_namespace):
        task_info = _TaskInfo(
            self.session_id,
            function_id=str(uuid.uuid1()),
            function_bytes=f_pickle.dumps(func),
        )
        futures = []
        for p in range(partitions):
            left = _Operand(namespace, name, p, partitions)
            right = _Operand(other_namespace, other_name, p, partitions)
            futures.append(self._pool.submit(do_func, _BinaryProcess(task_info, left, right)))
        results = []
        for f in futures:
            r = f.result()
            results.append(r)
        return results


def _get_splits(obj, max_message_size):
    obj_bytes = serialize(obj, protocol=4)
    byte_size = len(obj_bytes)
    num_slice = (byte_size - 1) // max_message_size + 1
    if num_slice <= 1:
        return obj, num_slice
    else:
        _max_size = max_message_size
        kv = [(serialize(i), obj_bytes[slice(i * _max_size, (i + 1) * _max_size)]) for i in range(num_slice)]
        return kv, num_slice


class Federation(object):
    def _federation_object_key(self, name: str, tag: str, s_party: Tuple[str, str], d_party: Tuple[str, str]):
        return f"{self._session_id}-{name}-{tag}-{s_party[0]}-{s_party[1]}-{d_party[0]}-{d_party[1]}"

    def __init__(self, session: Session, session_id: str, party: Tuple[str, str]):
        self._session_id = session_id
        self._party = party
        self._session = session
        self._max_message_size = DEFAULT_MESSAGE_MAX_SIZE
        self._other_status_tables = {}
        self._other_object_tables = {}
        self._federation_status_table_cache = None
        self._federation_object_table_cache = None

        self._meta = _FederationMetaManager(session_id, party)

    def destroy(self):
        self._session.cleanup(namespace=self._session_id, name="*")

    # noinspection PyUnusedLocal
    def remote(self, v, name: str, tag: str, parties: List[PartyMeta]):
        log_str = f"federation.standalone.remote.{name}.{tag}"

        if v is None:
            raise ValueError(f"[{log_str}]remote `None` to {parties}")

        LOGGER.debug(f"[{log_str}]remote data, type={type(v)}")

        if isinstance(v, Table):
            dtype = FederationDataType.TABLE
            LOGGER.debug(
                f"[{log_str}]remote "
                f"Table(namespace={v.namespace}, name={v.name}, partitions={v.partitions}), dtype={dtype}"
            )
        else:
            v_splits, num_slice = _get_splits(v, self._max_message_size)
            if num_slice > 1:
                v = _create_table(
                    session=self._session,
                    name=str(uuid.uuid1()),
                    namespace=self._session_id,
                    partitions=1,
                    need_cleanup=True,
                    error_if_exist=False,
                )
                v.put_all(kv_list=v_splits)
                dtype = FederationDataType.SPLIT_OBJECT
                LOGGER.debug(
                    f"[{log_str}]remote "
                    f"Table(namespace={v.namespace}, name={v.name}, partitions={v.partitions}), dtype={dtype}"
                )
            else:
                LOGGER.debug(f"[{log_str}]remote object with type: {type(v)}")
                dtype = FederationDataType.OBJECT

        for party in parties:
            _tagged_key = self._federation_object_key(name, tag, self._party, party)
            if isinstance(v, Table):
                saved_name = str(uuid.uuid1())
                LOGGER.debug(
                    f"[{log_str}]save Table(namespace={v.namespace}, name={v.name}, partitions={v.partitions}) as "
                    f"Table(namespace={v.namespace}, name={saved_name}, partitions={v.partitions})"
                )
                _v = v.copy_as(name=saved_name, namespace=v.namespace, need_cleanup=False)
                self._meta.set_status(party, _tagged_key, (_v.name, _v.namespace, dtype))
            else:
                self._meta.set_object(party, _tagged_key, v)
                self._meta.set_status(party, _tagged_key, _tagged_key)

    # noinspection PyProtectedMember
    def get(self, name: str, tag: str, parties: List[PartyMeta]) -> List:
        log_str = f"federation.standalone.get.{name}.{tag}"
        LOGGER.debug(f"[{log_str}]")
        results = []

        for party in parties:
            _tagged_key = self._federation_object_key(name, tag, party, self._party)
            results.append(self._meta.wait_status_set(_tagged_key))

        rtn = []
        for r in results:
            if isinstance(r, tuple):
                # noinspection PyTypeChecker
                table: Table = _load_table(session=self._session, name=r[0], namespace=r[1], need_cleanup=True)

                dtype = r[2]
                LOGGER.debug(
                    f"[{log_str}] got "
                    f"Table(namespace={table.namespace}, name={table.name}, partitions={table.partitions}), dtype={dtype}"
                )

                if dtype == FederationDataType.SPLIT_OBJECT:
                    obj_bytes = b"".join(map(lambda t: t[1], sorted(table.collect(), key=lambda x: x[0])))
                    obj = deserialize(obj_bytes)
                    rtn.append(obj)
                else:
                    rtn.append(table)
            else:
                obj = self._meta.get_object(r)
                if obj is None:
                    raise EnvironmentError(f"federation get None from {parties} with name {name}, tag {tag}")
                rtn.append(obj)
                self._meta.ack_object(r)
                LOGGER.debug(f"[{log_str}] got object with type: {type(obj)}")
            self._meta.ack_status(r)
        return rtn


def _create_table(
    session: "Session",
    name: str,
    namespace: str,
    partitions: int,
    key_serdes_type: int,
    value_serdes_type: int,
    partitioner_type: int,
    need_cleanup=True,
    error_if_exist=False,
):
    assert isinstance(name, str)
    assert isinstance(namespace, str)
    assert isinstance(partitions, int)
    if (exist_partitions := _TableMetaManager.get_table_meta(namespace, name)) is None:
        _TableMetaManager.add_table_meta(
            namespace, name, partitions, key_serdes_type, value_serdes_type, partitioner_type
        )
    else:
        if error_if_exist:
            raise RuntimeError(f"table already exist: name={name}, namespace={namespace}")
        partitions = exist_partitions

    return Table(
        session=session,
        namespace=namespace,
        name=name,
        partitions=partitions,
        key_serdes_type=key_serdes_type,
        value_serdes_type=value_serdes_type,
        partitioner_type=partitioner_type,
        need_cleanup=need_cleanup,
    )


def _load_table(session, name: str, namespace: str, need_cleanup=False):
    table_meta = _TableMetaManager.get_table_meta(namespace, name)
    if table_meta is None:
        raise RuntimeError(f"table not exist: name={name}, namespace={namespace}")
    return Table(
        session=session,
        namespace=namespace,
        name=name,
        need_cleanup=need_cleanup,
        partitions=table_meta.num_partitions,
        key_serdes_type=table_meta.key_serdes_type,
        value_serdes_type=table_meta.value_serdes_type,
        partitioner_type=table_meta.partitioner_type,
    )


class _TaskInfo:
    def __init__(self, task_id, function_id, function_bytes):
        self.task_id = task_id
        self.function_id = function_id
        self.function_bytes = function_bytes
        self._function_deserialized = None

    def get_func(self):
        if self._function_deserialized is None:
            self._function_deserialized = f_pickle.loads(self.function_bytes)
        return self._function_deserialized


class _MapReduceTaskInfo:
    def __init__(self, output_namespace, output_name, map_function_bytes, reduce_function_bytes):
        self.output_namespace = output_namespace
        self.output_name = output_name
        self.map_function_bytes = map_function_bytes
        self.reduce_function_bytes = reduce_function_bytes
        self._reduce_function_deserialized = None
        self._mapper_function_deserialized = None

    def get_mapper(self):
        if self._mapper_function_deserialized is None:
            self._mapper_function_deserialized = f_pickle.loads(self.map_function_bytes)
        return self._mapper_function_deserialized

    def get_reducer(self):
        if self._reduce_function_deserialized is None:
            self._reduce_function_deserialized = f_pickle.loads(self.reduce_function_bytes)
        return self._reduce_function_deserialized


class _Operand:
    def __init__(self, namespace, name, partition, num_partitions: int):
        self.namespace = namespace
        self.name = name
        self.partition = partition
        self.num_partitions = num_partitions

    def as_env(self, write=False):
        return _get_env(self.namespace, self.name, str(self.partition), write=write)

    def as_partition_env(self, partition, write=False):
        return _get_env(self.namespace, self.name, str(partition), write=write)


class _TaskInputInfo:
    def __init__(self, namespace, name, num_partitions):
        self.namespace = namespace
        self.name = name
        self.num_partitions = num_partitions


class _TaskOutputInfo:
    def __init__(self, namespace, name, num_partitions, partitioner):
        self.namespace = namespace
        self.name = name
        self.num_partitions = num_partitions
        self.partitioner = partitioner

    def get_partition_id(self, key):
        if self.partitioner is None:
            raise RuntimeError("partitioner is None")
        return self.partitioner(key, self.num_partitions)


class _MapReduceFunctorInfo:
    def __init__(self, mapper, reducer):
        if mapper is not None:
            self.mapper_bytes = f_pickle.dumps(mapper)
        else:
            self.mapper_bytes = None
        if reducer is not None:
            self.reducer_bytes = f_pickle.dumps(reducer)
        else:
            self.reducer_bytes = None

    def get_mapper(self):
        if self.mapper_bytes is None:
            raise RuntimeError("mapper is None")
        return f_pickle.loads(self.mapper_bytes)

    def get_reducer(self):
        if self.reducer_bytes is None:
            raise RuntimeError("reducer is None")
        return f_pickle.loads(self.reducer_bytes)


class _ReduceFunctorInfo:
    def __init__(self, reducer):
        if reducer is not None:
            self.reducer_bytes = f_pickle.dumps(reducer)
        else:
            self.reducer_bytes = None

    def get_reducer(self):
        if self.reducer_bytes is None:
            raise RuntimeError("reducer is None")
        return f_pickle.loads(self.reducer_bytes)


class _ReduceProcess:
    def __init__(
        self,
        partition_id: int,
        input_info: _TaskInputInfo,
        operator_info: _ReduceFunctorInfo,
    ):
        self.partition_id = partition_id
        self.input_info = input_info
        self.operator_info = operator_info

    def as_input_env(self, pid, write=False):
        return _get_env(self.input_info.namespace, self.input_info.name, str(pid), write=write)

    def input_cursor(self, stack: ExitStack):
        return stack.enter_context(stack.enter_context(self.as_input_env(self.partition_id).begin()).cursor())

    def get_reducer(self):
        return self.operator_info.get_reducer()


class _MapReduceProcess:
    def __init__(
        self,
        partition_id,
        input_info: _TaskInputInfo,
        output_info: _TaskOutputInfo,
        operator_info: _MapReduceFunctorInfo,
    ):
        self.partition_id = partition_id
        self.input_info = input_info
        self.output_info = output_info
        self.operator_info = operator_info

    def get_input_partition_num(self):
        return self.input_info.num_partitions

    def get_output_partition_num(self):
        return self.output_info.num_partitions

    def get_input_env(self, pid, write=False):
        return _get_env(self.input_info.namespace, self.input_info.name, str(pid), write=write)

    def get_output_env(self, pid, write=True):
        return _get_env(self.output_info.namespace, self.output_info.name, str(pid), write=write)

    def get_input_cursor(self, stack: ExitStack, pid=None):
        if pid is None:
            pid = self.partition_id
        return stack.enter_context(
            stack.enter_context(stack.enter_context(self.get_input_env(pid, write=False)).begin(write=False)).cursor()
        )

    def get_output_transaction(self, pid, stack: ExitStack):
        return stack.enter_context(stack.enter_context(self.get_output_env(pid, write=True)).begin(write=True))

    def get_output_partition_id(self, key: bytes):
        return self.output_info.get_partition_id(key)

    def get_mapper(self):
        return self.operator_info.get_mapper()

    def get_reducer(self):
        return self.operator_info.get_reducer()


class _BinaryProcess:
    def __init__(self, task_info: _TaskInfo, left: _Operand, right: _Operand):
        self.info = task_info
        self.left = left
        self.right = right

    def output_operand(self):
        return _Operand(self.info.task_id, self.info.function_id, self.left.partition, self.left.num_partitions)

    def get_func(self):
        return self.info.get_func()


def _get_env(*args, write=False):
    _path = _data_dir.joinpath(*args)
    return _open_env(_path, write=write)


def _open_env(path, write=False):
    path.mkdir(parents=True, exist_ok=True)

    t = 0
    while t < 100:
        try:
            env = lmdb.open(
                path.as_posix(),
                create=True,
                max_dbs=1,
                max_readers=1024,
                lock=write,
                sync=True,
                map_size=10_737_418_240,
            )
            return env
        except lmdb.Error as e:
            if "No such file or directory" in e.args[0]:
                time.sleep(0.01)
                t += 1
            else:
                raise e
    raise lmdb.Error(f"No such file or directory: {path}, with {t} times retry")


def _generator_from_cursor(cursor):
    for k, v in cursor:
        yield k, v


def _do_mrwi_no_shuffle(p: _MapReduceProcess):
    rtn = p.output_info
    with ExitStack() as s:
        dst_txn = p.get_output_transaction(p.partition_id, s)
        cursor = p.get_input_cursor(s)
        v = p.get_mapper()(p.partition_id, _generator_from_cursor(cursor))
        for k1, v1 in v:
            dst_txn.put(k1, v1)
        return rtn


def _do_mrwi_shuffle_no_reduce(p: _MapReduceProcess):
    rtn = p.output_info
    with ExitStack() as s:
        cursor = p.get_input_cursor(s)
        txn_map = {}
        for output_partition_id in range(p.get_output_partition_num()):
            txn_map[output_partition_id] = p.get_output_transaction(output_partition_id, s)
        output_kv_iter = p.get_mapper()(p.partition_id, _generator_from_cursor(cursor))
        for k_bytes, v_bytes in output_kv_iter:
            partition_id = p.get_output_partition_id(k_bytes)
            txn_map[partition_id].put(k_bytes, v_bytes)
        return rtn


def _serialize_shuffle_write_key(iteration_index: int, k_bytes: bytes) -> bytes:
    iteration_bytes = iteration_index.to_bytes(4, "big")  # 4 bytes for the iteration index
    serialized_key = iteration_bytes + k_bytes

    return serialized_key


def _deserialize_shuffle_write_key(serialized_key: bytes) -> (int, int, bytes):
    iteration_bytes = serialized_key[:4]
    k_bytes = serialized_key[4:]
    iteration_index = int.from_bytes(iteration_bytes, "big")
    return iteration_index, k_bytes


def _get_shuffle_partition_id(shuffle_source_partition_id: int, shuffle_destination_partition_id: int) -> str:
    return f"{shuffle_source_partition_id}_{shuffle_destination_partition_id}"


def _do_mrwi_map_and_shuffle_write(p: _MapReduceProcess):
    rtn = p.output_info
    with ExitStack() as s:
        cursor = p.get_input_cursor(s)
        shuffle_write_txn_map = {}
        for output_partition_id in range(p.get_output_partition_num()):
            shuffle_partition_id = _get_shuffle_partition_id(p.partition_id, output_partition_id)
            shuffle_write_txn_map[output_partition_id] = p.get_output_transaction(shuffle_partition_id, s)

        output_kv_iter = p.get_mapper()(p.partition_id, _generator_from_cursor(cursor))
        for index, (k_bytes, v_bytes) in enumerate(output_kv_iter):
            shuffle_write_txn_map[p.get_output_partition_id(k_bytes)].put(
                _serialize_shuffle_write_key(index, k_bytes), v_bytes, overwrite=False
            )
    return rtn


def _do_mrwi_shuffle_read_and_reduce(p: _MapReduceProcess):
    rtn = p.output_info
    reducer = p.get_reducer()
    with ExitStack() as s:
        dst_txn = p.get_output_transaction(p.partition_id, s)
        for input_partition_id in range(p.get_input_partition_num()):
            for k_bytes, v_bytes in p.get_input_cursor(
                s, pid=_get_shuffle_partition_id(input_partition_id, p.partition_id)
            ):
                _, key = _deserialize_shuffle_write_key(k_bytes)
                if (old := dst_txn.get(key)) is None:
                    dst_txn.put(key, v_bytes)
                else:
                    dst_txn.put(key, reducer(old, v_bytes))
    return rtn


def _do_reduce(p: _ReduceProcess):
    value = None
    with ExitStack() as s:
        cursor = p.input_cursor(s)
        for _, v_bytes in cursor:
            if value is None:
                value = v_bytes
            else:
                value = p.get_reducer()(value, v_bytes)
    return value


def _do_subtract_by_key(p: _BinaryProcess):
    rtn = p.output_operand()
    with ExitStack() as s:
        left_op = p.left
        right_op = p.right
        right_env = s.enter_context(right_op.as_env())
        left_env = s.enter_context(left_op.as_env())
        dst_env = s.enter_context(rtn.as_env(write=True))

        left_txn = s.enter_context(left_env.begin())
        right_txn = s.enter_context(right_env.begin())
        dst_txn = s.enter_context(dst_env.begin(write=True))

        cursor = s.enter_context(left_txn.cursor())
        for k_bytes, left_v_bytes in cursor:
            right_v_bytes = right_txn.get(k_bytes)
            if right_v_bytes is None:
                dst_txn.put(k_bytes, left_v_bytes)
    return rtn


def _do_join(p: _BinaryProcess):
    rtn = p.output_operand()
    with ExitStack() as s:
        right_env = s.enter_context(p.right.as_env())
        left_env = s.enter_context(p.left.as_env())
        dst_env = s.enter_context(rtn.as_env(write=True))

        left_txn = s.enter_context(left_env.begin())
        right_txn = s.enter_context(right_env.begin())
        dst_txn = s.enter_context(dst_env.begin(write=True))

        cursor = s.enter_context(left_txn.cursor())
        for k_bytes, v1_bytes in cursor:
            v2_bytes = right_txn.get(k_bytes)
            if v2_bytes is None:
                continue
            try:
                v3 = p.get_func()(v1_bytes, v2_bytes)
            except Exception as e:
                raise RuntimeError(
                    f"Error when joining:\n" f"left:\n" f"{v1_bytes}\n" f"right:\n" f"{v2_bytes}\n" f"error: {e}"
                ) from e
            dst_txn.put(k_bytes, v3)
    return rtn


def _do_union(p: _BinaryProcess):
    rtn = p.output_operand()
    with ExitStack() as s:
        left_env = s.enter_context(p.left.as_env())
        right_env = s.enter_context(p.right.as_env())
        dst_env = s.enter_context(rtn.as_env(write=True))

        left_txn = s.enter_context(left_env.begin())
        right_txn = s.enter_context(right_env.begin())
        dst_txn = s.enter_context(dst_env.begin(write=True))

        # process left op
        with left_txn.cursor() as left_cursor:
            for k_bytes, left_v_bytes in left_cursor:
                right_v_bytes = right_txn.get(k_bytes)
                if right_v_bytes is None:
                    dst_txn.put(k_bytes, left_v_bytes)
                else:
                    final_v = p.get_func()(left_v_bytes, right_v_bytes)
                    dst_txn.put(k_bytes, final_v)

        # process right op
        with right_txn.cursor() as right_cursor:
            for k_bytes, right_v_bytes in right_cursor:
                final_v_bytes = dst_txn.get(k_bytes)
                if final_v_bytes is None:
                    dst_txn.put(k_bytes, right_v_bytes)
    return rtn


class _FederationMetaManager:
    STATUS_TABLE_NAME_PREFIX = "__federation_status__"
    OBJECT_TABLE_NAME_PREFIX = "__federation_object__"

    def __init__(self, session_id, party: Tuple[str, str]) -> None:
        self.session_id = session_id
        self.party = party
        self._env = {}

    def wait_status_set(self, key):
        value = self.get_status(key)
        while value is None:
            time.sleep(0.1)
            value = self.get_status(key)
        LOGGER.debug("[GET] Got {} type {}".format(key, "Table" if isinstance(value, tuple) else "Object"))
        return value

    def get_status(self, key):
        return self._get(self._get_status_table_name(self.party), key)

    def set_status(self, party: Tuple[str, str], key: str, value):
        return self._set(self._get_status_table_name(party), key, value)

    def ack_status(self, key):
        return self._ack(self._get_status_table_name(self.party), key)

    def get_object(self, key):
        return self._get(self._get_object_table_name(self.party), key)

    def set_object(self, party: Tuple[str, str], key, value):
        return self._set(self._get_object_table_name(party), key, value)

    def ack_object(self, key):
        return self._ack(self._get_object_table_name(self.party), key)

    def _get_status_table_name(self, party: Tuple[str, str]):
        return f"{self.STATUS_TABLE_NAME_PREFIX}.{party[0]}_{party[1]}"

    def _get_object_table_name(self, party: Tuple[str, str]):
        return f"{self.OBJECT_TABLE_NAME_PREFIX}.{party[0]}_{party[1]}"

    def _get_env(self, name):
        if name not in self._env:
            self._env[name] = _get_env(self.session_id, name, str(0), write=True)
        return self._env[name]

    def _get(self, name, key):
        env = self._get_env(name)
        with env.begin(write=False) as txn:
            old_value_bytes = txn.get(serialize(key))
            if old_value_bytes is not None:
                old_value_bytes = deserialize(old_value_bytes)
            return old_value_bytes

    def _set(self, name, key, value):
        env = self._get_env(name)
        with env.begin(write=True) as txn:
            return txn.put(serialize(key), serialize(value))

    def _ack(self, name, key):
        env = self._get_env(name)
        with env.begin(write=True) as txn:
            txn.delete(serialize(key))


def _hash_namespace_name_to_partition(namespace: str, name: str, partitions: int) -> Tuple[bytes, int]:
    k_bytes = f"{name}.{namespace}".encode("utf-8")
    partition_id = int.from_bytes(hashlib.sha256(k_bytes).digest(), "big") % partitions
    return k_bytes, partition_id


class _TableMetaManager:
    namespace = "__META__"
    name = "fragments"
    num_partitions = 11
    _env = {}

    @classmethod
    def _get_or_create_meta_env(cls, p):
        if p not in cls._env:
            cls._env[p] = _get_env(cls.namespace, cls.name, str(p), write=True)
        return cls._env[p]

    @classmethod
    def _get_meta_env(cls, namespace: str, name: str):
        k_bytes, p = _hash_namespace_name_to_partition(namespace, name, cls.num_partitions)
        env = cls._get_or_create_meta_env(p)
        return k_bytes, env

    @classmethod
    def add_table_meta(
        cls,
        namespace: str,
        name: str,
        num_partitions: int,
        key_serdes_type: int,
        value_serdes_type: int,
        partitioner_type: int,
    ):
        k_bytes, env = cls._get_meta_env(namespace, name)
        meta = _TableMeta(num_partitions, key_serdes_type, value_serdes_type, partitioner_type)
        with env.begin(write=True) as txn:
            return txn.put(k_bytes, meta.serialize())

    @classmethod
    def get_table_meta(cls, namespace: str, name: str) -> "_TableMeta":
        k_bytes, env = cls._get_meta_env(namespace, name)
        with env.begin(write=False) as txn:
            old_value_bytes = txn.get(k_bytes)
            if old_value_bytes is not None:
                try:
                    num_partitions = deserialize(old_value_bytes)
                    old_value_bytes = _TableMeta(num_partitions, 0, 0, 0)
                except Exception:
                    old_value_bytes = _TableMeta.deserialize(old_value_bytes)

            return old_value_bytes

    @classmethod
    def destroy_table(cls, namespace: str, name: str):
        k_bytes, env = cls._get_meta_env(namespace, name)
        with env.begin(write=True) as txn:
            txn.delete(k_bytes)
        path = _data_dir.joinpath(namespace, name)
        shutil.rmtree(path, ignore_errors=True)


class _TableMeta:
    def __init__(self, num_partitions: int, key_serdes_type: int, value_serdes_type: int, partitioner_type: int):
        self.num_partitions = num_partitions
        self.key_serdes_type = key_serdes_type
        self.value_serdes_type = value_serdes_type
        self.partitioner_type = partitioner_type

    def serialize(self) -> bytes:
        num_partitions_bytes = self.num_partitions.to_bytes(4, "big")
        key_serdes_type_bytes = self.key_serdes_type.to_bytes(4, "big")
        value_serdes_type_bytes = self.value_serdes_type.to_bytes(4, "big")
        partitioner_type_bytes = self.partitioner_type.to_bytes(4, "big")
        return num_partitions_bytes + key_serdes_type_bytes + value_serdes_type_bytes + partitioner_type_bytes

    @classmethod
    def deserialize(cls, serialized_bytes: bytes) -> "_TableMeta":
        num_partitions = int.from_bytes(serialized_bytes[:4], "big")
        key_serdes_type = int.from_bytes(serialized_bytes[4:8], "big")
        value_serdes_type = int.from_bytes(serialized_bytes[8:12], "big")
        partitioner_type = int.from_bytes(serialized_bytes[12:16], "big")
        return cls(num_partitions, key_serdes_type, value_serdes_type, partitioner_type)
