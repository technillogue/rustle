# Copyright (c) 2022 sylv
import asyncio
import json
import logging
import os
import sys
import config

def FuckAiohttp(record: logging.LogRecord) -> bool:
    str_msg = str(getattr(record, "msg", ""))
    if "was destroyed but it is pending" in str_msg:
        return False
    if str_msg.startswith("task:") and str_msg.endswith(">"):
        return False
    return True

# adapted from https://stackoverflow.com/questions/50144628/python-logging-into-file-as-a-dictionary-or-json
class JsonFormatter(logging.Formatter):
    """
    Formatter that outputs JSON strings after parsing the LogRecord.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        # this is all the normal logrecord attributes except "levelname", "module", "funcName", "lineno"
        # we're doing this to recover extras
        # https://github.com/python/cpython/blob/main/Lib/logging/__init__.py#L1653
        exclude = [
            "name",
            "msg",
            "args",
            "levelno",
            "pathname",
            "filename",
            "exc_info",
            "exc_text",
            "stack_info",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
        ]
        message_dict = {k: v for k, v in record.__dict__.items() if k not in exclude}

        if record.exc_info:
            # Cache the traceback text to avoid converting it multiple times
            # (it's constant anyway)
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)

        if record.exc_text:
            message_dict["exc_info"] = record.exc_text

        if record.stack_info:
            message_dict["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(message_dict, default=str)


USE_VECTOR = config.get_secret("HONEYCOMB_API_KEY") and os.path.exists("./vector")


class Vector:
    # all the stuff in this class declaration will be run once and available from self
    logger = logging.getLogger("root")
    logger.setLevel("DEBUG")

    if USE_VECTOR:
        original_stdout = os.dup(sys.stdout.fileno())
        original_stderr = os.dup(sys.stderr.fileno())

        # explicit pipe we'll use for tee later
        tee_read, tee_write = os.pipe()
        # explicit pipe for vector -- we'll be passing this to tee
        vector_read, vector_write = os.pipe()
        # adapted from https://stackoverflow.com/a/651718
        # Cause tee's stdin to get a copy of our stdin/stdout (as well as that
        # of any child processes we spawn)
        # the pipe will buffer everything we write until vector starts reading
        # note that logging calls will appear before prints
        # note note any fatal errors between this and tee starting will be lost forever!
        os.dup2(tee_write, sys.stdout.fileno())
        os.dup2(tee_write, sys.stderr.fileno())

        # set up logging
        # write structured logs to only vector, not original stderr
        vector_file = os.fdopen(vector_write, mode="w")
        vector_handler = logging.StreamHandler(vector_file)
        vector_handler.addFilter(FuckAiohttp)
        vector_handler.setLevel("DEBUG")
        vector_handler.setFormatter(JsonFormatter())
        logger.addHandler(vector_handler)
        # we want to write formatted logs only to the original stderr, not vector
        # normally this would be sys.stder
        # but we need to open the duplicated fd as a file
        stderr_file = os.fdopen(original_stderr, mode="w")
        console_handler: logging.StreamHandler = logging.StreamHandler(stderr_file)
    else:
        console_handler = logging.StreamHandler()
    fmt = logging.Formatter("{levelname} {module}:{lineno}: {message}", style="{")
    console_handler.setLevel(
        ((os.getenv("LOGLEVEL") or os.getenv("LOG_LEVEL")) or "DEBUG").upper()
    )
    console_handler.setFormatter(fmt)
    console_logger.addFilter(FuckAiohttp)
    logger.addHandler(console_handler)
    # if i hear about epoll selector one more time i'mna end it
    logging.getLogger("asyncio").setLevel("INFO")

    logging.info("starting")

    async def init_vector(self) -> None:
        if not USE_VECTOR:
            return
        self.tee = await asyncio.create_subprocess_shell(
            # write to vector's fd and stdout
            f"tee /dev/fd/{self.vector_write}",
            # if we just set stdin to just PIPE, it would be a StreamWriter and not have a real fd
            # so we're using the explicit pipe we opened earlier
            stdin=self.tee_read,
            stdout=self.original_stdout,
            stderr=self.original_stderr,
            # tee should have access to the vector fd
            pass_fds=[self.vector_write],
        )
        self.vector = await asyncio.create_subprocess_shell(
            # "cat - > /tmp/fake_vector",
            "./vector --quiet -c vector.toml",
            stdin=self.vector_read,
            env={"HONEYCOMB_API_KEY": config.get_secret("HONEYCOMB_API_KEY")},
        )
        logging.info("started vector")

    # this seems to make things exit early without logging
    # anyway init waits for orphaned processes to exit so it's fine
    # async def cleanup(self) -> None:
    #     self.vector.terminate()
    #     self.tee.terminate()
    #     await self.vector.communicate()
    #     await self.tee.communicate()


def sync_start_vector() -> None:
    asyncio.run(Vector().init_vector())


async def main() -> None:
    vector = Vector()
    await vector.init_vector()
    # this goes to tee, vector sees it's not json and leaves it unchanged
    print("example print")
    # subprocesses inherit the same thing
    await (await asyncio.create_subprocess_shell("date")).wait()
    # logging is special and prettyprinted to original stdout but structured for vector
    logging.info("a log message with extra information", extra={"attribute": "42"})


if __name__ == "__main__":
    asyncio.run(main())
    print("message after asyncio")
    # open("/tmp/test", "a").write("we ran\n")
