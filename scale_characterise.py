#!/usr/bin/env python3
"""
Precision scale characterisation over the serial port.

Works with any balance that streams lines like "S    294.378 g" (stable) / "SD   294.379 g" (moving);
written for and tested with a Mettler AM/PM balance in continuous output mode.

Tests, following EURAMET cg-18 and the Sartorius balance-test-report white papers:
  - Repeatability       same load n times (5/6/10), zero read before each placement -> std dev s
  - Eccentricity        centre, front-left, rear-left, rear-right, front-right (cg-18 option 3)
  - Error of indication increasing loads with unloading in between -> E = I - m_conventional
  - Linearity (tare-load / "unknown weights") same test load on tares of ~0, 1/4, 1/2, 3/4 Max
  - ABBA comparison     reference A against test B, drift cancels in each A-B-B-A cycle
  - Free log            just record the stream (drift, creep, warm-up)

The cable is receive-only (balance pin 12 -> RxD, pin 13 -> GND), so the program cannot zero the
balance. Instead it reads the empty pan before every load and uses the difference ("net"), which
also cancels slow drift between steps. Every line from the balance goes to raw.csv; each recorded
step to steps.csv; results to report.txt and session.json, all in one session folder.

Mettler AM/PM: configuration file I-FACE: S = cONT, b = 9600, P = -e-, Pause = 0, AU = on.

Run:  python3 scale_characterise.py         (--simulate for a demo without a balance)
"""
import argparse, csv, datetime, json, math, os, queue, random, re, statistics, sys, threading, time

LINE = re.compile(r'^\s*(S|SD|SI|SIR)?\s*([-+]?\d+(?:\.\d+)?)\s*(\S+)?\s*$')
# optional temperature line from a patched Mettler ROM: 'T 297904' = the balance's raw temperature count
TLINE = re.compile(r'^\s*T\s+(\d+)\s*$')
# lines of the service cassette (patched ROM): R = raw A/D value (short: window sum + raw; the long form, about once a second,
# adds the unfiltered and filtered temperature counts and the window length), XV = its answer to the identify command
RLINE = re.compile(r'^R ([0-9A-F]{8}) ([0-9A-F]{6})(?: ([0-9A-F]{6}) ([0-9A-F]{6}) ([0-9A-F]{2}))?(?: G.*)?$')
XVLINE = re.compile(r'^XV SVC (\S+) ID ([0-9A-F]+)$')
STALE_S = 3.0                # seconds without any line before the stream counts as lost
COUNTS_PER_C = 2000          # Mettler service manual 4.2.7: count difference / 2000 = temperature difference in C
ECC_POSITIONS = ['1 centre', '2 front left', '3 rear left', '4 rear right', '5 front right']


# ============================================================== data sources
class SerialSource(threading.Thread):
    def __init__(self, port, baud=9600, parity='E', out=None):
        super().__init__(daemon=True)
        import serial
        self.params = (port, baud, parity)
        self.ser = serial.Serial(port, baud, bytesize=7 if parity != 'N' else 8,
                                 parity=parity, stopbits=1, timeout=0.5)
        self.ser.reset_input_buffer(); self.ser.readline()
        self.out = out or queue.Queue(); self.running = True

    def run(self):
        while self.running:
            try:
                raw = self.ser.readline()
            except Exception as e:
                if self.running:                  # a port closed on purpose (Reconnect, Disconnect) is not an error
                    self.out.put(('error', str(e), self))
                break
            if raw:
                self.out.put(('line', time.time(), raw.decode('ascii', 'replace').strip()))

    def write(self, data):
        """send bytes to the balance (needs the balance's receive wire); False if the port is gone"""
        try:
            self.ser.write(data); return True
        except Exception:
            return False

    def stop(self):
        self.running = False
        try: self.ser.close()
        except Exception: pass


class SimSource(threading.Thread):
    """Fake balance: the GUI sets .load_g; output settles like the comparator ROM with noise.  It also sends R lines like
    the service cassette and answers newline / XV / T (service=False: a receive-only cable, no answers)."""
    def __init__(self, out=None, d=0.001, service=True, tlines=True):
        super().__init__(daemon=True)
        self.out = out or queue.Queue(); self.running = True; self.d = d
        self._load = 0.0; self.shown = 0.0; self.drift = 0.0; self.t_change = 0
        self.temp = 297904; self.t_temp = 0; self.service = service; self.tlines = tlines
        self.tare = 0.0; self.target = 0.0; self.n = 0

    @property
    def load_g(self): return self._load

    @load_g.setter
    def load_g(self, g):                       # any handling counts as a disturbance, even an equal mass
        self._load = g; self.t_change = time.time(); self.shown -= 0.05

    def write(self, data):
        if not self.service:
            return True
        now = time.time()
        if data.strip() == b'':
            self.out.put(('line', now, 'ES'))
        elif data.strip() == b'XV':
            self.out.put(('line', now, 'XV SVC 10.50 ID 123456'))
        elif data.strip() == b'T':
            self.tare = self.target                # tare once stable: the reading goes to zero
        return True

    def run(self):
        while self.running:
            time.sleep(0.05)                       # 20 lines/s so demos run quickly
            self.n += 1
            self.drift += random.gauss(0, 0.00003)
            self.target = self.load_g * (1 + 2e-6) + 0.000004 * self.load_g ** 2 / 4000 + self.drift
            self.shown += (self.target - self.tare - self.shown) * 0.35
            v = round(self.shown + random.gauss(0, 0.0004), 3)
            st = 'S' if time.time() - self.t_change > 0.8 else 'SD'
            now = time.time()
            self.out.put(('line', now, f'{st:4s}{v:10.3f} g'))
            raw = 1500000 + round((self.shown + self.tare) * 1548 + random.gauss(0, 0.5))
            if self.n % 8 == 0:
                self.out.put(('line', now, f'R 00A17DC0 {raw & 0xFFFFFF:06X} {self.temp:06X} {self.temp:06X} 14'))
            else:
                self.out.put(('line', now, f'R 00A17DC0 {raw & 0xFFFFFF:06X}'))
            if now - self.t_temp > 2:
                self.t_temp = now; self.temp += random.randint(-3, 6)
                if self.tlines: self.out.put(('line', now, f'T {self.temp}'))

    def stop(self): self.running = False


# ============================================================== stability / recording
class Live:
    """Latest readings and a stability test: only 'S' lines for settle_s seconds, spread <= tol, no gap in the stream
    (a pause in the lines breaks the stable stretch)."""
    GAP = 1.5

    def __init__(self):
        self.hist = []            # (t, status, value)
        self.rhist = []           # (t, raw A/D count) from the service cassette's R lines
        self.last = None
        self._tlo = None          # start of the stretch the last value() averaged over

    def feed(self, t, txt):
        m = LINE.match(txt)
        if not m:
            return None
        st, v = m.group(1) or '', float(m.group(2))
        self.last = (t, st, v, m.group(3) or '')
        self.hist.append((t, st, v)); cut = t - 120
        while self.hist and self.hist[0][0] < cut: self.hist.pop(0)
        return self.last

    def feed_raw(self, t, raw):
        self.rhist.append((t, raw)); cut = t - 120
        while self.rhist and self.rhist[0][0] < cut: self.rhist.pop(0)

    def _stretch(self, tol, avg_s=None):
        """samples of the current stable stretch, newest first: S readings within +-tol of the latest, without a gap"""
        t_end, _, v = self.hist[-1]; out, prev = [], t_end
        for t, st, x in reversed(self.hist):
            if st != 'S' or abs(x - v) > tol or prev - t > self.GAP: break
            if avg_s is not None and t < t_end - avg_s: break
            out.append((t, x)); prev = t
        return out

    def stable_for(self, tol, now=None):
        """seconds of uninterrupted S readings within +-tol of the latest one (0 when the stream has stopped)"""
        if not self.hist or self.hist[-1][1] != 'S':
            return 0.0
        if now is not None and now - self.hist[-1][0] > self.GAP:
            return 0.0
        xs = self._stretch(tol)
        return xs[0][0] - xs[-1][0] if xs else 0.0

    def value(self, avg_s, tol):
        """mean of the current stable stretch (S readings within +-tol of the latest), at most the
        last avg_s seconds of it; the latest value if the balance is not stable"""
        if not self.hist: return None
        t_end, st_end, v = self.hist[-1]
        if st_end != 'S':
            self._tlo = None; return v
        xs = self._stretch(tol, avg_s)
        self._tlo = xs[-1][0]
        return statistics.mean(x for _, x in xs)

    def raw_stats(self, t_lo, t_hi=None):
        """(mean, std, n) of the raw counts since t_lo (the window value() averaged), or None without R lines"""
        if t_lo is None: return None
        xs = [r for t, r in self.rhist if t >= t_lo and (t_hi is None or t <= t_hi)]
        if len(xs) < 2: return None
        return statistics.mean(xs), statistics.stdev(xs), len(xs)


# ============================================================== weights
def parse_load(expr, weights):
    """'2kgA + 2kgB' -> (conventional g, U g, nominal g). Unknown names: a number in grams."""
    total = unc = nom = 0.0
    for part in [p.strip() for p in expr.split('+') if p.strip()]:
        if part in weights:
            w = weights[part]; total += w['conv']; nom += w['nominal']; unc += w['U_mg'] / 1000
        else:
            try:
                g = float(part); total += g; nom += g
            except ValueError:
                raise ValueError(f'unknown weight "{part}"')
    return total, unc, nom          # U of a combination: linear sum (weights of one set are correlated)


# ============================================================== test plans
def plan_repeatability(load, n):
    steps = []
    for i in range(n):
        steps += [dict(kind='zero', label=f'#{i+1} empty pan'),
                  dict(kind='load', label=f'#{i+1} place {load} centred', load=load)]
    return steps

def plan_eccentricity(load, recentre=True):
    """cg-18 order; with recentre the load goes back to the centre after every corner, so each corner
    can be compared with the centre readings just before and after it (cancels drift)"""
    order = [ECC_POSITIONS[0]]
    for p in ECC_POSITIONS[1:]:
        order += [p] + ([ECC_POSITIONS[0]] if recentre else [])
    steps = []
    for p in order:
        steps += [dict(kind='zero', label=f'{p}: empty pan'),
                  dict(kind='load', label=f'{p}: place {load} at {p.split(" ",1)[1]}', load=load, pos=p)]
    return steps

def plan_indication(loads):
    steps = []
    for L in loads:
        steps += [dict(kind='zero', label=f'empty pan before {L}'),
                  dict(kind='load', label=f'place {L}', load=L)]
    return steps

def plan_tare_linearity(test_load, tares):
    steps = []
    for k, T in enumerate(tares):
        steps += [dict(kind='tare', label=f'step {k+1}: place tare {T or "(none)"} only', tare=T),
                  dict(kind='load', label=f'step {k+1}: add {test_load} on top of the tare', load=test_load, tare=T)]
    return steps

def plan_abba(a, b, cycles):
    steps = []
    for c in range(cycles):
        for slot in ('A', 'B', 'B', 'A'):
            steps.append(dict(kind='zero', label=f'cycle {c+1}: empty pan before {slot}'))
            steps.append(dict(kind='load', label=f'cycle {c+1}: place {slot} = {a if slot == "A" else b}',
                              load=a if slot == 'A' else b, slot=slot, cycle=c))
    return steps


# ============================================================== evaluation
def fmt(g, d):
    nd = max(0, -int(math.floor(math.log10(d)))) if d else 4
    return f'{g:+.{nd + 1}f} g'

def nets(steps):
    """pair each load reading with the zero just before it -> list of (step, net)"""
    out, zero = [], None
    for s in steps:
        if 'value' not in s: continue
        if s['kind'] in ('zero', 'tare'):
            zero = s['value']
        elif s['kind'] == 'load':
            out.append((s, s['value'] - (zero if zero is not None else 0.0)))
    return out

def evaluate(test, steps, weights, d):
    r = {'test': test, 'lines': []}; L = r['lines']
    pairs = nets(steps)
    if not pairs:
        L.append('no readings'); return r
    if test == 'repeatability':
        xs = [n for _, n in pairs]; conv, U, nom = parse_load(pairs[0][0]['load'], weights)
        s = statistics.stdev(xs) if len(xs) > 1 else float('nan')
        dev = max(xs, key=lambda x: abs(x - conv)) - conv
        r.update(s=s, n=len(xs), mean=statistics.mean(xs), adjustment=dev, conv=conv, U=U)
        L.append(f'load {pairs[0][0]["load"]}  (conventional {conv:.6f} g, U {U*1000:.3f} mg)')
        L.append('net readings: ' + ', '.join(f'{x:.4f}' for x in xs))
        L.append(f'repeatability s = {s*1000:.3f} mg  ({s/d:.2f} d), n = {len(xs)}, mean {statistics.mean(xs):.5f} g')
        L.append(f'adjustment accuracy (largest deviation from conventional mass) = {dev*1000:+.3f} mg'
                 f'  (weight U {U*1000:.3f} mg)')
    elif test == 'eccentricity':
        centre = ECC_POSITIONS[0]
        cidx = [k for k, (s, n) in enumerate(pairs) if s['pos'] == centre]
        recentred = len(cidx) > 1
        devs = []
        for k, (s, n) in enumerate(pairs):
            if s['pos'] == centre: continue
            before = [pairs[j][1] for j in cidx if j < k][-1:]
            after = [pairs[j][1] for j in cidx if j > k][:1]
            refs = before + after if recentred else [pairs[cidx[0]][1]] if cidx else []
            if not refs: continue
            ref = statistics.mean(refs)
            devs.append((s['pos'], n, ref, n - ref))
        if not devs:
            L.append('no corner readings'); return r
        worst = max(devs, key=lambda t: abs(t[3]))
        cn = [pairs[j][1] for j in cidx]
        r.update(max_dev=worst[3], devs=[(p, dv) for p, _, _, dv in devs], centre=cn, recentred=recentred)
        for p, n, ref, dv in devs:
            L.append(f'{p:16s} net {n:.4f} g   centre {"(mean of before/after) " if recentred else ""}{ref:.4f} g'
                     f'   deviation {dv*1000:+.3f} mg')
        if recentred:
            L.append('centre readings: ' + ', '.join(f'{x:.4f}' for x in cn) +
                     f' g  (drift over the test {(cn[-1] - cn[0])*1000:+.3f} mg, spread {(max(cn) - min(cn))*1000:.3f} mg)')
        L.append(f'eccentricity: max deviation from centre = {worst[3]*1000:+.3f} mg at {worst[0]}'
                 + ('  (drift-corrected)' if recentred else ''))
    elif test == 'indication':
        rows = []
        for s, n in pairs:
            conv, U, nom = parse_load(s['load'], weights)
            rows.append((s['load'], conv, n, n - conv, U))
        rows.sort(key=lambda t: t[1])
        # linearity: residual from the straight line through zero error at zero and the error at the top load
        top = rows[-1]; slope = top[3] / top[1] if top[1] else 0.0
        r.update(rows=[dict(load=a, conv=b, net=c, error=e, U=u) for a, b, c, e, u in rows])
        L.append(f'{"load":18s} {"conventional g":>14s} {"indication g":>13s} {"error mg":>9s} {"lin. mg":>8s} {"U mg":>6s}')
        worst_l = 0.0
        for a, b, c, e, u in rows:
            lin = e - slope * b
            worst_l = max(worst_l, abs(lin))
            L.append(f'{a:18s} {b:14.6f} {c:13.4f} {e*1000:+9.3f} {lin*1000:+8.3f} {u*1000:6.3f}')
        r.update(max_error=max((x[3] for x in rows), key=abs), max_lin=worst_l)
        L.append(f'max |error of indication| = {abs(r["max_error"])*1000:.3f} mg; '
                 f'max deviation from straight line through 0 and top load = {worst_l*1000:.3f} mg')
    elif test == 'tare_linearity':
        deltas = [n for _, n in pairs]; mean = statistics.mean(deltas)
        cum = 0.0; points = []
        for k, dl in enumerate(deltas):
            cum += dl - mean; points.append(cum)
        r.update(deltas=deltas, deviations=points, max_lin=max(abs(p) for p in points))
        for k, ((s, dl), p) in enumerate(zip(pairs, points)):
            L.append(f'step {k+1}: tare {s.get("tare") or "none":14s} test load reads {dl:.4f} g'
                     f' ({(dl-mean)*1000:+.3f} mg vs mean)   curve P{k+1}: {p*1000:+.3f} mg')
        L.append(f'linearity (tare-load method) = {r["max_lin"]*1000:.3f} mg  '
                 f'(deviation of the cumulative curve from the line through first and last point)')
    elif test == 'abba':
        cyc = {}
        for s, n in pairs: cyc.setdefault(s['cycle'], []).append((s['slot'], n))
        diffs = []
        for c, items in sorted(cyc.items()):
            if [x[0] for x in items] != ['A', 'B', 'B', 'A']: continue
            a1, b1, b2, a2 = [x[1] for x in items]
            diffs.append(((b1 + b2) - (a1 + a2)) / 2)
        if not diffs:
            L.append('no complete A-B-B-A cycle'); return r
        mean = statistics.mean(diffs); s = statistics.stdev(diffs) if len(diffs) > 1 else float('nan')
        r.update(diffs=diffs, mean=mean, s=s, u_mean=s / math.sqrt(len(diffs)) if len(diffs) > 1 else float('nan'))
        a_expr = pairs[0][0]['load'] if pairs[0][0]['slot'] == 'A' else ''
        L.append('cycle differences B - A: ' + ', '.join(f'{x*1000:+.3f}' for x in diffs) + ' mg')
        L.append(f'mean B - A = {mean*1000:+.3f} mg, s = {s*1000:.3f} mg, u(mean) = {r["u_mean"]*1000:.3f} mg, n = {len(diffs)}')
        try:
            conv, U, _ = parse_load(a_expr, weights)
            L.append(f'=> m(B) = {conv + mean:.6f} g  (A conventional {conv:.6f} g, U(A) {U*1000:.3f} mg)')
        except ValueError:
            pass
    return r


def raw_view(test, steps, weights, d):
    """The same evaluation on the raw A/D counts of the service cassette.  The counts are converted to grams with the
    counts per gram of this run (regression of the displayed nets on the raw nets, through zero), so the display's
    rounding to d does not enter and differences show at the microgram level; the gain error is removed by that fit.
    -> (lines, counts per gram) or None when a recorded step has no raw value."""
    done = [s for s in steps if 'value' in s]
    if not done or any(s.get('raw') is None for s in done):
        return None
    zr = zg = None; pairs = []
    for s in done:
        if s['kind'] in ('zero', 'tare'): zr, zg = s['raw'], s['value']
        elif s['kind'] == 'load': pairs.append((s['raw'] - (zr if zr is not None else 0.0), s['value'] - (zg if zg is not None else 0.0)))
    num, den = sum(a * b for a, b in pairs), sum(a * a for a, b in pairs)
    if den <= 0 or num <= 0:
        return None
    gpc = num / den
    r2 = evaluate(test, [dict(s, value=s['raw'] * gpc) for s in done], weights, d)
    head = f'A/D counts view: {1 / gpc:.1f} counts per g fitted on this run, 1 count = {gpc * 1e6:.2f} ug; gain error removed by the fit'
    return [head] + r2['lines'], 1 / gpc


# ============================================================== GUI
def gui(args):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    root = tk.Tk(); root.title('Balance characterisation')
    q = queue.Queue(); live = Live()
    st = dict(source=None, raw=None, rawf=None, plan=None, idx=0, test=None, steps=[], results=[],
              folder=None, weights={}, recorded_t=0, temp=None, temp0=None, ref=None, wt=[], wv=[], tt=[], tc=[],
              rt=[], rc=[], last_t=None, svc=None, twoway=False, lost=False, probe_t=0, tline_t=0, cpg=None)

    # ---------------- session folder and files
    def ensure_session():
        if st['folder']: return
        base = args.dir or os.path.join(os.getcwd(), 'sessions')
        st['folder'] = os.path.join(base, datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
        os.makedirs(st['folder'], exist_ok=True)
        st['rawf'] = open(os.path.join(st['folder'], 'raw.csv'), 'w', newline='')
        st['raw'] = csv.writer(st['rawf']); st['raw'].writerow(['time', 'status', 'value', 'unit', 'line'])
        stepf = open(os.path.join(st['folder'], 'steps.csv'), 'w', newline='')
        st['stepf'] = stepf; st['stepw'] = csv.writer(stepf)
        st['stepw'].writerow(['time', 'test', 'step', 'kind', 'label', 'load', 'value_g', 'stable_s', 'temp_count', 'temp', 'raw_mean', 'raw_std', 'raw_n'])
        folder_lbl.config(text=st['folder'])

    # ---------------- top: connection and live reading
    top = ttk.Frame(root, padding=6); top.pack(fill='x')
    ttk.Label(top, text='Port').pack(side='left')
    port = ttk.Combobox(top, width=24); port.pack(side='left', padx=4)
    try:
        import serial.tools.list_ports as lp
        port['values'] = [p.device for p in lp.comports()]
        usb = [p for p in port['values'] if 'usb' in p.lower()]
        if usb: port.set(usb[0])
    except Exception:
        pass
    baud = ttk.Combobox(top, width=6, values=['9600', '4800', '2400', '1200']); baud.set('9600'); baud.pack(side='left')
    par = ttk.Combobox(top, width=3, values=['E', 'O', 'N']); par.set('E'); par.pack(side='left', padx=4)
    sim = tk.BooleanVar(value=args.simulate)
    ttk.Checkbutton(top, text='simulate', variable=sim).pack(side='left')
    def connect():
        disconnect(); st['temp'] = st['temp0'] = None; show_temp(False)
        for k_ in ('wt', 'wv', 'tt', 'tc', 'rt', 'rc'): st[k_].clear()
        try:
            src = (SimSource(q, service=not args.sim_plain, tlines=not args.sim_no_t) if sim.get()
                   else SerialSource(port.get(), int(baud.get()), par.get(), q))
        except Exception as e:
            messagebox.showerror('Serial port', str(e)); return
        ensure_session(); st['source'] = src; src.start(); conn_btn.config(text='Reconnect')
        st.update(last_t=time.time(), svc=None, twoway=False, lost=False, tline_t=0)
        link_lbl.config(text='')
        if not args.no_probe: root.after(1200, lambda: probe(src))

    def probe(src):
        """ask the balance who it is: a newline (the receiver drops the first byte), then the service cassette's XV.
        A service cassette answers 'XV SVC ...', a standard cassette 'ES', a receive-only cable nothing."""
        if st['source'] is not src: return
        link_lbl.config(text='asking the balance...'); st['probe_t'] = time.time()
        src.write(b'\n')
        root.after(600, lambda: src.write(b'XV\r\n'))
        root.after(3000, lambda: probe_done(src))

    def probe_done(src):
        if st['source'] is not src: return
        st['probe_t'] = 0
        if st['svc']:
            link_lbl.config(text=f'service cassette {st["svc"][0]}, ID {st["svc"][1]}: commands available')
        elif st['twoway']:
            link_lbl.config(text='balance answers commands (standard cassette): tare available')
        else:
            link_lbl.config(text='receive-only cable: the balance cannot be tared from here')
        update_tare()

    def update_tare():
        tare_btn.state(['!disabled' if (st['twoway'] and st['source'] and not st['lost']) else 'disabled'])

    def tare_now():
        src = st['source']
        if src and st['twoway'] and src.write(b'T\r\n'):
            say('  tare sent (the balance tares when the reading is stable)')

    def start_reconnect():
        src = st['source']
        if not isinstance(src, SerialSource) or st.get('reconnecting'): return
        st['reconnecting'] = True
        def attempt():
            if st['source'] is not src or not st['lost']:
                st['reconnecting'] = False; return
            try:
                new = SerialSource(*src.params, q)
            except Exception:
                root.after(2000, attempt); return
            src.stop(); st['source'] = new; new.start()
            st.update(lost=False, reconnecting=False, last_t=time.time())
            say('  serial port back'); update_tare()
            if not args.no_probe: root.after(1200, lambda: probe(new))
        root.after(2000, attempt)
    def disconnect():
        if st['source']: st['source'].stop(); st['source'] = None
    conn_btn = ttk.Button(top, text='Connect', command=connect); conn_btn.pack(side='left', padx=6)
    def open_plot():
        import scale_plot
        try: k = float(cpc.get().replace(',', '.'))
        except (NameError, ValueError): k = COUNTS_PER_C
        scale_plot.run_plot(root, lambda: (st['wt'], st['wv'], st['tt'], st['tc'], st['rt'], st['rc']), 'Weight and temperature', k,
                            cpg_fn=lambda: st['cpg'])
    ttk.Button(top, text='Plot', command=open_plot).pack(side='left')
    link_lbl = ttk.Label(root, text='', padding=(8, 0)); link_lbl.pack(anchor='w', after=top)     # what the balance answered
    reading = ttk.Label(top, text='---', font=('Menlo', 26)); reading.pack(side='right')
    stab_lbl = ttk.Label(top, text='', width=22); stab_lbl.pack(side='right', padx=8)
    temp_lbl = ttk.Label(top, text='', width=26, font=('Menlo', 11))      # shown once a T line arrives
    banner = tk.Label(root, text='', fg='white', bg='#b03a2e', font=('Helvetica', 14, 'bold'), pady=4)   # no data from the balance

    body = ttk.Frame(root, padding=6); body.pack(fill='both', expand=True)
    left = ttk.Frame(body); left.pack(side='left', fill='y')
    right = ttk.Frame(body); right.pack(side='left', fill='both', expand=True, padx=(8, 0))

    # ---------------- balance and session info
    info = ttk.LabelFrame(left, text='Balance and conditions', padding=6); info.pack(fill='x')
    fields = {}
    for i, (k, v) in enumerate([('model', 'PM4600'), ('serial / ID', ''), ('Max [g]', '4100'),
                                ('d [g]', '0.001'), ('temperature [°C]', ''), ('site class A/B/C', 'B'),
                                ('operator', ''), ('notes', '')]):
        ttk.Label(info, text=k).grid(row=i, column=0, sticky='w')
        e = ttk.Entry(info, width=22); e.insert(0, v); e.grid(row=i, column=1, pady=1); fields[k] = e

    # ---------------- temperature (needs the patched ROM that sends 'T nnnnnn' lines)
    tb = ttk.LabelFrame(left, text='Temperature (balance count)', padding=6)   # shown once a T line arrives
    cpc = tk.StringVar(value=str(COUNTS_PER_C)); tnow = tk.StringVar()
    ttk.Label(tb, text='counts per °C').grid(row=0, column=0, sticky='w')
    ttk.Entry(tb, textvariable=cpc, width=8).grid(row=0, column=1, sticky='w')
    ttk.Label(tb, text='thermometer [°C]').grid(row=1, column=0, sticky='w')
    ttk.Entry(tb, textvariable=tnow, width=8).grid(row=1, column=1, sticky='w')
    def set_ref():
        try: st['ref'] = (st['temp'][1], float(tnow.get().replace(',', '.')))
        except (TypeError, ValueError): messagebox.showinfo('Temperature', 'Wait for a T line and enter the thermometer reading first.')
    ttk.Button(tb, text='Set reference', command=set_ref).grid(row=1, column=2, padx=4)
    ttk.Label(tb, text='without a reference only changes are shown (Δ °C since the first T line)',
              foreground='gray').grid(row=2, column=0, columnspan=3, sticky='w')
    def temp_c(count):
        """(text, number) for a count: absolute °C once a reference is set, else the change since the first count."""
        try: k = float(cpc.get().replace(',', '.')) or COUNTS_PER_C
        except ValueError: k = COUNTS_PER_C
        if st['ref']: return f'{st["ref"][1] + (count - st["ref"][0]) / k:.2f} °C'
        if st['temp0'] is not None: return f'Δ {(count - st["temp0"]) / k:+.2f} °C'
        return ''

    def show_temp(on):
        if on:
            temp_lbl.pack(side='right', padx=8); tb.pack(fill='x', pady=(6, 0), after=info)
        else:
            temp_lbl.pack_forget(); tb.pack_forget()

    # ---------------- weights
    wf = ttk.LabelFrame(left, text='Test weights (conventional values)', padding=6); wf.pack(fill='x', pady=6)
    tree = ttk.Treeview(wf, columns=('nominal', 'conv', 'U', 'cls'), height=6)
    for c, w, t in (('#0', 70, 'name'), ('nominal', 60, 'nominal g'), ('conv', 95, 'conv. g'), ('U', 50, 'U mg'), ('cls', 35, 'class')):
        tree.heading(c, text=t); tree.column(c, width=w)
    tree.pack()
    cfg_dir = args.config_dir or os.path.join(os.path.expanduser('~'), '.scale_characterise')
    weights_file = os.path.join(cfg_dir, 'weights.json')
    DEFAULT_WEIGHTS = {n: dict(nominal=nom, conv=conv, U_mg=U, cls='E2')
                       for n, nom, conv, U in (('1kg', 1000, 1000, 1.6), ('2kgA', 2000, 2000, 3.0), ('2kgB', 2000, 2000, 3.0))}
    def save_weights():
        try:
            os.makedirs(cfg_dir, exist_ok=True)
            with open(weights_file, 'w') as f_: json.dump(st['weights'], f_, indent=1)
        except OSError:
            pass
    def refresh_weights(save=True):
        tree.delete(*tree.get_children())
        for n, w in st['weights'].items():
            tree.insert('', 'end', text=n, values=(w['nominal'], f'{w["conv"]:.6f}', w['U_mg'], w['cls']))
        if save: save_weights()
    def edit_weight(name=None):
        w = st['weights'].get(name, dict(nominal=0, conv=0, U_mg=0, cls='E2'))
        dlg = tk.Toplevel(root); dlg.title('Weight'); ents = {}
        for i, (k, v) in enumerate([('name', name or ''), ('nominal g', w['nominal']), ('conventional g', w['conv']),
                                    ('U mg (k=2)', w['U_mg']), ('class', w['cls'])]):
            ttk.Label(dlg, text=k).grid(row=i, column=0, sticky='w', padx=4)
            e = ttk.Entry(dlg); e.insert(0, str(v)); e.grid(row=i, column=1, padx=4, pady=2); ents[k] = e
        def ok():
            try:
                n = ents['name'].get().strip().replace('+', ' ')
                nom = float(ents['nominal g'].get()); conv = float(ents['conventional g'].get() or nom)
                st['weights'].pop(name, None)
                st['weights'][n] = dict(nominal=nom, conv=conv or nom, U_mg=float(ents['U mg (k=2)'].get() or 0),
                                        cls=ents['class'].get())
            except ValueError as e:
                messagebox.showerror('Weight', str(e)); return
            refresh_weights(); dlg.destroy()
        ttk.Button(dlg, text='OK', command=ok).grid(row=9, column=0, columnspan=2, pady=4)
    wb = ttk.Frame(wf); wb.pack(fill='x')
    ttk.Button(wb, text='Add', command=edit_weight).pack(side='left')
    ttk.Button(wb, text='Edit', command=lambda: tree.selection() and edit_weight(tree.item(tree.selection()[0])['text'])).pack(side='left')
    ttk.Button(wb, text='Remove', command=lambda: [st['weights'].pop(tree.item(i)['text'], None) for i in tree.selection()] and refresh_weights()).pack(side='left')
    ttk.Label(wf, text='loads are written as names joined by +,\ne.g. "2kgA + 2kgB", or a number in g',
              foreground='gray').pack(anchor='w')
    try:
        with open(weights_file) as f_: st['weights'] = json.load(f_)          # the set used last time
    except (OSError, ValueError):
        st['weights'] = {k: dict(v) for k, v in DEFAULT_WEIGHTS.items()}
    def reset_weights():
        if messagebox.askyesno('Test weights', 'Replace the list with the built-in example weights?'):
            st['weights'] = {k: dict(v) for k, v in DEFAULT_WEIGHTS.items()}; refresh_weights()
    ttk.Button(wb, text='Defaults', command=reset_weights).pack(side='left')
    refresh_weights(save=False)

    # ---------------- recording settings
    rs = ttk.LabelFrame(left, text='Recording', padding=6); rs.pack(fill='x')
    settle = tk.DoubleVar(value=12.0); tol = tk.DoubleVar(value=2.0); avg = tk.DoubleVar(value=3.0)
    auto = tk.BooleanVar(value=True); plaus = tk.DoubleVar(value=1.0)
    for i, (t, v) in enumerate((('stable for [s]', settle), ('within ± [d]', tol), ('average last [s]', avg),
                                ('expected within ± [% of load]', plaus))):
        ttk.Label(rs, text=t).grid(row=i, column=0, sticky='w'); ttk.Entry(rs, textvariable=v, width=8).grid(row=i, column=1)
    ttk.Checkbutton(rs, text='record automatically when stable\nand close to the expected value', variable=auto).grid(row=4, column=0, columnspan=2, sticky='w')
    autotare = tk.BooleanVar(value=False)
    ttk.Checkbutton(rs, text='tare at empty-pan steps (balance must\nanswer commands; hides slow zero drift)', variable=autotare).grid(row=5, column=0, columnspan=2, sticky='w')

    # ---------------- tests
    nb = ttk.Notebook(right); nb.pack(fill='x')
    tests = {}
    def tab(name, title):
        f = ttk.Frame(nb, padding=6); nb.add(f, text=title); tests[name] = f; return f
    def entry(f, row, label, default, width=28):
        ttk.Label(f, text=label).grid(row=row, column=0, sticky='w')
        e = ttk.Entry(f, width=width); e.insert(0, default); e.grid(row=row, column=1, sticky='w', pady=1); return e
    f = tab('repeatability', 'Repeatability')
    rep_load = entry(f, 0, 'load (≥ 0.4 Max)', '2kgA'); rep_n = ttk.Combobox(f, values=['5', '6', '10'], width=4)
    rep_n.set('10'); ttk.Label(f, text='repetitions').grid(row=1, column=0, sticky='w'); rep_n.grid(row=1, column=1, sticky='w')
    f = tab('eccentricity', 'Eccentricity')
    ecc_load = entry(f, 0, 'load (≥ Max/3)', '2kgA')
    ecc_recentre = tk.BooleanVar(value=True)
    ttk.Checkbutton(f, text='back to the centre after every corner (cancels drift)', variable=ecc_recentre).grid(row=1, column=0, columnspan=2, sticky='w')
    ttk.Label(f, text='positions: centre, front left, rear left, rear right, front right', foreground='gray').grid(row=2, column=0, columnspan=2, sticky='w')
    f = tab('indication', 'Error of indication')
    ind_loads = entry(f, 0, 'loads, separated by ;', '1kg; 2kgA; 1kg + 2kgA; 2kgA + 2kgB', 48)
    ttk.Label(f, text='cg-18: 5 points (0, ¼, ½, ¾, Max) or 11; zero is read before each load', foreground='gray').grid(row=1, column=0, columnspan=2, sticky='w')
    f = tab('tare_linearity', 'Linearity (tare loads)')
    tl_load = entry(f, 0, 'test load (~¼ Max)', '1kg')
    tl_tares = entry(f, 1, 'tares, separated by ;', '0; ~1 kg object; 2kgA; 2kgB + ~1 kg object', 48)
    ttk.Label(f, text='tares ≈ 0, 1, 2, 3 x the test load, so the segments join up; any objects will do,\n'
                      'their mass need not be known (keep tare + test load under Max)', foreground='gray').grid(row=2, column=0, columnspan=2, sticky='w')
    f = tab('abba', 'ABBA comparison')
    ab_a = entry(f, 0, 'A (reference)', '2kgA'); ab_b = entry(f, 1, 'B (test)', '2kgB')
    ab_n = entry(f, 2, 'cycles', '5', 6)
    f = tab('free', 'Free log')
    ttk.Label(f, text='Records nothing step by step: the raw stream is always saved to raw.csv.\n'
                      'Use for warm-up, drift and creep runs; write remarks below.', foreground='gray').grid(row=0, column=0, sticky='w')
    free_note = entry(f, 1, 'remark (added to the report with a timestamp)', '', 48)

    # ---------------- run panel
    run = ttk.LabelFrame(right, text='Run', padding=6); run.pack(fill='x', pady=6)
    instr = ttk.Label(run, text='Connect, choose a test and press Start.', font=('Helvetica', 22, 'bold'), wraplength=760)
    instr.pack(anchor='w')
    prog = ttk.Label(run, text='', foreground='gray'); prog.pack(anchor='w')
    stab_row = ttk.Frame(run); stab_row.pack(fill='x', pady=(2, 0))
    stab_bar = ttk.Progressbar(stab_row, mode='determinate', maximum=12.0, length=260); stab_bar.pack(side='left')
    state_lbl = ttk.Label(stab_row, text='', font=('Helvetica', 13, 'bold')); state_lbl.pack(side='left', padx=10)
    btns = ttk.Frame(run); btns.pack(fill='x', pady=4)

    out = tk.Text(right, height=18, font=('Menlo', 11)); out.pack(fill='both', expand=True)
    def say(t):
        out.insert('end', t + '\n'); out.see('end')

    def start():
        if not st['source']:
            messagebox.showwarning('Not connected', 'Connect to the balance (or tick simulate) first.'); return
        if st['plan'] is not None and st['idx'] < len(st['plan']) and not messagebox.askyesno(
                'Test running', f'The {st["test"]} test is still running (step {st["idx"] + 1} of {len(st["plan"])}).\n'
                                'Starting a new test discards its unfinished steps. Continue?'):
            return
        name = list(tests)[nb.index(nb.select())]
        try:
            if name == 'repeatability':
                parse_load(rep_load.get(), st['weights']); plan = plan_repeatability(rep_load.get().strip(), int(rep_n.get()))
            elif name == 'eccentricity':
                parse_load(ecc_load.get(), st['weights']); plan = plan_eccentricity(ecc_load.get().strip(), ecc_recentre.get())
            elif name == 'indication':
                loads = [x.strip() for x in ind_loads.get().split(';') if x.strip()]
                for l in loads: parse_load(l, st['weights'])
                plan = plan_indication(loads)
            elif name == 'tare_linearity':
                tares = [x.strip() for x in tl_tares.get().split(';')]
                tares = ['' if t in ('0', '') else t for t in tares]
                plan = plan_tare_linearity(tl_load.get().strip(), tares)
            elif name == 'abba':
                parse_load(ab_a.get(), st['weights']); parse_load(ab_b.get(), st['weights'])
                plan = plan_abba(ab_a.get().strip(), ab_b.get().strip(), int(ab_n.get()))
            else:
                note = free_note.get().strip()
                say(f'[{datetime.datetime.now():%H:%M:%S}] remark: {note or "(mark)"}')
                st['results'].append(dict(test='remark', time=time.time(), text=note)); return
        except ValueError as e:
            messagebox.showerror('Test setup', str(e)); return
        st.update(plan=plan, idx=0, test=name, steps=plan, recorded_t=time.time())
        say(f'\n=== {name} started {datetime.datetime.now():%H:%M:%S}')
        show_step()

    def sim_mass(expr):                    # tares may be free text ("~1 kg object"): guess for the simulator
        if not expr: return 0.0
        tot = 0.0
        for part in expr.split('+'):
            part = part.strip()
            if part in st['weights']: tot += st['weights'][part]['conv']
            else:
                m = re.search(r'([\d.]+)\s*(kg|g)', part)
                if m: tot += float(m.group(1)) * (1000 if m.group(2) == 'kg' else 1)
        return tot

    def show_step():
        p, i = st['plan'], st['idx']
        if p is None:
            return
        if i >= len(p):
            finish(); return
        s = p[i]
        sim_load = 0.0
        if s['kind'] == 'zero':
            text = f'Empty the pan.  ({s["label"]})'
            if autotare.get() and st['twoway'] and st['source'] and live.last and abs(live.last[2]) > 0.5 and not st['lost']:
                tare_now()                       # a stale tare (reading far from zero on an empty pan)
        elif s['kind'] == 'tare':
            text = f'{s["label"]}'
            sim_load = sim_mass(s.get('tare'))
        else:
            text = f'{s["label"]}'
            sim_load = parse_load(s['load'], st['weights'])[0] + sim_mass(s.get('tare'))
        if isinstance(st['source'], SimSource):
            if args.demo_slow:                 # self-test: the operator changes the load 4 s late
                root.after(4000, lambda L=sim_load: setattr(st['source'], 'load_g', L))
            else:
                st['source'].load_g = sim_load
        instr.config(text=text); prog.config(text=f'step {i+1} of {len(p)}  -  {st["test"]}')
        st['recorded_t'] = time.time(); st['moved'] = False

    def expected():
        """(expected reading, window) for the current step, or None when it cannot be predicted (tares).
        Empty pan: the previous empty-pan reading (0 at first). Load: the empty-pan or tare reading just
        recorded plus the conventional mass of the named weights. Window: ±plaus % of the load, >= 0.5 g."""
        p, i = st['plan'], st['idx']
        if p is None or i >= len(p): return None
        s = p[i]
        if s['kind'] == 'tare': return None
        ref = None
        for k in range(i - 1, -1, -1):
            if p[k]['kind'] in ('zero', 'tare') and 'value' in p[k]:
                ref = p[k]['value']; break
            if s['kind'] == 'load' and p[k]['kind'] in ('zero', 'tare'):
                break                                   # its own zero/tare was skipped: no reference
        mass = None
        for k in list(range(i, len(p))) + list(range(i - 1, -1, -1)):   # this step's load, else the nearest one
            if p[k]['kind'] == 'load':
                mass = parse_load(p[k]['load'], st['weights'])[0]; break
        win = max(abs(mass or 0) * plaus.get() / 100, 0.5)
        if s['kind'] == 'zero':
            return (ref if ref is not None else 0.0), win
        if ref is None and any(q['kind'] == 'tare' for q in p):
            return None                                 # load on an unknown tare without its tare reading
        return (ref or 0.0) + parse_load(s['load'], st['weights'])[0], win

    def plausible(v):
        e = expected()
        if e is not None:
            return abs(v - e[0]) <= e[1]
        p, i = st['plan'], st['idx']
        if p is None or i >= len(p): return True
        s = p[i]
        if s['kind'] == 'tare':                         # unknown object: the pan must hold something new
            if not s.get('tare'):
                return abs(v) <= 0.5                    # "no tare" = empty pan
            return abs(v) > 0.5 and st.get('moved', False)   # something was moved since the prompt
        return True

    def fresh():
        return st['last_t'] is not None and time.time() - st['last_t'] <= STALE_S and not st['lost']

    def record(manual=False):
        p, i = st['plan'], st['idx']
        if p is None or i >= len(p) or live.last is None: return
        if not fresh():
            if manual: messagebox.showwarning('No data', 'The balance is not sending: nothing was recorded.')
            return
        v = live.value(avg.get(), tol.get() * float(fields['d [g]'].get() or 0.001)); s = p[i]
        rs = live.raw_stats(live._tlo)
        s['raw'], s['raw_std'], s['raw_n'] = rs if rs else (None, None, 0)
        s['value'] = v; s['time'] = time.time()
        s['stable_s'] = live.stable_for(tol.get() * float(fields['d [g]'].get() or 0.001), time.time())
        s['temp_count'] = st['temp'][1] if st['temp'] else None
        st['stepw'].writerow([datetime.datetime.now().isoformat(timespec='seconds'), st['test'], i + 1, s['kind'],
                              s['label'], s.get('load', s.get('tare', '')), f'{v:.5f}', f'{s["stable_s"]:.1f}',
                              st['temp'][1] if st['temp'] else '', temp_c(st['temp'][1]) if st['temp'] else '',
                              f'{rs[0]:.2f}' if rs else '', f'{rs[1]:.2f}' if rs else '', rs[2] if rs else ''])
        st['stepf'].flush()
        e = expected()
        flag = '' if plausible(v) else f'  (!) far from the expected {e[0]:.3f} g'
        say(f'  {s["label"]:45s} {v:12.4f} g' + ('  (manual)' if manual else '') + flag)
        root.bell(); st['idx'] += 1; show_step()

    def back():
        if st['plan'] and st['idx'] > 0:
            st['idx'] -= 1; st['plan'][st['idx']].pop('value', None); say('  (step undone)'); show_step()

    def skip():
        if st['plan'] and st['idx'] < len(st['plan']):
            say(f'  {st["plan"][st["idx"]]["label"]:45s} skipped'); st['idx'] += 1; show_step()

    def abort():
        if st['plan']:
            if not messagebox.askyesno('Finish test', 'Stop here and evaluate the steps recorded so far?'):
                return
            say('  aborted - evaluating what was recorded'); finish()

    def finish():
        d = float(fields['d [g]'].get() or 0.001)
        r = evaluate(st['test'], st['plan'], st['weights'], d)
        rv = raw_view(st['test'], st['plan'], st['weights'], d)
        if rv:
            r['lines'] += [''] + rv[0]; r['counts_per_g'] = rv[1]; st['cpg'] = rv[1]
        r['time'] = time.time(); r['steps'] = [{k: v for k, v in s.items()} for s in st['plan']]
        st['results'].append(r)
        say(f'--- result: {st["test"]}')
        for l in r['lines']: say('  ' + l)
        st['plan'] = None; instr.config(text='Done. Choose another test or save the report.'); prog.config(text='')
        if isinstance(st['source'], SimSource): st['source'].load_g = 0.0
        save(silent=True)

    def save(silent=False):
        ensure_session()
        meta = {k: e.get() for k, e in fields.items()}
        if st['svc']: meta['service cassette'] = f'firmware {st["svc"][0]}, EEPROM ID {st["svc"][1]}'
        if st['cpg']: meta['counts per g (raw A/D, last test)'] = f'{st["cpg"]:.1f}'
        if st['temp0'] is not None:
            meta['temperature count first / last'] = f'{st["temp0"]} / {st["temp"][1]}'
            meta['temperature change'] = temp_c(st['temp'][1]) if not st['ref'] else f'{temp_c(st["temp"][1])} (reference {st["ref"][1]:g} °C at count {st["ref"][0]})'
        with open(os.path.join(st['folder'], 'session.json'), 'w') as f:
            json.dump(dict(balance=meta, weights=st['weights'], results=st['results'],
                           settings=dict(settle_s=settle.get(), tol_d=tol.get(), avg_s=avg.get(),
                                         plausibility_pct=plaus.get())), f, indent=1, default=str)
        with open(os.path.join(st['folder'], 'report.txt'), 'w') as f:
            f.write(f'Balance characterisation - {datetime.datetime.now():%Y-%m-%d %H:%M}\n')
            for k, v in meta.items(): f.write(f'{k:18s}: {v}\n')
            f.write('\nTest weights:\n')
            for n, w in st['weights'].items():
                f.write(f'  {n:10s} nominal {w["nominal"]:g} g  conventional {w["conv"]:.6f} g  U {w["U_mg"]} mg  {w["cls"]}\n')
            for r in st['results']:
                f.write(f'\n[{datetime.datetime.fromtimestamp(r["time"]):%H:%M:%S}] {r["test"]}\n')
                for l in r.get('lines', [r.get('text', '')]): f.write('  ' + l + '\n')
        if not silent: messagebox.showinfo('Saved', f'Report saved in\n{st["folder"]}')

    for t, c in (('Start', start), ('Record now (space)', lambda: record(True)), ('Undo step (backspace)', back),
                 ('Skip', skip), ('Finish / abort', abort), ('Save report', save)):
        ttk.Button(btns, text=t, command=c).pack(side='left', padx=2)
    tare_btn = ttk.Button(btns, text='Tare balance', command=tare_now, state='disabled'); tare_btn.pack(side='left', padx=(12, 2))
    folder_lbl = ttk.Label(right, text='', foreground='gray'); folder_lbl.pack(anchor='w')

    # ---------------- event loop
    def iso(t): return datetime.datetime.fromtimestamp(t).isoformat(timespec='milliseconds')

    def take_temp(t, count, txt):
        """a temperature count, from a T line or from the long R line"""
        st['temp'] = (t, count); st['tt'].append(t); st['tc'].append(count)
        if st['temp0'] is None:
            st['temp0'] = count; show_temp(True)
        if st['raw']:
            st['raw'].writerow([iso(t), 'T', count, '', txt])

    def poll():
        try:
            while True:
                ev = q.get_nowait()
                if ev[0] == 'error':
                    if len(ev) > 2 and ev[2] is not st['source']:
                        continue                         # an old connection that was replaced meanwhile
                    st['lost'] = True; say('serial error: ' + ev[1] + ' - trying to reconnect'); update_tare(); start_reconnect(); continue
                _, t, txt = ev
                st['last_t'] = t
                tm = TLINE.match(txt)
                if tm:
                    st['tline_t'] = t; take_temp(t, int(tm.group(1)), txt)
                    continue
                rm = RLINE.match(txt)
                if rm:                                   # raw A/D value of the service cassette
                    raw = int(rm.group(2), 16); raw -= (1 << 24) if raw & 0x800000 else 0
                    live.feed_raw(t, raw)
                    if len(st['rt']) < 600000: st['rt'].append(t); st['rc'].append(raw)
                    if st['raw']: st['raw'].writerow([iso(t), 'R', raw, 'cnt', txt])
                    if rm.group(4) and t - st['tline_t'] > 15:       # no T lines: the filtered count of the long R line
                        take_temp(t, int(rm.group(4), 16), 'from R line')
                    continue
                xm = XVLINE.match(txt)
                if xm:
                    st['svc'] = (xm.group(1), xm.group(2)); st['twoway'] = True
                    if not fields['serial / ID'].get().strip():
                        fields['serial / ID'].insert(0, 'ID ' + xm.group(2))
                    if st['source'] and not st['probe_t']: probe_done(st['source'])
                    update_tare()
                    if st['raw']: st['raw'].writerow([iso(t), 'other', '', '', txt])
                    continue
                if txt == 'ES' and st['probe_t'] and t - st['probe_t'] < 6:
                    st['twoway'] = True; update_tare()
                res = live.feed(t, txt)
                if res and len(st['wt']) < 600000:
                    st['wt'].append(t); st['wv'].append(res[2])
                if res and res[1] != 'S' and t > st['recorded_t']:
                    st['moved'] = True
                if st['raw']:
                    st['raw'].writerow([iso(t), res[1] if res else 'other', res[2] if res else '', res[3] if res else '', txt])
        except queue.Empty:
            pass
        now = time.time()
        stale = bool(st['source']) and (st['lost'] or (st['last_t'] is not None and now - st['last_t'] > STALE_S))
        if stale:
            banner.config(text=('SERIAL PORT LOST - reconnecting...' if st['lost'] else
                                f'NO DATA FROM THE BALANCE for {now - st["last_t"]:.0f} s (asleep, cable, or the balance stopped sending)')
                               + '  -  nothing is recorded meanwhile')
            if not banner.winfo_ismapped(): banner.pack(fill='x', after=top)
        elif banner.winfo_ismapped():
            banner.pack_forget()
        if st['temp']:
            temp_lbl.config(text=f'T {st["temp"][1]}  {temp_c(st["temp"][1])}')
        if live.last:
            _, s, v, u = live.last
            reading.config(text=f'{s:2s} {v:10.3f} {u}', foreground='#999999' if stale else '')
            d = float(fields['d [g]'].get() or 0.001)
            sf = live.stable_for(tol.get() * d, now)
            stab_lbl.config(text=('no data' if stale else f'stable {sf:4.1f} s') if (stale or s == 'S') else 'moving')
            if st['plan'] is not None and st['idx'] < len(st['plan']):
                e = expected(); ok = plausible(v)
                base = f'step {st["idx"]+1} of {len(st["plan"])}  -  {st["test"]}'
                prog.config(text=base + ('' if e is None else f'   expected ≈ {e[0]:.3f} g'))
                stab_bar.config(maximum=max(settle.get(), 0.1), value=min(sf, settle.get()))
                if stale:
                    state_lbl.config(text='no data from the balance', foreground='#D64541')
                elif not ok:
                    state_lbl.config(text='waiting for the right load' + ('' if e is None else f' (reads {v:.3f}, expected {e[0]:.3f} g)'), foreground='#D64541')
                elif s != 'S':
                    state_lbl.config(text='moving: wait for the reading to settle', foreground='#D9822B')
                elif sf < settle.get():
                    state_lbl.config(text=f'stable {sf:.1f} of {settle.get():g} s', foreground='#D9822B')
                else:
                    state_lbl.config(text='stable: recording' if auto.get() else 'stable: press Record now', foreground='#2E9E4F')
                if auto.get() and ok and not stale and now - st['recorded_t'] > 2 and sf >= settle.get():
                    record()
            else:
                stab_bar.config(value=0); state_lbl.config(text='')
        if st['rawf']: st['rawf'].flush()
        root.after(100, poll)
    poll()
    def key_ok():
        w = root.focus_get()
        return not isinstance(w, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox, ttk.Button, ttk.Spinbox, tk.Spinbox))
    root.bind('<space>', lambda e: record(True) if key_ok() else None)
    root.bind('<BackSpace>', lambda e: back() if key_ok() else None)
    if args.demo:                          # self-test: simulated balance runs one test unattended
        nb.select(list(tests).index(args.demo))
        def demo_go():
            sim.set(True); settle.set(0.8); rep_n.set('5'); connect(); start()
        def demo_check():
            if st['plan'] is None and st['results']:
                save(silent=True); print('DEMO folder', st['folder']); root.destroy()
            else:
                root.after(500, demo_check)
        root.after(300, demo_go); root.after(2000, demo_check)
    if args.autoconnect:
        root.after(300, connect)
    if args.demo_stall:                    # self-test: the simulated balance stops sending in the middle of a test
        def stall_go():
            sim.set(True); settle.set(0.8); rep_n.set('10'); connect(); start()
        def stall():
            st['at_stall'] = st['idx']; st['source'].running = False
        def stall_check():
            print('STALL banner:', banner.winfo_ismapped(), '|', banner.cget('text')[:70])
            print('STALL steps recorded before/after:', st['at_stall'], st['idx'], '(must be equal)')
            print('STALL reading colour:', reading.cget('foreground'))
            root.destroy()
        root.after(300, stall_go); root.after(int(args.demo_stall * 1000), stall); root.after(int(args.demo_stall * 1000) + 6000, stall_check)
    if args.shot:                          # README screenshot: capture the window mid-run, then quit
        root.geometry('1480x930+0+30'); root.lift(); root.attributes('-topmost', True)
        def shot():
            import subprocess
            root.update_idletasks()
            x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
            subprocess.run(['screencapture', '-x', '-o', '-R', f'{x},{y - 28},{w},{h + 28}', args.shot])
            root.destroy()
        root.after(int(args.shot_after * 1000), shot)
    root.protocol('WM_DELETE_WINDOW', lambda: (save(silent=True) if st['folder'] else None, disconnect(), root.destroy()))
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--simulate', action='store_true', help='start with the simulated balance selected')
    ap.add_argument('--dir', help='folder for session sub-folders (default ./sessions)')
    ap.add_argument('--demo', choices=['repeatability', 'eccentricity', 'indication', 'tare_linearity', 'abba'],
                    help=argparse.SUPPRESS)
    ap.add_argument('--no-probe', action='store_true', help='do not send anything to the balance on connect (no identify query, no tare)')
    ap.add_argument('--config-dir', help='folder for the saved test weights (default ~/.scale_characterise)')
    ap.add_argument('--autoconnect', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--demo-stall', type=float, help=argparse.SUPPRESS)
    ap.add_argument('--sim-plain', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--sim-no-t', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--shot', help=argparse.SUPPRESS)
    ap.add_argument('--demo-slow', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--shot-after', type=float, default=10, help=argparse.SUPPRESS)
    gui(ap.parse_args())


if __name__ == '__main__':
    main()
