"""`acrobe test` — transport self-tests.

`datagram-loopback` drives a :class:`Datagram` whose peer is expected
to echo every message back unchanged, validates each echoed packet,
and reports throughput and latency statistics.

Each packet starts with a 4-byte little-endian sequence number,
followed by pseudo-random bytes drawn from a seeded PRNG so a run is
reproducible with ``--seed``.
"""

import asyncio
import random
import statistics
import time

import asyncclick as click

from . import base
from ..protocol.datagram import Datagram


class SizeParamType(click.ParamType):
    """Byte count with an optional 1024-based ``k`` / ``M`` / ``G``
    suffix (e.g. ``4096``, ``4k``, ``1M``)."""

    name = "size"

    SUFFIXES = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}

    def convert(self, value, param, ctx):
        if isinstance(value, int):
            return value
        text = value.strip()
        scale = self.SUFFIXES.get(text[-1:].lower(), 1)
        if scale != 1:
            text = text[:-1]
        try:
            count = int(text, 0)
        except ValueError:
            self.fail(f"{value!r} is not a valid size", param, ctx)
        if count < 0:
            self.fail(f"{value!r} is negative", param, ctx)
        return count * scale


SIZE = SizeParamType()


class LoopbackFailure(Exception):
    """Fatal error that aborts a loopback run."""


class LoopbackStats:
    """Counters and per-packet latencies of a loopback run."""

    ERROR_LOG_MAX = 20

    def __init__(self):
        self.sent_packets = 0
        self.sent_bytes = 0
        self.received_packets = 0
        self.valid_packets = 0
        self.valid_bytes = 0
        self.reordered = 0
        self.lost = 0
        self.errors = {}
        self.error_log = []
        self.latencies = []
        self.t_start = None
        self.t_end = None

    def begin(self):
        self.t_start = time.perf_counter()

    def end(self):
        self.t_end = time.perf_counter()

    def record_sent(self, size):
        self.sent_packets += 1
        self.sent_bytes += size

    def record_valid(self, size, latency):
        self.valid_packets += 1
        self.valid_bytes += size
        self.latencies.append(latency)

    def record_error(self, kind, message):
        self.errors[kind] = self.errors.get(kind, 0) + 1
        if len(self.error_log) < self.ERROR_LOG_MAX:
            self.error_log.append(message)

    @property
    def error_count(self):
        return sum(self.errors.values()) + self.lost

    @property
    def elapsed(self):
        return self.t_end - self.t_start

    @staticmethod
    def format_duration(seconds):
        if seconds < 1e-3:
            return f"{seconds * 1e6:.1f} us"
        if seconds < 1:
            return f"{seconds * 1e3:.3f} ms"
        return f"{seconds:.3f} s"

    @staticmethod
    def format_bytes(count):
        for unit in ("B", "KiB", "MiB"):
            if count < 1024:
                return f"{count:.1f} {unit}"
            count /= 1024
        return f"{count:.1f} GiB"

    def report(self):
        """Return the human-readable report as a list of lines."""
        elapsed = self.elapsed
        lines = [
            f"packets:    {self.sent_packets} sent, "
            f"{self.received_packets} received, "
            f"{self.valid_packets} valid",
            f"payload:    {self.sent_bytes} bytes sent, "
            f"{self.valid_bytes} bytes echoed back",
            f"elapsed:    {self.format_duration(elapsed)}",
        ]
        if elapsed > 0:
            lines.append(
                f"throughput: {self.valid_packets / elapsed:.1f} packets/s, "
                f"{self.format_bytes(self.valid_bytes / elapsed)}/s "
                f"(each direction)")
        if self.latencies:
            lat = sorted(self.latencies)
            if len(lat) > 1:
                p99 = statistics.quantiles(lat, n=100)[98]
                stddev = statistics.stdev(lat)
            else:
                p99 = lat[0]
                stddev = 0.0
            fmt = self.format_duration
            lines.append(
                f"latency:    min {fmt(lat[0])}, "
                f"mean {fmt(statistics.fmean(lat))}, "
                f"median {fmt(statistics.median(lat))}, "
                f"p99 {fmt(p99)}, max {fmt(lat[-1])}, "
                f"stddev {fmt(stddev)}")
        lines.append(f"reordered:  {self.reordered}")
        errors = dict(self.errors)
        if self.lost:
            errors["lost"] = self.lost
        if errors:
            detail = ", ".join(f"{k} {v}" for k, v in sorted(errors.items()))
            lines.append(f"errors:     {self.error_count} ({detail})")
        else:
            lines.append("errors:     0")
        return lines


class DatagramLoopbackTest:
    """Send packets through a looped-back :class:`Datagram` and
    validate the echoes.

    Up to `window` packets are in flight at once. Echoes are matched
    to sent packets by sequence number, so a transport that reorders
    messages still validates; reordering is counted. A packet whose
    echo does not come back within `timeout` aborts the run, as the
    remaining receives can no longer be attributed.
    """

    HEADER = 4

    def __init__(self, datagram, *, count=None, volume=None,
                 min_size=64, max_size=64, window=1, timeout=5.0,
                 seed=0, keep_going=False):
        if count is None and volume is None:
            raise ValueError("count or volume must be given")
        if min_size < self.HEADER:
            raise ValueError(f"min_size must be at least {self.HEADER}")
        if max_size < min_size:
            raise ValueError("max_size must not be below min_size")
        if window < 1:
            raise ValueError("window must be at least 1")
        self.datagram = datagram
        self.count = count
        self.volume = volume
        self.min_size = min_size
        self.max_size = max_size
        self.window = window
        self.timeout = timeout
        self.keep_going = keep_going
        self.rng = random.Random(seed)
        self.stats = LoopbackStats()
        self.outstanding = {}
        self.failure = None

    def __limit_reached(self):
        if self.count is not None and self.stats.sent_packets >= self.count:
            return True
        if self.volume is not None and self.stats.sent_bytes >= self.volume:
            return True
        return False

    def __payload(self, seq):
        size = self.rng.randint(self.min_size, self.max_size)
        return (seq & 0xffffffff).to_bytes(self.HEADER, "little") \
            + self.rng.randbytes(size - self.HEADER)

    def __error(self, kind, message):
        self.stats.record_error(kind, message)
        if not self.keep_going:
            raise LoopbackFailure(message)

    def __check(self, data, t_received):
        self.stats.received_packets += 1
        if len(data) < self.HEADER:
            self.__error("unexpected",
                         f"short message of {len(data)} bytes")
            return
        seq = int.from_bytes(data[:self.HEADER], "little")
        entry = self.outstanding.get(seq)
        if entry is None:
            self.__error("unexpected",
                         f"packet {seq} received but not outstanding")
            return
        if seq != next(iter(self.outstanding)):
            self.stats.reordered += 1
        del self.outstanding[seq]
        payload, t_sent = entry
        if len(data) != len(payload):
            self.__error("length",
                         f"packet {seq}: sent {len(payload)} bytes, "
                         f"received {len(data)}")
            return
        if data != payload:
            offset = next(i for i, (a, b) in enumerate(zip(data, payload))
                          if a != b)
            self.__error("corrupt",
                         f"packet {seq}: first mismatch at offset {offset} "
                         f"(sent 0x{payload[offset]:02x}, "
                         f"received 0x{data[offset]:02x})")
            return
        self.stats.record_valid(len(data), t_received - t_sent)

    async def __exchange(self, slots, payload):
        try:
            send = self.datagram.send(payload)
            recv = self.datagram.recv()
            await send
            try:
                data, _ = await asyncio.wait_for(
                    asyncio.shield(recv), self.timeout)
            except asyncio.TimeoutError:
                raise LoopbackFailure(
                    f"no message received within {self.timeout} s")
            self.__check(bytes(data), time.perf_counter())
        except Exception as exc:
            if self.failure is None:
                self.failure = exc
        finally:
            slots.release()

    async def run(self):
        """Run the test to completion. Returns the first fatal error,
        or ``None``; statistics are in :attr:`stats` either way."""
        slots = asyncio.Semaphore(self.window)
        tasks = set()
        self.stats.begin()
        try:
            seq = 0
            while self.failure is None and not self.__limit_reached():
                await slots.acquire()
                if self.failure is not None:
                    break
                payload = self.__payload(seq)
                self.outstanding[seq] = (payload, time.perf_counter())
                self.stats.record_sent(len(payload))
                task = asyncio.create_task(self.__exchange(slots, payload))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                seq += 1
            while tasks and self.failure is None:
                await asyncio.wait(set(tasks),
                                   return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.stats.end()
            for task in tasks:
                task.cancel()
        if self.failure is None:
            self.stats.lost = len(self.outstanding)
        return self.failure


@base.cli.group(name="test", help="Transport self-tests")
async def selftest():
    pass


@selftest.command(
    name="datagram-loopback",
    help="Exercise a Datagram whose peer echoes every message back, "
         "validate the echoes and report throughput and latency. "
         "With --window above 1, latency includes queueing time.")
@click.option("-r", "--root", "root_path", required=True,
              help="Path to a Datagram (e.g. udp/127.0.0.1:9999)")
@click.option("-n", "--count", type=click.IntRange(min=1), default=None,
              help="Number of packets (default 1000 when --volume is "
                   "not given)")
@click.option("-V", "--volume", type=SIZE, default=None,
              help="Total payload to send, k/M/G suffixes accepted")
@click.option("--min-size", type=SIZE, default=64, show_default=True,
              help="Minimum packet size")
@click.option("--max-size", type=SIZE, default=64, show_default=True,
              help="Maximum packet size")
@click.option("-w", "--window", type=click.IntRange(min=1), default=1,
              show_default=True, help="Maximum packets in flight")
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True),
              default=5.0, show_default=True,
              help="Seconds to wait for each echo")
@click.option("--seed", type=int, default=None,
              help="PRNG seed for sizes and contents (default: random)")
@click.option("-k", "--keep-going", is_flag=True,
              help="Count validation errors instead of stopping at the "
                   "first one")
@click.pass_context
async def datagram_loopback(ctx, root_path, count, volume, min_size,
                            max_size, window, timeout, seed, keep_going):
    if min_size < DatagramLoopbackTest.HEADER:
        raise click.BadParameter(
            f"must be at least {DatagramLoopbackTest.HEADER}",
            param_hint="--min-size")
    if max_size < min_size:
        raise click.BadParameter(
            "must not be below --min-size", param_hint="--max-size")
    if count is None and volume is None:
        count = 1000
    if seed is None:
        seed = random.randrange(1 << 32)

    leaf = await ctx.obj.resolve(root_path)
    if not isinstance(leaf, Datagram):
        raise click.ClickException(
            f"{root_path!r} does not resolve to a Datagram "
            f"(got {type(leaf).__name__})")

    click.echo(f"datagram loopback on {leaf.fqdn}, seed {seed}")
    test = DatagramLoopbackTest(
        leaf, count=count, volume=volume, min_size=min_size,
        max_size=max_size, window=window, timeout=timeout, seed=seed,
        keep_going=keep_going)
    failure = await test.run()

    stats = test.stats
    for line in stats.report():
        click.echo(line)
    for message in stats.error_log:
        click.echo(f"error: {message}", err=True)
    hidden = sum(stats.errors.values()) - len(stats.error_log)
    if hidden > 0:
        click.echo(f"error: ... {hidden} more", err=True)

    if failure is not None:
        raise click.ClickException(f"aborted: {failure}")
    if stats.error_count:
        raise click.ClickException(
            f"{stats.error_count} errors in {stats.sent_packets} packets")
