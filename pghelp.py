#!/usr/bin/python3.9
# Copyright (c) 2022 sylv
# Copyright (c) 2021 The Forest Team
import asyncio
import copy
import logging
import os
import time
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Optional, Union
import asyncpg

from . import utils


Loop = Optional[asyncio.events.AbstractEventLoop]

MAX_RESP_LOG_LEN = int(os.getenv("MAX_RESP_LOG_LEN", "256"))
LOG_LEVEL_DEBUG = bool(os.getenv("DEBUG", None))


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if LOG_LEVEL_DEBUG else logging.INFO)
    if not logger.hasHandlers():
        sh = logging.StreamHandler()
        sh.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(sh)
    return logger


# this should be used for every insance


class OneTruePool:
    connecting: Optional[asyncio.Future] = None
    pool: Optional[asyncpg.Pool] = None

    async def connect(
        self, url: str, table: str, database_name: Optional[str] = None
    ) -> None:
        if not self.pool:
            if self.connecting:
                await self.connecting
            else:
                self.connecting = asyncio.Future()
                logging.debug("creating pool for %s", table)
                # this is helpful for connecting to an actually local db where your system username is different
                # but counterproductive if you're proxying a database connection through localhost
                # if "localhost" in self.database:
                #     pool = await asyncpg.create_pool(user="postgres")
                self.pool = await asyncpg.create_pool(url, database=database_name)
                logging.debug("created pool %s for %s", self.pool, table)
                self.connecting.set_result(True)

    def acquire(self) -> asyncpg.pool.PoolAcquireContext:
        """returns an async context manager. sugar around pool.pool.acquire
        this *isn't* async, because pool.acquire returns an async context manager and not a coroutine"""
        if not self.pool:
            raise Exception("no pool, use pool.connect first")
        return self.pool.acquire()

    closing_event = asyncio.Event()
    listener_tasks: list[asyncio.Task] = []

    async def close(self) -> None:
        logging.info("closing pool")
        self.closing_event.set()
        for listener_task in self.listener_tasks:
            # wait for them to remove listners and return connections
            # maybe add a timeout here just in case?
            await listener_task
        try:
            if self.pool:
                await self.pool.close()
        except (asyncpg.PostgresError, asyncpg.InternalClientError) as e:
            logging.error(e)

    async def _start_listener_task(
        self, channel: str, notification_handler: Callable
    ) -> None:
        async with self.acquire() as conn:
            try:
                await conn.add_listener(channel, notification_handler)
                await self.closing_event.wait()
                logging.info(
                    "got closing event from %s, %s", channel, notification_handler
                )
            finally:
                try:
                    await conn.remove_listener(channel, notification_handler)
                # conn was already released/closed
                except asyncpg.InterfaceError:
                    pass

    async def start_listener(self, channel: str, handler: Callable) -> None:
        task = asyncio.create_task(self._start_listener_task(channel, handler))
        task.add_done_callback(utils.log_task_result)
        self.listener_tasks.append(task)


pool = OneTruePool()


class SimpleInterface:
    def __init__(self, database: str, database_name: Optional[str] = None) -> None:
        self.database = database
        self.database_name = database_name

    @asynccontextmanager
    async def get_connection(self) -> AsyncGenerator:
        if not pool.pool:
            await pool.connect(self.database, "simple interface", self.database_name)
        assert pool.pool
        async with pool.acquire() as conn:
            logging.info("connection acquired")
            yield conn


class PGExpressions(dict):
    def __init__(self, table: str = "", **kwargs: str) -> None:
        self.table = table
        self.logger = get_logger(f"{self.table}_expressions")
        super().__init__(**kwargs)
        if "exists" not in self:
            self[
                "exists"
            ] = f"SELECT * FROM pg_tables WHERE tablename = '{self.table}';"
        if "create_table" not in self:
            logging.warning(f"'create_table' not defined for {self.table}")

    def get_query(self, key: str) -> str:
        logging.debug(f"self.get invoked for {key}")
        return dict.__getitem__(self, key).replace("{self.table}", self.table)


class PGInterface:
    """Implements an abstraction for both sync and async PG requests:
    - provided a map of method names to SQL query strings
    - an optional database URI ( defaults to "")
    - and an optional event loop"""

    def __init__(
        self,
        query_strings: PGExpressions,
        database: str = "",
        loop: Loop = None,
        database_name: Optional[str] = None,
        pool: OneTruePool = pool,
    ) -> None:
        """Accepts a PGExpressions argument containing postgresql expressions, a database string, and an optional event loop."""

        self.loop = loop or asyncio.get_event_loop()
        self.database: Union[str, dict] = copy.deepcopy(
            database
        )  # either a db uri or canned resps
        self.database_name = database_name
        self.queries = query_strings
        self.table = self.queries.table
        self.MAX_RESP_LOG_LEN = MAX_RESP_LOG_LEN
        # self.loop.create_task(pool.connect_pg(database, self.table))
        if isinstance(database, dict):
            self.invocations: list[dict] = []
        self.logger = get_logger(
            f'{self.table}{"_fake" if not self.database else ""}_interface'
        )
        self.pool = pool

    _autocreating_table = False
    _autocreating_functions = False

    async def execute(
        self,
        qstring: str,
        *args: Any,
    ) -> list[asyncpg.Record]:
        """Invoke the asyncpg connection's `_execute` given a provided query string and set of arguments"""
        qstring = qstring.replace("\n", " ").replace(
            "  ", ""
        )  # remove whitespace for the logs
        timeout: int = 180
        if not self.pool.pool and not isinstance(self.database, dict):
            await self.pool.connect(self.database, self.table, self.database_name)
        assert self.pool.pool
        logging.debug('execute("%s" , *%s)', qstring, args)
        async with self.pool.acquire() as connection:
            result: tuple[asyncpg.Record, list, bool]
            try:
                # _execute takes query, args, limit, timeout
                result = await connection._execute(
                    qstring, args, 0, timeout, return_status=True
                )
            except asyncpg.TooManyConnectionsError:
                await connection.execute(
                    """
                    SELECT pg_terminate_backend(pg_stat_activity.pid)
                    FROM pg_stat_activity
                    WHERE pg_stat_activity.datname = 'postgres'
                    AND pid <> pg_backend_pid();
                    """
                )
                result = await connection._execute(
                    qstring, args, 0, timeout, return_status=True
                )
            except asyncpg.UndefinedTableError:
                if self._autocreating_table:
                    logging.error(
                        "would try creating the table, but we already tried to do that"
                    )
                    raise
                self._autocreating_table = True
                logging.info("creating table %s", self.table)
                await self.create_table()
                self._autocreating_table = False
                result = await connection._execute(
                    qstring, args, 0, timeout, return_status=True
                )
            except (asyncpg.UndefinedFunctionError, asyncpg.UndefinedObjectError):
                if "create_functions" not in self.queries:
                    logging.error("undefined function and can't autocreate")
                    raise
                if self._autocreating_functions:
                    logging.error(
                        "would try creating functions, but we already tried to do that"
                    )
                    raise
                self._autocreating_functions = True
                logging.info("creating functions for %s", self.table)
                await connection.execute(self.queries.get_query("create_functions"))
                self._autocreating_table = False
                result = await connection._execute(
                    qstring, args, 0, timeout, return_status=True
                )
            # list[asyncpg.Record], str, bool

            return result[0]

    def sync_execute(self, qstring: str, *args: Any) -> asyncpg.Record:
        """Synchronous wrapper for `self.execute`"""
        ret = self.loop.run_until_complete(self.execute(qstring, *args))
        return ret

    def sync_close(self) -> Any:
        logging.info(f"closing connection: {self.pool.pool}")
        if self.pool.pool:
            ret = self.loop.run_until_complete(self.pool.pool.close())
            return ret
        return None

    def truncate(self, thing: str) -> str:
        """Logging helper. Truncates and formats."""
        if len(thing) > self.MAX_RESP_LOG_LEN:
            return (
                f"{thing[:self.MAX_RESP_LOG_LEN]}..."
                f"[{len(thing)-self.MAX_RESP_LOG_LEN} omitted]"
            )
        return thing

    def __getattribute__(self, key: str) -> Callable[..., asyncpg.Record]:
        """Implicitly define methods on this class for every statement in self.query_strings.
        If method is prefaced with "sync_": wrap as a synchronous function call.
        If statement in self.query_strings looks like an f-string, treat it
        as such by evaling before passing to `executer`."""
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            pass
        if key.startswith(
            "sync_"
        ):  # sync_ prefix implicitly wraps query as synchronous
            qstring = key.replace("sync_", "")
            executer = self.sync_execute
        else:
            executer = self.execute
            qstring = key
        try:
            statement = self.queries.get_query(qstring)
        except KeyError as e:
            raise ValueError(f"No statement of name {qstring} or {key} found!") from e
        if not self.pool.pool and isinstance(self.database, dict):
            canned_response = self.database.get(qstring, [[None]]).pop(0)
            if qstring in self.database and not self.database.get(qstring, []):
                self.database.pop(qstring)

            def return_canned(*args: Any, **kwargs: Any) -> Any:
                self.invocations.append({qstring: (args, kwargs)})
                if callable(canned_response):
                    resp = canned_response(*args, **kwargs)
                else:
                    resp = canned_response
                short_strresp = self.truncate(f"{resp}")
                logging.info(
                    f"returning `{short_strresp}` for expression: "
                    f"`{qstring}` eval'd with `{args}` & `{kwargs}`"
                )
                return resp

            return return_canned
        if "$1" in statement or "{" in statement and "}" in statement:

            def executer_with_args(*args: Any) -> Any:
                """Closure over 'statement' in local state for application to arguments.
                Allows deferred execution of f-strs, allowing PGExpresssions to operate on `args`."""
                start_time = time.time()
                rebuilt_statement = eval(
                    f'f"""{statement}"""'
                )  # pylint: disable=eval-used
                if (
                    rebuilt_statement != statement
                    and "args" in statement
                    and "$1" not in statement
                ):
                    args = ()
                resp = executer(rebuilt_statement, *args)
                short_strresp = self.truncate(f"{resp}")
                short_args = self.truncate(str(args))
                logging.debug(f"{rebuilt_statement} {short_args} -> {short_strresp}")
                elapsed = f"{time.time() - start_time:.4f}s"  # round to miliseconds
                logging.info(
                    "query %s took %s", self.truncate(rebuilt_statement), elapsed
                )
                return resp

            return executer_with_args

        def executer_without_args() -> Any:
            """Closure over local state for executer without arguments."""
            return executer(statement)

        return executer_without_args
