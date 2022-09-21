#!/usr/bin/python3.10
# Copyright (c) 2022 sylv
# Copyright (c) 2021 The Forest Team
import asyncio
import functools
import logging
import os
import time
from functools import _HashedSeq  # type: ignore # it's not in __all__ so mypy is sad
from typing import Awaitable, Callable, Dict, Optional, ParamSpec, TypeVar, cast

#### Configure Parameters

# edge cases:
# accessing an unset secret loads other variables and potentially overwrites existing ones
def parse_secrets(secrets: str) -> dict[str, str]:
    pairs = [
        line.strip().split("=", 1)
        for line in secrets.split("\n")
        if line and not line.startswith("#")
    ]
    can_be_a_dict = cast(list[tuple[str, str]], pairs)
    return dict(can_be_a_dict)


# to dump: "\n".join(f"{k}={v}" for k, v in secrets.items())


@functools.cache  # don't load the same env more than once
def load_secrets(env: Optional[str] = None, overwrite: bool = False) -> None:
    if not env:
        env = os.environ.get("ENV", "dev").lower()
    try:
        logging.info("loading secrets from %s_secrets", env)
        secrets = parse_secrets(open(f"{env}_secrets").read())
        if overwrite:
            new_env = secrets
        else:
            # mask loaded secrets with existing env
            new_env = secrets | os.environ
        os.environ.update(new_env)
    except FileNotFoundError:
        pass


secret_cache: Dict[str, str] = {}

# potentially split this into get_flag and get_secret; move all of the flags into fly.toml;
# maybe keep all the tomls and dockerfiles in a separate dir with a deploy script passing --config and --dockerfile explicitly
def get_secret(key: str, env: Optional[str] = None, fail_if_none: bool = False) -> str:
    try:
        secret = os.environ[key]
    except KeyError:
        load_secrets(env)
        secret = os.environ.get(key, "")
        if not secret:
            if fail_if_none:
                raise ValueError(f"Unable to find secret {key}, got {secret}")
            secret = ""
    if secret.lower() in ("0", "false", "no"):
        return ""

    return secret


## Parameters for easy access and ergonomic use

APP_NAME = os.getenv("FLY_APP_NAME")
URL = os.getenv("URL_OVERRIDE", f"https://{APP_NAME}.fly.dev")
LOCAL = os.getenv("FLY_APP_NAME") is None
ENV = os.getenv("ENV", "DEV")


def log_task_result(task: asyncio.Task) -> None:
    """
    Done callback which logs task done result
    args:
        task (asyncio.task): task to be handled
    """
    name = task.get_name() + "-" + getattr(task.get_coro(), "__name__", "")
    try:
        result = task.result()
        logging.info("final result of %s was %s", name, result)
    except asyncio.CancelledError:
        logging.info("task %s was cancelled", name)
    except Exception:  # pylint: disable=broad-except
        logging.exception("%s errored", name)

def wrap_task(task: asyncio.Task) -> asyncio.Task:
    task.add_done_callback(log_task_result)
    return task


# def cached(ttl: int) -> Callable[[Callable[P, T]], Callable[P, T]]:
#     "decorator to cache function with a ttl"
#
#     def decorator(fn: Callable[P, T]) -> Callable[P, T]:
#         @functools.lru_cache
#         def cached_fn(*args: P.args, ttl_hash: int, **kwargs: P.kwargs) -> T:
#             del ttl_hash
#             return fn(*args, **kwargs)
#
#         @functools.wraps(fn)
#         def caching_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
#             # ttl_hash is the same value within `timeout` time period
#
#             ttl_hash = round(time.time() / ttl)
#             return cached_fn(*args, ttl_hash=ttl_hash, **kwargs)
#
#         return caching_wrapper

#     return decorator

T = TypeVar("T")
P = ParamSpec("P")


def swr_cache(
    ttl: int = 60, swr_ttl: int = 3600
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    "ttl: how long a result stays fresh. swr_ttl: how long a result can be returned while revalidating"

    def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        cache: dict[_HashedSeq, tuple[int, int, T]] = {}

        async def revalidate(key: _HashedSeq, *args: P.args, **kwargs: P.kwargs) -> T:
            value = await fn(*args, **kwargs)
            cache[key] = (round(time.time() / ttl), round(time.time() / swr_ttl), value)
            return value

        @functools.wraps(fn)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            # build a quickly hashable key from the function arguments as per lru_cache
            key = functools._make_key(args, kwargs, typed=False)
            ttl_key, swr_ttl_key, value = cache.get(key, (None, None, None))
            if value and swr_ttl_key == round(time.time() / swr_ttl):
                logging.info("swr ttl key matches")
                if ttl_key != round(time.time() / ttl):
                    logging.info("returning stale while revalidating for %s", fn)
                    asyncio.create_task(revalidate(key, *args, **kwargs))
                logging.info("ttl key matches, not revalidating")
                return value
            return await revalidate(key, *args, **kwargs)

        return wrapped

    return decorator
