/* SysHealth dashboard — memory pressure graph.
   No build step and no third-party libraries: the SVG is drawn by hand so the
   page works on an air-gapped box, which is where this agent usually runs. */

(function () {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var POLL_MS = 5000;
  var HISTORY_LIMIT = 720;
  var STALE_AFTER_MS = 20000;

  // The analyser escalates at 2x baseline (degraded) and 5x (critical).
  var THRESHOLDS = [
    { multiple: 2, label: 'Degraded (2× baseline)', shortLabel: 'Degraded (2×)', state: 'degraded' },
    { multiple: 5, label: 'Critical (5× baseline)', shortLabel: 'Critical (5×)', state: 'critical' }
  ];

  var state = {
    samples: [],
    rangeMinutes: 15,
    view: 'chart',
    activeIndex: null,
    activeFromKeyboard: false,
    layout: null
  };

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

  function num(value) {
    var n = typeof value === 'number' ? value : parseFloat(value);
    return isFinite(n) ? n : null;
  }

  /* ---------- data ---------- */

  // The server stamps `received_at` (epoch seconds); older payloads only carry
  // the display string, so fall back to parsing that.
  function sampleTime(raw) {
    var epoch = num(raw.received_at);
    if (epoch !== null) {
      return epoch * 1000;
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
    return {
      t: t,
      psi: psi,
      avgPsi: avg === null ? psi : avg,
      state: String(raw.state || 'UNKNOWN'),
      scan: num(raw.pgscan_delta),
      steal: num(raw.pgsteal_delta),
      reason: typeof raw.reason === 'string' ? raw.reason : '',
      baseline: num(raw.baseline)
    };
  }

  function inRange(samples) {
    if (!state.rangeMinutes || !samples.length) {
      return samples;
    }
    var cutoff = samples[samples.length - 1].t - state.rangeMinutes * 60000;
    return samples.filter(function (s) {
      return s.t >= cutoff;
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
    if (value === null || !isFinite(value)) {
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

  // Split on gaps so a restarted agent does not get a straight line drawn
  // across the minutes it was away.
  function segmentize(points) {
    if (points.length < 2) {
      return points.length ? [points] : [];
    }
    var deltas = [];
    for (var i = 1; i < points.length; i++) {
      deltas.push(points[i].t - points[i - 1].t);
    }
    var sorted = deltas.slice().sort(function (a, b) {
      return a - b;
    });
    var median = sorted[Math.floor(sorted.length / 2)] || 0;
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

  function renderChart(samples) {
    var svg = $('#chart');
    var wrap = $('#plot-wrap');
    var empty = $('#empty-state');

    clear(svg);
    state.layout = null;
    hideTooltip();

    if (!samples.length) {
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
      top: 22,
      right: narrow ? 46 : 66,
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
    samples.forEach(function (s) {
      dataMax = Math.max(dataMax, s.psi, s.avgPsi);
      dataMin = Math.min(dataMin, s.psi);
    });

    // Threshold rules may widen the scale, but never by more than 2x the data:
    // a far-away critical line must not squash the plot into the floor.
    var baseline = null;
    for (var b = samples.length - 1; b >= 0; b--) {
      if (samples[b].baseline !== null && samples[b].baseline > 0) {
        baseline = samples[b].baseline;
        break;
      }
    }
    var thresholds = [];
    if (baseline) {
      THRESHOLDS.forEach(function (t) {
        thresholds.push({
          value: baseline * t.multiple,
          label: t.label,
          shortLabel: t.shortLabel,
          state: t.state
        });
      });
    }
    var scaleMax = dataMax;
    thresholds.forEach(function (t) {
      if (dataMax === 0 || t.value <= dataMax * 2) {
        scaleMax = Math.max(scaleMax, t.value);
      }
    });

    var scale = niceScale(scaleMax);
    var digits = decimalsFor(scale.step);
    // Ticks stay as coarse as the step; single values keep two places so a
    // small reading never rounds away to "0".
    var valueDigits = Math.max(2, digits);

    var t0 = samples[0].t;
    var t1 = samples[samples.length - 1].t;
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
    unit.textContent = '% stalled';
    gridGroup.appendChild(unit);

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

    var segments = segmentize(samples);

    function pathFor(points, key) {
      return points
        .map(function (p, i) {
          return (i ? 'L' : 'M') + x(p.t).toFixed(2) + ' ' + y(p[key]).toFixed(2);
        })
        .join(' ');
    }

    segments.forEach(function (points) {
      if (points.length === 1) {
        return;
      }
      // Area wash under the primary series: a tint, never a saturated block.
      var area = pathFor(points, 'psi') +
        ' L' + x(points[points.length - 1].t).toFixed(2) + ' ' + (pad.top + plotH) +
        ' L' + x(points[0].t).toFixed(2) + ' ' + (pad.top + plotH) + ' Z';
      dataGroup.appendChild(svgEl('path', {
        d: area, fill: 'var(--series-psi)', 'fill-opacity': 0.1, stroke: 'none'
      }));

      // Rolling average is context, so it takes the de-emphasis grey.
      dataGroup.appendChild(svgEl('path', {
        d: pathFor(points, 'avgPsi'),
        fill: 'none',
        stroke: 'var(--series-avg)',
        'stroke-width': 2,
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
        'stroke-dasharray': '5 4'
      }));

      dataGroup.appendChild(svgEl('path', {
        d: pathFor(points, 'psi'),
        fill: 'none',
        stroke: 'var(--series-psi)',
        'stroke-width': 2,
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round'
      }));
    });

    var last = samples[samples.length - 1];
    var lastX = x(last.t);
    var lastY = y(last.psi);

    if (samples.length === 1) {
      dataGroup.appendChild(svgEl('circle', {
        cx: lastX, cy: lastY, r: 4,
        fill: 'var(--series-psi)',
        stroke: 'var(--surface-1)', 'stroke-width': 2
      }));
    } else {
      // End marker carries a 2px surface ring so it stays legible over the line.
      dataGroup.appendChild(svgEl('circle', {
        cx: lastX, cy: lastY, r: 4.5,
        fill: 'var(--series-psi)',
        stroke: 'var(--surface-1)', 'stroke-width': 2
      }));
    }

    // One selective direct label: the current value at the line end. Measured
    // against the right edge so it is never clipped.
    var endText = formatPsi(last.psi, valueDigits);
    var estimatedWidth = endText.length * 7.2;
    var overflows = lastX + 10 + estimatedWidth > width - 4;
    var endLabel = svgEl('text', {
      x: overflows ? width - 4 : lastX + 10,
      y: Math.max(pad.top + 4, Math.min(lastY + 4, pad.top + plotH)),
      'text-anchor': overflows ? 'end' : 'start',
      fill: 'var(--text-primary)',
      'font-size': 12,
      'font-weight': 600,
      'font-variant-numeric': 'tabular-nums',
      stroke: 'var(--surface-1)',
      'stroke-width': 3,
      'stroke-linejoin': 'round',
      'paint-order': 'stroke fill'
    });
    endLabel.textContent = endText;
    dataGroup.appendChild(endLabel);

    var crosshair = svgEl('g', { visibility: 'hidden' });
    crosshair.appendChild(svgEl('line', {
      x1: 0, x2: 0, y1: pad.top, y2: pad.top + plotH,
      stroke: 'var(--axis)', 'stroke-width': 1
    }));
    crosshair.appendChild(svgEl('circle', {
      cx: 0, cy: 0, r: 4.5,
      fill: 'var(--series-psi)',
      stroke: 'var(--surface-1)', 'stroke-width': 2
    }));
    crosshair.appendChild(svgEl('circle', {
      cx: 0, cy: 0, r: 3.5,
      fill: 'var(--series-avg)',
      stroke: 'var(--surface-1)', 'stroke-width': 2
    }));

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
      samples: samples, x: x, y: y, pad: pad, plotW: plotW, plotH: plotH,
      width: width, digits: valueDigits, crosshair: crosshair, overlay: overlay
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
      'Memory pressure over time. ' + samples.length + ' samples from ' +
      formatClock(t0) + ' to ' + formatClock(t1) + '. Current PSI ' +
      formatPsi(last.psi, valueDigits) + ', range ' + formatPsi(dataMin, valueDigits) +
      ' to ' + formatPsi(dataMax, valueDigits) +
      '. Use the arrow keys to read individual samples, or switch to the table view.'
    );

    if (state.activeIndex !== null) {
      setActiveIndex(Math.min(state.activeIndex, samples.length - 1));
    }
  }

  /* ---------- hover & crosshair ---------- */

  function nearestIndex(clientX) {
    var layout = state.layout;
    var rect = $('#chart').getBoundingClientRect();
    var scaleX = rect.width ? layout.width / rect.width : 1;
    var localX = (clientX - rect.left) * scaleX;

    var best = 0;
    var bestDist = Infinity;
    for (var i = 0; i < layout.samples.length; i++) {
      var dist = Math.abs(layout.x(layout.samples[i].t) - localX);
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
    if (index === null || !layout.samples[index]) {
      layout.crosshair.setAttribute('visibility', 'hidden');
      hideTooltip();
      return;
    }

    var sample = layout.samples[index];
    var cx = layout.x(sample.t);
    var nodes = layout.crosshair.childNodes;
    nodes[0].setAttribute('x1', cx);
    nodes[0].setAttribute('x2', cx);
    nodes[1].setAttribute('cx', cx);
    nodes[1].setAttribute('cy', layout.y(sample.psi));
    nodes[2].setAttribute('cx', cx);
    nodes[2].setAttribute('cy', layout.y(sample.avgPsi));
    layout.crosshair.setAttribute('visibility', 'visible');

    showTooltip(sample, cx);

    if (state.activeFromKeyboard) {
      $('#chart-readout').textContent =
        formatClock(sample.t) + ': PSI ' + formatPsi(sample.psi, layout.digits) +
        ', rolling average ' + formatPsi(sample.avgPsi, layout.digits) +
        ', state ' + sample.state + (sample.reason ? '. ' + sample.reason : '');
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
  function showTooltip(sample, cx) {
    var tip = $('#tooltip');
    var layout = state.layout;
    clear(tip);

    var time = document.createElement('p');
    time.className = 'tt-time';
    time.textContent = formatClock(sample.t);
    tip.appendChild(time);

    [
      { name: 'PSI avg10', value: sample.psi, key: 'tt-key-psi' },
      { name: 'Rolling average', value: sample.avgPsi, key: 'tt-key-avg' }
    ].forEach(function (row) {
      var line = document.createElement('div');
      line.className = 'tt-row';

      var key = document.createElement('span');
      key.className = 'tt-key ' + row.key;
      line.appendChild(key);

      var value = document.createElement('span');
      value.className = 'tt-value';
      value.textContent = formatPsi(row.value, layout.digits);
      line.appendChild(value);

      var name = document.createElement('span');
      name.className = 'tt-name';
      name.textContent = row.name;
      line.appendChild(name);

      tip.appendChild(line);
    });

    var stateRow = document.createElement('p');
    stateRow.className = 'tt-state';
    var dot = document.createElement('span');
    dot.className = 'state-dot';
    dot.setAttribute('data-state', stateKey(sample.state));
    stateRow.appendChild(dot);
    var stateName = document.createElement('span');
    stateName.textContent = sample.state;
    stateRow.appendChild(stateName);
    tip.appendChild(stateRow);

    if (sample.reason) {
      var reason = document.createElement('p');
      reason.className = 'tt-reason';
      reason.textContent = sample.reason;
      tip.appendChild(reason);
    }

    tip.removeAttribute('hidden');

    var wrapWidth = $('#plot-wrap').clientWidth || layout.width;
    var pxPerUnit = wrapWidth / layout.width;
    var anchor = cx * pxPerUnit;
    var tipWidth = tip.offsetWidth;
    var left = anchor + 14;
    if (left + tipWidth > wrapWidth) {
      left = anchor - tipWidth - 14;
    }
    tip.style.left = Math.max(0, left) + 'px';
    tip.style.top = layout.pad.top + 'px';
  }

  function onChartKeydown(event) {
    var layout = state.layout;
    if (!layout || !layout.samples.length) {
      return;
    }
    var last = layout.samples.length - 1;
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

  /* ---------- summary, table, chrome ---------- */

  function renderSummary(latest, total) {
    var badge = $('#status-badge');
    var key = latest ? stateKey(latest.state) : 'unknown';

    $('#hero-psi').textContent = latest ? formatPsi(latest.psi) : '—';
    $('#status-text').textContent = latest ? latest.state : 'No data';
    badge.setAttribute('data-state', key);
    $('#hero-reason').textContent = latest && latest.reason
      ? latest.reason
      : 'Waiting for the SysHealth agent to push its first sample.';

    $('#tile-avg').textContent = latest ? formatPsi(latest.avgPsi) : '—';
    $('#tile-scan').textContent = latest ? formatCount(latest.scan) : '—';
    $('#tile-steal').textContent = latest ? formatCount(latest.steal) : '—';
    $('#tile-count').textContent = formatCount(total);
  }

  function renderTable(samples) {
    var body = $('#table-body');
    clear(body);

    samples.slice().reverse().forEach(function (sample) {
      var row = document.createElement('tr');

      function cell(text, className) {
        var td = document.createElement('td');
        if (className) {
          td.className = className;
        }
        td.textContent = text;
        row.appendChild(td);
        return td;
      }

      cell(formatClock(sample.t));
      cell(formatPsi(sample.psi), 'num');
      cell(formatPsi(sample.avgPsi), 'num');

      var stateTd = document.createElement('td');
      var wrapper = document.createElement('span');
      wrapper.className = 'state-cell';
      var dot = document.createElement('span');
      dot.className = 'state-dot';
      dot.setAttribute('data-state', stateKey(sample.state));
      wrapper.appendChild(dot);
      var name = document.createElement('span');
      name.textContent = sample.state;
      wrapper.appendChild(name);
      stateTd.appendChild(wrapper);
      row.appendChild(stateTd);

      cell(formatCount(sample.scan), 'num');
      cell(formatCount(sample.steal), 'num');
      cell(sample.reason || '—', 'reason');

      body.appendChild(row);
    });
  }

  function renderFeedStatus() {
    var feed = $('#feed-status');
    if (!state.samples.length) {
      feed.textContent = 'No samples received yet';
      return;
    }
    var age = Date.now() - state.samples[state.samples.length - 1].t;
    var stale = age > STALE_AFTER_MS;
    feed.textContent = (stale ? 'Last sample ' : 'Updated ') + formatAgo(age);
    feed.setAttribute('data-stale', stale ? 'true' : 'false');
  }

  function renderAll() {
    var scoped = inRange(state.samples);
    var latest = state.samples.length ? state.samples[state.samples.length - 1] : null;

    renderSummary(latest, state.samples.length);
    renderFeedStatus();
    renderTable(scoped);

    $('#chart-subtitle').textContent = scoped.length
      ? scoped.length + ' samples · PSI avg10 per sample, with the analyser’s rolling average.'
      : 'PSI avg10 per sample, with the analyser’s rolling average.';

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
      renderChart(inRange(state.samples));
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
        renderChart(inRange(state.samples));
      }
    });

    apply();
  }

  /* ---------- polling ---------- */

  function load() {
    var scoped = $('#scoped');
    scoped.classList.add('is-loading');

    return fetch('history?limit=' + HISTORY_LIMIT, { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('HTTP ' + response.status);
        }
        return response.json();
      })
      .then(function (payload) {
        var rows = Array.isArray(payload) ? payload : [];
        state.samples = rows
          .map(normalize)
          .filter(Boolean)
          .sort(function (a, b) {
            return a.t - b.t;
          });
        renderAll();
      })
      .catch(function () {
        $('#feed-status').textContent = 'Server unreachable — retrying';
      })
      .then(function () {
        scoped.classList.remove('is-loading');
      });
  }

  function init() {
    wireTheme();

    wireSegmented('#range-control', function (button) {
      state.rangeMinutes = parseInt(button.getAttribute('data-minutes'), 10) || 0;
      state.activeIndex = null;
      renderAll();
    });

    wireSegmented('#view-control', function (button) {
      setView(button.getAttribute('data-view'));
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
          renderChart(inRange(state.samples));
        }, 80);
      }).observe($('#plot-wrap'));
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
