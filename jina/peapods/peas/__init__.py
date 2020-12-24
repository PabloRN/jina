__copyright__ = "Copyright (c) 2020 Jina AI Limited. All rights reserved."
__license__ = "Apache-2.0"

import argparse
import os
import time
from collections import defaultdict
from contextlib import ExitStack
from multiprocessing.synchronize import Event
from typing import Dict, List

import zmq

from ..zmq import ZmqStreamlet
from ... import Message
from ... import Request
from ...enums import PeaRoleType, SkipOnErrorType
from ...excepts import RequestLoopEnd, NoExplicitMessage, ExecutorFailToLoad, MemoryOverHighWatermark, DriverError, \
    ChainedPodException, BadConfigSource
from ...executors import BaseExecutor
from ...logging import JinaLogger
from ...logging.profile import used_memory
from ...proto import jina_pb2

__all__ = ['BasePea']


class BasePea(ExitStack):
    """BasePea is an unary service unit which provides network interface and
    communicates with others via protobuf and ZeroMQ. It also is a context manager of an Executor .
    """

    def __init__(self, args: 'argparse.Namespace'):
        """ Create a new :class:`BasePea` object

        :param args: the arguments received from the CLI
        """
        super().__init__()
        self.args = args

        self.last_active_time = time.perf_counter()
        self.last_dump_time = time.perf_counter()

        self._request = None
        self._message = None

        # all pending messages collected so far, key is the request id
        self._pending_msgs = defaultdict(list)  # type: Dict[str, List['Message']]
        self._partial_requests = None
        self._partial_messages = None

        self.name = self.args.name or self.__class__.__name__  #: this is the process name
        if self.args.role == PeaRoleType.HEAD:
            self.name = f'{self.args.name}-head'
        elif self.args.role == PeaRoleType.TAIL:
            self.name = f'{self.args.name}-tail'
        elif self.args.role == PeaRoleType.PARALLEL:
            self.name = f'{self.name}-{self.args.pea_id}'

        self.logger = JinaLogger(self.name,
                                 log_id=self.args.log_id,
                                 log_config=self.args.log_config)


    def _handle(self, msg: 'Message') -> 'BasePea':
        """Register the current message to this pea, so that all message-related properties are up-to-date, including
        :attr:`request`, :attr:`prev_requests`, :attr:`message`, :attr:`prev_messages`. And then call the executor to handle
        this message if its envelope's  status is not ERROR, else skip handling of message.

        :param msg: the message received
        """

        if self.expect_parts > 1 and self.expect_parts > len(self.partial_requests):
            # NOTE: reduce priority is higher than chain exception
            # otherwise a reducer will lose its function when eailier pods raise exception
            raise NoExplicitMessage

        if msg.envelope.status.code != jina_pb2.StatusProto.ERROR or self.args.skip_on_error < SkipOnErrorType.HANDLE:
            self.executor(self.request_type)
        else:
            raise ChainedPodException
        return self

    @property
    def is_idle(self) -> bool:
        """Return ``True`` when current time is ``max_idle_time`` seconds late than the last active time"""
        return (time.perf_counter() - self.last_active_time) > self.args.max_idle_time

    @property
    def request(self) -> 'Request':
        """Get the current request body inside the protobuf message"""
        return self._request

    @property
    def message(self) -> 'Message':
        """Get the current protobuf message to be processed"""
        return self._message

    @property
    def request_type(self) -> str:
        """Get the type of message being processed"""
        return self._message.envelope.request_type

    def _load_executor(self):
        """Load the executor to this BasePea, specified by ``uses`` CLI argument.

        """
        try:
            try:
                self.executor = BaseExecutor.load_config(self.args.uses,
                                                         separated_workspace=self.args.separated_workspace,
                                                         pea_id=self.args.pea_id,
                                                         read_only=self.args.read_only)
            except BadConfigSource:
                # retry loading but with "uses_internal" as the source
                self.executor = BaseExecutor.load_config(self.args.uses_internal,
                                                         separated_workspace=self.args.separated_workspace,
                                                         pea_id=self.args.pea_id,
                                                         read_only=self.args.read_only)
            self.executor.attach(pea=self)
        except FileNotFoundError as ex:
            self.logger.error(f'fail to load file dependency: {repr(ex)}')
            raise ExecutorFailToLoad from ex
        except Exception as ex:
            raise ExecutorFailToLoad from ex

    def _save_executor(self):
        """Save the contained executor according to the `dump_interval` parameter
        """
        if (time.perf_counter() - self.last_dump_time) > self.args.dump_interval > 0:
            self.executor.save()
            self.last_dump_time = time.perf_counter()
            if hasattr(self, 'zmqlet'):
                self.zmqlet.print_stats()

    @property
    def expect_parts(self) -> int:
        """The expected number of partial messages before trigger :meth:`handle` """
        return self.args.num_part if self.message.is_data_request else 1

    @property
    def partial_requests(self) -> List['Request']:
        """The collected partial requests under the current ``request_id`` """
        return self._partial_requests

    @property
    def partial_messages(self) -> List['Message']:
        """The collected partial messages under the current ``request_id`` """
        return self._partial_messages

    def _pre_hook(self, msg: 'Message') -> 'BasePea':
        """Pre-hook function, what to do after first receiving the message """
        msg.add_route(self.name, self.args.identity)
        self._request = msg.request
        self._message = msg

        part_str = ' '
        if self.expect_parts > 1:
            req_id = msg.envelope.request_id
            self._pending_msgs[req_id].append(msg)
            self._partial_messages = self._pending_msgs[req_id]
            self._partial_requests = [v.request for v in self._partial_messages]
            part_str = f' ({len(self.partial_requests)}/{self.expect_parts} parts) '

        self.logger.info(f'recv {msg.envelope.request_type}{part_str}from {msg.colored_route}')
        return self

    def _post_hook(self, msg: 'Message') -> 'BasePea':
        """Post-hook function, what to do before handing out the message """
        self.last_active_time = time.perf_counter()
        self._save_executor()
        self.check_memory_watermark()

        if self.expect_parts > 1:
            msgs = self._pending_msgs.pop(msg.envelope.request_id)
            msg.merge_envelope_from(msgs)

        msg.update_timestamp()
        return self

    def _callback(self, msg: 'Message'):
        self.is_post_hook_done = False  #: if the post_hook is called
        self._pre_hook(msg)._handle(msg)._post_hook(msg)
        self.is_post_hook_done = True
        return msg

    def _teardown(self):
        self.close_zmqlet()

    def _msg_callback(self, msg: 'Message') -> None:
        """Callback function after receiving the message

        When nothing is returned then nothing is send out via :attr:`zmqlet.sock_out`.
        """
        try:
            # notice how executor related exceptions are handled here
            # generally unless executor throws an OSError, the exception are caught and solved inplace
            self.zmqlet.send_message(self._callback(msg))
        except RequestLoopEnd as ex:
            # this is the proper way to end when a terminate signal is sent
            self.logger.info(f'Terminating loop requested by terminate signal {repr(ex)}')
            self.zmqlet.send_message(msg)
            self._teardown()
        except (SystemError, zmq.error.ZMQError, KeyboardInterrupt) as ex:
            # save executor
            self.logger.info(f'{repr(ex)} causes the breaking from the event loop')
            self.zmqlet.send_message(msg)
            self._teardown()
        except MemoryOverHighWatermark:
            self.logger.critical(
                f'memory usage {used_memory()} GB is above the high-watermark: {self.args.memory_hwm} GB')
        except NoExplicitMessage:
            # silent and do not propagate message anymore
            # 1. wait partial message to be finished
            # 2. dealer send a control message and no need to go on
            pass
        except (RuntimeError, Exception, ChainedPodException) as ex:
            # general runtime error and nothing serious, we simply mark the message to error and pass on
            if not self.is_post_hook_done:
                self._post_hook(msg)
            if isinstance(ex, ChainedPodException):
                msg.add_exception()
                self.logger.warning(repr(ex))
            else:
                msg.add_exception(ex, executor=getattr(self, 'executor'))
                self.logger.error(repr(ex))
            if 'JINA_RAISE_ERROR_EARLY' in os.environ:
                raise
            self.zmqlet.send_message(msg)

    def request_loop(self, is_ready_event: 'Event'):
        """The body of the request loop
        """
        self.zmqlet = ZmqStreamlet(self.args, logger=self.logger)
        is_ready_event.set()
        self.zmqlet.start(self._msg_callback)

    def _load_plugins(self):
        """Loads the plugins if needed necessary to load executors
        """
        if self.args.py_modules:
            from ...importer import PathImporter
            PathImporter.add_modules(*self.args.py_modules)

    def close_zmqlet(self):
        """Close the zmqlet if exists"""
        if hasattr(self, 'zmqlet'):
            self.zmqlet.close()

    def _initialize_executor(self):
        try:
            self._load_plugins()
            self._load_executor()
            return self.executor
        except Exception as ex:
            self.logger.critical(f'can not start a executor from {self.args.uses}', exc_info=True)
            raise ex

    def run(self, is_ready_event: 'Event'):
        """Start the request loop of this BasePea. It will listen to the network protobuf message via ZeroMQ. """
        try:
            # Every logger created in this process will be identified by the `Pod Id` and use the same name
            self.request_loop(is_ready_event)
        except KeyboardInterrupt:
            self.logger.info('Loop interrupted by user')
        except SystemError as ex:
            self.logger.error(f'SystemError interrupted pea loop {repr(ex)}')
        except DriverError as ex:
            self.logger.critical(f'driver error: {repr(ex)}', exc_info=True)
        except zmq.error.ZMQError:
            self.logger.critical('zmqlet can not be initiated')
        except Exception as ex:
            # this captures the general exception from the following places:
            # - self.zmqlet.recv_message
            # - self.zmqlet.send_message
            self.logger.critical(f'unknown exception: {repr(ex)}', exc_info=True)
        finally:
            self.logger.info(f'request loop ended, tearing down ...')
            self._teardown()

    def check_memory_watermark(self):
        """Check the memory watermark """
        if used_memory() > self.args.memory_hwm > 0:
            raise MemoryOverHighWatermark

    def __enter__(self) -> 'BasePea':
        executor = self._initialize_executor()

        if executor:
            self.enter_context(executor)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        super().__exit__(exc_type, exc_val, exc_tb)
        self._teardown()
