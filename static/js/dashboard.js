/* SysHealth dashboard — memory pressure graph.
   No build step and no third-party libraries: the SVG is drawn by hand so the
   page works on an air-gapped box, which is where this agent usually runs. */

(function () {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var POLL_MS = 5000;
  var HISTORY_LIMIT = 720;
  var STALE_AFTER_MS = 20000;

  // Where each state begins, in % of time spent waiting for memory. Every
  // sample carries the values its agent judged it by; these are for agents
  // too old to say. Sources and reasoning: docs/thresholds.md.
  var DEFAULT_THRESHOLDS = { degraded: 10, critical: 40, critical_full: 10 };

  var STATE_TEXT = {
    healthy: {
      calm: 'Enough memory for the current load, with no measurable slowdown.',
      busy: 'Some short waits, too small to matter for most apps.',
      action: 'Nothing to do.'
    },
    degraded: {
      calm: 'Programs are noticeably slowed down waiting for memory. Users may see slower responses.',
      action: 'Reduce the load, or move to an instance with more memory.'
    },
    critical: {
      calm: 'The machine is thrashing. It spends its time freeing memory instead of working. ' +
            'Expect timeouts, and the kernel may start killing processes.',
      action: 'Act now: cut the load, or move to a bigger instance.'
    }
  };

  var MODE_HELP = {
    peak: 'One level, held for the whole test. Answers: can each size take my app at its busiest?',
    ramp: 'Climbs from the lowest level to the highest in even steps. ' +
          'Answers: at what load does each size start to struggle?',
    wave: 'Climbs, then comes back down, like a day of traffic. ' +
          'Shows whether each size recovers once the load drops.',
    bursts: 'Alternates between a base load and a spike. Shows how each size handles short peaks.'
  };

  // Instance sizes are an ordered scale, so they take one hue stepped
  // light→dark rather than four unrelated colours: bigger box, darker line.
  // The step is fixed per size, so selecting one instance never repaints another.
  var SIZE_COLORS = {
    micro: 'var(--size-micro)',
    small: 'var(--size-small)',
    medium: 'var(--size-medium)',
    large: 'var(--size-large)'
  };

  var ALL = 'all';

  var state = {
    instances: [],
    selected: ALL,
    rangeMinutes: 15,
    view: 'chart',
    activeIndex: null,
    activeFromKeyboard: false,
    layout: null,
    // The load test form.
    testMode: 'peak',
    testPattern: 'ramp',
    preview: null,
    // The agents report a test only in their next sample, up to a poll and a
    // push later. These bridge that gap, so the page does not offer Start
    // again just after it was pressed, or show a test it was just told to stop.
    startedAt: null,
    stopped: null
  };

  var REPORT_LAG_MS = 15000;

  function $(sel) {
    return document.querySelector(sel);
  }

  function svgEl(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, String(attrs[key]));
      }
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function num(value) {
    var n = typeof value === 'number' ? value : parseFloat(value);
    return isFinite(n) ? n : null;
  }

  /* ---------- data ---------- */

  // The server stamps `received_ts` (epoch seconds) and `received_at` (a
  // display string). Older payloads carry only the string, or only the agent's
  // own clock, so fall back through both.
  function sampleTime(raw) {
    var stamped = num(raw.received_ts);
    if (stamped !== null) {
      return stamped * 1000;
    }
    var epoch = num(raw.received_at);
    if (epoch !== null) {
      return epoch * 1000;
    }
    if (typeof raw.received_at === 'string') {
      var received = raw.received_at.match(
        /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/
      );
      if (received) {
        return new Date(
          +received[1], +received[2] - 1, +received[3],
          +received[4], +received[5], +received[6]
        ).getTime();
      }
    }
    if (typeof raw.timestamp === 'string') {
      var m = raw.timestamp.match(
        /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/
      );
      if (m) {
        return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime();
      }
    }
    var ts = num(raw.timestamp);
    // A bare epoch is seconds when it is small enough to be one.
    return ts === null ? null : (ts < 1e11 ? ts * 1000 : ts);
  }

  function normalize(raw) {
    if (!raw || typeof raw !== 'object') {
      return null;
    }
    var t = sampleTime(raw);
    var psi = num(raw.psi);
    if (t === null || psi === null) {
      return null;
    }
    var avg = num(raw.avg_psi);
    var full = num(raw.psi_full);
    var avgFull = num(raw.avg_psi_full);
    return {
      t: t,
      psi: psi,
      avgPsi: avg === null ? psi : avg,
      psiFull: full,
      avgFull: avgFull === null ? full : avgFull,
      state: String(raw.state || 'UNKNOWN'),
      scan: num(raw.pgscan_delta),
      steal: num(raw.pgsteal_delta),
      reason: typeof raw.reason === 'string' ? raw.reason : '',
      thresholds: normalizeThresholds(raw.thresholds),
      memMb: num(raw.mem_total_mb),
      test: normalizeTest(raw.load_test)
    };
  }

  function normalizeThresholds(raw) {
    if (!raw || typeof raw !== 'object') {
      return null;
    }
    var degraded = num(raw.degraded);
    var critical = num(raw.critical);
    var criticalFull = num(raw.critical_full);
    if (degraded === null || critical === null || !(critical > degraded)) {
      return null;
    }
    return {
      degraded: degraded,
      critical: critical,
      critical_full: criticalFull === null ? DEFAULT_THRESHOLDS.critical_full : criticalFull
    };
  }

  function normalizeTest(raw) {
    if (!raw || typeof raw !== 'object' || !raw.test_id) {
      return null;
    }
    return {
      id: String(raw.test_id),
      mode: String(raw.mode || 'peak'),
      summary: typeof raw.summary === 'string' ? raw.summary : '',
      step: num(raw.step),
      steps: num(raw.steps),
      mb: num(raw.load_mb) || 0,
      killed: num(raw.stress_killed) || 0,
      remaining: num(raw.remaining_sec)
    };
  }

  // The newest thresholds any of these instances reported.
  function thresholdsOf(list) {
    var found = null;
    var foundAt = -Infinity;
    list.forEach(function (inst) {
      var samples = inst.all || inst.samples;
      for (var i = samples.length - 1; i >= 0; i--) {
        if (samples[i].thresholds) {
          if (samples[i].t > foundAt) {
            found = samples[i].thresholds;
            foundAt = samples[i].t;
          }
          break;
        }
      }
    });
    return found || DEFAULT_THRESHOLDS;
  }

  function memOf(inst) {
    var samples = inst.all || inst.samples;
    var last = samples[samples.length - 1];
    return last ? last.memMb : null;
  }

  function instanceColor(type) {
    var size = String(type || '').split('.')[1];
    // Anything off the ladder takes a separate hue rather than another step of
    // the ramp, so it can never be misread as a size.
    return SIZE_COLORS[size] || 'var(--size-other)';
  }

  function shortId(id) {
    var text = String(id || '');
    return text.length > 12 ? text.slice(-8) : text;
  }

  // Two boxes of the same size share a colour, because colour encodes size and
  // they genuinely are the same size. A dash pattern tells them apart on the
  // chart, so identity never rests on a colour that cannot carry it.
  var DASH_PATTERNS = [null, '7 4', '2 3', '9 3 2 3'];

  // Machines are named by what they run on, which is the thing being compared.
  // An agent that has not reported a type falls back to its hostname.
  function labelInstances(list) {
    var counts = {};
    var seenSoFar = {};

    list.forEach(function (inst) {
      counts[inst.type] = (counts[inst.type] || 0) + 1;
    });

    list.forEach(function (inst) {
      var nth = seenSoFar[inst.type] || 0;
      seenSoFar[inst.type] = nth + 1;

      if (inst.type === 'unknown') {
        inst.label = inst.hostname;
      } else if (counts[inst.type] > 1) {
        inst.label = inst.type + ' · ' + shortId(inst.hostname);
      } else {
        inst.label = inst.type;
      }

      inst.dash = counts[inst.type] > 1
        ? DASH_PATTERNS[nth % DASH_PATTERNS.length]
        : null;
    });
  }

  function selectedInstances() {
    var withData = state.instances.filter(function (inst) {
      return inst.samples.length;
    });
    if (state.selected === ALL) {
      return withData;
    }
    return withData.filter(function (inst) {
      return inst.id === state.selected;
    });
  }

  function comparing() {
    return state.selected === ALL && selectedInstances().length > 1;
  }

  // The range is measured from the newest sample anywhere, so instances stay
  // on a common window even if one of them stopped reporting.
  function latestTime() {
    var newest = null;
    state.instances.forEach(function (inst) {
      var last = inst.samples[inst.samples.length - 1];
      if (last && (newest === null || last.t > newest)) {
        newest = last.t;
      }
    });
    return newest;
  }

  function inRange(samples) {
    var newest = latestTime();
    if (!state.rangeMinutes || newest === null) {
      return samples;
    }
    var cutoff = newest - state.rangeMinutes * 60000;
    return samples.filter(function (s) {
      return s.t >= cutoff;
    });
  }

  function scopedInstances() {
    return selectedInstances()
      .map(function (inst) {
        return {
          id: inst.id,
          hostname: inst.hostname,
          type: inst.type,
          label: inst.label,
          color: inst.color,
          dash: inst.dash,
          online: inst.online,
          secondsSince: inst.secondsSince,
          samples: inRange(inst.samples),
          all: inst.samples
        };
      })
      .filter(function (inst) {
        return inst.samples.length;
      });
  }

  /* ---------- formatting ---------- */

  // Exactly the places the tick step needs: step 0.05 gives "0.05", not "0.050".
  function decimalsFor(step) {
    if (!(step > 0)) {
      return 2;
    }
    var exponent = Math.floor(Math.log10(step));
    var places = exponent >= 0 ? 0 : -exponent;
    var mantissa = step / Math.pow(10, exponent);
    if (Math.abs(mantissa - Math.round(mantissa)) > 1e-9) {
      places += 1;
    }
    return Math.min(4, places);
  }

  function formatPsi(value, digits) {
    if (value === null || value === undefined || !isFinite(value)) {
      return '—';
    }
    return value.toFixed(typeof digits === 'number' ? digits : 2);
  }

  function formatCount(value) {
    if (value === null || !isFinite(value)) {
      return '—';
    }
    return value.toLocaleString();
  }

  function pad2(n) {
    return n < 10 ? '0' + n : String(n);
  }

  function formatClock(t) {
    var d = new Date(t);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function formatAgo(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) {
      return s + 's ago';
    }
    var m = Math.floor(s / 60);
    if (m < 60) {
      return m + 'm ' + (s % 60) + 's ago';
    }
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm ago';
  }

  function stateKey(name) {
    var key = String(name || '').toLowerCase();
    return key === 'healthy' || key === 'degraded' || key === 'critical' ? key : 'unknown';
  }

  var STATE_RANK = { unknown: 0, healthy: 1, degraded: 2, critical: 3 };

  // A share of time, for people: enough places that a small reading never
  // rounds to "0", no more than that.
  function formatPct(value) {
    if (value === null || value === undefined || !isFinite(value)) {
      return '—';
    }
    if (value === 0) {
      return '0';
    }
    return value < 1 ? value.toFixed(2) : value.toFixed(1);
  }

  function formatMb(mb) {
    if (mb === null || mb === undefined || !isFinite(mb)) {
      return '—';
    }
    if (mb >= 1024) {
      var gb = mb / 1024;
      return (Math.abs(gb - Math.round(gb)) < 0.05 ? Math.round(gb) : gb.toFixed(1)) + ' GB';
    }
    return Math.round(mb) + ' MB';
  }

  function formatDuration(seconds) {
    var s = Math.max(0, Math.round(seconds));
    if (s < 60) {
      return s + ' s';
    }
    var m = Math.floor(s / 60);
    var rest = s % 60;
    return m + ' min' + (rest ? ' ' + rest + ' s' : '');
  }

  // The number, said as time lost: "of every 10 seconds, 1.4 were spent waiting".
  function describeWait(pct) {
    if (pct === null || pct === undefined || !isFinite(pct)) {
      return '';
    }
    if (pct < 1) {
      return 'Programs almost never had to wait for memory.';
    }
    var secs = pct / 10;
    var said = secs < 9.95 ? secs.toFixed(1).replace(/\.0$/, '') : '10';
    return 'Out of every 10 seconds, programs spent about ' + said +
      (said === '1' ? ' second' : ' seconds') + ' waiting for memory.';
  }

  function describeState(key, pct) {
    var text = STATE_TEXT[key];
    if (!text) {
      return { meaning: '', action: '' };
    }
    var meaning = key === 'healthy' && pct !== null && pct >= 1 ? text.busy : text.calm;
    return { meaning: meaning, action: text.action };
  }

  /* ---------- band scale ---------- */

  // Where a value sits on the scale, 0 to 1. Each band gets a third of the
  // width: on a linear 0-100 bar the whole healthy band would be a sliver at
  // the left, and that is where nearly every reading lives. The band edges
  // are labelled, so the stretch is never hidden.
  function bandPosition(value, th) {
    var v = Math.max(0, Math.min(100, value));
    if (v < th.degraded) {
      return (v / th.degraded) / 3;
    }
    if (v < th.critical) {
      return 1 / 3 + ((v - th.degraded) / (th.critical - th.degraded)) / 3;
    }
    return 2 / 3 + ((v - th.critical) / (100 - th.critical)) / 3;
  }

  function bandName(value, th) {
    if (value >= th.critical) {
      return 'critical';
    }
    return value >= th.degraded ? 'degraded' : 'healthy';
  }

  function renderScale(container, value, th, compact) {
    clear(container);
    var track = el('div', 'band-track');
    ['healthy', 'degraded', 'critical'].forEach(function (key) {
      var zone = el('span', 'band-zone');
      zone.setAttribute('data-state', key);
      track.appendChild(zone);
    });
    var has = value !== null && value !== undefined && isFinite(value);
    if (has) {
      var marker = el('span', 'band-marker');
      marker.style.left = (bandPosition(value, th) * 100).toFixed(2) + '%';
      track.appendChild(marker);
    }
    container.appendChild(track);

    if (!compact) {
      var labels = el('div', 'band-labels');
      [
        ['Healthy', 'under ' + th.degraded + '%'],
        ['Degraded', th.degraded + '–' + th.critical + '%'],
        ['Critical', th.critical + '% or more']
      ].forEach(function (pair) {
        var label = el('span', 'band-label');
        label.appendChild(el('strong', null, pair[0]));
        label.appendChild(el('span', null, pair[1]));
        labels.appendChild(label);
      });
      container.appendChild(labels);
    }

    container.setAttribute('role', 'img');
    container.setAttribute('aria-label', has
      ? formatPct(value) + '%, in the ' + bandName(value, th) + ' band. Healthy under ' +
        th.degraded + '%, degraded from ' + th.degraded + '%, critical from ' + th.critical + '%.'
      : 'No reading yet.');
  }

  function renderScaleGuide(th) {
    var list = $('#scale-guide-list');
    clear(list);
    [
      ['healthy', 'Healthy', 'under ' + th.degraded + '%',
        'Under ' + (th.degraded / 10) + ' s in every 10 lost to waiting. ' +
        'Below 1% there is no measurable slowdown at all.'],
      ['degraded', 'Degraded', th.degraded + '% to ' + th.critical + '%',
        (th.degraded / 10) + ' to ' + (th.critical / 10) + ' s in every 10. The app is ' +
        'noticeably slower than it would be with enough memory.'],
      ['critical', 'Critical', th.critical + '% or more, or the whole machine stalled ' +
        th.critical_full + '% of the time',
        'More time goes to waiting than the app can absorb. Thrashing: timeouts and ' +
        'out-of-memory kills come next.']
    ].forEach(function (row) {
      var item = el('li', 'scale-guide-item');
      var head = el('p', 'scale-guide-head');
      var dot = el('span', 'state-dot');
      dot.setAttribute('data-state', row[0]);
      head.appendChild(dot);
      head.appendChild(el('strong', null, row[1]));
      head.appendChild(el('span', 'scale-guide-range', row[2]));
      item.appendChild(head);
      item.appendChild(el('p', 'scale-guide-text', row[3]));
      list.appendChild(item);
    });
  }

  /* ---------- scales ---------- */

  function niceScale(maxValue) {
    if (!(maxValue > 0)) {
      return { max: 1, step: 0.25, ticks: [0, 0.25, 0.5, 0.75, 1] };
    }
    var target = 4;
    var rough = maxValue / target;
    var magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    var normalized = rough / magnitude;
    var stepFactor = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    var step = stepFactor * magnitude;
    var top = Math.ceil(maxValue / step) * step;
    var ticks = [];
    for (var i = 0; i * step <= top + step * 1e-6; i++) {
      ticks.push(i * step);
    }
    return { max: top, step: step, ticks: ticks };
  }

  function medianInterval(points) {
    if (points.length < 2) {
      return 0;
    }
    var deltas = [];
    for (var i = 1; i < points.length; i++) {
      deltas.push(points[i].t - points[i - 1].t);
    }
    deltas.sort(function (a, b) {
      return a - b;
    });
    return deltas[Math.floor(deltas.length / 2)] || 0;
  }

  // Split on gaps so a restarted agent does not get a straight line drawn
  // across the minutes it was away.
  function segmentize(points) {
    if (points.length < 2) {
      return points.length ? [points] : [];
    }
    var median = medianInterval(points);
    var maxGap = median > 0 ? median * 4 : Infinity;

    var segments = [];
    var current = [points[0]];
    for (var j = 1; j < points.length; j++) {
      if (points[j].t - points[j - 1].t > maxGap) {
        segments.push(current);
        current = [];
      }
      current.push(points[j]);
    }
    segments.push(current);
    return segments;
  }

  /* ---------- chart ---------- */

  // One drawing routine for both modes. Single instance: pressure plus its
  // rolling average and its thresholds. Several: one pressure line each.
  function buildSeries(scoped, multi) {
    if (!scoped.length) {
      return [];
    }
    if (multi) {
      return scoped.map(function (inst) {
        return {
          key: inst.id,
          name: inst.label,
          color: inst.color,
          points: inst.samples,
          value: function (s) { return s.psi; },
          area: false,
          dash: inst.dash,
          primary: true
        };
      });
    }

    var only = scoped[0];
    return [
      {
        key: only.id + ':psi',
        name: 'Now (10 s)',
        color: only.color,
        points: only.samples,
        value: function (s) { return s.psi; },
        area: true,
        dash: only.dash,
        primary: true
      },
      {
        key: only.id + ':avg',
        name: 'Last minute (state)',
        color: 'var(--series-avg)',
        points: only.samples,
        value: function (s) { return s.avgPsi; },
        area: false,
        dash: '5 4',
        primary: false
      }
    ];
  }

  function renderLegend(series) {
    var legend = $('#legend');
    clear(legend);

    series.forEach(function (s) {
      var item = document.createElement('li');
      var key = el('span', 'legend-key');
      key.style.background = s.color;
      if (s.dash) {
        key.classList.add('legend-key-dashed');
      }
      item.appendChild(key);
      item.appendChild(el('span', null, s.name));
      legend.appendChild(item);
    });
  }

  // End labels are mandatory once several lines share the plot; when lines
  // converge, nudge the labels apart and connect each to its line with a leader.
  function placeEndLabels(entries, top, bottom) {
    var minGap = 15;
    entries.sort(function (a, b) {
      return a.y - b.y;
    });
    entries.forEach(function (entry) {
      entry.labelY = entry.y;
    });
    for (var i = 1; i < entries.length; i++) {
      if (entries[i].labelY - entries[i - 1].labelY < minGap) {
        entries[i].labelY = entries[i - 1].labelY + minGap;
      }
    }
    var overflow = entries.length
      ? entries[entries.length - 1].labelY - bottom
      : 0;
    if (overflow > 0) {
      entries.forEach(function (entry) {
        entry.labelY -= overflow;
      });
    }
    entries.forEach(function (entry) {
      entry.labelY = Math.max(top, entry.labelY);
    });
    return entries;
  }

  // Load-test steps on screen. Every instance runs the same plan at the same
  // moment, so one track serves them all: each step spans from the first
  // sample any of them reported in it to the last.
  function loadSteps(scoped) {
    var byKey = {};
    var steps = [];
    scoped.forEach(function (inst) {
      inst.samples.forEach(function (s) {
        if (!s.test) {
          return;
        }
        var key = s.test.id + ':' + s.test.step;
        var step = byKey[key];
        if (!step) {
          step = byKey[key] = { mb: s.test.mb, t0: s.t, t1: s.t };
          steps.push(step);
        }
        step.t0 = Math.min(step.t0, s.t);
        step.t1 = Math.max(step.t1, s.t);
      });
    });
    return steps.sort(function (a, b) {
      return a.t0 - b.t0;
    });
  }

  function renderChart(scoped) {
    var svg = $('#chart');
    var wrap = $('#plot-wrap');
    var empty = $('#empty-state');
    var multi = scoped.length > 1;
    var series = buildSeries(scoped, multi);
    var steps = loadSteps(scoped);

    clear(svg);
    state.layout = null;
    hideTooltip();
    renderLegend(series);

    if (!series.length) {
      svg.setAttribute('hidden', '');
      empty.removeAttribute('hidden');
      svg.setAttribute('aria-label', 'Memory pressure chart. No samples in the selected range.');
      return;
    }

    empty.setAttribute('hidden', '');
    svg.removeAttribute('hidden');

    var width = Math.max(280, Math.round(wrap.clientWidth) || 720);
    var height = 344;
    var narrow = width < 560;
    var pad = {
      // Room above the plot for the load-test track when there is one.
      top: steps.length ? 46 : 22,
      right: narrow ? 52 : (multi ? 84 : 66),
      bottom: 30,
      left: narrow ? 44 : 54
    };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;

    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);

    var dataMax = 0;
    var dataMin = Infinity;
    var t0 = Infinity;
    var t1 = -Infinity;

    series.forEach(function (s) {
      s.points.forEach(function (p) {
        var v = s.value(p);
        dataMax = Math.max(dataMax, v);
        dataMin = Math.min(dataMin, v);
        t0 = Math.min(t0, p.t);
        t1 = Math.max(t1, p.t);
      });
    });
    if (!isFinite(dataMin)) {
      dataMin = 0;
    }

    // The thresholds are absolute - the same share of time on every size - so
    // they hold whether one instance is on screen or several.
    var th = thresholdsOf(scoped);
    var thresholds = [
      { value: th.degraded, label: 'Degraded from ' + th.degraded + '%',
        shortLabel: 'Degraded ' + th.degraded + '%', state: 'degraded' },
      { value: th.critical, label: 'Critical from ' + th.critical + '%',
        shortLabel: 'Critical ' + th.critical + '%', state: 'critical' }
    ];

    // The scale always reaches the degraded line, so a quiet machine reads as
    // what it is - flat, far below trouble - instead of having its noise blown
    // up to fill the plot. The critical line may widen it further, but never
    // past 2x the data: a far-away line must not squash the plot to the floor.
    var scaleMax = Math.max(dataMax, th.degraded);
    if (th.critical <= dataMax * 2) {
      scaleMax = Math.max(scaleMax, th.critical);
    }

    var scale = niceScale(scaleMax);
    var digits = decimalsFor(scale.step);
    // Ticks stay as coarse as the step; single values keep two places so a
    // small reading never rounds away to "0".
    var valueDigits = Math.max(2, digits);
    var span = t1 - t0;

    function x(t) {
      return span > 0 ? pad.left + ((t - t0) / span) * plotW : pad.left + plotW / 2;
    }

    function y(v) {
      return pad.top + plotH - (Math.min(v, scale.max) / scale.max) * plotH;
    }

    var gridGroup = svgEl('g', {});
    var dataGroup = svgEl('g', {});
    // Threshold rules sit under the data; their labels sit over it, so the
    // pressure line never draws through the text.
    var annotationGroup = svgEl('g', {});
    var annotationLabels = svgEl('g', {});
    var axisGroup = svgEl('g', {});

    // Gridlines and ticks: solid hairlines, one step off the surface.
    scale.ticks.forEach(function (value) {
      var ty = y(value);
      gridGroup.appendChild(svgEl('line', {
        x1: pad.left, x2: pad.left + plotW, y1: ty, y2: ty,
        stroke: value === 0 ? 'var(--axis)' : 'var(--gridline)',
        'stroke-width': 1
      }));
      var label = svgEl('text', {
        x: pad.left - 10, y: ty + 4,
        'text-anchor': 'end',
        fill: 'var(--text-muted)',
        'font-size': 11,
        'font-variant-numeric': 'tabular-nums'
      });
      label.textContent = value.toFixed(digits);
      gridGroup.appendChild(label);
    });

    var unit = svgEl('text', {
      x: pad.left, y: pad.top - 9,
      'text-anchor': 'start',
      fill: 'var(--text-muted)',
      'font-size': 11
    });
    unit.textContent = '% of time waiting for memory';
    gridGroup.appendChild(unit);

    // The bands, tinted faintly behind everything: where a line sits reads at
    // a glance, before any label is.
    function band(from, to, stateName) {
      if (from >= scale.max) {
        return;
      }
      var top = y(Math.min(to, scale.max));
      gridGroup.insertBefore(svgEl('rect', {
        x: pad.left, y: top, width: plotW, height: Math.max(0, y(from) - top),
        fill: stateName === 'critical' ? 'var(--status-critical)' : 'var(--status-warning)',
        'fill-opacity': 0.07
      }), gridGroup.firstChild);
    }
    band(th.degraded, th.critical, 'degraded');
    band(th.critical, 100, 'critical');

    // X axis: a handful of evenly spaced clock labels, never one per sample.
    // A zero-width span (one sample) gets exactly one, or they stack up.
    var slots = span > 0 ? Math.max(2, Math.min(6, Math.floor(plotW / 110))) : 1;
    var axisY = pad.top + plotH;
    axisGroup.appendChild(svgEl('line', {
      x1: pad.left, x2: pad.left + plotW, y1: axisY, y2: axisY,
      stroke: 'var(--axis)', 'stroke-width': 1
    }));
    for (var k = 0; k < slots; k++) {
      var ratio = slots === 1 ? 0.5 : k / (slots - 1);
      var tickT = t0 + span * ratio;
      var tickX = x(tickT);
      var anchor = k === 0 ? 'start' : k === slots - 1 ? 'end' : 'middle';
      var tickLabel = svgEl('text', {
        x: tickX, y: axisY + 18,
        'text-anchor': anchor,
        fill: 'var(--text-muted)',
        'font-size': 11,
        'font-variant-numeric': 'tabular-nums'
      });
      tickLabel.textContent = formatClock(tickT);
      axisGroup.appendChild(tickLabel);
    }

    // The load-test track: what the fleet was asked to hold, step by step, in
    // line with the pressure it caused. Darker means more memory.
    if (steps.length) {
      var peakMb = steps.reduce(function (acc, st) {
        return Math.max(acc, st.mb);
      }, 0) || 1;
      var trackY = 6;
      var trackH = 15;
      var trackLabel = svgEl('text', {
        x: pad.left - 8, y: trackY + 11,
        'text-anchor': 'end',
        fill: 'var(--text-muted)',
        'font-size': 10
      });
      trackLabel.textContent = narrow ? 'Load' : 'Test load';
      axisGroup.appendChild(trackLabel);

      steps.forEach(function (st, i) {
        var next = steps[i + 1];
        // A step runs until the next one starts; the last runs to its last sample.
        var end = next && next.t0 - st.t1 < 15000 ? next.t0 : st.t1;
        var x0 = x(Math.max(st.t0, t0));
        var x1 = x(Math.min(end, t1));
        if (x1 - x0 < 1) {
          x1 = x0 + 1;
        }
        axisGroup.appendChild(svgEl('rect', {
          x: x0, y: trackY, width: x1 - x0, height: trackH, rx: 2,
          fill: 'var(--load-track)',
          'fill-opacity': st.mb > 0 ? 0.25 + 0.6 * (st.mb / peakMb) : 0.08
        }));
        if (x1 - x0 >= 40) {
          var stepLabel = svgEl('text', {
            x: (x0 + x1) / 2, y: trackY + 11,
            'text-anchor': 'middle',
            fill: st.mb / peakMb > 0.55 ? 'var(--load-track-ink)' : 'var(--text-primary)',
            'font-size': 10,
            'font-weight': 600,
            'font-variant-numeric': 'tabular-nums'
          });
          stepLabel.textContent = st.mb > 0 ? formatMb(st.mb) : 'none';
          axisGroup.appendChild(stepLabel);
        }
      });
    }

    // Threshold annotations. The dashed hairline stays in annotation ink so it
    // is legible in both themes; severity rides the labelled status dot.
    var drawn = [];
    thresholds
      .slice()
      .sort(function (a, b) {
        return b.value - a.value;
      })
      .forEach(function (t) {
        if (t.value > scale.max) {
          return;
        }
        var ty = y(t.value);
        var collides = drawn.some(function (prev) {
          return Math.abs(prev - ty) < 16;
        });
        if (collides || axisY - ty < 12) {
          return;
        }
        drawn.push(ty);

        annotationGroup.appendChild(svgEl('line', {
          x1: pad.left, x2: pad.left + plotW, y1: ty, y2: ty,
          stroke: 'var(--text-muted)',
          'stroke-width': 1,
          'stroke-dasharray': '4 3'
        }));

        // Sit the label under its rule when there is no room above it.
        var below = ty - 14 < pad.top;
        var labelY = below ? ty + 14 : ty - 5;

        annotationLabels.appendChild(svgEl('circle', {
          cx: pad.left + 6, cy: labelY - 4, r: 3.5,
          fill: t.state === 'critical' ? 'var(--status-critical)' : 'var(--status-warning)',
          stroke: 'var(--surface-1)',
          'stroke-width': 1.5
        }));
        var annotation = svgEl('text', {
          x: pad.left + 15, y: labelY,
          fill: 'var(--text-secondary)',
          'font-size': 11,
          // A surface-coloured halo keeps the label readable where the line
          // passes behind it.
          stroke: 'var(--surface-1)',
          'stroke-width': 3,
          'stroke-linejoin': 'round',
          'paint-order': 'stroke fill'
        });
        annotation.textContent = narrow ? t.shortLabel : t.label;
        annotationLabels.appendChild(annotation);
      });

    var endEntries = [];

    series.forEach(function (s) {
      function pathFor(points) {
        return points
          .map(function (p, i) {
            return (i ? 'L' : 'M') + x(p.t).toFixed(2) + ' ' + y(s.value(p)).toFixed(2);
          })
          .join(' ');
      }

      segmentize(s.points).forEach(function (points) {
        if (points.length === 1) {
          dataGroup.appendChild(svgEl('circle', {
            cx: x(points[0].t), cy: y(s.value(points[0])), r: 3,
            fill: s.color, stroke: 'var(--surface-1)', 'stroke-width': 2
          }));
          return;
        }

        // Area wash under a lone primary series: a tint, never a saturated
        // block, and never stacked up under several overlapping lines.
        if (s.area) {
          var area = pathFor(points) +
            ' L' + x(points[points.length - 1].t).toFixed(2) + ' ' + (pad.top + plotH) +
            ' L' + x(points[0].t).toFixed(2) + ' ' + (pad.top + plotH) + ' Z';
          dataGroup.appendChild(svgEl('path', {
            d: area, fill: s.color, 'fill-opacity': 0.1, stroke: 'none'
          }));
        }

        var line = svgEl('path', {
          d: pathFor(points),
          fill: 'none',
          stroke: s.color,
          'stroke-width': 2,
          'stroke-linecap': 'round',
          'stroke-linejoin': 'round'
        });
        if (s.dash) {
          line.setAttribute('stroke-dasharray', s.dash);
        }
        dataGroup.appendChild(line);
      });

      var last = s.points[s.points.length - 1];
      if (!last) {
        return;
      }
      var lastX = x(last.t);
      var lastY = y(s.value(last));

      // End marker carries a 2px surface ring so it stays legible over the line.
      dataGroup.appendChild(svgEl('circle', {
        cx: lastX, cy: lastY, r: 4.5,
        fill: s.color,
        stroke: 'var(--surface-1)', 'stroke-width': 2
      }));

      if (s.primary) {
        endEntries.push({
          x: lastX,
          y: lastY,
          color: s.color,
          text: formatPsi(s.value(last), valueDigits)
        });
      }
    });

    // Direct labels at the line ends — selective by construction: one per
    // series, never one per point.
    placeEndLabels(endEntries, pad.top + 6, pad.top + plotH);
    endEntries.forEach(function (entry) {
      var labelX = Math.min(entry.x + 10, width - 4);
      if (Math.abs(entry.labelY - entry.y) > 2) {
        dataGroup.appendChild(svgEl('path', {
          d: 'M' + entry.x.toFixed(2) + ' ' + entry.y.toFixed(2) +
             ' L' + (labelX - 4).toFixed(2) + ' ' + entry.labelY.toFixed(2),
          fill: 'none',
          stroke: entry.color,
          'stroke-width': 1,
          'stroke-opacity': 0.5
        }));
      }
      var label = svgEl('text', {
        x: labelX,
        y: entry.labelY + 4,
        'text-anchor': 'start',
        fill: 'var(--text-primary)',
        'font-size': 12,
        'font-weight': 600,
        'font-variant-numeric': 'tabular-nums',
        stroke: 'var(--surface-1)',
        'stroke-width': 3,
        'stroke-linejoin': 'round',
        'paint-order': 'stroke fill'
      });
      label.textContent = entry.text;
      dataGroup.appendChild(label);
    });

    // Every distinct sample time on screen: the crosshair snaps to these, so a
    // reader aims at a moment rather than at any one line.
    var stops = [];
    var seenStop = {};
    series.forEach(function (s) {
      s.points.forEach(function (p) {
        if (!seenStop[p.t]) {
          seenStop[p.t] = true;
          stops.push(p.t);
        }
      });
    });
    stops.sort(function (a, b) {
      return a - b;
    });

    var tolerance = Math.max(
      series.reduce(function (acc, s) {
        return Math.max(acc, medianInterval(s.points));
      }, 0) * 1.5,
      1000
    );

    var crosshair = svgEl('g', { visibility: 'hidden' });
    crosshair.appendChild(svgEl('line', {
      x1: 0, x2: 0, y1: pad.top, y2: pad.top + plotH,
      stroke: 'var(--axis)', 'stroke-width': 1
    }));
    series.forEach(function (s) {
      crosshair.appendChild(svgEl('circle', {
        cx: 0, cy: 0, r: 4,
        fill: s.color,
        stroke: 'var(--surface-1)', 'stroke-width': 2
      }));
    });

    // The whole plot is the hit target: readers aim at a time, not at a 2px line.
    var overlay = svgEl('rect', {
      x: pad.left, y: pad.top, width: plotW, height: plotH,
      fill: 'transparent'
    });

    svg.appendChild(gridGroup);
    svg.appendChild(annotationGroup);
    svg.appendChild(dataGroup);
    svg.appendChild(annotationLabels);
    svg.appendChild(axisGroup);
    svg.appendChild(crosshair);
    svg.appendChild(overlay);

    state.layout = {
      series: series, stops: stops, tolerance: tolerance,
      x: x, y: y, pad: pad, plotW: plotW, plotH: plotH,
      width: width, digits: valueDigits, multi: multi,
      crosshair: crosshair, overlay: overlay
    };

    overlay.addEventListener('pointermove', onPointerMove);
    overlay.addEventListener('pointerdown', onPointerMove);
    overlay.addEventListener('pointerleave', function () {
      if (!state.activeFromKeyboard) {
        setActiveIndex(null);
      }
    });

    svg.setAttribute(
      'aria-label',
      (multi
        ? 'Memory pressure over time for ' + series.length + ' instances: ' +
          series.map(function (s) { return s.name; }).join(', ') + '. '
        : 'Memory pressure over time for ' + scoped[0].label + '. ') +
      stops.length + ' sample times from ' + formatClock(t0) + ' to ' + formatClock(t1) +
      ', values from ' + formatPsi(dataMin, valueDigits) + ' to ' +
      formatPsi(dataMax, valueDigits) +
      '. Use the arrow keys to read individual samples, or switch to the table view.'
    );

    if (state.activeIndex !== null) {
      setActiveIndex(Math.min(state.activeIndex, stops.length - 1));
    }
  }

  /* ---------- hover & crosshair ---------- */

  function nearestAt(points, t, tolerance) {
    var best = null;
    var bestDist = Infinity;
    for (var i = 0; i < points.length; i++) {
      var dist = Math.abs(points[i].t - t);
      if (dist < bestDist) {
        bestDist = dist;
        best = points[i];
      }
    }
    return bestDist <= tolerance ? best : null;
  }

  function nearestIndex(clientX) {
    var layout = state.layout;
    var rect = $('#chart').getBoundingClientRect();
    var scaleX = rect.width ? layout.width / rect.width : 1;
    var localX = (clientX - rect.left) * scaleX;

    var best = 0;
    var bestDist = Infinity;
    for (var i = 0; i < layout.stops.length; i++) {
      var dist = Math.abs(layout.x(layout.stops[i]) - localX);
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    }
    return best;
  }

  function onPointerMove(event) {
    if (!state.layout) {
      return;
    }
    state.activeFromKeyboard = false;
    setActiveIndex(nearestIndex(event.clientX));
  }

  function setActiveIndex(index) {
    var layout = state.layout;
    state.activeIndex = index;

    if (!layout) {
      return;
    }
    if (index === null || layout.stops[index] === undefined) {
      layout.crosshair.setAttribute('visibility', 'hidden');
      hideTooltip();
      return;
    }

    var t = layout.stops[index];
    var cx = layout.x(t);
    var nodes = layout.crosshair.childNodes;
    nodes[0].setAttribute('x1', cx);
    nodes[0].setAttribute('x2', cx);

    var readings = layout.series.map(function (s, i) {
      var point = nearestAt(s.points, t, layout.tolerance);
      var dot = nodes[i + 1];
      if (point) {
        dot.setAttribute('cx', layout.x(point.t));
        dot.setAttribute('cy', layout.y(s.value(point)));
        dot.setAttribute('visibility', 'visible');
      } else {
        dot.setAttribute('visibility', 'hidden');
      }
      return { series: s, point: point };
    });

    layout.crosshair.setAttribute('visibility', 'visible');
    showTooltip(t, readings, cx);

    if (state.activeFromKeyboard) {
      $('#chart-readout').textContent = formatClock(t) + '. ' +
        readings.map(function (r) {
          return r.series.name + ' ' +
            (r.point ? formatPsi(r.series.value(r.point), layout.digits) : 'no sample');
        }).join(', ') + '.';
    }
  }

  function hideTooltip() {
    var tip = $('#tooltip');
    if (tip) {
      tip.setAttribute('hidden', '');
    }
  }

  // Everything here is built with textContent: `reason` and `state` come off
  // the wire and are never trusted as markup.
  function showTooltip(t, readings, cx) {
    var tip = $('#tooltip');
    var layout = state.layout;
    clear(tip);

    tip.appendChild(el('p', 'tt-time', formatClock(t)));

    readings.forEach(function (reading) {
      var line = el('div', 'tt-row');
      var key = el('span', 'tt-key');
      key.style.background = reading.series.color;
      if (reading.series.dash) {
        key.classList.add('legend-key-dashed');
      }
      line.appendChild(key);

      var value = el('span', 'tt-value',
        reading.point ? formatPsi(reading.series.value(reading.point), layout.digits) : '—');
      line.appendChild(value);
      line.appendChild(el('span', 'tt-name', reading.series.name));
      tip.appendChild(line);
    });

    // Comparing instances, the states differ per line, so they ride the rows
    // above rather than a single footer.
    var anchor = null;
    for (var i = 0; i < readings.length; i++) {
      if (readings[i].point) {
        anchor = readings[i].point;
        break;
      }
    }

    if (anchor && !layout.multi) {
      if (anchor.psiFull !== null) {
        tip.appendChild(el('p', 'tt-extra',
          'Whole machine stalled ' + formatPct(anchor.psiFull) + '%'));
      }

      var stateRow = el('p', 'tt-state');
      var dot = el('span', 'state-dot');
      dot.setAttribute('data-state', stateKey(anchor.state));
      stateRow.appendChild(dot);
      stateRow.appendChild(el('span', null, anchor.state));
      tip.appendChild(stateRow);

      if (anchor.reason) {
        tip.appendChild(el('p', 'tt-reason', anchor.reason));
      }
    } else if (anchor) {
      var worst = readings.reduce(function (acc, r) {
        if (!r.point) {
          return acc;
        }
        var rank = { critical: 3, degraded: 2, healthy: 1, unknown: 0 }[stateKey(r.point.state)];
        return rank > acc.rank ? { rank: rank, reading: r } : acc;
      }, { rank: -1, reading: null });

      if (worst.reading) {
        var row = el('p', 'tt-state');
        var worstDot = el('span', 'state-dot');
        worstDot.setAttribute('data-state', stateKey(worst.reading.point.state));
        row.appendChild(worstDot);
        row.appendChild(el('span', null,
          worst.reading.point.state + ' · ' + worst.reading.series.name));
        tip.appendChild(row);
      }
    }

    var tested = null;
    for (var j = 0; j < readings.length && !tested; j++) {
      tested = readings[j].point && readings[j].point.test;
    }
    if (tested) {
      tip.appendChild(el('p', 'tt-extra', 'Test load ' + formatMb(tested.mb) +
        (tested.steps > 1 ? ' · step ' + tested.step + ' of ' + tested.steps : '')));
    }

    tip.removeAttribute('hidden');

    var wrapWidth = $('#plot-wrap').clientWidth || layout.width;
    var pxPerUnit = wrapWidth / layout.width;
    var anchorX = cx * pxPerUnit;
    var tipWidth = tip.offsetWidth;
    var left = anchorX + 14;
    if (left + tipWidth > wrapWidth) {
      left = anchorX - tipWidth - 14;
    }
    tip.style.left = Math.max(0, left) + 'px';
    tip.style.top = layout.pad.top + 'px';
  }

  function onChartKeydown(event) {
    var layout = state.layout;
    if (!layout || !layout.stops.length) {
      return;
    }
    var last = layout.stops.length - 1;
    var index = state.activeIndex === null ? last : state.activeIndex;
    var next = null;

    if (event.key === 'ArrowRight') {
      next = Math.min(last, index + 1);
    } else if (event.key === 'ArrowLeft') {
      next = Math.max(0, index - 1);
    } else if (event.key === 'Home') {
      next = 0;
    } else if (event.key === 'End') {
      next = last;
    } else if (event.key === 'Escape') {
      state.activeFromKeyboard = false;
      setActiveIndex(null);
      return;
    } else {
      return;
    }

    event.preventDefault();
    state.activeFromKeyboard = true;
    setActiveIndex(next);
  }

  /* ---------- summary ---------- */

  function testProgress(test, sample) {
    var parts = [];
    if (test.steps > 1) {
      parts.push('Step ' + test.step + ' of ' + test.steps);
    }
    if (test.remaining !== null) {
      var left = test.remaining - (Date.now() - sample.t) / 1000;
      parts.push(formatDuration(Math.max(0, left)) + ' left');
    }
    if (test.killed) {
      parts.push('test killed by the kernel ' + test.killed + '×');
    }
    return parts.join(' · ');
  }

  // A test the instance is under right now - not one it reported before it
  // went quiet.
  function currentTest(inst) {
    var latest = inst.all[inst.all.length - 1];
    return latest && inst.online && latest.test ? latest.test : null;
  }

  function renderSingleSummary(inst) {
    var latest = inst ? inst.all[inst.all.length - 1] : null;
    var key = latest ? stateKey(latest.state) : 'unknown';
    var th = inst ? thresholdsOf([inst]) : DEFAULT_THRESHOLDS;
    // The headline is the minute the state is judged on, so the number and
    // the badge beside it can never disagree.
    var minute = latest ? latest.avgPsi : null;

    $('#hero-scope').textContent = inst ? inst.label : 'No instance reporting';
    $('#hero-presence').setAttribute('data-online', inst && inst.online ? 'true' : 'false');
    $('#hero-presence').hidden = !inst;
    $('#hero-psi').textContent = latest ? formatPct(minute) : '—';
    renderScale($('#hero-scale'), minute, th, false);
    $('#status-text').textContent = latest ? latest.state : 'No data';
    $('#status-badge').setAttribute('data-state', key);

    if (latest) {
      var said = describeState(key, minute);
      // Over a line but not yet for long enough to change the state.
      if (key === 'healthy' && minute >= th.degraded) {
        said = {
          meaning: 'Pressure has just crossed the ' + bandName(minute, th) + ' line. ' +
                   'The state changes if it stays there for 15 seconds.',
          action: 'Watch it.'
        };
      }
      $('#hero-meaning').textContent = (describeWait(minute) + ' ' + said.meaning).trim();
      $('#hero-action').textContent = said.action ? 'What to do: ' + said.action : '';
      // The analyser's own words, only where they add something the lines
      // above do not: a state still being confirmed, the first minute, or a
      // whole-machine stall that the headline number does not show.
      $('#hero-reason').textContent = /confirming|collecting|whole machine/i.test(latest.reason)
        ? latest.reason
        : '';
    } else {
      $('#hero-meaning').textContent = 'Waiting for the SysHealth agent to push its first sample.';
      $('#hero-action').textContent = '';
      $('#hero-reason').textContent = '';
    }

    $('#tile-now').textContent = latest ? formatPct(latest.psi) + '%' : '—';
    $('#tile-full').textContent = latest && latest.psiFull !== null
      ? formatPct(latest.psiFull) + '%'
      : '—';
    $('#tile-full-note').textContent = 'Every program waiting at once, so no work got done. ' +
      'Critical from ' + th.critical_full + '% over a minute.';

    var test = inst ? currentTest(inst) : null;
    $('#tile-test').textContent = test ? formatMb(test.mb) : 'None';
    $('#tile-test-note').textContent = test
      ? testProgress(test, latest) || 'Running'
      : 'No test running';
    $('#tile-scan').textContent = latest ? formatCount(latest.scan) : '—';
  }

  function renderCompareSummary(scoped) {
    var grid = $('#overview-compare');
    clear(grid);

    scoped.forEach(function (inst) {
      var latest = inst.all[inst.all.length - 1];
      var th = thresholdsOf([inst]);
      var mem = memOf(inst);
      var peak = inst.samples.reduce(function (acc, s) {
        return Math.max(acc, s.psi);
      }, 0);

      var tile = el('div', 'cmp-tile');

      var head = el('div', 'cmp-head');
      var key = el('span', 'legend-key');
      key.style.background = inst.color;
      head.appendChild(key);
      head.appendChild(el('span', 'cmp-type', inst.label));

      var presence = el('span', 'presence');
      presence.setAttribute('data-online', inst.online ? 'true' : 'false');
      presence.title = inst.online ? 'Reporting' : 'Not reporting';
      head.appendChild(presence);
      tile.appendChild(head);

      tile.appendChild(el('p', 'cmp-host',
        inst.hostname + (mem ? ' · ' + formatMb(mem) + ' RAM' : '')));

      var value = el('p', 'cmp-value', formatPct(latest.avgPsi));
      value.appendChild(el('span', 'cmp-pct', '%'));
      tile.appendChild(value);
      tile.appendChild(el('p', 'cmp-unit', 'waiting for memory, last minute'));

      var scale = el('div', 'band-scale band-scale-compact');
      renderScale(scale, latest.avgPsi, th, true);
      tile.appendChild(scale);

      var stateRow = el('p', 'cmp-state');
      var dot = el('span', 'state-dot');
      dot.setAttribute('data-state', stateKey(latest.state));
      stateRow.appendChild(dot);
      stateRow.appendChild(el('span', null, latest.state));
      tile.appendChild(stateRow);

      var note = 'now ' + formatPct(latest.psi) + '% · peak ' + formatPct(peak) + '% in range';
      if (!inst.online && inst.secondsSince !== null) {
        note += ' · last seen ' + formatAgo(inst.secondsSince * 1000);
      }
      tile.appendChild(el('p', 'cmp-note', note));

      var test = currentTest(inst);
      if (test) {
        tile.appendChild(el('p', 'cmp-note cmp-test',
          'Holding ' + formatMb(test.mb) + ' · ' + (testProgress(test, latest) || 'running')));
      }

      grid.appendChild(tile);
    });
  }

  function renderTable(scoped, multi) {
    var body = $('#table-body');
    clear(body);

    $('#table-instance-head').hidden = !multi;

    var rows = [];
    scoped.forEach(function (inst) {
      inst.samples.forEach(function (sample) {
        rows.push({ inst: inst, sample: sample });
      });
    });
    rows.sort(function (a, b) {
      return b.sample.t - a.sample.t;
    });

    rows.forEach(function (row) {
      var sample = row.sample;
      var tr = document.createElement('tr');

      function cell(text, className) {
        var td = el('td', className, text);
        tr.appendChild(td);
      }

      cell(formatClock(sample.t));

      if (multi) {
        var instTd = document.createElement('td');
        var wrapper = el('span', 'state-cell');
        var key = el('span', 'legend-key');
        key.style.background = row.inst.color;
        wrapper.appendChild(key);
        wrapper.appendChild(el('span', null, row.inst.label));
        instTd.appendChild(wrapper);
        tr.appendChild(instTd);
      }

      cell(formatPct(sample.psi) + '%', 'num');
      cell(formatPct(sample.avgPsi) + '%', 'num');
      cell(sample.psiFull === null ? '—' : formatPct(sample.psiFull) + '%', 'num');

      var stateTd = document.createElement('td');
      var stateWrap = el('span', 'state-cell');
      var stateDot = el('span', 'state-dot');
      stateDot.setAttribute('data-state', stateKey(sample.state));
      stateWrap.appendChild(stateDot);
      stateWrap.appendChild(el('span', null, sample.state));
      stateTd.appendChild(stateWrap);
      tr.appendChild(stateTd);

      cell(sample.test ? formatMb(sample.test.mb) : '—', 'num');
      cell(formatCount(sample.scan), 'num');
      cell(sample.reason || '—', 'reason');

      body.appendChild(tr);
    });
  }

  function renderFeedStatus() {
    var feed = $('#feed-status');
    var newest = latestTime();
    if (newest === null) {
      feed.textContent = 'No samples received yet';
      return;
    }
    var age = Date.now() - newest;
    var stale = age > STALE_AFTER_MS;
    feed.textContent = (stale ? 'Last sample ' : 'Updated ') + formatAgo(age);
    feed.setAttribute('data-stale', stale ? 'true' : 'false');
  }

  function renderInstanceControl() {
    var group = $('#instance-control');
    var withData = state.instances.filter(function (inst) {
      return inst.samples.length;
    });

    // Fall back to comparing when the selected instance stops existing.
    if (state.selected !== ALL && !withData.some(function (inst) {
      return inst.id === state.selected;
    })) {
      state.selected = ALL;
    }

    clear(group);
    $('#instance-filter').hidden = withData.length < 2;
    if (withData.length < 2) {
      return;
    }

    function button(value, text, color) {
      var node = el('button', null);
      node.type = 'button';
      node.setAttribute('role', 'radio');
      node.setAttribute('data-instance', value);
      var selected = state.selected === value;
      node.setAttribute('aria-checked', selected ? 'true' : 'false');
      if (selected) {
        node.classList.add('is-selected');
      }
      if (color) {
        var key = el('span', 'legend-key');
        key.style.background = color;
        node.appendChild(key);
      }
      node.appendChild(el('span', null, text));
      group.appendChild(node);
    }

    button(ALL, 'Compare all', null);
    withData.forEach(function (inst) {
      button(inst.id, inst.label, inst.color);
    });
  }

  function renderAll() {
    var scoped = scopedInstances();
    var multi = scoped.length > 1;

    renderInstanceControl();
    renderFeedStatus();

    $('#overview-single').hidden = multi;
    $('#overview-compare').hidden = !multi;

    if (multi) {
      renderCompareSummary(scoped);
    } else {
      renderSingleSummary(scoped[0] || null);
    }

    renderTable(scoped, multi);

    var sampleCount = scoped.reduce(function (acc, inst) {
      return acc + inst.samples.length;
    }, 0);

    $('#card-title').textContent = multi
      ? 'Memory pressure by instance'
      : 'Memory pressure over time';
    var th = thresholdsOf(scoped);
    var peak = scoped.reduce(function (acc, inst) {
      return inst.samples.reduce(function (m, s) {
        return Math.max(m, s.psi);
      }, acc);
    }, 0);
    var subtitle = multi
      ? sampleCount + ' samples across ' + scoped.length +
        ' instances, every one judged against the same lines.'
      : (sampleCount ? sampleCount + ' samples, one every 5 s. ' : '') +
        'The grey dashed line is the minute the state is judged on.';
    if (sampleCount && peak < th.degraded) {
      subtitle += ' Highest reading ' + formatPct(peak) + '%, under the ' +
        th.degraded + '% degraded line.';
    }
    $('#chart-subtitle').textContent = subtitle;

    renderScaleGuide(thresholdsOf(state.instances));
    renderLoadTest();

    if (state.view === 'chart') {
      renderChart(scoped);
    }
  }

  function setView(view) {
    state.view = view;
    var chartCard = $('#chart-card');
    var tableCard = $('#table-card');

    if (view === 'table') {
      chartCard.setAttribute('hidden', '');
      tableCard.removeAttribute('hidden');
    } else {
      tableCard.setAttribute('hidden', '');
      chartCard.removeAttribute('hidden');
      renderChart(scopedInstances());
    }
  }

  function wireSegmented(selector, onPick) {
    var group = $(selector);
    group.addEventListener('click', function (event) {
      var button = event.target.closest('button');
      if (!button || button.classList.contains('is-selected')) {
        return;
      }
      Array.prototype.forEach.call(group.querySelectorAll('button'), function (b) {
        var selected = b === button;
        b.classList.toggle('is-selected', selected);
        b.setAttribute('aria-checked', selected ? 'true' : 'false');
      });
      onPick(button);
    });
  }

  function wireTheme() {
    var button = $('#theme-toggle');
    var label = $('#theme-label');
    var modes = ['auto', 'light', 'dark'];
    var stored = null;

    try {
      stored = window.localStorage.getItem('syshealth-theme');
    } catch (err) {
      stored = null;
    }

    var mode = modes.indexOf(stored) >= 0 ? stored : 'auto';

    function apply() {
      if (mode === 'auto') {
        document.documentElement.removeAttribute('data-theme');
      } else {
        document.documentElement.setAttribute('data-theme', mode);
      }
      label.textContent = mode.charAt(0).toUpperCase() + mode.slice(1);
      try {
        window.localStorage.setItem('syshealth-theme', mode);
      } catch (err) {
        /* storage unavailable — the toggle still works for this page load */
      }
    }

    button.addEventListener('click', function () {
      mode = modes[(modes.indexOf(mode) + 1) % modes.length];
      apply();
      if (state.view === 'chart') {
        renderChart(scopedInstances());
      }
    });

    apply();
  }

  /* ---------- polling ---------- */

  function load() {
    var scoped = $('#scoped');
    scoped.classList.add('is-loading');

    return fetch('series?limit=' + HISTORY_LIMIT, { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('HTTP ' + response.status);
        }
        return response.json();
      })
      // Only the fetch is caught here. Rendering runs after, so a bug in a
      // render path surfaces as itself instead of being reported as a dead
      // server.
      .then(
        function (payload) { return { ok: true, payload: payload }; },
        function () { return { ok: false }; }
      )
      .then(function (result) {
        scoped.classList.remove('is-loading');

        if (!result.ok) {
          $('#feed-status').textContent = 'Server unreachable — retrying';
          return;
        }

        var rows = result.payload && Array.isArray(result.payload.instances)
          ? result.payload.instances
          : [];
        state.instances = rows.map(function (row) {
          return {
            // Hostname is the server's key, so it is the stable identity here.
            id: String(row.hostname),
            hostname: String(row.hostname),
            type: String(row.instance_type || 'unknown'),
            online: row.online !== false,
            secondsSince: num(row.seconds_since),
            color: instanceColor(row.instance_type),
            samples: (Array.isArray(row.samples) ? row.samples : [])
              .map(normalize)
              .filter(Boolean)
              .sort(function (a, b) {
                return a.t - b.t;
              })
          };
        });
        labelInstances(state.instances);
        renderAll();
      });
  }

  /* ---------- load test ---------- */

  function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().then(
        function (data) { return { ok: response.ok, status: response.status, data: data }; },
        function () { return { ok: false, status: response.status, data: {} }; }
      );
    });
  }

  function onlineInstances() {
    return state.instances.filter(function (inst) {
      return inst.online && inst.samples.length;
    });
  }

  // Instances under a test right now.
  function runningTests() {
    var now = Date.now();
    var stopped = state.stopped && now - state.stopped.at < REPORT_LAG_MS ? state.stopped : null;
    return onlineInstances()
      .map(function (inst) {
        return { inst: inst, test: currentTest({ all: inst.samples, online: true }) };
      })
      .filter(function (r) {
        // Stopped before any agent had reported it: no id to match, so hide
        // whatever test turns up until the agents have caught up.
        return r.test && !(stopped && (stopped.all || stopped.ids[r.test.id]));
      });
  }

  // Started from this page, not reported by any agent yet.
  function startPending(running) {
    if (running.length) {
      state.startedAt = null;
    }
    return state.startedAt !== null && Date.now() - state.startedAt < REPORT_LAG_MS;
  }

  // What the form asks for, as the server's build_plan expects it. Values go
  // as typed; the server is the one place that decides what is valid.
  function formSpec() {
    var form = $('#lt-form');
    function value(name) {
      return form.elements[name].value.trim();
    }
    var spec = { mode: state.testMode, workers: value('workers') };
    if (state.testMode === 'peak') {
      spec.memory_mb = value('memory_mb');
      spec.duration_sec = value('duration_sec');
    } else {
      spec.pattern = state.testPattern;
      spec.low_mb = value('low_mb');
      spec.high_mb = value('high_mb');
      spec.steps = value('steps');
      spec.step_sec = value('step_sec');
    }
    return spec;
  }

  function syncFormMode() {
    var variable = state.testMode === 'variable';
    Array.prototype.forEach.call(document.querySelectorAll('.lt-fields[data-for]'), function (node) {
      node.hidden = node.getAttribute('data-for') !== state.testMode;
    });
    $('#lt-mode-help').textContent = MODE_HELP[variable ? state.testPattern : 'peak'];
    var bursts = variable && state.testPattern === 'bursts';
    $('#lt-steps-label').textContent = bursts ? 'Bursts' : 'Steps';
    $('#lt-steps-note').textContent = bursts
      ? 'How many spikes'
      : (state.testPattern === 'wave' ? 'Levels on the way up' : '2 to 20');
  }

  var previewTimer = null;
  var previewSeq = 0;

  function previewSoon() {
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(previewNow, 250);
  }

  function previewNow() {
    var seq = ++previewSeq;
    postJson('load-plan', formSpec())
      .then(function (result) {
        // A slower, older answer must not overwrite a newer one.
        if (seq !== previewSeq) {
          return;
        }
        if (result.ok) {
          state.preview = result.data;
          $('#lt-error').hidden = true;
        } else {
          state.preview = null;
          $('#lt-error').textContent = result.data.error || 'This test cannot be run.';
          $('#lt-error').hidden = false;
        }
        renderPreview();
        renderLoadTest();
      })
      .catch(function () {
        if (seq === previewSeq) {
          state.preview = null;
          $('#lt-error').textContent = 'Could not reach the server to check this test.';
          $('#lt-error').hidden = false;
          renderPreview();
          renderLoadTest();
        }
      });
  }

  // The plan, drawn: memory held over time, with each instance's RAM as a
  // line, so "will this fit?" is answered before anything runs.
  function renderPreview() {
    var svg = $('#lt-chart');
    var preview = state.preview;
    clear(svg);
    clear($('#lt-fit'));
    $('#lt-plot').hidden = !preview;
    $('#lt-summary').textContent = preview
      ? preview.plan.summary + ' · ' + formatDuration(preview.plan.total_sec) + ' in all'
      : '';
    $('#lt-commands').textContent = preview ? preview.commands.join('\n') : '';
    if (!preview) {
      return;
    }

    var plan = preview.plan;
    var width = Math.max(260, Math.round($('#lt-plot').clientWidth) || 600);
    var height = 132;
    var pad = { top: 14, right: 12, bottom: 22, left: 58 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);

    var mems = onlineInstances().map(function (inst) {
      return { inst: inst, mb: memOf(inst) };
    }).filter(function (m) {
      return m.mb;
    });

    // Tall enough for the plan, and for any RAM line close above it - the
    // line that matters most is the first size the plan fits under.
    var top = plan.peak_mb * 1.15;
    mems.forEach(function (m) {
      if (m.mb > top && m.mb <= plan.peak_mb * 1.6) {
        top = m.mb * 1.08;
      }
    });

    function x(sec) {
      return pad.left + (sec / plan.total_sec) * plotW;
    }
    function y(mb) {
      return pad.top + plotH - (Math.min(mb, top) / top) * plotH;
    }

    svg.appendChild(svgEl('line', {
      x1: pad.left, x2: pad.left + plotW, y1: y(0), y2: y(0),
      stroke: 'var(--axis)', 'stroke-width': 1
    }));

    mems.forEach(function (m) {
      if (m.mb > top) {
        return;
      }
      svg.appendChild(svgEl('line', {
        x1: pad.left, x2: pad.left + plotW, y1: y(m.mb), y2: y(m.mb),
        stroke: m.inst.color, 'stroke-width': 1, 'stroke-dasharray': '4 3'
      }));
      var label = svgEl('text', {
        x: pad.left + plotW - 2, y: y(m.mb) - 4,
        'text-anchor': 'end', fill: 'var(--text-secondary)', 'font-size': 10,
        stroke: 'var(--surface-1)', 'stroke-width': 3, 'paint-order': 'stroke fill'
      });
      label.textContent = m.inst.label + ' RAM';
      svg.appendChild(label);
    });

    var d = '';
    var at = 0;
    plan.phases.forEach(function (phase, i) {
      d += (i ? ' L' : 'M') + x(at).toFixed(1) + ' ' + y(phase.mb).toFixed(1);
      at += phase.seconds;
      d += ' L' + x(at).toFixed(1) + ' ' + y(phase.mb).toFixed(1);
    });
    var area = d + ' L' + x(at).toFixed(1) + ' ' + y(0) + ' L' + x(0) + ' ' + y(0) + ' Z';
    svg.appendChild(svgEl('path', { d: area, fill: 'var(--load-track)', 'fill-opacity': 0.18 }));
    svg.appendChild(svgEl('path', {
      d: d, fill: 'none', stroke: 'var(--load-track)', 'stroke-width': 2,
      'stroke-linejoin': 'round'
    }));

    [[plan.peak_mb, formatMb(plan.peak_mb)], [0, '0']].forEach(function (tick) {
      var t = svgEl('text', {
        x: pad.left - 8, y: y(tick[0]) + 4, 'text-anchor': 'end',
        fill: 'var(--text-muted)', 'font-size': 10, 'font-variant-numeric': 'tabular-nums'
      });
      t.textContent = tick[1];
      svg.appendChild(t);
    });
    [[0, 'start', '0'], [plan.total_sec, 'end', formatDuration(plan.total_sec)]].forEach(function (tick) {
      var t = svgEl('text', {
        x: x(tick[0]), y: height - 6, 'text-anchor': tick[1],
        fill: 'var(--text-muted)', 'font-size': 10, 'font-variant-numeric': 'tabular-nums'
      });
      t.textContent = tick[2];
      svg.appendChild(t);
    });

    svg.setAttribute('aria-label', 'Planned load: ' + plan.summary + '. ' +
      mems.map(function (m) {
        return m.inst.label + ' has ' + formatMb(m.mb) + ' of RAM';
      }).join(', ') + '.');

    // The same comparison in words: the peak as a share of each size's RAM.
    // The OS keeps a few hundred MB of that for itself, so 80% is already tight.
    mems.forEach(function (m) {
      var share = plan.peak_mb / m.mb;
      var item = el('li', 'lt-fit-item');
      var key = el('span', 'legend-key');
      key.style.background = m.inst.color;
      item.appendChild(key);
      item.appendChild(el('strong', null, m.inst.label));
      item.appendChild(el('span', null, ' ' + formatMb(m.mb) + ' RAM · peak is ' +
        Math.round(share * 100) + '% of it · ' +
        (share >= 1 ? 'more than it has: expect heavy swapping or the OOM killer'
          : share >= 0.8 ? 'tight once the OS takes its share'
          : share >= 0.5 ? 'should fit' : 'plenty of room')));
      item.setAttribute('data-fit', share >= 1 ? 'over' : share >= 0.8 ? 'tight' : 'ok');
      $('#lt-fit').appendChild(item);
    });
  }

  // How each instance coped with one test, read back from its samples.
  function testResults(testId) {
    var info = null;
    var rows = [];
    var running = false;

    state.instances.forEach(function (inst) {
      var during = inst.samples.filter(function (s) {
        return s.test && s.test.id === testId;
      });
      if (!during.length) {
        return;
      }
      info = during[during.length - 1].test;
      var th = thresholdsOf([inst]);
      var start = during[0].t;
      var end = during[during.length - 1].t;
      if (currentTest({ all: inst.samples, online: inst.online }) &&
          currentTest({ all: inst.samples, online: inst.online }).id === testId) {
        running = true;
      }

      // The state is judged on a minute and must hold for 15 s, so it trails
      // the load: look a minute past the end for the worst of it.
      var worstRank = 0;
      var worstState = 'UNKNOWN';
      var worstMinute = 0;
      inst.samples.forEach(function (s) {
        if (s.t < start || s.t > end + 60000) {
          return;
        }
        var rank = STATE_RANK[stateKey(s.state)];
        if (rank > worstRank) {
          worstRank = rank;
          worstState = s.state;
        }
        worstMinute = Math.max(worstMinute, s.avgPsi);
      });

      var worst10 = 0;
      var killed = 0;
      var byStep = {};
      during.forEach(function (s) {
        worst10 = Math.max(worst10, s.psi);
        killed = Math.max(killed, s.test.killed);
        (byStep[s.test.step] = byStep[s.test.step] || []).push(s);
      });

      // Sustained pressure at each load level: the mean of a step's readings,
      // so one spike does not condemn a level any more than it changes a
      // state. Each step's first 10 s are skipped - the 10-second reading
      // still carries the step before it, which on the way down would blame a
      // low level for a high one's pressure. A level met twice (a wave, or
      // bursts) is judged by its worse visit.
      var levels = {};
      Object.keys(byStep).forEach(function (step) {
        var samples = byStep[step];
        var settled = samples.filter(function (s) {
          return s.t >= samples[0].t + 10000;
        });
        if (!settled.length) {
          settled = samples;
        }
        var mb = samples[0].test.mb;
        var level = levels[mb] = levels[mb] || { mb: mb, some: 0, full: 0 };
        var some = 0;
        var full = 0;
        settled.forEach(function (s) {
          some += s.psi;
          full += s.psiFull || 0;
        });
        level.some = Math.max(level.some, some / settled.length);
        level.full = Math.max(level.full, full / settled.length);
      });
      var ordered = Object.keys(levels).map(function (k) {
        return levels[k];
      }).sort(function (a, b) {
        return a.mb - b.mb;
      });

      var fineUpTo = null;
      var degradedFrom = null;
      var criticalFrom = null;
      var stillFine = true;
      ordered.forEach(function (level) {
        var critical = level.some >= th.critical || level.full >= th.critical_full;
        var degraded = level.some >= th.degraded;
        if (critical && criticalFrom === null) {
          criticalFrom = level.mb;
        }
        if ((degraded || critical) && degradedFrom === null) {
          degradedFrom = level.mb;
        }
        if (degraded || critical) {
          stillFine = false;
        } else if (stillFine) {
          fineUpTo = level.mb;
        }
      });

      // How bad it got: the worse of the states it reported and the
      // sustained pressure at its worst load level. Both count - with short
      // steps the one-minute state never catches up with a level that was
      // plainly too much.
      var levelRank = criticalFrom !== null ? 3 : degradedFrom !== null ? 2 : ordered.length ? 1 : 0;

      rows.push({
        inst: inst,
        rank: Math.max(STATE_RANK[stateKey(worstState)], levelRank),
        worstState: worstState,
        worstMinute: worstMinute,
        worst10: worst10,
        killed: killed,
        fineUpTo: fineUpTo,
        degradedFrom: degradedFrom,
        criticalFrom: criticalFrom
      });
    });

    return { info: info, running: running, rows: rows };
  }

  function lastTestId() {
    var newest = null;
    var id = null;
    state.instances.forEach(function (inst) {
      for (var i = inst.samples.length - 1; i >= 0; i--) {
        if (inst.samples[i].test) {
          if (newest === null || inst.samples[i].t > newest) {
            newest = inst.samples[i].t;
            id = inst.samples[i].test.id;
          }
          break;
        }
      }
    });
    return id;
  }

  function outcome(row, running, variable) {
    if (row.killed) {
      return { key: 'critical', text: 'Ran out of memory: the kernel killed the test' };
    }
    var key = ['unknown', 'healthy', 'degraded', 'critical'][row.rank];
    var text = { critical: 'Could not cope', degraded: 'Coped, but slowed down',
                 healthy: 'Coped comfortably', unknown: 'Too early to tell' }[key];
    if (variable && key === 'critical' && row.criticalFrom !== null) {
      text = 'Could not cope from ' + formatMb(row.criticalFrom);
    } else if (variable && key === 'degraded' && row.degradedFrom !== null) {
      text = 'Slowed down from ' + formatMb(row.degradedFrom);
    } else if (running && key === 'healthy') {
      text = 'No trouble so far';
    }
    return { key: key, text: text };
  }

  function renderResults() {
    var id = lastTestId();
    var box = $('#lt-results');
    box.hidden = !id;
    if (!id) {
      return;
    }
    var result = testResults(id);
    var variable = result.info && result.info.mode === 'variable';

    $('#lt-results-title').textContent = result.running ? 'Test in progress' : 'Last test';
    $('#lt-results-summary').textContent = (result.info ? result.info.summary : '') +
      (result.running ? ' · results so far' : '');

    Array.prototype.forEach.call(box.querySelectorAll('th[data-variable]'), function (th) {
      th.hidden = !variable;
    });

    var body = $('#lt-results-body');
    clear(body);
    result.rows.forEach(function (row) {
      var tr = document.createElement('tr');
      var instTd = document.createElement('td');
      var wrap = el('span', 'state-cell');
      var key = el('span', 'legend-key');
      key.style.background = row.inst.color;
      wrap.appendChild(key);
      wrap.appendChild(el('span', null, row.inst.label));
      instTd.appendChild(wrap);
      tr.appendChild(instTd);

      var said = outcome(row, result.running, variable);
      var outTd = document.createElement('td');
      var outWrap = el('span', 'state-cell');
      var dot = el('span', 'state-dot');
      dot.setAttribute('data-state', said.key);
      outWrap.appendChild(dot);
      outWrap.appendChild(el('span', null, said.text));
      outTd.appendChild(outWrap);
      tr.appendChild(outTd);

      tr.appendChild(el('td', 'num', formatPct(row.worstMinute) + '%'));
      tr.appendChild(el('td', 'num', formatPct(row.worst10) + '%'));
      if (variable) {
        tr.appendChild(el('td', 'num', row.fineUpTo === null ? 'not even the lowest' : formatMb(row.fineUpTo)));
        tr.appendChild(el('td', 'num', row.degradedFrom === null ? 'never' : formatMb(row.degradedFrom)));
        tr.appendChild(el('td', 'num', row.criticalFrom === null ? 'never' : formatMb(row.criticalFrom)));
      }
      body.appendChild(tr);
    });

    // The question the test was run to answer. Rows arrive smallest size
    // first, so the first one that coped is the smallest that will do.
    var verdict = '';
    if (!result.running && result.rows.length > 1) {
      var coped = result.rows.filter(function (row) {
        return !row.killed && row.rank === STATE_RANK.healthy;
      });
      verdict = coped.length
        ? 'Smallest instance that stayed healthy under this load: ' + coped[0].inst.label + '.'
        : 'None of the instances stayed healthy under this load.';
    } else if (result.running) {
      verdict = 'The verdict appears when the test finishes. States lag the load by up to a minute.';
    }
    $('#lt-verdict').textContent = verdict;
  }

  function renderLoadTest() {
    var running = runningTests();
    var pending = startPending(running);
    var chip = $('#test-chip');
    if (pending) {
      chip.textContent = 'Test starting…';
      chip.hidden = false;
    } else if (running.length) {
      var first = running[0];
      var latest = first.inst.samples[first.inst.samples.length - 1];
      chip.textContent = 'Test running · ' + formatMb(first.test.mb) +
        (testProgress(first.test, latest) ? ' · ' + testProgress(first.test, latest) : '');
      chip.hidden = false;
    } else {
      chip.hidden = true;
    }

    var busy = running.length || pending;
    var online = onlineInstances().length;
    var start = $('#lt-start');
    start.disabled = !!busy || !state.preview || !online;
    start.textContent = busy
      ? 'Test running…'
      : 'Start on ' + online + (online === 1 ? ' instance' : ' instances');
    $('#lt-stop').disabled = !busy;

    if (!$('#loadtest-panel').hidden) {
      renderResults();
    }
  }

  function showStatus(prefix, results) {
    var status = $('#lt-status');
    clear(status);
    status.appendChild(el('span', null, prefix));
    Object.keys(results || {}).forEach(function (host) {
      var row = el('span', 'stress-row');
      row.appendChild(el('strong', null, host));
      row.appendChild(el('span', null, ' ' + results[host]));
      status.appendChild(row);
    });
  }

  function setPanelOpen(open) {
    $('#loadtest-panel').hidden = !open;
    $('#loadtest-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      previewNow();
      renderResults();
    }
  }

  function wireLoadTest() {
    var form = $('#lt-form');

    $('#loadtest-toggle').addEventListener('click', function () {
      setPanelOpen($('#loadtest-panel').hidden);
    });
    $('#loadtest-close').addEventListener('click', function () {
      setPanelOpen(false);
      $('#loadtest-toggle').focus();
    });
    $('#test-chip').addEventListener('click', function () {
      setPanelOpen(true);
      $('#loadtest-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });

    wireSegmented('#lt-mode', function (button) {
      state.testMode = button.getAttribute('data-mode');
      syncFormMode();
      previewSoon();
    });
    wireSegmented('#lt-pattern', function (button) {
      state.testPattern = button.getAttribute('data-pattern');
      syncFormMode();
      previewSoon();
    });
    form.addEventListener('input', previewSoon);

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var start = $('#lt-start');
      start.disabled = true;
      start.textContent = 'Starting…';
      postJson('run-stress', formSpec())
        .then(function (result) {
          if (!result.ok) {
            showStatus(result.data.error || 'Could not start the test (HTTP ' + result.status + ').');
            return;
          }
          state.startedAt = Date.now();
          state.stopped = null;
          showStatus('Started ' + result.data.plan.summary + ':', result.data.results);
        })
        .catch(function (err) {
          showStatus('Could not start the test: ' + err.message);
        })
        .then(load);
    });

    $('#lt-stop').addEventListener('click', function () {
      var stop = $('#lt-stop');
      var ids = {};
      var known = runningTests();
      known.forEach(function (r) {
        ids[r.test.id] = true;
      });
      stop.disabled = true;
      postJson('stop-stress')
        .then(function (result) {
          if (result.ok) {
            state.startedAt = null;
            state.stopped = { at: Date.now(), ids: ids, all: !known.length };
          }
          showStatus(result.ok ? 'Stop sent:' : 'Could not stop the test.', result.data.results);
        })
        .catch(function (err) {
          showStatus('Could not stop the test: ' + err.message);
        })
        .then(load);
    });

    syncFormMode();
  }

  // dev.py serves /dev-mode; server.py does not. A 404 leaves the banner
  // hidden, which is the production case.
  function checkDemoMode() {
    fetch('dev-mode', { headers: { Accept: 'application/json' } })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data && data.demo) {
          $('#demo-banner').hidden = false;
        }
      })
      .catch(function () {
        /* No dev-mode route means a real server. Nothing to show. */
      });
  }

  function init() {
    wireTheme();
    wireLoadTest();
    checkDemoMode();

    wireSegmented('#range-control', function (button) {
      state.rangeMinutes = parseInt(button.getAttribute('data-minutes'), 10) || 0;
      state.activeIndex = null;
      renderAll();
    });

    wireSegmented('#view-control', function (button) {
      setView(button.getAttribute('data-view'));
    });

    // Rebuilt on every poll, so the handler lives on the container.
    $('#instance-control').addEventListener('click', function (event) {
      var button = event.target.closest('button');
      if (!button) {
        return;
      }
      state.selected = button.getAttribute('data-instance');
      state.activeIndex = null;
      renderAll();
    });

    $('#chart').addEventListener('keydown', onChartKeydown);
    $('#chart').addEventListener('blur', function () {
      if (state.activeFromKeyboard) {
        state.activeFromKeyboard = false;
        setActiveIndex(null);
      }
    });

    if (window.ResizeObserver) {
      var pending = null;
      new window.ResizeObserver(function () {
        if (state.view !== 'chart') {
          return;
        }
        window.clearTimeout(pending);
        pending = window.setTimeout(function () {
          renderChart(scopedInstances());
        }, 80);
      }).observe($('#plot-wrap'));

      var pendingPreview = null;
      new window.ResizeObserver(function () {
        window.clearTimeout(pendingPreview);
        pendingPreview = window.setTimeout(renderPreview, 80);
      }).observe($('#lt-plot'));
    }

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) {
        load();
      }
    });

    load();
    window.setInterval(load, POLL_MS);
    window.setInterval(renderFeedStatus, 1000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
