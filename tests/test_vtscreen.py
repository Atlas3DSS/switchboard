import fcntl
import os
import select
import struct
import subprocess
import sys
import termios
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from switchboard.vtscreen import Screen  # noqa: E402

FAKE_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_cli.py")


def scr(data, cols=20, rows=5):
    s = Screen(cols, rows)
    s.feed(data)
    return s


class TestBasics(unittest.TestCase):
    def test_print_and_controls(self):
        s = scr("ab\r\ncd\x08X\tY\x07")
        self.assertEqual(s.lines()[:2], ["ab", "cX      Y"])
        self.assertEqual(s.cursor, (1, 9))

    def test_cursor_moves(self):
        s = Screen(20, 5)
        steps = [("\x1b[3;5H", (2, 4)), ("\x1b[2A", (0, 4)), ("\x1b[A", (0, 4)), ("\x1b[3C", (0, 7)),
                 ("\x1b[10D", (0, 0)), ("\x1b[2B", (2, 0)), ("\x1b[99B", (4, 0)), ("\x1b[7G", (4, 6)),
                 ("\x1b[2F", (2, 0)), ("\x1b[3`", (2, 2)), ("\x1b[E", (3, 0)), ("\x1b[2d", (1, 0)),
                 ("\x1b[4;9f", (3, 8)), ("\x1b[99;99H", (4, 19)), ("\x1b[H", (0, 0)), ("\x1b[0;0H", (0, 0))]
        for seq, pos in steps:
            s.feed(seq)
            self.assertEqual(s.cursor, pos, repr(seq))
        s.feed("\x1b[2;3HA\x1b[4;1HB\x1b[1;20HC")
        self.assertEqual(s.lines()[:4], [" " * 19 + "C", "  A", "", "B"])
        s.feed("\x1b[HZ\x1b[sQ\x1b[3;3H\x1b[uR\x1b7\x1b[5;1H\x1b8S")
        self.assertEqual(s.lines()[0], "ZRS" + " " * 16 + "C")

    def test_wrap_pending(self):
        s = scr("x" * 20, cols=20)
        self.assertEqual(s.cursor, (0, 19))
        self.assertEqual(s.lines()[1], "")
        s.feed("\r\n")  # pending wrap must not produce an extra blank line
        self.assertEqual(s.cursor, (1, 0))
        s = scr("x" * 20 + "yz", cols=20)
        self.assertEqual(s.lines()[:2], ["x" * 20, "yz"])
        s = scr("\x1b[?7l" + "a" * 25 + "\x1b[?7h", cols=20)
        self.assertEqual(s.lines()[:2], ["a" * 20, ""])

    def test_wide_and_combining(self):
        s = scr("a中b", cols=10)
        self.assertEqual(s.lines()[0], "a中b")
        self.assertEqual(s.cursor, (0, 4))
        s.feed("\x1b[1;3HX")  # overwrite right half of the wide char
        self.assertEqual(s.lines()[0], "a Xb")
        s = scr("é‍!", cols=10)
        self.assertEqual(s.lines()[0], "é‍!")
        self.assertEqual(s.cursor, (0, 2))
        s = scr("123456789中", cols=10)  # wide char at last column wraps
        self.assertEqual(s.lines()[:2], ["123456789", "中"])

    def test_erase(self):
        s = scr("abcdef\r\nghijkl\r\nmnopqr\x1b[2;3H\x1b[K")
        self.assertEqual(s.lines()[:3], ["abcdef", "gh", "mnopqr"])
        s.feed("\x1b[1K")
        self.assertEqual(s.lines()[1], "")
        s.feed("\x1b[1;4H\x1b[1J")
        self.assertEqual(s.lines()[:3], ["    ef", "", "mnopqr"])
        s.feed("\x1b[J")
        self.assertEqual(s.lines()[:3], ["", "", ""])
        s = scr("abcdef\x1b[1;2H\x1b[2X\x1b[2K")
        self.assertEqual(s.lines()[0], "")
        s = scr("abcdef\x1b[1;2H\x1b[2X")
        self.assertEqual(s.lines()[0], "a  def")
        s.feed("\x1b[2P")
        self.assertEqual(s.lines()[0], "adef")
        s.feed("\x1b[3@")
        self.assertEqual(s.lines()[0], "a   def")
        s = scr("a\r\nb\r\nc\x1b[2;1H\x1b[L")
        self.assertEqual(s.lines()[:4], ["a", "", "b", "c"])
        s.feed("\x1b[2M")
        self.assertEqual(s.lines()[:3], ["a", "c", ""])
        s = scr("abc\x1b[2J\x1b[3J")
        self.assertEqual(s.lines(), [""] * 5)


class TestScrolling(unittest.TestCase):
    def test_lf_scrolls_full_screen(self):
        s = scr("1\r\n2\r\n3\r\n4\r\n5\r\n6", rows=5)
        self.assertEqual(s.lines(), ["2", "3", "4", "5", "6"])

    def test_scroll_region_lf_and_ri(self):
        s = scr("top\r\na\r\nb\r\nc\r\nfoot", rows=5)
        s.feed("\x1b[2;4r")
        self.assertEqual(s.cursor, (0, 0))
        s.feed("\x1b[4;1H\nd")  # LF at bottom margin scrolls only the region
        self.assertEqual(s.lines(), ["top", "b", "c", "d", "foot"])
        s.feed("\x1b[2;1H\x1bMz")  # RI at top margin scrolls the region down
        self.assertEqual(s.lines(), ["top", "z", "b", "c", "foot"])
        s.feed("\x1b[S")
        self.assertEqual(s.lines(), ["top", "b", "c", "", "foot"])
        s.feed("\x1b[2T")
        self.assertEqual(s.lines(), ["top", "", "", "b", "foot"])
        s.feed("\x1b[r\x1b[5;1H\x1bD")  # full-screen region again; ESC D at bottom scrolls
        self.assertEqual(s.lines(), ["", "", "b", "foot", ""])
        s.feed("\x1b[1;1H\x1bM")
        self.assertEqual(s.lines(), ["", "", "", "b", "foot"])


class TestModes(unittest.TestCase):
    def test_alt_screen_restores_main(self):
        s = scr("main1\r\nmain2", rows=5)
        s.feed("\x1b[?1049h")
        self.assertTrue(s.alt)
        self.assertEqual(s.lines(), [""] * 5)
        s.feed("\x1b[3;3Halt")
        self.assertEqual(s.lines()[2], "  alt")
        s.feed("\x1b[?1049l")
        self.assertFalse(s.alt)
        self.assertEqual(s.lines()[:2], ["main1", "main2"])
        self.assertEqual(s.cursor, (1, 5))
        s.feed("\x1b[?1049h")
        self.assertEqual(s.lines(), [""] * 5)  # 1049 clears alt on entry
        s.feed("\x1b[?1049l\x1b[?47hX\x1b[?47l")
        self.assertEqual(s.lines()[1], "main2")

    def test_reset(self):
        s = scr("\x1b[2mhello\x1b[?1049h\x1bc")
        self.assertFalse(s.alt)
        self.assertEqual(s.lines(), [""] * 5)
        self.assertFalse(s.dim)

    def test_ignored_sequences(self):
        junk = ("\x1b[?25l\x1b[?2004h\x1b[?1h\x1b=\x1b>\x1b(B\x1b)0\x1b[?2026h\x1b[>4;1m\x1b[?u\x1b[<u"
                "\x1b[>1u\x1b[=1;1u\x1b[6n\x1b[c\x1b[>c\x1b[0 q\x1b[!p\x1b[22;0;0t\x1b#8\x1b[?2026l"
                "\x1bP+q544e\x1b\\\x1b_Gf=24;AAAA\x1b\\\x1b^pm\x1b\\\x1bXsos\x1b\\\x1b]0;title\x07")
        s = scr(junk + "ok")
        self.assertEqual(s.lines()[0], "ok")
        self.assertEqual(s.cursor, (0, 2))

    def test_osc8_hyperlink_swallowed(self):
        s = scr("\x1b]8;;https://example.com/a?b=1\x1b\\link\x1b]8;;\x1b\\ \x1b]2;t\x07x", cols=30)
        self.assertEqual(s.lines()[0], "link x")

    def test_dec_line_drawing(self):
        s = scr("\x1b(0lqk\x1b(B lqk")
        self.assertEqual(s.lines()[0], "┌─┐ lqk")


class TestDim(unittest.TestCase):
    def test_dim_tracking(self):
        s = scr("a\x1b[2mb\x1b[22mc\x1b[2md\x1b[0me\x1b[2mf\x1b[mg\x1b[1;2mh\x1b[;mi")
        self.assertEqual(s.lines()[0], "abcdefghi")
        self.assertEqual(s.lines(dim=False)[0], "a c e g i")

    def test_extended_colour_not_misread(self):
        # 38;2;2;2;2 must not set dim; 48;5;2 must not set dim; 38;2;22;0;0 must not clear dim
        s = scr("\x1b[38;2;2;2;2mA\x1b[48;5;2mB\x1b[2;38;2;22;0;0mC\x1b[38;5;0mD\x1b[0m"
                "\x1b[38:2::2:2:2mE\x1b[2;38:5:22mF\x1b[4:3mG\x1b[0m")
        self.assertEqual(s.lines()[0], "ABCDEFG")
        self.assertEqual(s.lines(dim=False)[0], "AB  E")

    def test_composer_placeholder(self):
        s = scr("\x1b[1;32m❯\x1b[0m \x1b[2mTry \"fix\"\x1b[22m", cols=40)
        self.assertEqual(s.lines()[0], "❯ Try \"fix\"")
        self.assertEqual(s.lines(dim=False)[0], "❯")
        s.feed("\r\x1b[2K\x1b[1;32m❯\x1b[0m hello")
        self.assertEqual(s.lines(dim=False)[0], "❯ hello")

    def test_dim_wide_keeps_alignment(self):
        s = scr("\x1b[2m中\x1b[0mx", cols=10)
        self.assertEqual(s.lines(dim=False)[0], "  x")


class TestFeedSplits(unittest.TestCase):
    def test_split_escape_sequences(self):
        data = ("\x1b[2;3Hab\x1b[38;2;10;20;30mc\x1b]8;;http://x\x1b\\d\x1b]8;;\x07\x1b[2me\x1b[0m"
                "\x1bP1$r0m\x1b\\f\x1b(0q\x1b(Bg").encode()
        whole = Screen(20, 5)
        whole.feed(data)
        for step in (1, 2, 3, 5):
            s = Screen(20, 5)
            for k in range(0, len(data), step):
                s.feed(data[k:k + step])
            self.assertEqual(s.lines(), whole.lines(), step)
            self.assertEqual(s.lines(dim=False), whole.lines(dim=False), step)
            self.assertEqual(s.cursor, whole.cursor)
        self.assertEqual(whole.lines()[1], "  abcdef─g")
        self.assertEqual(whole.lines(dim=False)[1], "  abcd f─g")

    def test_split_utf8(self):
        data = "❯ 中文 é ⏺".encode()
        s = Screen(20, 3)
        for b in data:
            s.feed(bytes([b]))
        self.assertEqual(s.lines()[0], "❯ 中文 é ⏺")
        s.feed(b"\xff!")
        self.assertEqual(s.lines()[0], "❯ 中文 é ⏺�!")


class TestResize(unittest.TestCase):
    def test_resize_smaller_and_larger(self):
        s = scr("abcdefghij\r\n12345中\r\nthird", cols=10, rows=4)
        s.resize(6, 4)
        self.assertEqual(s.lines(), ["abcdef", "12345", "third", ""])
        s.resize(12, 6)
        self.assertEqual(s.lines(), ["abcdef", "12345", "third", "", "", ""])
        self.assertEqual(s.cursor, (2, 5))
        s.feed("\x1b[6;12HZ")
        self.assertEqual(s.lines()[5], " " * 11 + "Z")
        s.resize(5, 2)  # cursor row kept visible: lines dropped from the top
        self.assertEqual(len(s.lines()), 2)
        self.assertEqual(s.cursor[0], 1)
        s.resize(0, 0)
        self.assertEqual(len(s.lines()), 1)
        s.feed("x\x1b[5;5H中\x1b[?1049hq\x1b[?1049l")
        s.resize(3, 3)
        self.assertEqual(len(s.lines()), 3)

    def test_resize_in_alt_screen(self):
        s = scr("main\x1b[?1049h\x1b[5;1Halt", cols=10, rows=5)
        s.resize(8, 3)
        self.assertEqual(s.lines()[2], "alt")
        s.feed("\x1b[?1049l")
        self.assertEqual(s.lines()[0], "main")


class TestPerformance(unittest.TestCase):
    def test_two_megabyte_replay(self):
        frame = ("\x1b[?2026h\x1b[H\x1b[2K\x1b[38;2;215;119;87m╭" + "─" * 60 + "╮\x1b[0m\r\n"
                 "\x1b[2K│ \x1b[1;32m❯\x1b[0m \x1b[2mTry \"fix the tests\"\x1b[22m" + " " * 30 + "│\r\n"
                 "\x1b[2K  some ordinary output text that is mostly ascii, 中文 ⏺ done\r\n"
                 "\x1b[2K\x1b[38;5;246m  esc to interrupt\x1b[39m\x1b]0;title\x07\x1b[?2026l")
        data = (frame * (2_000_000 // len(frame.encode()) + 1)).encode()
        s = Screen(120, 40)
        t = time.time()
        for k in range(0, len(data), 4096):
            s.feed(data[k:k + 4096])
        self.assertLess(time.time() - t, 3.0)
        self.assertIn("esc to interrupt", s.lines()[3])


class TestFakeCliPty(unittest.TestCase):
    def test_fake_cli_in_pty(self):
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", FAKE_BUSY_S="0.3")
        for k in ("FAKE_PROJECTS", "FAKE_CLI_LOG"):
            env.pop(k, None)
        proc = subprocess.Popen([sys.executable, FAKE_CLI], stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True, env=env, close_fds=True)
        os.close(slave)
        s = Screen(80, 24)

        def pump(until, cond):
            while time.time() < until:
                r, _, _ = select.select([master], [], [], 0.05)
                if r:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        return
                    if not chunk:
                        return
                    s.feed(chunk)
                if cond():
                    return

        try:
            start = time.time()
            pump(start + 5, lambda: any(l.startswith("❯") for l in s.lines()))
            os.write(master, b"hi")
            time.sleep(0.1)
            os.write(master, b"\r")
            pump(start + 7, lambda: any("ok: hi" in l for l in s.lines()))
            pump(time.time() + 0.3, lambda: False)  # let the composer redraw settle
        finally:
            proc.kill()
            proc.wait(5)
            os.close(master)
        lines = s.lines()
        self.assertTrue(any(l.startswith("FAKE CLI") for l in lines), lines)
        self.assertIn("> hi", lines)
        self.assertTrue(any(l.endswith("ok: hi") for l in lines), lines)
        comp = [l for l in s.lines(dim=False) if l.startswith("❯")]
        self.assertEqual(comp, ["❯"], lines)
        self.assertTrue(any(l.startswith("❯ Try") for l in lines), lines)
        self.assertFalse(s.alt)


if __name__ == "__main__":
    unittest.main()
