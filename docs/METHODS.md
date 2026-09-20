# Methods

Every data-cleaning and modelling decision taken on this dataset, with the reason
and the evidence number behind it.

Source: printed-report CSV exports from **Express** (Thai accounting package) for
a metal roofing / PU foam business. Data covers **2025-12-01 to 2026-08-31**
(Gregorian).

| Artefact | Count |
|---|---|
| Sales documents | 6,256 (6,088 cash `HS`, 168 credit `IV`) |
| Sales lines | 31,578 |
| Deposits (`AI`) | 193 |
| Deposit applications | 275 |
| Customers | 1,478 |
| Products | 634 (615 stocked) |
| Revenue ex-VAT | 44,493,479.89 |

Pipeline: `src/parse_express.py` → `src/forecast_baseline.py` → `src/forecast_plan.py`
→ `src/export_app.py`. Plain Python + pandas throughout; no AI/LLM calls anywhere
in the pipeline.

---

## 1. Parsing

### 1.1 Encoding

**Decision.** Read as `cp874`, write as `utf-8-sig`.

**Reason.** Express emits Thai in the cp874/TIS-620 codepage. Output carries a BOM
because Thai Excel opens plain UTF-8 as mojibake.

### 1.2 These are report layouts, not tables

**Decision.** Parse as a printed report — skip repeating page headers, detect row
types by pattern rather than by column position.

**Reason.** The exports are print jobs saved as CSV. A page header block recurs
every ~50 rows, column widths are fixed, and long values bleed into the adjacent
column. Reading them with `pd.read_csv` directly produces plausible-looking but
wrong data, which is worse than an error.

### 1.3 Literal inch marks break CSV quoting

**Decision.** Custom field splitter instead of the `csv` module.

**Evidence.** Product names contain unescaped inch marks — `ลอน760/3"` — which
terminate a quoted field early and shift every subsequent column on that row.

### 1.4 Buddhist Era dates

**Decision.** Subtract 543 from the year. Document numbers encode `YYMM/DDNN` in BE
and are left as printed (`HS6904/2320`), with the Gregorian date in
`doc_date_iso`.

**Reason.** The document number is the business's own reference and must stay
matchable to paper; the ISO date is what analysis joins on.

### 1.5 Customer names are clipped at different widths per report

**Decision.** `NAME_CLIP_MIN = 29`, and truncation is tested **bidirectionally**.

**Evidence.** Name column widths measured from the data: cash report 29 chars,
credit 36, deposits 52. The same customer is therefore clipped to a different
length in each report, so either of two spellings can be the truncated one. A
one-directional prefix test classified all 14 unmatched names as weak "fuzzy"
matches; the bidirectional test promoted `พ-073` from 0.89 fuzzy to 0.97 truncated.

### 1.6 What did not parse

**Decision.** Unparsed rows are written to `data/clean/_unparsed.csv`, never
dropped silently.

**Evidence.** **1 row** of ~38,000 failed to parse: a handwritten annotation in the
deposits report (`ชำระวันจัดส่ง 3,373 บาท` — "pay 3,373 baht on delivery day").
It is a note, not a transaction.

---

## 2. Revenue definition

### 2.1 Revenue = `sum(amount_ex_vat)`, not `goods_value`

**Decision.** Line-level `amount_ex_vat` is the revenue metric.

**Reason.** `goods_value` is a header field that behaves inconsistently across the
VAT-inclusive documents (§2.2). Summing lines reconciles to the header total and
keeps revenue decomposable by product group.

**Evidence.** `monthly_sales.csv` reconciles to `sales_lines.csv` to the cent:
44,493,479.89 vs 44,493,479.89, difference 0.0000.

### 2.2 VAT-inclusive documents detected by arithmetic, not by a flag

**Decision.** Detect VAT-inclusive pricing by testing the identity
`goods_value + vat == total`; where it holds, `amount_ex_vat = amount / 1.07`.

**Reason.** Express does not flag these rows. They are only visible as an
arithmetic inconsistency between the header fields.

**Evidence.** **6 documents** of 6,256 price VAT-inclusive. They are the entire
contents of `_reconciliation_mismatches.csv`, each carrying
`status = vat_inclusive_pricing` — i.e. all six "mismatches" are explained, and
there are no unexplained reconciliation failures.

---

## 3. Deposits, vouchers and cancellations

### 3.1 Deposit links are printed in both reports

**Decision.** Deduplicate deposit→document links across the two source reports;
record which reports each link came from in `sources`.

**Reason.** Naively unioning the two reports double-counts every link.

### 3.2 `SR` vouchers kept out of revenue

**Decision.** Add a `doc_type` column (`AI` / `SR`) to `deposit_applications`.
Revenue is **unchanged** — `SR` is not netted off.

**Reason.** `SR` rows sit in the deposit-application stream but look like a
different instrument. Their character is only confirmable from source paperwork,
so they are labelled for later netting rather than assumed to be returns.

**Evidence.**

| doc_type | links | total | median |
|---|---|---|---|
| AI | 196 | 4,296,961.50 | 19,100.00 |
| SR | 79 | 166,752.42 | 651.00 |

The two populations differ by ~30x in median size, and every `SR` link originates
from the cash-sales report only. That is suggestive but not conclusive, hence no
revenue change.

### 3.3 Cancelled documents

**Decision.** Add `is_cancelled`; exclude cancelled rows from demand and revenue.

**Evidence.** **0** cancelled sales headers and lines. **1** cancelled deposit:
`AI6908/1701` (น-534, 2026-08-17, 10,000, `cleared_flag = N`, 0 applications).
Revenue is identical with and without the filter — the flag is there for future
data, not because it changes today's numbers.

---

## 4. Customer identity

### 4.1 Two confirmed merges, twelve new codes

**Decision.** Merge only the two matches the owner confirmed; give the remaining
12 name-only customers generated codes `CR-001`…`CR-012`. Recorded in
`customer_aliases.csv` (raw_name → customer_code → match_type).

**Evidence.** 1,490 → **1,478** customers (−14 uncoded, +12 generated, 2 folded).
`พ-073` now carries 16 documents across both spellings; `ด-057` carries 10.
0 duplicate and 0 null customer codes remain.

### 4.2 Numbered sister companies are not near-duplicates

**Decision.** Flag `digits_differ` and sort those candidates to the bottom of the
review file.

**Evidence.** `โปรสมาร์ท รูฟ 9 จำกัด` vs `โปรสมาร์ท รูฟ จำกัด` scores 0.95 on string
similarity but they are separate legal entities. String distance alone would have
merged them.

### 4.3 Codes back-filled onto headers

**Decision.** Resolve credit-customer names to codes on the header table, not just
inside the customer table.

**Evidence.** 134 headers carried a null `customer_code` because the resolution
happened only in the customer dimension. Now **0**.

### 4.4 Nothing merges without sign-off

**Decision.** Fuzzy match output goes to `data/review/`, never `data/clean/`.

**Reason.** A wrong merge is invisible downstream and silently corrupts
per-customer analysis. Review files are an input to a human decision, not an
output of the parse.

---

## 5. Products, units and groups

### 5.1 Product groups are a hand-maintained input

**Decision.** `data/reference/product_groups.csv` maps the 2-digit SKU prefix to
category, group name, main unit and forecast scope. Read with `dtype=str` and
`zfill(2)`.

**Reason.** Business knowledge, not derivable from the data. The `dtype=str` is
load-bearing: default pandas type inference turns prefix `01` into integer `1` and
the join then silently produces nulls. (This trap caused one false bug report
during development — an inspection script, not the pipeline, reported a corrupted
`salesperson_code` that was correct on disk.)

### 5.2 One unit per group

**Decision.** `weekly_demand.csv` keeps only each group's main unit.

**Reason.** Several groups transact in mixed units (metres and sheets). Summing
across units produces a meaningless quantity.

**Evidence.** The main unit covers ≥90% of quantity in 15 of 19 groups. The four
below that threshold — 99 (0%), 11 (22.7%), 77 (25.0%), 12 (33.7%) — are all
out of forecast scope (§6.1). Coverage is re-checked on every parse and printed in
the validation report rather than assumed stable.

### 5.3 Header keys denormalised onto lines

**Decision.** Copy `doc_date`, `doc_date_iso`, `salesperson_code` and
`customer_code` from header onto `sales_lines`.

**Reason.** Lines carried no date, so any time-series work needed a join first.
Owner-confirmed as a deliberate denormalisation, with the header remaining the
single source of truth.

---

## 6. Forecasting setup

### 6.1 Forecast scope

**Decision.** 15 of 19 groups are in scope. Excluded: **99** and **77** (not
procured — freight, giveaways, off-grade, bending services) and **11** and **12**
(low value; handled by ABC / slow-mover analysis instead).

**Evidence.** The 15 in-scope groups carry **97.1%** of revenue ex-VAT.

### 6.2 Weekly panel

**Decision.** Complete group × week grid, absent rows materialised as real zeros.
Incomplete edge weeks excluded.

**Reason.** A missing row and a zero-demand week are different facts; if absent
rows are left absent, every moving average silently skips them.

**Evidence.** **39 complete weeks, 2025-12-01 to 2026-08-24.** The trailing week
`2026-08-31` holds 1 day and 29 documents against a normal ~150 and is excluded —
including it would read as a 80% collapse in the final week.

### 6.3 Backtest design

**Decision.** Rolling-origin backtest with an expanding training window. 8-week
holdout, 4-week forecast horizon, 5 origins per scenario. Origins sit strictly
before the holdout, so no future data reaches a forecast.

**Metric: WAPE**, `sum|actual − forecast| / sum actual`, reported at two levels:

- **weekly** — week-by-week accuracy, includes timing noise
- **4-week total** — what the owner actually orders against; target 10–15%

**Reason for WAPE over MAPE.** MAPE divides by each actual, so a single near-zero
week produces an unbounded percentage and dominates the average. Several groups
here have zero weeks, which makes MAPE undefined outright.

**Models** (simplest first): `naive` (last observed week), `ma4`, `ma8`,
`ses` (simple exponential smoothing, α = 0.3, fixed — not tuned).

### 6.4 Two holdouts: normal vs declining conditions

**Decision.** Run the identical backtest twice.

| scenario | holdout weeks | conditions |
|---|---|---|
| earlier | 2026-05-04 … 2026-06-22 | stable |
| recent | 2026-07-06 … 2026-08-24 | declining |

**Reason.** A single holdout over a period of falling demand measures the decline,
not the model.

---

## 7. Forecast results

### 7.1 Pooled WAPE, 4-week total

| model | earlier (stable) | recent (declining) | delta |
|---|---|---|---|
| naive | 23.6% | 30.2% | +6.5 |
| ma4 | 14.5% | 22.6% | +8.1 |
| **ma8** | **14.2%** | **22.5%** | +8.3 |
| ses α=0.3 | 11.5% | 21.6% | +10.1 |

The 10–15% target is met in stable conditions and missed by ~8 points in the
decline.

### 7.2 The degradation is level error, not noise

**Evidence.** Pooled **weekly** WAPE barely moves between scenarios
(ma8: 25.9% → 27.4%) while the **4-week total** WAPE nearly doubles
(14.2% → 22.5%).

Week-to-week timing error is unchanged; what changed is a persistent bias that
only accumulates once you sum four weeks. Confirming this per group, signed bias
approaches WAPE in magnitude for the largest groups — group 10 is ~100%
systematic (−17.8% demand), group 02 ~97% (−28.3%), group 88 ~100% (−21.1%).

**Interpretation.** Total in-scope demand fell from 39,725 units/week over the
first 31 weeks to 32,942 over the last 8 — **−17.1%**. No backward-looking model
can absorb a level shift; it can only notice it afterwards. This is a demand
story, not a modelling failure, and the trend alert (§8.3) exists to surface it
early rather than to fix it.

### 7.3 The revenue decline is real

**Evidence.** July 5,127,781 → August 4,307,415 = **−16.0%**. Selling days fell
27 → 26 (August's non-selling days are all Sundays), so per selling day the
decline is **−12.8%**.

**Substitution does not explain it.** Testing whether customers moved from
0.30 standard (group 02) to grade-B (group 03):

| flow | metres/week |
|---|---|
| Group 02 loss | −1,420 |
| Group 03 gain | +247 |
| All metre-group losses | −2,508 |
| All metre-group gains | +800 |

Group 03's gain offsets **17.4%** of group 02's loss and **9.8%** of all
metre-group losses. Substitution is real but minor; the decline is genuine.

### 7.4 Model choice: ma8

**Decision.** `ma8` for all non-intermittent groups.

**Reason.** `ses` is marginally better pooled (11.5% vs 14.2% earlier), but the
gaps between `ma4`/`ma8`/`ses` are 0.4–1.6 points measured on 5 origins — inside
the noise. A mean of the last eight weeks is something the owner can verify by
hand on a printout, and a forecast nobody can check is a forecast nobody trusts.
Explainability wins a statistical tie. `naive` is clearly worse (+9 points) and is
rejected on the numbers.

### 7.5 Forecast ranges are empirical, not statistical

**Decision.** The range on `next_4_weeks.csv` is the 10th–90th percentile of
observed `actual / forecast` ratios for the same model over every backtest window,
pooled across both scenarios (10 windows per group).

**This is not a confidence interval.** "Low" means: in 8 of 10 backtested windows
this group came in above this number. Both scenarios are pooled deliberately —
a range built only on the stable period would be reassuring and wrong.

**Two guards.**

- A window only contributes a ratio if its forecast is ≥20% of that group's median
  window forecast. Without this, near-zero denominators dominate: one group-04
  window forecast 33.6 m against 814 m actual, a 24x ratio that dragged the 90th
  percentile to 11.7x and produced a "high" of 19,567 m on a 1,669 m forecast.
- Below 8 surviving windows the range is left **blank**. A number computed from 3
  observations is false precision; an empty cell is honest.

**Ranges below the point forecast are a signal, not a bug.** For groups 10, 01, 15
and 06 the entire range sits below the ma8 forecast, because ma8 over-forecast
them in 9 of 10 windows. These rows are flagged `range_below_forecast` rather than
clipped — it means "order toward the low end".

**Limitation.** Ten windows is a thin basis for a percentile. `n_windows` is on
every row so the reader can see it.

---

## 8. Intermittency, reorder points, stock checks

### 8.1 The first intermittency test was wrong

**Original test.** Share of zero weeks over the whole 39-week panel ≥ 25%.
This classified 7 groups as intermittent.

**Problem.** That measure cannot distinguish three different situations:

| zeros sit | meaning | intermittent? |
|---|---|---|
| leading | product had not launched | no — short history |
| trailing | product stopped selling | no — possibly discontinued |
| interior | on the shelf, some weeks nobody buys | **yes** |

**Corrected test.** Share of zero weeks **between first and last sale**.

| grp | launched | pre-launch wks | %zero panel | %zero interior | verdict |
|---|---|---|---|---|---|
| 06 ผนังลอนเล็ก | 2026-02-16 | 11 | 30.8% | **3.6%** | forecastable |
| 08 ลอนฝ้า | 2026-02-23 | 12 | 33.3% | **3.7%** | forecastable |
| 07 ผนังเซาะร่อง | 2026-02-16 | 11 | 35.9% | **10.7%** | forecastable |
| 13 รั้วเหล็ก | 2026-02-23 | 12 | 41.0% | **14.8%** | forecastable |
| 04 เกรด APT | 2025-12-01 | 0 | 53.8% | 53.8% | intermittent |
| 16 เทปกันลั่น | 2025-12-08 | 1 | 43.6% | 42.1% | intermittent |
| 05 สแน็ปล็อค | 2026-05-04 | 22 | 82.1% | 41.7% | intermittent |

Groups 06, 07, 08 and 13 are **new products launched in February 2026**, not
intermittent demand. Their leading zeros are the product's absence. Misclassifying
them would have routed four growing products into reorder-point handling instead
of forecasting.

**Confirmed classification: intermittent = {04, 16, 05}**, forecastable = the
other 12.

**Consequence for the model.** All means now skip pre-launch weeks
(`is_pre_launch` on the panel). For the current data this changes no number —
every in-scope group has ≥8 weeks of trading history — but it prevents a group
launched within the last 8 weeks being forecast at a fraction of its true rate.

### 8.2 Reorder points

**Formula.**

```
reorder_point = mean weekly demand × lead time
              + z × weekly std dev × √(lead time)
                ^ demand while waiting    ^ safety stock
```

`√(lead time)` because variance accumulates linearly over independent weeks, so
standard deviation grows with the square root. Lead time = **1 week** (owner's
figure); z = **1.65** for a 95% service level, i.e. roughly one stockout per twenty
replenishment cycles.

**Statistics use each group's active window** (first sale onward), not the full
panel — the same pre-launch-zero problem as §8.1, which would otherwise halve the
mean and set the buffer too low.

**Group 04 override.** Its statistics come from the **last 8 weeks only**. Demand
stepped up ~3.3x in mid-June (full active mean 127/wk vs last-8 mean 417/wk); a
mean over the whole window describes a product that no longer exists at that rate
and would have set the reorder point at roughly a third of true lead-time demand.
The trade-off is that 8 weeks is a thin base for a variance estimate on a lumpy
series. Every row records its `basis_window`.

**Honest caveat.** The normal approximation assumes demand is symmetric about its
mean. For group 05 — selling in 18% of weeks — that is plainly false; the real
distribution is a spike at zero with occasional large orders. The approximation
**overstates** the buffer, so these numbers are conservative: they tie up cash
rather than risk a stockout. That is the right direction to be wrong in, and it is
why Croston or bootstrapping is the correct next step if the cash matters.

### 8.3 Trend alerts use the median week, not the mean

**Decision.** Headline measure is the **median** of the last 4 weeks against the
median of the last 8; flag at |change| > 15%. The mean-based figures are retained
as secondary columns.

**Reason.** This business sells in lumps — one roofing job can be seven times a
normal week — so a mean-based alert fires whenever a big customer happens to buy,
which is not news. The median asks the question the owner actually means: *has the
typical week changed?* A single outlier barely moves it, while a real shift in
level moves every week and therefore moves the median too.

**Evidence — the measure changed three verdicts.**

| grp | last 8 weeks | median Δ | mean Δ | verdict |
|---|---|---|---|---|
| 14 ท่อเหล็ก/แป | 86, 182, 106, 149, 90, 90, 65, 111 | −8.2% | −19.0% | **de-flagged** |
| 16 เทปกันลั่น | 6, 9, 6, **160**, 0, 48, 14, 6 | **+33.3%** | **−45.3%** | **direction reversed** |
| 07 ผนังเซาะร่อง | 172, **1,173**, 36, 170, 0, 78, 0, 19 | −83.2% | −88.2% | still flagged, less inflated |

Group 14 was flagged at −19.0% on the mean, but its weekly series is flat — the
mean was being pulled by a single 182 m week early in the window. Group 16 is
starker: the mean says −45.3% *falling* while the median says +33.3% *rising*,
because one 160-roll order sits in the earlier half of the window. The mean was
measuring that order, not the trend.

Where the two measures disagree the row is flagged `outlier_driven`, which is
itself the useful signal: **median flat + mean moved = one large order, not a
trend.**

**A median cannot detect a group that stops.** Group 05 sells in under a fifth of
weeks, so its median was *already zero* before it stopped selling. A
median-vs-median comparison reads 0 → 0 and reports no change — silently dropping
the single most important alert in the set. A separate test on the **total**
catches it regardless: `stopped = (last 4 weeks sum == 0) AND (last 8 weeks sum >
0)`. This was a real regression introduced by the switch to medians and caught in
testing.

**Damping.** The last 8 weeks *contains* the last 4, so the comparison is
deliberately damped — a 4-week surge moves it by at most half its true size. That
makes it slow to false-alarm, which is what you want for something read weekly.
`pct_change_vs_prior_4` (last 4 vs the 4 before, no overlap) is the sharper number
and is reported for sanity-checking.

**Current alerts.** 5 of 15 flagged: **05 stopped** (no sales in 4 weeks),
07 (−83.2%), 03 (−20.0%) falling; 16 (+33.3%), 13 (+145.1%) rising. 1 group (14)
suppressed as outlier-driven. Of the five, 05 and 16 are intermittent and 07 and
13 are new products still finding their level — so **03 is the only established
group moving on trend**, and it is the grade-B substitute discussed in §7.3.

### 8.4 Stock check (not "dead stock")

**Decision on naming.** These outputs say **"stock check recommended"**, never
"dead stock".

**Reason.** This pipeline has **sales data only — there is no inventory feed.** A
SKU that has not sold in 90 days may be sitting in the warehouse as dead capital,
*or the shelf may be empty and that is precisely why it has not sold.* The data
cannot distinguish those two, and they call for opposite actions — write it down
versus reorder it. What the data does support is "go and look at this one", so
that is what the column says. Calling it dead stock would assert a fact about
inventory that this dataset cannot establish.

**Decision on tiers.** `risk_level`:

| tier | dormancy | recommendation | SKUs |
|---|---|---|---|
| `active` | < 60 days | – | 405 |
| `watch` | 60–89 days | monitor | 62 |
| `risk` | 90+ days | stock check recommended | 148 |

**The 30-day tier is deliberately absent from the dashboard.** At 30 days the list
runs to **319 of 615 SKUs** — over half the catalogue. A "problem" list containing
half of everything is not actionable and trains the reader to ignore it. The
30-day column is retained in the analyst-facing file
(`data/forecast/dead_stock_risk.csv`) for analysis, but the dashboard export
(`data/app/stock_check.csv`) carries only `watch` and `risk`: 210 rows.

**Reason for SKU level.** Group averages hide dormancy — one live SKU keeps a
whole group looking alive while the rest of it goes quiet. Group 05 is the
example: the group-level reorder point was proposing a 323 m buffer against a
product whose last sale was 2026-07-13.

**As-of date.** Dormancy is measured from the **last date in the export**
(2026-08-31), not from today. Measuring from today would silently convert a
reporting lag into an apparent stock problem — at the time of writing that would
have added 19 days to every SKU.

**Quantity-bearing lines only.** A zero-quantity line is usually descriptive or
corrective and must not keep a dormant SKU looking alive.

**Evidence.** Of 615 stocked SKUs, 148 are at `risk` (1,452,318 lifetime revenue —
3.3% of total) and 62 at `watch`. 0 SKUs have never sold. The largest `risk` item
is `02-208` (ค.ข้าง457/แดงมะขาม/0.35), last sold 2026-03-26, 158 days, 200,732
lifetime revenue. The longest dormant is `10-019` at 270 days.

---

## 9. Dashboard export

### 9.1 The dashboard reads from `data/app/`, never from `data/clean/`

**Decision.** `src/export_app.py` writes a dedicated set of ten CSVs to
`data/app/`, and the dashboard reads only those. It never opens `data/clean/` or
`data/forecast/` directly.

**Reason.** `data/clean/` is an *analysis* surface — wide, denormalised, and free
to change shape whenever a question needs a new column. If the dashboard bound to
it, every analytical change would risk breaking the front end. `data/app/` is a
published contract: a narrow, named set of files with a documented purpose. The
two can now move independently. The export step also re-derives the aggregates
(`kpi_monthly`, `sales_by_group_month`, `sales_by_person_month`) from the line
table rather than trusting a stored copy, so the dashboard cannot drift from
source.

**Verification.** Monthly revenue in `kpi_monthly.csv` reconciles against
`sum(amount_ex_vat)` on the line table to a difference of 0.0000.

### 9.2 Codes are written as zero-padded strings

**Decision.** `sku_prefix`, `salesperson_code` and `customer_code` are re-padded
to their full width on export, and `data/app/README.md` opens with an instruction
to read them as text.

**Reason.** These are identifiers that merely look numeric. Any reader that
type-infers turns `"02"` into `2`, and the join to `dim_product_group.csv` then
silently returns nothing — a failure that produces an empty chart rather than an
error. This is not hypothetical: it bit three separate inspection scripts during
this project, including one written *after* the warning existed. The export
guarantees the values on disk are correct; the README tells the reader how to
load them.

Booleans are written as lowercase `true`/`false` for the same reason — a single,
predictable spelling. Note that pandas infers those as real booleans on read, so
they must be compared as `df.alert` and not `df.alert == "true"`.

### 9.3 Caveats travel with the files

**Decision.** `data/app/README.md` is generated by the export script, listing
every file with its columns, the dashboard page that consumes it, and any caveat
that must not be separated from the data.

**Reason.** The dangerous columns here are the ones that look self-explanatory
and are not: forecasts for intermittent groups are indicative only and must not
drive ordering, and the stock-check list is dormancy, not confirmed dead stock
(§8.4). A caveat kept in a separate document is a caveat that will be missed.
Generating the README from the same constants the export uses also keeps it from
going stale.

**`is_partial_month` is currently `False` for all nine months** — this export ends
exactly on a month boundary (2026-08-31), so nothing is truncated. The flag is
carried anyway because the next export almost certainly will land mid-month, and
an unflagged partial month plots as a collapse in revenue rather than as missing
days. The column is a guard against a future reading error, not a description of
a current one.

---

## 10. Limitations

1. **The models are baselines.** No tuning, no seasonality, no trend term, no
   external drivers. α = 0.3 is a default, not a fitted value.
2. **Nine months of data**, so no year-over-year comparison and no measurable
   annual seasonality. Any seasonal pattern is currently indistinguishable from
   the level shift.
3. **Forecast ranges rest on 10 backtest windows per group.** Directionally
   useful, not precise.
4. **The level shift is unexplained.** §7.3 rules out product substitution as the
   main cause; it does not establish what the cause is. Lost customers,
   competitor pricing, and seasonal construction slowdown are untested.
5. **`SR` vouchers are labelled, not netted.** If they are confirmed as returns,
   revenue falls by 166,752.42 (0.37%).
6. **Reorder points assume a 1-week lead time** as a single fixed number, with no
   allowance for lead-time variability — which in practice is often the larger
   source of stockout risk than demand variability.
7. **12 customers hold generated codes** (`CR-001`…). If Express later assigns them
   real codes, the alias table needs updating or they will double-count.
8. **Intermittent-group forecasts are published but should not be ordered
   against.** They carry `confidence = "indicative only"`; the reorder point is
   the intended instrument for those three groups.
