# -*- coding: utf-8 -*-
# Copyright (C) MUJIN Inc
"""A ZMQ DEALER client that can have any number of requests outstanding at once."""

import collections
import logging
import os
import threading
from typing import Deque
from typing import Optional

from . import ConnectionLostError
from . import TimeoutError as MujinTimeoutError
from . import UserInterrupt
from . import zmq

log = logging.getLogger(__name__)


class ZmqDealerClient(object):
    """
    Sends requests to a ZMQ ROUTER server without waiting for each response before sending the next one.

    Requests are framed as an empty delimiter followed by the payload, exactly the same as requests from a REQ socket.
    Since responses can arrive in any order, callers are responsible for matching each response with its request.

    The client is owned by a single thread. The only method that is safe to call from other threads is Interrupt().
    """

    # Defaults allow Destroy() to run on an instance whose constructor did not finish.
    _url: str = ""
    _ctx: Optional[zmq.Context] = None
    _ctxown: Optional[zmq.Context] = None
    _socket: Optional[zmq.Socket] = None
    _monitorSocket: Optional[zmq.Socket] = None
    _poller: Optional[zmq.Poller] = None

    # Incremented whenever the socket is replaced after a disconnect.
    # Responses to requests sent on an earlier generation can never arrive,
    # since the server addresses them to a peer that no longer exists.
    _connectionGeneration: int

    # Responses received while handling a disconnect, returned before the disconnect itself is reported.
    _receivedResponses: Deque[bytes]

    # Set when a disconnect has been handled but not yet reported to the caller with ConnectionLostError.
    _isConnectionLostPending: bool

    # A pipe that other threads write to in order to wake the owning thread from Receive().
    # An OS pipe is used rather than a ZMQ socket, since ZMQ sockets must not be shared between threads.
    _interruptReadFileDescriptor: Optional[int] = None
    _interruptWriteFileDescriptor: Optional[int] = None

    # Mutex used to guard the write side of the pipe.
    # Ensures Interrupt() never writes to a descriptor that Destroy() closed and the OS reused.
    _interruptMutex: Optional[threading.Lock] = None
    _isInterrupted: bool

    def __init__(
        self,
        hostname: str = "",
        port: int = 0,
        ctx: Optional[zmq.Context] = None,
        url: Optional[str] = None,
    ) -> None:
        """
        Creates a new client. The connection is opened lazily on the first Send().

        Args:
            hostname: Hostname or IP to connect to.
            port: Port to connect to.
            ctx: The ZMQ context to use. If not provided, a new context is created and owned by this client.
            url: The ZMQ URL to connect to, used instead of hostname and port if provided.
        """
        self._interruptMutex = threading.Lock()
        self._url = url if url is not None else "tcp://%s:%d" % (hostname, int(port))
        self._ctxown = None
        if ctx is None:
            self._ctxown = zmq.Context()
            ctx = self._ctxown
        self._ctx = ctx
        self._socket = None
        self._monitorSocket = None
        self._poller = None
        self._connectionGeneration = 0
        self._receivedResponses = collections.deque()
        self._isConnectionLostPending = False

        self._interruptReadFileDescriptor, self._interruptWriteFileDescriptor = os.pipe()

        # Don't allow either end of the pipe to block.
        # Interrupt() must never stall the calling thread.
        os.set_blocking(self._interruptReadFileDescriptor, False)
        os.set_blocking(self._interruptWriteFileDescriptor, False)
        self._isInterrupted = False

    def __del__(self) -> None:
        self.Destroy()

    def Destroy(self) -> None:
        """Closes the connection and releases all resources. Unanswered requests are abandoned."""
        self._CloseSocket()
        if self._interruptMutex is None:
            return  # The constructor did not get far enough to acquire any resources.
        with self._interruptMutex:
            for fileDescriptor in (
                self._interruptReadFileDescriptor,
                self._interruptWriteFileDescriptor,
            ):
                if fileDescriptor is not None:
                    os.close(fileDescriptor)
            self._interruptReadFileDescriptor = None
            self._interruptWriteFileDescriptor = None
        if self._ctxown is not None:
            try:
                self._ctxown.destroy()
            except Exception:
                log.exception("Caught exception when destroying ZMQ context")
            self._ctxown = None
        self._ctx = None

    def GetUrl(self) -> str:
        """Returns the URL this client connects to."""
        return self._url

    def GetConnectionGeneration(self) -> int:
        """
        Returns the generation of the current connection.

        Increments any time the underlying transport is reconnected.
        If a client's request was sent on an older connection generation, that request's response will not arrive.
        """
        return self._connectionGeneration

    def Send(self, message: bytes) -> int:
        """
        Queues a request for sending without waiting for its response.

        Args:
            message: The request payload.

        Returns:
            The connection generation the request was sent on.

        Raises:
            TimeoutError: If the outgoing queue is full and the request could not be queued.
        """
        socket = self._EnsureSocket()
        try:
            socket.send_multipart([b"", message], zmq.NOBLOCK)
        except zmq.Again as e:
            raise MujinTimeoutError("Outgoing queue to %s is full, could not send request" % self._url) from e
        return self._connectionGeneration

    def Receive(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """
        Waits for the next response from the server.

        Args:
            timeout: Maximum time to wait in seconds.
                     If None, waits until a response arrives, the connection is lost, or Interrupt() is called.

        Returns:
            The response payload, or None if the timeout elapsed or Interrupt() was called first.
            Use ConsumeInterrupt() to tell these apart.

        Raises:
            ConnectionLostError: If the server disconnected.
                                 All requests sent on earlier connection generations will not be answered.
        """
        # If there's already a pending response, we can return immediately
        if len(self._receivedResponses) > 0:
            return self._receivedResponses.popleft()

        # If the connection died, inform the caller that any in-flight requests are now dead
        if self._isConnectionLostPending:
            self._isConnectionLostPending = False
            raise ConnectionLostError(
                "Lost connection to %s, responses to outstanding requests will not arrive" % self._url
            )

        # If the connection is valid still, poll for new input
        socket = self._EnsureSocket()
        assert self._poller is not None
        timeoutMS = None if timeout is None else max(0, int(timeout * 1000))
        polledEvents = dict(self._poller.poll(timeoutMS))

        # If we got interrupted by a local event, reset the pipe and flag the interrupt
        if self._interruptReadFileDescriptor in polledEvents:
            self._DrainInterruptPipe()
            self._isInterrupted = True

        # It's possible that a single poll cycle receives both valid responses _and_ a disconnect.
        # If this happens, return the responses first so that the caller gets as much data as possible.
        if polledEvents.get(socket, 0) & zmq.POLLIN:
            response = self._ReceiveResponse(socket)
            if response is not None:
                return response

        # If the connection was reset, we need to invalidate existing requests and reconnect.
        if self._monitorSocket in polledEvents and self._HasPeerDisconnected():
            self._HandleDisconnect()
            return self.Receive(timeout=0)

        return None

    def Interrupt(self) -> None:
        """
        Wakes the owning thread from Receive(), or makes its next Receive() return immediately.
        Safe to call from any thread.
        """
        assert self._interruptMutex is not None
        with self._interruptMutex:
            # If the client was already destroyed, there's nothing to interrupt
            if self._interruptWriteFileDescriptor is None:
                return

            # Write a byte to the interrupt FD to notify the client's main thread.
            # If the pipe is already full, there's an interrupt pending already.
            try:
                os.write(self._interruptWriteFileDescriptor, b"\0")
            except BlockingIOError:
                pass

    def ConsumeInterrupt(self) -> bool:
        """Returns True if Receive() observed an Interrupt() since the last call, and clears it."""
        isInterrupted = self._isInterrupted
        self._isInterrupted = False
        return isInterrupted

    def _EnsureSocket(self) -> zmq.Socket:
        # Check we haven't already been destroyed
        if self._ctx is None:
            raise UserInterrupt("ZMQ dealer client for %s is destroyed" % self._url)

        # If we already have an active socket, nothing to do
        if self._socket is not None:
            return self._socket

        # Note that we don't sent a routing ID, and instead leave it up to the server.
        # This is to avoid any potential duplication across clients, which would result in conflation.
        socket = self._ctx.socket(zmq.DEALER)

        # Detect a dead peer quickly even when no responses are expected for a while.
        socket.setsockopt(zmq.TCP_KEEPALIVE, 1)
        socket.setsockopt(zmq.TCP_KEEPALIVE_IDLE, 2)
        socket.setsockopt(zmq.TCP_KEEPALIVE_INTVL, 2)
        socket.setsockopt(zmq.TCP_KEEPALIVE_CNT, 2)
        socket.setsockopt(zmq.LINGER, 0)

        # Need to set the monitor socket before connect to catch disconnect events
        monitorSocket = socket.get_monitor_socket(zmq.EVENT_DISCONNECTED)
        socket.connect(self._url)

        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        poller.register(monitorSocket, zmq.POLLIN)
        assert self._interruptReadFileDescriptor is not None
        poller.register(self._interruptReadFileDescriptor, zmq.POLLIN)

        self._socket = socket
        self._monitorSocket = monitorSocket
        self._poller = poller
        return socket

    def _CloseSocket(self) -> None:
        if self._monitorSocket is not None:
            try:
                self._monitorSocket.close(linger=0)
            except Exception:
                log.exception("Caught exception when closing monitor socket")
            self._monitorSocket = None

        if self._socket is not None:
            try:
                self._socket.close(linger=0)
            except Exception:
                log.exception("Caught exception when closing socket")
            self._socket = None

        self._poller = None

    def _ReceiveResponse(self, socket: zmq.Socket) -> Optional[bytes]:
        frames = socket.recv_multipart(zmq.NOBLOCK)
        if len(frames) != 2 or len(frames[0]) != 0:
            log.warning(
                "Discarding response from %s with unexpected framing (%d frames)",
                self._url,
                len(frames),
            )
            return None
        return frames[1]

    def _HasPeerDisconnected(self) -> bool:
        assert self._monitorSocket is not None
        hasDisconnected = False
        while self._monitorSocket.poll(0) != 0:
            self._monitorSocket.recv_multipart(zmq.NOBLOCK)
            hasDisconnected = True
        return hasDisconnected

    def _HandleDisconnect(self) -> None:
        """Keeps any responses that landed just before the disconnect, then replaces the socket."""
        socket = self._socket
        assert socket is not None
        while socket.poll(50, zmq.POLLIN) & zmq.POLLIN:
            response = self._ReceiveResponse(socket)
            if response is not None:
                self._receivedResponses.append(response)

        # Replacing the socket kills anything that was still in flight.
        # The caller must handle the change in connection generation index and re-queue as needed.
        log.warning(
            "Lost connection to %s, failing outstanding requests (connection generation %d)",
            self._url,
            self._connectionGeneration,
        )
        self._CloseSocket()
        self._connectionGeneration += 1
        self._isConnectionLostPending = True

    def _DrainInterruptPipe(self) -> None:
        assert self._interruptReadFileDescriptor is not None
        try:
            while os.read(self._interruptReadFileDescriptor, 4096):
                pass
        except BlockingIOError:
            pass
