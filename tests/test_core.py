"""Tests of the parts of scale_characterise.py that work without a window or a balance."""
import os, random, statistics, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import scale_characterise as sc


class LiveTests(unittest.TestCase):
    def test_a_gap_in_the_stream_breaks_the_stable_stretch(self):
        live = sc.Live()
        for k in range(40): live.feed(100 + k * 0.1, 'S    1000.000 g')       # 4 s of stable readings
        self.assertAlmostEqual(live.stable_for(0.002, now=103.95), 3.9, delta=0.15)
        self.assertEqual(live.stable_for(0.002, now=110.0), 0.0)               # the stream stopped 6 s ago
        for k in range(10): live.feed(120 + k * 0.1, 'S    1000.000 g')       # resumes after a 16 s pause
        self.assertLess(live.stable_for(0.002, now=120.95), 1.0)               # the old stretch does not count

    def test_raw_samples_of_the_averaged_window(self):
        live = sc.Live()
        for k in range(50):
            live.feed(k * 0.1, 'S    1.000 g'); live.feed_raw(k * 0.1, 1000 + (k % 2))
        v = live.value(3.0, 0.002)
        n, std = live.raw_stats(live._tlo)[2], live.raw_stats(live._tlo)[1]
        self.assertAlmostEqual(v, 1.0)
        self.assertGreater(n, 25); self.assertLess(n, 35); self.assertLess(std, 1.0)


class LineTests(unittest.TestCase):
    def test_service_cassette_lines(self):
        self.assertFalse(sc.LINE.match('R 00A17DC0 0791E4'))                 # not a weight
        self.assertEqual(sc.RLINE.match('R 00A17DC0 0791E4').group(2), '0791E4')
        self.assertEqual(sc.RLINE.match('R 00A17F9F 0791DC 051BEB 051BE9 14').group(4), '051BE9')
        self.assertTrue(sc.RLINE.match('R 01458580 0CB736 051C98 051C98 18 G0280C400 450C55 1AD0'))   # debug build
        self.assertEqual(sc.XVLINE.match('XV SVC 10.50 ID 874543').groups(), ('10.50', '874543'))
        self.assertFalse(sc.RLINE.match('ES')); self.assertFalse(sc.XVLINE.match('XV'))


class FakePort:
    """stands in for serial.Serial: no data, and reading a closed port fails like the real one"""
    def __init__(self, *a, **k): self.closed = False
    def reset_input_buffer(self): pass
    def readline(self):
        if self.closed: raise OSError(9, 'Bad file descriptor')
        time.sleep(0.05); return b''
    def write(self, data): pass
    def close(self): self.closed = True


class SourceTests(unittest.TestCase):
    def source(self):
        import serial, queue
        self._orig = serial.Serial; serial.Serial = FakePort
        self.addCleanup(lambda: setattr(serial, 'Serial', self._orig))
        q = queue.Queue(); s = sc.SerialSource('fake', 9600, 'E', q); s.start(); time.sleep(0.2)
        return s, q

    def test_closing_the_port_on_purpose_is_not_an_error(self):
        s, q = self.source()
        s.stop(); time.sleep(0.3)                           # Reconnect / Disconnect stop the old source
        self.assertTrue(q.empty())

    def test_a_port_lost_under_a_running_source_is_reported(self):
        s, q = self.source()
        s.ser.close(); time.sleep(0.3)                      # the adapter went away
        ev = q.get_nowait()
        self.assertEqual(ev[0], 'error'); self.assertIs(ev[2], s)    # tagged with its source, so an old one can be ignored


class RawViewTests(unittest.TestCase):
    """the A/D counts resolve what the 1 mg display rounds away"""
    def steps(self, noise_mg=0.15, n=10, cpg=1548.0, seed=3):
        rnd = random.Random(seed); out = []
        for i in range(n):
            z = rnd.gauss(0, noise_mg / 1000); m = 2000 + rnd.gauss(0, noise_mg / 1000)
            for kind, g in (('zero', z), ('load', m + z)):
                out.append(dict(kind=kind, label=f'#{i+1}', load='2kgA', value=round(g, 3),        # the display rounds to 1 mg
                                raw=1500000 + g * cpg))
        return out

    def test_repeatability_from_counts_is_finer_than_the_display(self):
        steps = self.steps()
        view = sc.raw_view('repeatability', steps, {'2kgA': dict(nominal=2000, conv=2000.0, U_mg=3.0, cls='E2')}, 0.001)
        self.assertIsNotNone(view)
        lines, cpg = view
        self.assertAlmostEqual(cpg, 1548.0, delta=3.0)
        s_line = [l for l in lines if l.startswith('repeatability s')][0]
        s_raw_mg = float(s_line.split('=')[1].split('mg')[0])
        nets = [steps[i + 1]['value'] - steps[i]['value'] for i in range(0, len(steps), 2)]
        s_disp_mg = statistics.stdev(nets) * 1000
        self.assertGreater(s_raw_mg, 0.05); self.assertLess(s_raw_mg, 0.4)       # about the 0.2 mg of the noise
        self.assertNotEqual(round(s_raw_mg, 3), round(s_disp_mg, 3))

    def test_no_raw_no_view(self):
        steps = self.steps(); steps[3]['raw'] = None
        self.assertIsNone(sc.raw_view('repeatability', steps, {}, 0.001))


if __name__ == '__main__':
    unittest.main()
