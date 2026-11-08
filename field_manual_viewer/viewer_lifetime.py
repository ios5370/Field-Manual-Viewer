"""Track live viewer connections, without timers inside background browser tabs."""
import threading
import time
from contextlib import contextmanager


class ViewerLifetime:
    def __init__(self,idle_seconds=5,startup_seconds=90,clock=time.monotonic):
        self.clock=clock
        self.idle_seconds=idle_seconds
        self.startup_seconds=startup_seconds
        self.started=clock()
        self.last_empty=self.started
        self.seen=False
        self.reserved_until=0
        self.closing=False
        self.active=set()
        self.lock=threading.Lock()

    def join(self):
        token=object()
        with self.lock:
            if self.closing:raise RuntimeError('Viewer is closing')
            self.active.add(token);self.seen=True
            self.reserved_until=0
        return token

    def leave(self,token):
        with self.lock:
            self.active.discard(token)
            if not self.active:self.last_empty=self.clock()

    def should_stop(self):
        with self.lock:
            return self._idle()

    def _idle(self):
        if self.active or self.clock()<self.reserved_until:return False
        return self.clock()-(self.last_empty if self.seen else self.started) >= (self.idle_seconds if self.seen else self.startup_seconds)

    def reserve_launch(self):
        with self.lock:
            if self.closing:return False
            self.reserved_until=self.clock()+30
            return True

    def begin_shutdown(self):
        with self.lock:
            if not self._idle():return False
            self.closing=True
            return True

    def request_shutdown(self):
        with self.lock:self.closing=True

    @contextmanager
    def connection(self):
        token=self.join()
        try:yield
        finally:self.leave(token)
