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
ECC_POSITIONS = ['1 centre', '2 front left', '3 rear left', '4 rear right', '5 front right']


# ============================================================== data sources
class SerialSource(threading.Thread):
    def __init__(self, port, baud=9600, parity='E', out=None):
        super().__init__(daemon=True)
        import serial
        self.ser = serial.Serial(port, baud, bytesize=7 if parity != 'N' else 8,
                                 parity=parity, stopbits=1, timeout=0.5)
        self.ser.reset_input_buffer(); self.ser.readline()
        self.out = out or queue.Queue(); self.running = True

    def run(self):
        while self.running:
            try:
                raw = self.ser.readline()
            except Exception as e:
                self.out.put(('error', str(e))); break
            if raw:
                self.out.put(('line', time.time(), raw.decode('ascii', 'replace').strip()))

    def stop(self):
        self.running = False
        try: self.ser.close()
        except Exception: pass


class SimSource(threading.Thread):
    """Fake balance: the GUI sets .load_g; output settles like the comparator ROM with noise."""
    def __init__(self, out=None, d=0.001):
        super().__init__(daemon=True)
        self.out = out or queue.Queue(); self.running = True; self.d = d
        self._load = 0.0; self.shown = 0.0; self.drift = 0.0; self.t_change = 0

    @property
    def load_g(self): return self._load

    @load_g.setter
    def load_g(self, g):                       # any handling counts as a disturbance, even an equal mass
        self._load = g; self.t_change = time.time(); self.shown -= 0.05

    def run(self):
        while self.running:
            time.sleep(0.05)                       # 20 lines/s so demos run quickly
            self.drift += random.gauss(0, 0.00003)
            target = self.load_g * (1 + 2e-6) + 0.000004 * self.load_g ** 2 / 4000 + self.drift
            self.shown += (target - self.shown) * 0.35
            v = round(self.shown + random.gauss(0, 0.0004), 3)
            st = 'S' if time.time() - self.t_change > 0.8 else 'SD'
            self.out.put(('line', time.time(), f'{st:4s}{v:10.3f} g'))

    def stop(self): self.running = False


# ============================================================== stability / recording
class Live:
    """Latest readings and a stability test: only 'S' lines for settle_s seconds, spread <= tol."""
    def __init__(self):
        self.hist = []            # (t, status, value)
        self.last = None

    def feed(self, t, txt):
        m = LINE.match(txt)
        if not m:
            return None
        st, v = m.group(1) or '', float(m.group(2))
        self.last = (t, st, v, m.group(3) or '')
        self.hist.append((t, st, v)); cut = t - 120
        while self.hist and self.hist[0][0] < cut: self.hist.pop(0)
        return self.last

    def stable_for(self, tol):
        """seconds of uninterrupted S readings within +-tol of the latest one"""
        if not self.hist or self.hist[-1][1] != 'S':
            return 0.0
        t_end, _, v = self.hist[-1]; t0 = t_end
        for t, st, x in reversed(self.hist):
            if st != 'S' or abs(x - v) > tol: break
            t0 = t
        return t_end - t0

    def value(self, avg_s, tol):
        """mean of the current stable stretch (S readings within +-tol of the latest), at most the
        last avg_s seconds of it; the latest value if the balance is not stable"""
        if not self.hist: return None
        t_end, st_end, v = self.hist[-1]
        if st_end != 'S': return v
        xs = []
        for t, st, x in reversed(self.hist):
            if st != 'S' or abs(x - v) > tol or t < t_end - avg_s: break
            xs.append(x)
        return statistics.mean(xs)


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


# ============================================================== GUI
def gui(args):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    root = tk.Tk(); root.title('Balance characterisation')
    q = queue.Queue(); live = Live()
    st = dict(source=None, raw=None, rawf=None, plan=None, idx=0, test=None, steps=[], results=[],
              folder=None, weights={}, recorded_t=0)

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
        st['stepw'].writerow(['time', 'test', 'step', 'kind', 'label', 'load', 'value_g', 'stable_s'])
        folder_lbl.config(text=st['folder'])

    # ---------------- top: connection and live reading
    top = ttk.Frame(root, padding=6); top.pack(fill='x')
    ttk.Label(top, text='Port').pack(side='left')
    port = ttk.Combobox(top, width=30); port.pack(side='left', padx=4)
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
        disconnect()
        try:
            src = SimSource(q) if sim.get() else SerialSource(port.get(), int(baud.get()), par.get(), q)
        except Exception as e:
            messagebox.showerror('Serial port', str(e)); return
        ensure_session(); st['source'] = src; src.start(); conn_btn.config(text='Reconnect')
    def disconnect():
        if st['source']: st['source'].stop(); st['source'] = None
    conn_btn = ttk.Button(top, text='Connect', command=connect); conn_btn.pack(side='left', padx=6)
    reading = ttk.Label(top, text='---', font=('Menlo', 26)); reading.pack(side='right')
    stab_lbl = ttk.Label(top, text='', width=22); stab_lbl.pack(side='right', padx=8)

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

    # ---------------- weights
    wf = ttk.LabelFrame(left, text='Test weights (conventional values)', padding=6); wf.pack(fill='x', pady=6)
    tree = ttk.Treeview(wf, columns=('nominal', 'conv', 'U', 'cls'), height=6)
    for c, w, t in (('#0', 70, 'name'), ('nominal', 60, 'nominal g'), ('conv', 95, 'conv. g'), ('U', 50, 'U mg'), ('cls', 35, 'class')):
        tree.heading(c, text=t); tree.column(c, width=w)
    tree.pack()
    def refresh_weights():
        tree.delete(*tree.get_children())
        for n, w in st['weights'].items():
            tree.insert('', 'end', text=n, values=(w['nominal'], f'{w["conv"]:.6f}', w['U_mg'], w['cls']))
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
    for n, nom, conv, U in (('1kg', 1000, 1000, 1.6), ('2kgA', 2000, 2000, 3.0), ('2kgB', 2000, 2000, 3.0)):
        st['weights'][n] = dict(nominal=nom, conv=conv, U_mg=U, cls='E2')
    refresh_weights()

    # ---------------- recording settings
    rs = ttk.LabelFrame(left, text='Recording', padding=6); rs.pack(fill='x')
    settle = tk.DoubleVar(value=12.0); tol = tk.DoubleVar(value=2.0); avg = tk.DoubleVar(value=3.0)
    auto = tk.BooleanVar(value=True); plaus = tk.DoubleVar(value=1.0)
    for i, (t, v) in enumerate((('stable for [s]', settle), ('within [d]', tol), ('average last [s]', avg),
                                ('expected within ± [% of load]', plaus))):
        ttk.Label(rs, text=t).grid(row=i, column=0, sticky='w'); ttk.Entry(rs, textvariable=v, width=8).grid(row=i, column=1)
    ttk.Checkbutton(rs, text='record automatically when stable\nand close to the expected value', variable=auto).grid(row=4, column=0, columnspan=2, sticky='w')

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
    instr = ttk.Label(run, text='Connect, choose a test and press Start.', font=('Helvetica', 15), wraplength=620)
    instr.pack(anchor='w')
    prog = ttk.Label(run, text='', foreground='gray'); prog.pack(anchor='w')
    btns = ttk.Frame(run); btns.pack(fill='x', pady=4)

    out = tk.Text(right, height=18, font=('Menlo', 11)); out.pack(fill='both', expand=True)
    def say(t):
        out.insert('end', t + '\n'); out.see('end')

    def start():
        if not st['source']:
            messagebox.showwarning('Not connected', 'Connect to the balance (or tick simulate) first.'); return
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

    def record(manual=False):
        p, i = st['plan'], st['idx']
        if p is None or i >= len(p) or live.last is None: return
        v = live.value(avg.get(), tol.get() * float(fields['d [g]'].get() or 0.001)); s = p[i]
        s['value'] = v; s['time'] = time.time()
        s['stable_s'] = live.stable_for(tol.get() * float(fields['d [g]'].get() or 0.001))
        st['stepw'].writerow([datetime.datetime.now().isoformat(timespec='seconds'), st['test'], i + 1, s['kind'],
                              s['label'], s.get('load', s.get('tare', '')), f'{v:.5f}', f'{s["stable_s"]:.1f}'])
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
            say('  aborted - evaluating what was recorded'); finish()

    def finish():
        d = float(fields['d [g]'].get() or 0.001)
        r = evaluate(st['test'], st['plan'], st['weights'], d)
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

    for t, c in (('Start', start), ('Record now', lambda: record(True)), ('Undo step', back),
                 ('Skip', skip), ('Finish / abort', abort), ('Save report', save)):
        ttk.Button(btns, text=t, command=c).pack(side='left', padx=2)
    folder_lbl = ttk.Label(right, text='', foreground='gray'); folder_lbl.pack(anchor='w')

    # ---------------- event loop
    def poll():
        try:
            while True:
                ev = q.get_nowait()
                if ev[0] == 'error':
                    say('serial error: ' + ev[1]); continue
                _, t, txt = ev
                res = live.feed(t, txt)
                if res and res[1] != 'S' and t > st['recorded_t']:
                    st['moved'] = True
                if st['raw']:
                    st['raw'].writerow([datetime.datetime.fromtimestamp(t).isoformat(timespec='milliseconds'),
                                        res[1] if res else 'other', res[2] if res else '', res[3] if res else '', txt])
        except queue.Empty:
            pass
        if live.last:
            _, s, v, u = live.last
            reading.config(text=f'{s:2s} {v:10.3f} {u}')
            d = float(fields['d [g]'].get() or 0.001)
            sf = live.stable_for(tol.get() * d)
            stab_lbl.config(text=f'stable {sf:4.1f} s' if s == 'S' else 'moving')
            if st['plan'] is not None and st['idx'] < len(st['plan']):
                e = expected(); ok = plausible(v)
                base = f'step {st["idx"]+1} of {len(st["plan"])}  -  {st["test"]}'
                prog.config(text=base + ('' if e is None else
                            f'   expected ≈ {e[0]:.3f} g' + ('' if ok else '  - waiting for the right load')))
                if auto.get() and ok and time.time() - st['recorded_t'] > 2 and sf >= settle.get():
                    record()
        if st['rawf']: st['rawf'].flush()
        root.after(100, poll)
    poll()
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
    if args.shot:                          # README screenshot: capture the window mid-run, then quit
        root.geometry('1250x800+0+40')
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
    ap.add_argument('--shot', help=argparse.SUPPRESS)
    ap.add_argument('--demo-slow', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--shot-after', type=float, default=10, help=argparse.SUPPRESS)
    gui(ap.parse_args())


if __name__ == '__main__':
    main()
