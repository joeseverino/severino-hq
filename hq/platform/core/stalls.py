"""The stall watch: a test process that stops making progress fails the test
it is in, saying where it is stuck (docs/Development.md, "Policy, fonts and
stalls")."""

import contextlib
import faulthandler
import functools
import io
import os
import signal
import sys
import threading
import traceback

# Bound here, so a test that patches time.monotonic moves its own clock and never the stall watch's.
from time import monotonic, process_time, thread_time
from types import FrameType
lazy import ctypes
lazy from collections.abc import Callable
lazy from multiprocessing.sharedctypes import Synchronized

from hq.platform.core.live_objects import live


def block_on_output() -> None:
    """Make writes to stdout and stderr wait for room again.

    A child that shares the run's pipes (Playwright's driver does) can leave
    them non-blocking, and a long report written then stops part way with
    ``BlockingIOError``, its failures never printed.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            os.set_blocking(stream.fileno(), True)
        except OSError, ValueError, io.UnsupportedOperation:  # noqa: S112 - a stream with no descriptor has no blocking mode to set
            continue


# Seconds a test process may go without progress (a test, a class fixture or a
# subtest starting, a subtest ending, or its process tree spending CPU) before
# the test it is in fails: short enough that a hung test frees the machine's
# heavy turn within minutes. A test that is working spends CPU however slowly
# load lets it, so no load makes it stall. The environment overrides it; 0
# turns the watch off.
STALL_SECONDS = "HQ_TEST_STALL_SECONDS"
DEFAULT_STALL_SECONDS = 300.0

# The share of one core below which a process tree counts as idle: an idle
# tree, a browser with a page open included, spends about 0.03% of a core,
# and a working one far more than 1% on a machine loaded many times over.
IDLE_SHARE = 0.01

# How many times in each bound the watch reads the tree's CPU time.
READS_PER_BOUND = 10

# How long a stalled test has to end once interrupted before its process exits,
# and how long the watch's own thread has to act before faulthandler's C
# watchdog ends the process without it: the thread needs the interpreter, which
# a call that never releases it does not give up.
_FALLBACK_GRACE = 30.0

# What interrupts the test's thread: a real signal, so a blocking call in it
# returns with EINTR and the handler runs there.
_INTERRUPT = signal.SIGALRM


class Stalled(Exception):
    """Raised in a test that made no progress for the stall bound.

    An ``Exception``, so unittest records it as the error of the test or class
    fixture it ends, Django's fixtures roll back, and the run goes on. Code
    under test that swallows it leaves the test stalled, and the process exits
    after the grace.
    """


def suspended_greenlets(stream) -> None:
    """The stack of every greenlet that is not running. Playwright's sync API
    runs the test on one and its event loop on another, so the thread's stack
    shows only the loop and the test's own call is here."""

    if (greenlet := sys.modules.get("greenlet")) is None:
        return
    for each in live(greenlet.greenlet):
        if each.gr_frame is not None:
            stream.write(f"--- suspended greenlet {each!r} (most recent call last) ---\n")
            stream.writelines(traceback.format_stack(each.gr_frame))


def tree_cpu() -> dict[int, float]:
    """CPU seconds, user and system, of this process and of each process
    under it, by pid: this process's, less the calling thread's, with its
    reaped children's, each live descendant's its own.

    This process's own is read from the clocks the kernel keeps to the
    nanosecond, the two back to back: ``os.times`` counts in clock ticks, so
    less a thread clock it grows by a whole tick whenever the calling thread's
    own reads cross one, and a walk between the two would count as work.

    The standard library reads only this process and its reaped children,
    so a live descendant is read from the kernel: ``/proc`` on Linux, libproc
    on macOS. Both through ``os`` and ``ctypes`` calls alone, since this runs
    on the watch's thread while a test may have patched ``open``, ``Path`` or
    ``subprocess``.
    """

    own = process_time() - thread_time()
    reaped = os.times()
    spent = {os.getpid(): own + reaped.children_user + reaped.children_system}
    spent |= _darwin_descendants(os.getpid()) if sys.platform == "darwin" else _linux_descendants(os.getpid())
    return spent


def advanced(before: dict[int, float], after: dict[int, float]) -> float:
    """CPU seconds the tree spent between two readings: each process's own
    growth, so one that ended between them takes nothing from the rest."""

    return sum(max(0.0, spent - before.get(pid, 0.0)) for pid, spent in after.items())


def _linux_descendants(root: int) -> dict[int, float]:
    ticks = os.sysconf("SC_CLK_TCK")
    parents, spent = {}, {}
    for name in os.listdir("/proc"):  # noqa: PTH208 - os calls alone, as tree_cpu says
        if not name.isdigit():
            continue
        try:
            handle = os.open(f"/proc/{name}/stat", os.O_RDONLY)
            try:
                stat = os.read(handle, 4096)
            finally:
                os.close(handle)
        except OSError:  # noqa: S112 - a process that ended while the walk read the rest
            continue
        # After the command's closing parenthesis: state, ppid, ... utime, stime, cutime, cstime (proc_pid_stat(5)).
        fields = stat[stat.rindex(b")") + 2 :].split()
        parents[int(name)] = int(fields[1])
        spent[int(name)] = sum(int(field) for field in fields[11:15]) / ticks
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    found, frontier = {}, [root]
    while frontier:
        for pid in children.get(frontier.pop(), ()):
            found[pid] = spent[pid]
            frontier.append(pid)
    return found


@functools.cache
def _libproc():
    """libSystem's process calls, and the factor from their time unit to seconds (mach_timebase_info)."""

    class RusageInfo(ctypes.Structure):
        # rusage_info_v0, sys/resource.h: the fields this reads, and the eight after them.
        _fields_ = [
            ("ri_uuid", ctypes.c_uint8 * 16),
            ("ri_user_time", ctypes.c_uint64),
            ("ri_system_time", ctypes.c_uint64),
            ("ri_rest", ctypes.c_uint64 * 8),
        ]

    class Timebase(ctypes.Structure):
        _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    base = Timebase()
    system.mach_timebase_info(ctypes.byref(base))
    return system, RusageInfo, base.numer / base.denom / 1e9


def _darwin_descendants(root: int) -> dict[int, float]:
    system, RusageInfo, seconds = _libproc()
    pids = (ctypes.c_int * 4096)()
    found, frontier = {}, [root]
    while frontier:
        # proc_listchildpids answers how many pids it wrote.
        count = system.proc_listchildpids(frontier.pop(), pids, ctypes.sizeof(pids))
        for pid in pids[: max(count, 0)]:
            usage = RusageInfo()
            # RUSAGE_INFO_V0; a process that ended since it was listed is left out.
            if pid in found or system.proc_pid_rusage(pid, 0, ctypes.byref(usage)) != 0:
                continue
            found[pid] = (usage.ri_user_time + usage.ri_system_time) * seconds
            frontier.append(pid)
    return found


class StallWatch:
    """Fail the test or class fixture a test process is in once it has made no
    progress for ``seconds``, saying where it is stuck.

    A daemon thread waits for the deadline each ``mark`` moves, reading the CPU
    time of the process's tree as it waits: a reading that grew by at least
    ``IDLE_SHARE`` of a core moves the deadline too, since wall time is what
    load stretches. At the deadline it reports and interrupts the test thread,
    whose handler raises ``Stalled``. With no mark within ``grace`` the process
    exits, a parallel worker first telling Django's parent so the run stops
    (docs/Development.md).
    """

    def __init__(self, seconds: float | None, stream=None, grace: float = _FALLBACK_GRACE):
        self.seconds = seconds
        self.stream = stream
        self.grace = grace
        self.evidence: list = []
        # A parallel worker's share of the run's worker counter: negative, it
        # stops Django's parent (ParallelTestSuite.run).
        self.workers: Synchronized[int] | None = None
        self._ready = threading.Condition()
        self._watching = 0
        self._label: str | None = None
        self._deadline: float | None = None
        self._marks = 0
        self._thread: threading.Thread | None = None
        self._main: int | None = None
        self._previous: Callable[[int, FrameType | None], object] | int | None = None
        self._due: str | None = None

    @classmethod
    def from_environment(cls) -> StallWatch:
        value = os.environ.get(STALL_SECONDS, "").strip()
        return cls(float(value) if value else DEFAULT_STALL_SECONDS)

    @property
    def enabled(self) -> bool:
        return bool(self.seconds)

    @contextlib.contextmanager
    def watching(self):
        """Watch this process while the block runs, restoring what an outer
        run was watching when it ends."""

        if not self.enabled:
            yield
            return
        with self._ready:
            saved = (self._label, self._deadline)
            self._watching += 1
            if self._watching == 1:
                self._take_interrupt()
            if self._thread is None:
                self._thread = threading.Thread(target=self._wait, name="stall-watch", daemon=True)
                self._thread.start()
        try:
            yield
        finally:
            with self._ready:
                self._watching -= 1
                self._label, self._deadline = saved if self._watching else (None, None)
                if not self._watching:
                    self._give_back_interrupt()
                self._ready.notify()
            if self._deadline is None:
                faulthandler.cancel_dump_traceback_later()

    def _take_interrupt(self) -> None:
        # Only the main thread may set a handler, and only its handlers run.
        if threading.current_thread() is threading.main_thread():
            self._main = threading.get_ident()
            self._previous = signal.signal(_INTERRUPT, self._interrupted)

    def _give_back_interrupt(self) -> None:
        if self._main is not None:
            signal.signal(_INTERRUPT, self._previous)
            self._main = self._previous = self._due = None

    def _interrupted(self, signum, frame) -> None:
        label, self._due = self._due, None
        if label is not None:
            raise Stalled(
                f"{label} made no progress for {self.seconds:g}s, its processes idle; its stall report is above"
            )
        if callable(self._previous):
            self._previous(signum, frame)

    def mark(self, label: str) -> None:
        """``label`` starts now; it has ``seconds`` to finish or be followed."""

        if not self._watching:
            return
        with self._ready:
            self._label = label
            self._deadline = monotonic() + self.seconds
            self._marks += 1
            self._ready.notify()
        self._backstop(self.seconds + self.grace)

    @staticmethod
    def _backstop(seconds: float) -> None:
        """Arm faulthandler's watchdog, which dumps every stack and exits the
        process. To the process's own stderr: a test may have swapped
        ``sys.stderr`` for a stream with no descriptor."""

        faulthandler.dump_traceback_later(seconds, exit=True, file=sys.__stderr__)

    @contextlib.contextmanager
    def paused(self):
        """No deadline while the block runs: a wait on a child process that
        has a watch of its own. What was running has the whole bound again
        once it ends."""

        with self._ready:
            label, self._deadline = self._label, None
        faulthandler.cancel_dump_traceback_later()
        try:
            yield
        finally:
            if label is not None:
                self.mark(label)

    def report(self, label: str) -> None:
        """Write what has stalled, the evidence for it, and every thread's stack."""

        block_on_output()
        # The process's own stderr: the test's may be --buffer's, printed only once it fails.
        stream = self.stream or sys.__stderr__
        stream.write(f"\n=== stalled {self.seconds:g}s: {label} (pid {os.getpid()}) ===\n")
        for evidence in list(self.evidence):
            evidence(stream)
        stream.write("--- every thread's stack ---\n")
        stream.flush()
        faulthandler.dump_traceback(file=stream, all_threads=True)
        suspended_greenlets(stream)
        stream.write(f"=== end of stall report: {label} ===\n")
        stream.flush()

    def stop(self, label: str, marks: int) -> None:
        """Fail what stalled, or end the process if it does not stop."""

        # Armed before the interrupt, so the end of the watch, which the
        # interrupt can bring about, always comes after it and cancels it.
        self._backstop(self.grace + _FALLBACK_GRACE)
        if self._main is not None:
            self._due = label
            signal.pthread_kill(self._main, _INTERRUPT)
        with self._ready:
            moved = self._ready.wait_for(lambda: self._marks != marks or not self._watching, timeout=self.grace)
        if not moved:
            self.end(label)

    def end(self, label: str) -> None:
        """Exit the process: what stalled did not stop when interrupted."""

        stream = self.stream or sys.__stderr__
        stream.write(f"=== {label} did not stop {self.grace:g}s after it was interrupted: the process exits ===\n")
        stream.flush()
        if self.workers is not None:
            with self.workers.get_lock():
                self.workers.value = -1
        os._exit(1)

    def cpu(self) -> dict[int, float]:
        """The tree's CPU time by pid, less what this watch's own thread spent reading it."""

        return tree_cpu()

    def _wait(self) -> None:
        last: tuple[float, dict[int, float]] | None = None
        while True:
            with self._ready:
                if self._deadline is None:
                    last = None
                    self._ready.wait()
                    continue
                now = monotonic()
                due = min(self._deadline, last[0] + self.seconds / READS_PER_BOUND) if last else now
                if now < due:
                    self._ready.wait(due - now)
                    continue
            # Read outside the lock, so a mark never waits on it.
            when, spent = monotonic(), self.cpu()
            busy = last is not None and advanced(last[1], spent) >= IDLE_SHARE * (when - last[0])
            first, last = last is None, (when, spent)
            if not first and (stalled := self._judged(when, busy)):
                faulthandler.cancel_dump_traceback_later()
                self.report(stalled[0])
                self.stop(*stalled)

    def _judged(self, when: float, busy: bool) -> tuple[str, int] | None:
        """What stalled, as of a reading taken at ``when``: the label and its
        mark's count once the deadline has passed with the tree idle, else
        None, the deadline moved on if the tree was busy."""

        with self._ready:
            if self._deadline is None:
                return None
            if busy:
                self._deadline = max(self._deadline, when + self.seconds)
                self._backstop(self._deadline - when + self.grace)
                return None
            if when < self._deadline:
                return None
            stalled, self._deadline = (self._label or "", self._marks), None
            return stalled


stall_watch = StallWatch.from_environment()


class StallMarks:
    """Restart the stall watch as each test begins and each subtest ends."""

    def startTest(self, test):
        stall_watch.mark(test.id())
        return super().startTest(test)

    def addSubTest(self, test, subtest, err):
        stall_watch.mark(f"{test.id()} after {subtest.id().removeprefix(test.id()).strip()}")
        return super().addSubTest(test, subtest, err)
