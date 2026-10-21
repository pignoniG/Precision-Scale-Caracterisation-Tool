#!/usr/bin/env python3
"""
Plot a balance log: weight over time, with the temperature count on a second axis.

Used from the GUI (Plot button, live) and on its own to look at a saved log:

    python3 scale_plot.py sessions/20261017_214545/raw.csv
    python3 scale_plot.py warmup.csv --counts-per-c 2000

It reads the CSV files written by this tool (raw.csv) and by scale_logger.py: any file with a 'time'
column (ISO time or seconds) and 'status' / 'value' columns.  Weight lines are the ones with status
S or SD; temperature lines (status T) carry the balance's raw temperature count.

The weight can be shown as the change from the first reading in view (mg), which is the usual way to
look at drift.  Temperature is shown as °C (counts / counts-per-°C, as the service manual does)
relative to the first count in view, or as the raw count.
"""
import argparse, csv, datetime, math, sys, time

COUNTS_PER_C = 2000


def load_csv(path):
    """-> (weight_t, weight_g, temp_t, temp_count, raw_t, raw_count) with times as epoch seconds.  Raw counts are the R
    lines of the service cassette (status R)."""
    wt, wv, tt, tc, rt, rc = [], [], [], [], [], []
    t0 = None
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            try:
                st, v = (r.get('status') or '').strip(), float(r['value'])
            except (KeyError, ValueError, TypeError):
                continue
            tm = r.get('time', '')
            try:
                t = datetime.datetime.fromisoformat(tm).timestamp()
            except ValueError:
                try:
                    t = float(tm)
                except ValueError:
                    continue
            if st == 'T':
                tt.append(t); tc.append(v)
            elif st == 'R':
                rt.append(t); rc.append(v)
            elif st in ('S', 'SD', 'SI', 'SIR', ''):
                wt.append(t); wv.append(v)
    return wt, wv, tt, tc, rt, rc


def nice_ticks(lo, hi, n=6):
    if hi - lo < 1e-12:
        lo, hi = lo - 0.5, hi + 0.5
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t = math.ceil(lo / step) * step
    out = []
    while t <= hi + step * 1e-9:
        out.append(round(t, 10)); t += step
    return out


def fmt_num(v, step):
    d = max(0, -int(math.floor(math.log10(step))) if step > 0 else 0)
    return f'{v:.{d}f}'


def run_plot(parent, data_fn, title='Balance plot', counts_per_c=COUNTS_PER_C, own_mainloop=False, cpg_fn=None):
    """Open a plot window. data_fn() -> (wt, wv, tt, tc[, rt, rc]), called every second (live) or once (file);
    rt, rc are raw A/D counts, cpg_fn() the counts per gram estimated by the last evaluated test."""
    import tkinter as tk
    from tkinter import ttk, filedialog

    win = tk.Toplevel(parent) if parent else tk.Tk()
    win.title(title); win.geometry('1000x560')
    state = dict(fn=data_fn, view=None)

    bar = ttk.Frame(win, padding=4); bar.pack(fill='x')
    span = tk.StringVar(value='all')
    ttk.Label(bar, text='show').pack(side='left')
    spans = {'last 5 min': 300, 'last 30 min': 1800, 'last 2 h': 7200, 'last 6 h': 21600, 'all': None}
    cb = ttk.Combobox(bar, textvariable=span, values=list(spans), width=11, state='readonly'); cb.pack(side='left', padx=4)
    rel = tk.BooleanVar(value=True); ttk.Checkbutton(bar, text='weight as change [mg]', variable=rel).pack(side='left', padx=8)
    use_raw = tk.BooleanVar(value=False)
    ttk.Checkbutton(bar, text='from raw A/D', variable=use_raw).pack(side='left')
    ttk.Label(bar, text='counts/g').pack(side='left', padx=(4, 0))
    cpg = tk.StringVar(value=''); ttk.Entry(bar, textvariable=cpg, width=7).pack(side='left')
    temp_mode = tk.StringVar(value='°C change')
    ttk.Label(bar, text='temperature').pack(side='left')
    ttk.Combobox(bar, textvariable=temp_mode, values=['°C change', 'count', 'off'], width=9, state='readonly').pack(side='left', padx=4)
    ttk.Label(bar, text='counts/°C').pack(side='left', padx=(8, 0))
    cpc = tk.StringVar(value=str(counts_per_c)); ttk.Entry(bar, textvariable=cpc, width=6).pack(side='left')
    def open_file():
        p = filedialog.askopenfilename(parent=win, title='Open a log', filetypes=[('CSV', '*.csv'), ('All', '*')])
        if p:
            data = load_csv(p); state['fn'] = lambda: data; win.title(f'Balance plot - {p.split("/")[-1]}'); draw()
    ttk.Button(bar, text='Open log...', command=open_file).pack(side='left', padx=8)
    readout = ttk.Label(bar, text='', font=('Menlo', 11)); readout.pack(side='right')

    cv = tk.Canvas(win, bg='white', highlightthickness=0); cv.pack(fill='both', expand=True)
    ML, MR, MT, MB = 78, 78, 16, 40

    def draw(_=None):
        cv.delete('all')
        data = state['fn']()
        wt, wv, tt, tc = data[:4]
        rt, rc = data[4:6] if len(data) >= 6 else ([], [])
        if cpg_fn and not cpg.get().strip() and cpg_fn():
            cpg.set('%.1f' % cpg_fn())                      # counts per gram found by the last test
        try: kg = float(cpg.get().replace(',', '.')) or None
        except ValueError: kg = None
        raw_on = use_raw.get() and len(rt) >= 2
        if raw_on: wt, wv = rt, (rc if not kg else [c / kg for c in rc])         # grams when counts/g is known, else counts
        W, H = max(cv.winfo_width(), 300), max(cv.winfo_height(), 200)
        pw, ph = W - ML - MR, H - MT - MB
        if len(wt) < 2 and len(tt) < 2:
            cv.create_text(W / 2, H / 2, text='waiting for data...', fill='#888'); state['view'] = None; return
        t_end = max(([wt[-1]] if wt else []) + ([tt[-1]] if tt else []))
        t_start = min(([wt[0]] if wt else []) + ([tt[0]] if tt else []))
        if spans[span.get()]: t_start = max(t_start, t_end - spans[span.get()])
        if t_end - t_start < 1: t_end = t_start + 1
        sel = lambda ts, vs: [(t, v) for t, v in zip(ts, vs) if t >= t_start]
        wpts, tpts = sel(wt, wv), sel(tt, tc)
        try: k = float(cpc.get().replace(',', '.')) or COUNTS_PER_C
        except ValueError: k = COUNTS_PER_C
        tx = lambda t: ML + (t - t_start) / (t_end - t_start) * pw
        # --- weight series (left axis)
        def bin_series(pts, n):                         # one mean per pixel column so long logs stay fast
            if len(pts) <= n: return pts
            out, size = [], len(pts) / n
            for i in range(n):
                seg = pts[int(i * size):int((i + 1) * size)]
                if seg: out.append((sum(p[0] for p in seg) / len(seg), sum(p[1] for p in seg) / len(seg)))
            return out
        wpts = bin_series(wpts, pw)
        if wpts:
            base = sum(v for _, v in wpts[:5]) / len(wpts[:5]) if (rel.get() or (raw_on and not kg)) else 0.0
            sc = (1000.0 if kg else 1.0) if raw_on else (1000.0 if rel.get() else 1.0)
            wdraw = [(t, (v - base) * sc) for t, v in wpts]
            lo, hi = min(v for _, v in wdraw), max(v for _, v in wdraw)
            pad = (hi - lo) * 0.06 or (1 if rel.get() else 0.001); lo -= pad; hi += pad
            yw = lambda v: MT + (hi - v) / (hi - lo) * ph
            ticks = nice_ticks(lo, hi)
            step = ticks[1] - ticks[0] if len(ticks) > 1 else 1
            for v in ticks:
                cv.create_line(ML, yw(v), ML + pw, yw(v), fill='#e4e4e4')
                cv.create_text(ML - 6, yw(v), text=fmt_num(v, step), anchor='e', fill='#1f5fbf', font=('Helvetica', 10))
            cv.create_text(14, MT + ph / 2, text=('raw change [counts]' if not kg else 'raw A/D change [mg]') if raw_on and (rel.get() or not kg) else
                           ('weight change [mg]' if rel.get() else 'weight [g]'),
                           angle=90, fill='#1f5fbf', font=('Helvetica', 11))
            cv.create_line(*[c for t, v in wdraw for c in (tx(t), yw(v))], fill='#1f5fbf', width=1.5) if len(wdraw) > 1 else None
        # --- temperature series (right axis)
        tpts = bin_series(tpts, pw)
        if tpts and temp_mode.get() != 'off':
            c0 = tpts[0][1]
            conv = (lambda c: (c - c0) / k) if temp_mode.get() == '°C change' else (lambda c: c)
            tdraw = [(t, conv(c)) for t, c in tpts]
            lo2, hi2 = min(v for _, v in tdraw), max(v for _, v in tdraw)
            pad = (hi2 - lo2) * 0.06 or (0.02 if temp_mode.get() != 'count' else 2); lo2 -= pad; hi2 += pad
            yt = lambda v: MT + (hi2 - v) / (hi2 - lo2) * ph
            ticks = nice_ticks(lo2, hi2)
            step = ticks[1] - ticks[0] if len(ticks) > 1 else 1
            for v in ticks:
                cv.create_text(ML + pw + 6, yt(v), text=fmt_num(v, step), anchor='w', fill='#c0392b', font=('Helvetica', 10))
            cv.create_text(W - 12, MT + ph / 2, text='temperature change [°C]' if temp_mode.get() == '°C change' else 'temperature count',
                           angle=270, fill='#c0392b', font=('Helvetica', 11))
            if len(tdraw) > 1:
                cv.create_line(*[c for t, v in tdraw for c in (tx(t), yt(v))], fill='#c0392b', width=1.5)
            else:
                cv.create_oval(tx(tdraw[0][0]) - 3, yt(tdraw[0][1]) - 3, tx(tdraw[0][0]) + 3, yt(tdraw[0][1]) + 3, fill='#c0392b')
        # --- time axis
        long_run = (t_end - t_start) > 2 * 3600
        for v in nice_ticks(0, t_end - t_start, 8):
            x = tx(t_start + v)
            cv.create_line(x, MT, x, MT + ph, fill='#f0f0f0'); cv.create_line(x, MT + ph, x, MT + ph + 4, fill='#333')
            cv.create_text(x, MT + ph + 16, text=time.strftime('%H:%M' if long_run else '%H:%M:%S', time.localtime(t_start + v)),
                           font=('Helvetica', 10), fill='#333')
        cv.create_text(ML + pw / 2, H - 8, text='time (local)', fill='#333', font=('Helvetica', 10))
        cv.create_rectangle(ML, MT, ML + pw, MT + ph, outline='#888')
        state['view'] = (t_start, t_end, ML, pw, wt, wv, tt, tc)

    def hover(ev):
        v = state['view']
        if not v: return
        t_start, t_end, ml, pw, wt, wv, tt, tc = v
        t = t_start + (ev.x - ml) / pw * (t_end - t_start)
        if not (t_start <= t <= t_end): readout.config(text=''); return
        txt = time.strftime('%H:%M:%S', time.localtime(t))
        def near(ts, vs):
            if not ts: return None
            i = min(range(len(ts)), key=lambda j: abs(ts[j] - t)); return vs[i]
        w = near(wt, wv); c = near(tt, tc)
        if w is not None: txt += f'   {w:.3f} g'
        if c is not None: txt += f'   T {c:.0f}'
        readout.config(text=txt)
    cv.bind('<Motion>', hover); cv.bind('<Configure>', draw)
    for var in (span, rel, temp_mode, cpc, use_raw, cpg): var.trace_add('write', lambda *a: draw())

    def tick():
        try:
            if not win.winfo_exists(): return
        except Exception: return
        draw(); win.after(1000, tick)
    tick()
    if own_mainloop: win.mainloop()
    return win


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('log', nargs='?', help='CSV log (raw.csv from a session, or a scale_logger.py file)')
    ap.add_argument('--counts-per-c', type=float, default=COUNTS_PER_C)
    ap.add_argument('--png', help='write the plot to this PostScript file and exit (for scripts)')
    a = ap.parse_args()
    data = load_csv(a.log) if a.log else ([], [], [], [], [], [])
    w = run_plot(None, lambda: data, 'Balance plot' + (f' - {a.log}' if a.log else ''), a.counts_per_c)
    if a.png:
        w.update(); w.after(300, lambda: (w.children[list(w.children)[-1]].postscript(file=a.png, colormode='color'), w.destroy()))
    w.mainloop()


if __name__ == '__main__':
    main()
