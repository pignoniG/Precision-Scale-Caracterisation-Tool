# Precision Scale Characterisation Tool

A small Python tool for characterising a laboratory balance through its serial output: it walks you
through the tests used in balance calibrations and test reports, records every reading, and writes a
report. Written for a modified Mettler-Toledo PM4600, but it works with any balance that streams lines
like `S    294.378 g` (stable) and `SD   294.379 g` (moving).

![The characterisation GUI during an error-of-indication run](docs/screenshot.png)

The test procedures follow the EURAMET calibration guide cg-18 and the Sartorius white papers on balance
test reports, cg-18 calibration and handling test weights. This is a tool for understanding your own
balance, not an accredited calibration: it reports the measured quantities, not a full uncertainty budget.

## Tests

| Test | Procedure | Result |
|---|---|---|
| **Repeatability** | the same load placed 5, 6 or 10 times, pan read empty before each placement | standard deviation `s`; adjustment accuracy = largest deviation from the conventional mass |
| **Eccentricity** | one load at centre, front left, rear left, rear right, front right, unloading in between (cg-18 option 3); by default the load goes back to the centre after every corner | largest deviation from the centre; with the extra centre placements each corner is compared with the mean of the centre readings before and after it, which cancels drift |
| **Error of indication** | increasing loads with unloading in between (cg-18: 5 points 0, ¼, ½, ¾, Max, or 11) | error `E = indication - conventional mass` for each point, and the deviation from a straight line through zero and the top load |
| **Linearity, tare-load method** | the same test load placed on tares of about 0, 1, 2, 3 × the test load | linearity curve that does not depend on knowing the test weight's mass ("testing with unknown weights") |
| **ABBA comparison** | reference A and test mass B in A-B-B-A cycles | mean difference B - A with its standard deviation and the standard uncertainty of the mean; with a calibrated A, the mass of B |
| **Free log** | nothing step by step; for warm-up, drift and creep runs | the raw stream, plus time-stamped remarks |

**Zeroing.** The program reads the empty pan (or the tare) before every load and works with the difference.
That removes slow drift between steps, so you don't need to press tare between placements. With a
two-way cable (see the service cassette below) a **Tare balance** button and an optional automatic tare at
the empty-pan steps are available; the automatic tare is off by default because it hides slow zero drift,
and it is never used at the tare steps of the tare-load test.

**Stale data.** If the balance stops sending (asleep, cable pulled, adapter dropped), a red banner says so, the
reading goes grey and nothing is recorded until lines arrive again; a lost serial port is reopened by itself.
A pause in the stream also breaks the stable stretch, so a frozen value can never be recorded as stable.

**Recording.** A step is recorded when the balance has reported stable (`S`) readings, all within ± a few
display steps of each other, for a set time (default 12 s, suited to a balance with long averaging). The
recorded value is the mean of that stable stretch (at most the last 3 s). Recording can be automatic, so
your hands stay free for the weights, or manual with **Record now**. **Undo step** and **Skip** are there
for mistakes.

Automatic recording also waits for the *right* load: an empty-pan step must read near the previous empty
pan, a load step near that empty-pan reading plus the conventional mass of the named weights (window ±1 %
of the load, at least 0.5 g, adjustable). Tares have no known mass, so a tare step only needs something on
the pan and some movement since the prompt. The expected reading is shown while waiting; **Record now**
still records anything, but marks implausible values in the log.

**Weights.** Enter each weight with its nominal value, its conventional value from the calibration
certificate and its expanded uncertainty `U`. Loads are written as names joined by `+`
(`2kgA + 2kgB`) or as a number in grams. The uncertainties of combined weights are added linearly,
because weights of one set are usually calibrated together and so correlated. Tares in the tare-load test
can be any objects; their mass is never used.

**While a test runs.** The run panel shows the instruction in large type, the expected reading, a bar for the
time the reading has been stable and a coloured state ("moving", "stable 7.2 of 12 s", "waiting for the right
load"). Space records now, backspace undoes the last step. Start and Finish ask before they discard or end
a running test. The list of test weights is saved in `~/.scale_characterise/weights.json` and comes back at
the next start (**Defaults** restores the examples).

## Plot

The **Plot** button opens a live graph of the weight over time; when the balance sends temperature lines
(see below) the temperature is drawn on a second axis. Weight can be shown as the change from the first
reading in view, in mg, which is the usual way to look at warm-up and drift. Choose the time span (last
5 min ... all), show temperature as °C change or raw count, and point at the graph for the values. The
window can also show the raw A/D value of the service cassette (**from raw A/D**, in counts or, once counts per
gram are known, in mg), which resolves drift far below the 1 mg display. It works on any log: **Open log...** loads a `raw.csv` or a `scale_logger.py` file, or from the shell:

```bash
python3 scale_plot.py sessions/20261017_214545/raw.csv
```

It uses only tkinter, so there is nothing extra to install.

## Output

Each session gets its own folder under `sessions/`:

- `raw.csv` - every line the balance sent, time-stamped
- `steps.csv` - each recorded step: test, instruction, load, value, how long it had been stable
- `report.txt` - balance data, weights and the results of every test in the session
- `session.json` - the same in machine-readable form, including every step

## Setup

Requirements: Python 3 with tkinter (included in the python.org installers) and pyserial
(`python3 -m pip install pyserial`).

```bash
python3 scale_characterise.py              # the GUI
python3 scale_characterise.py --simulate   # try it without a balance
python3 scale_logger.py -o warmup.csv --minutes 180   # plain logger, no GUI
python3 -m unittest discover -s tests               # tests of the parts that need neither window nor balance
```

### Mettler-Toledo AM/PM balances

**Cable.** The Data I/O socket has RS-232 and current-loop signals; for this tool only two wires are
needed, from the balance's RS-232 output to the PC's input (pinout from the service manual):

| Balance pin | Signal | PC side (DB9) |
|---|---|---|
| 12 | RS out (data from the balance) | pin 2, RxD |
| 13 | RS COM (RS-232 ground) | pin 5, GND |
| 2 | RS in (data to the balance) | not needed |
| 6 | shield | connector shell |

Use a real RS-232 adapter. A bare TTL-level USB-serial board reads the RS-232 signal inverted, giving
regular but unreadable characters, and the RS-232 voltage can damage it.

**Balance configuration** (configuration file, sector I-FACE):

| Setting | Value |
|---|---|
| S (transmission mode) | `cONT`, every reading continuously |
| b (baud rate) | 9600 |
| P (parity) | `-e-`, even |
| Pause | `0` (do not use `H`: hardware handshake would stop the output with this cable) |
| AU (suppress special characters) | `on` |

The PC side is then 9600 baud, 7 data bits, even parity, 1 stop bit, no flow control.

### Service cassette (optional)

With the [service cassette](https://github.com/pignoniG/Mettler_PM_UnofficialServiceCassette) ROM and a two-way
cable (the balance's RS in pin 2 to the PC's TxD) the tool uses more of the balance. On connect it asks the
balance who it is: a newline and `XV`. A service cassette answers `XV SVC <version> ID <number>`, which fills
*serial / ID* and goes into the report; a standard cassette answers `ES`, which is enough to enable the tare
(the stock `T` command); no answer means a receive-only cable and the tool stays passive. `--no-probe` sends
nothing at all. The service cassette also sends an `R` line with the raw A/D value:

- every step records the mean raw value (`raw_mean`, `raw_std`, `raw_n` in `steps.csv`) and `raw.csv` gets the
  values with status `R`;
- each test report gets an **A/D counts view**: the same evaluation on the raw counts, converted to grams
  with the counts per gram fitted on that run (1 count is about 0.65 µg on a PM4600), so repeatability,
  eccentricity and linearity are resolved far below the display step; the gain error is removed by that fit,
  so the error-of-indication column of that view is relative to the fitted gain;
- the temperature is taken from the `R` line when no `T` lines arrive.

The program and the EEPROM tool cannot hold the serial port at the same time.

### Temperature (optional)

A Mettler balance measures its own temperature for the compensation, but does not normally send it. With
a ROM patched to send a line `T 297904` (the raw count the service keypad shows), the GUI shows the
count and the temperature next to the weight, writes the count to `raw.csv` (status `T`) and to every step
in `steps.csv`, and puts the first and last count in the report. The service manual converts counts at
2000 per °C (`counts per °C`, editable). The count has no known zero, so by default the GUI shows the
change since the first reading; type a thermometer reading and press **Set reference** to get absolute °C.
Balances that don't send `T` lines are unaffected.

## Limitations

- No measurement-uncertainty budget; the weights' `U` is reported next to the results for comparison.
- The program assumes the balance does not re-zero itself between steps in a way that would change the
  empty-pan reading during a load (auto-zero only acts with an empty pan, so that's normally fine).
- Eccentricity results scale roughly with the load, as the white papers point out; compare them only at
  the same test load.

## Licence

MIT - see [LICENSE](LICENSE). The analysis and code were developed with the help of Claude (Anthropic).
