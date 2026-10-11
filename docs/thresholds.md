# What the number means, and where the lines are

SysHealth puts one number on every machine and calls it HEALTHY, DEGRADED or
CRITICAL. This note says what the number is, why the lines sit where they do,
and which published work each one comes from.

## The number

The number is **Linux PSI (Pressure Stall Information) for memory**, read from
`/proc/pressure/memory`. It is a share of wall-clock time, 0 to 100%:

```
some avg10=0.09 avg60=0.57 avg300=0.38 total=1254807180
full avg10=0.08 avg60=0.49 avg300=0.35 total=1112854986
```

- **`some`**: the share of time in which *at least one* task was stalled waiting
  for memory. The kernel documentation defines it as "the share of time in which
  at least some tasks are stalled on a given resource" [1].
- **`full`**: the share of time in which *all* non-idle tasks were stalled at
  once, so the machine did no useful work at all [1].
- **`avg10`**: the average over the last 10 seconds. The kernel also keeps 60 s
  and 300 s averages [1].

The stall time is counted only where a shortage of memory causes it: when a task
has to reclaim pages before it can allocate, when it re-reads a file page that
was just evicted, and when it waits on swap-in [2, §3.2.3].

**How to read it.** Meta, who built PSI, put it this way: "memory pressure of 20%
indicates that the workload could have run 20% faster if it had access to more
memory" [3]. So the dashboard words it as time lost: *out of every 10 seconds,
programs spent about 2 waiting for memory.*

**What it is not.** PSI is not memory *usage*. A machine can be 90% full and
perfectly healthy, because nothing waits. A machine can also be 60% full and
thrashing. The TMO paper describes the older approach, in which operators had to
correlate "kernel time, variations in application throughput, event counters for
reclaim activity, file re-reads, and swap-ins" to estimate what PSI now measures
directly [2, §3.2.4].

### What the dashboard shows

| Where | Value | Why |
|---|---|---|
| Headline number | Mean of `some avg10` over the last minute (12 samples at 5 s) | The state is judged on this, so the number and the badge can never disagree |
| "Right now" tile, chart line | `some avg10` of the latest sample | Reacts fastest |
| "Whole machine stalled" tile | `full avg10` | The critical signal in every system below |
| "Memory scanned" tile | `pgscan_direct` delta | A clue to *why*, not a measure of harm (see below) |

## Why the 2×/5× baseline rule was replaced

The previous rule was DEGRADED at 2× an idle baseline and CRITICAL at 5×, plus
CRITICAL whenever `pgscan_direct` rose by more than 1000 in 5 s. A recording of
the live four-size fleet on 23 August 2026 shows the problem:

| Instance | Average pressure | Old verdict |
|---|---|---|
| t3.medium | **0.10%** | CRITICAL, "10.17x baseline" |
| t3.micro | 4.2% | CRITICAL, "317.25x baseline" |

0.10% means the t3.medium lost about a hundredth of a second in every ten
seconds, which is nothing. It read as CRITICAL because its idle baseline was the
fallback, 0.01. That fallback is used whenever the idle reading is exactly zero,
as it usually is on a large instance. Dividing by a number that close to zero
blows noise up into alarms.

PSI does not need a baseline. It is already normalised to wall-clock time, so
10% means the same thing on a t3.micro as on a t3.large: one second in every ten
lost to waiting. None of the systems below compares PSI to an idle baseline.
They all use absolute thresholds.

The `pgscan` rule is gone for a related reason. PSI's author describes systems
with fast storage where "there is *always* some clean and uptodate cache to
reclaim", so reclaim activity carries on even while "tasks spend 90% of the time
thrashing" [4]. A lot of scanning can be harmless and a little can hurt, so
scanning is still shown but no longer decides the state.

## What production systems use

The thresholds are drawn from software that acts on memory PSI in production,
on servers and on phones:

| System | Signal | Threshold | What happens | Source |
|---|---|---|---|---|
| Meta Senpai (TMO, ASPLOS '22) | `some` | **0.1%** | The target pressure: Senpai keeps memory just tight enough to stay at this level, which costs no meaningful performance | [2, §3.3] |
| Linux kernel docs | `some` | 150 ms per 1 s (**15%**) | The example trigger in the PSI documentation | [1] |
| Android lmkd (default) | `some` | 70 ms per 1 s (**7%**) | Low-memory notification ("medium" level) | [5, 6] |
| Android lmkd (default) | `full` | 700 ms per 1 s (**70%**) | Critical memory notification | [5, 6] |
| Android lmkd (legacy strategy) | `some` / `some` / `full` | 70 / 100 / 70 ms per 1 s (**7% / 10% / 7%**) | low / medium / critical levels | [6] |
| Meta oomd (example ruleset) | 1-minute pressure | **40** for 15 s (with the workload at 5), or **80** for 30 s | Kills "a memory hog" | [7] |
| systemd-oomd | `full` avg10 | **60%** for 30 s | Kills processes in the cgroup | [8] |

TMO also says what the two lines are for. `full` pressure "can be used to detect
unacceptable losses of productivity that require immediate remediation", while
`some` "measures the latency impact" and "is sensitive enough to detect aggregate
delays below the threshold that would cause applications to suffer meaningful
performance losses" [2, §3.2.4].

## The bands SysHealth uses

| State | When | In plain words |
|---|---|---|
| **HEALTHY** | `some` under **10%** | Under 1 s in every 10 lost to waiting. Below 1% there is no measurable slowdown. |
| **DEGRADED** | `some` **10%** to under 40% | 1 to 4 s in every 10. The app is noticeably slower than it would be with enough memory. |
| **CRITICAL** | `some` **40%** or more, *or* `full` **10%** or more | Thrashing. Timeouts are likely, and the out-of-memory killer is next. |

All three are judged on the **one-minute** mean, and a state must hold for **3
samples (15 s)** before it changes.

How each line was chosen:

- **Degraded at 10% `some`.** This sits between Android's 7% and the kernel
  example's 15%. Both of those are measured over 1-second windows. Ours is a
  one-minute mean, so the level has to be sustained: a burst of a few seconds
  does not trip it. At 10% an app runs at
  least 10% slower than it would with enough memory [3], which users of an
  interactive service can notice.
- **Critical at 40% `some`.** This is the level at which Meta's example oomd
  ruleset starts killing processes after 15 s [7]. At 40% the work takes at least
  1.67× as long (1 / 0.6).
- **Critical at 10% `full`.** Every system above treats `full` as the emergency
  signal [2, 6, 8]. Production values run from 7% (lmkd's legacy level) to 60–70%
  (systemd-oomd and lmkd's default, which are *kill* thresholds). A monitor should
  warn *before* the killers act, so the line sits at the early end. Sustained over
  a minute, 10% means the whole machine did nothing for six seconds.
- **15 s persistence** matches the duration in the oomd example [7]. The one-minute
  mean plays the role of oomd's "1m pressure".
- **Below 1%** is called out inside HEALTHY because Meta runs its whole fleet on
  purpose at 0.1% `some` without meaningful performance loss [2, §3.3]. A reading
  that low is a machine using its memory well, not a problem.

## Limits

- **No single standard number exists.** These are taken from systems that make
  kill-or-keep decisions on PSI. Phones (lmkd) and server fleets (oomd,
  systemd-oomd) choose differently, and the bands above sit between them on purpose.
- **Kubernetes does not use these numbers yet.** It exposes PSI (generally
  available since 1.36) but does not yet set node conditions from it [9]. That
  means no cluster default to borrow.
- **Latency-sensitive apps may want a lower degraded line**, for example 5%. The
  values are constants at the top of `analyzer.py`. Each sample carries the
  values its agent used, so the dashboard draws whatever was actually applied.
- **This is machine-wide pressure.** It includes the agent and the OS, not just
  the app. Per-container PSI would isolate the app, but that is a different
  collector.
- **The README's experimental results predate this change.** They were labelled
  under the 2×/5× rule and did not record `full`, so they should be re-run.

## Sources

1. Linux kernel documentation, *PSI - Pressure Stall Information*.
   <https://docs.kernel.org/accounting/psi.html>
2. J. Weiner, N. Agarwal, D. Schatzberg, L. Yang, H. Wang, B. Sanouillet,
   B. Sharma, T. Heo, M. Jain, C. Tang, D. Skarlatos. *TMO: Transparent Memory
   Offloading in Datacenters.* ASPLOS '22, ACM, 2022.
   <https://doi.org/10.1145/3503222.3507731>.
   PDF: <https://www.cs.cmu.edu/~dskarlat/publications/tmo_asplos22.pdf>
3. Meta, resctl-demo documentation, *Monitoring Resource Contention with PSI*.
   <https://facebookmicrosites.github.io/resctl-demo-website/docs/demo_docs/res_protection/psi/>
4. J. Weiner, *psi: pressure stall information for CPU, memory, and IO v2*,
   patch series cover letter, July 2018. <https://lwn.net/Articles/759658/>.
   Also J. Corbet, *Tracking pressure-stall information*, LWN, July 2018.
   <https://lwn.net/Articles/759781/>
5. Android Open Source Project, *Low memory killer daemon*
   (`ro.lmk.psi_partial_stall_ms`, `ro.lmk.psi_complete_stall_ms`).
   <https://source.android.com/docs/core/perf/lmkd>
6. AOSP source, `system/memory/lmkd/lmkd.cpp`: `PSI_WINDOW_SIZE_MS 1000`,
   `DEF_PARTIAL_STALL 70`, `DEF_COMPLETE_STALL 700`, the `psi_thresholds` table,
   and the `use_new_strategy` override.
   <https://android.googlesource.com/platform/system/memory/lmkd/+/refs/heads/main/lmkd.cpp>
7. Meta, oomd, `docs/configuration.md`, example "memory pressure protection"
   ruleset. <https://github.com/facebookincubator/oomd/blob/main/docs/configuration.md>
8. systemd, `oomd.conf(5)`: `DefaultMemoryPressureLimit=` (60%, "the fraction of
   time in a 10 second window in which all tasks in the control group were
   delayed") and `DefaultMemoryPressureDurationSec=` (30 s).
   <https://www.freedesktop.org/software/systemd/man/latest/oomd.conf.html>
9. Kubernetes enhancement KEP-4205, *PSI based on cgroupv2*.
   <https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/4205-psi-metric>

Further reading: M.-C. Lee, W. S. Lim, Y.-H. Chang, T.-W. Kuo, *SARA: A
Stall-Aware Memory Allocation Strategy for Mixed-Criticality Systems*, arXiv
2511.19991 (2025), which uses PSI memory stalls to predict missed deadlines.
<https://arxiv.org/abs/2511.19991>
