import time
import unittest

from tests.support import Base
from stack_ui import podman_api as pa


def frame(stream, text):
    return bytes([stream, 0, 0, 0]) + len(text).to_bytes(4, 'big') + text


class LogFramesTest(unittest.TestCase):
    def test_two_frames_in_one_chunk(self):
        f = pa.LogFrames()
        self.assertEqual(f.feed(frame(1, b'a out\n') + frame(2, b'b err\n')), ['a out', 'b err'])

    def test_every_split_position_gives_the_same_lines(self):
        data = frame(1, b'one\n') + frame(2, b'two\n')
        f = pa.LogFrames()
        got = []
        for i in range(len(data)):                 # one byte at a time: a split in every header and body
            got += f.feed(data[i:i + 1])
        self.assertEqual(got, ['one', 'two'])

    def test_a_line_split_across_frames_and_the_unfinished_last_line(self):
        f = pa.LogFrames()
        self.assertEqual(f.feed(frame(1, b'hel')), [])
        self.assertEqual(f.feed(frame(1, b'lo\nnext')), ['hello'])
        self.assertEqual(f.rest(), ['next'])
        self.assertEqual(f.rest(), [])

    def test_plain_text_without_frames_a_tty(self):
        f = pa.LogFrames()
        self.assertEqual(f.feed(b'2026-11-07T10:00:00Z one\r\n2026-11-07T10:00:01Z tw'), ['2026-11-07T10:00:00Z one'])
        self.assertEqual(f.feed(b'o\n'), ['2026-11-07T10:00:01Z two'])

    def test_invalid_utf8_is_replaced_and_an_empty_chunk_is_harmless(self):
        f = pa.LogFrames()
        self.assertEqual(f.feed(b''), [])
        self.assertEqual(f.feed(frame(1, b'bad \xff byte\n')), ['bad � byte'])

    def test_blank_lines_are_kept(self):
        self.assertEqual(pa.LogFrames().feed(frame(1, b'a\n\nb\n')), ['a', '', 'b'])

    def test_a_line_that_never_ends_stays_bounded_and_is_still_delivered_cut(self):
        limit = pa.LogFrames.LINE_LIMIT
        f = pa.LogFrames()
        for _ in range(100):                           # a progress line redrawn with \r: 1 MB without a newline
            f.feed(frame(1, b'x' * 10000))
        self.assertLessEqual(len(f.text), limit)
        self.assertEqual(f.feed(frame(1, b'rest of it\nnext\n')), ['x' * limit, 'next'])

    def test_the_bound_holds_without_frames_too_and_a_normal_line_is_not_cut(self):
        f = pa.LogFrames()
        self.assertEqual(f.feed(b'y' * 1_000_000), [])
        self.assertLessEqual(len(f.text), pa.LogFrames.LINE_LIMIT)
        self.assertEqual(f.feed(b'\nshort\n'), ['y' * pa.LogFrames.LINE_LIMIT, 'short'])
        self.assertEqual(pa.LogFrames().feed(b'z' * 5000 + b'\n'), ['z' * 5000])


class PumpTest(unittest.TestCase):
    def run_pump(self, chunks, error=None):
        class Fake:
            def __init__(self):
                self.i = 0

            def read1(self, n):
                if self.i < len(chunks):
                    self.i += 1
                    return chunks[self.i - 1]
                if error:
                    raise error
                return b''
        out = []
        pa._pump(Fake(), out.append, lambda: False)
        return out

    def test_lines_then_the_end_and_the_unfinished_line_is_flushed(self):
        out = self.run_pump([b'a\nb', b'\nc'])
        self.assertEqual(out[:3], ['a', 'b', 'c'])
        self.assertIs(out[3], pa._END)

    def test_an_error_after_a_line_becomes_a_failure(self):
        out = self.run_pump([b'a\n'], OSError('boom'))
        self.assertEqual(out[0], 'a')
        self.assertIsInstance(out[1], pa._Failure)
        self.assertIn('boom', out[1].message)

    def test_a_stop_turns_the_error_into_a_plain_end(self):
        class Fake:
            def read1(self, n):
                raise OSError('closed')
        out = []
        states = [False]                                  # not stopped when it reads, stopped when it fails
        pa._pump(Fake(), out.append, lambda: states.pop(0) if states else True)
        self.assertEqual(out, [pa._END])


class LogStreamTest(Base):
    def setUp(self):
        super().setUp()
        self.mock.add_container('a')

    def open(self, **kw):
        s = pa.LogStream('id-a', **kw)
        self.addCleanup(s.close)
        return s

    @staticmethod
    def take(lines, n):
        out = []
        for item in lines:
            if item is not None:
                out.append(item)
                if len(out) == n:
                    break
        return out

    def test_history_then_new_lines_then_the_end(self):
        self.mock.logs['id-a'] = frame(1, b'old one\nold two\n')
        lines = self.open().lines(idle=5)
        self.assertEqual(self.take(lines, 2), ['old one', 'old two'])
        self.mock.push_log('id-a', frame(1, b'new\n'))
        self.assertEqual(self.take(lines, 1), ['new'])
        self.mock.end_log('id-a')
        self.assertEqual(list(lines), [])

    def test_the_call_follows_and_passes_tail_and_since(self):
        self.mock.end_log('id-a')
        list(self.open(tail=500, since='2026-11-07T10:00:00Z').lines(idle=5))
        self.assertIn(('GET', '/containers/id-a/logs',
                       {'stdout': 'true', 'stderr': 'true', 'timestamps': 'true', 'follow': 'true', 'tail': '500',
                        'since': '2026-11-07T10:00:00Z'}), self.mock.calls)

    def test_a_quiet_stream_yields_none(self):
        self.assertIsNone(next(self.open().lines(idle=0.05)))

    def test_a_refused_call_raises_before_any_line(self):
        with self.assertRaises(pa.PodmanError) as ctx:
            pa.LogStream('nope')
        self.assertEqual(ctx.exception.code, 404)
        self.mock.fail[('GET', '/containers/id-a/logs')] = (500, 'no log driver')
        with self.assertRaises(pa.PodmanError) as ctx:
            pa.LogStream('id-a')
        self.assertIn('no log driver', str(ctx.exception))

    def test_close_ends_the_lines_and_a_late_line_is_not_delivered(self):
        s = self.open()
        s.close()
        self.mock.push_log('id-a', frame(1, b'late\n'))
        self.assertEqual([x for x in s.lines(idle=0.05) if x is not None], [])

    def test_close_frees_a_reader_blocked_on_a_full_queue(self):
        self.patch(pa.LogStream, 'QUEUE_MAX', 2)
        s = self.open()
        for i in range(10):
            self.mock.push_log('id-a', frame(1, f'line {i}\n'.encode()))
        time.sleep(0.3)                      # the reader fills the queue and waits for room
        s.close()
        s.thread.join(timeout=3)
        self.assertFalse(s.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
