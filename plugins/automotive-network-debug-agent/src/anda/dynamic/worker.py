"""在单一 STA 线程中持久持有 CANoe COM Session。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from queue import Queue
from threading import Thread
from typing import Any

from anda.common.errors import DynamicBackendError

_STOP = object()


class BackendWorker:
    """把所有 Backend 调用串行投递到创建 COM 对象的同一线程。"""

    def __init__(self, backend_factory: Callable[[], Any]) -> None:
        self._backend_factory = backend_factory
        self._queue: Queue[Any] = Queue()
        self._ready: Future[Any] = Future()
        self._thread = Thread(
            target=self._run,
            name="anda-dynamic-canoe-sta",
            daemon=True,
        )
        self._thread.start()
        try:
            self._ready.result(timeout=30)
        except FutureTimeoutError as exc:
            self._queue.put(_STOP)
            raise DynamicBackendError("CANoe Backend STA 线程启动超时") from exc
        except Exception as exc:
            raise DynamicBackendError(f"CANoe Backend 初始化失败：{exc}") from exc

    def _run(self) -> None:
        try:
            backend = self._backend_factory()
            self._ready.set_result(True)
        except Exception as exc:  # noqa: BLE001 - 必须跨线程转交初始化失败
            self._ready.set_exception(exc)
            return

        backend_closed = False
        while True:
            item = self._queue.get()
            if item is _STOP:
                if not backend_closed and hasattr(backend, "close"):
                    try:
                        backend.close()
                    except Exception:  # noqa: BLE001,S110 - 线程终止时只能尽力回收
                        pass
                return
            method_name, args, kwargs, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = getattr(backend, method_name)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - Future 保留原异常类型
                future.set_exception(exc)
            else:
                if method_name == "close":
                    backend_closed = True
                future.set_result(result)

    def call(
        self,
        method_name: str,
        *args: Any,
        timeout: float = 180,
        **kwargs: Any,
    ) -> Any:
        future: Future[Any] = Future()
        self._queue.put((method_name, args, kwargs, future))
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise DynamicBackendError(
                f"CANoe Backend 操作 {method_name} 在 {timeout:g} 秒内未完成；"
                "该 Session 状态可能仍在变化，请先调用 get_dynamic_status"
            ) from exc

    def shutdown(self, timeout: float = 10) -> bool:
        self._queue.put(_STOP)
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()
