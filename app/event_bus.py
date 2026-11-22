import asyncio
import inspect
import logging

log = logging.getLogger("relay")


class EventBus:
    """
    Named events middleware can subscribe to (see MIDDLEWARE.md).

    Everything runs on the relay's single event loop. A handler that raises makes the
    message fail with a temporary error (fail closed), so mail is never sent with a hook
    silently skipped.
    """

    def __init__(self):
        self.__events = {}  # event_name -> [handlers]

    def subscribe(self, event_name, handler):
        self.__events.setdefault(event_name, []).append(handler)

    def unsubscribe(self, event_name, handler):
        handlers = self.__events.get(event_name, [])
        if handler in handlers:
            handlers.remove(handler)

    def has_subscribers(self, *event_names) -> bool:
        return any(self.__events.get(name) for name in event_names)

    def clear(self):
        self.__events.clear()

    async def publish(self, event_name, *args):
        """Run every handler in order; returns the last handler's result (False if none)."""
        result = False
        # snapshot handlers to avoid mutation during iteration
        for handler in list(self.__events.get(event_name, [])):
            log.debug(f"Publishing event: {event_name}")
            result = handler(*args)
            if inspect.isawaitable(result):
                result = await result
        return result

    def publishSync(self, event_name, *args):
        """
        For callers that cannot await (the SMTP authenticator). Plain handlers run inline;
        coroutine handlers are scheduled on the running loop and their result is not waited for.
        """
        result = False
        for handler in list(self.__events.get(event_name, [])):
            if inspect.iscoroutinefunction(handler):
                task = asyncio.get_running_loop().create_task(handler(*args))
                task.add_done_callback(_log_task_failure)
            else:
                result = handler(*args)
        return result


def _log_task_failure(task: asyncio.Task):
    if not task.cancelled() and task.exception() is not None:
        log.error("Async auth middleware failed", exc_info=task.exception())


event_bus_instance = EventBus()
