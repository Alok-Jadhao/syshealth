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

  function instanceColor(type) {
    var size = String(type || '').split('.')[1];
    // Anything off the size ladder keeps the base series hue rather than the
    // de-emphasis grey, which is reserved for context marks.
    return SIZE_COLORS[size] || 'var(--series-psi)';
  }

  function shortId(id) {
    var text = String(id || '');
    return text.length > 12 ? text.slice(-8) : text;
  }

  function labelInstances(list) {
    var seen = {};
    list.forEach(function (inst) {
      seen[inst.type] = (seen[inst.type] || 0) + 1;
    });
    list.forEach(function (inst) {
      inst.label = seen[inst.type] > 1
        ? inst.type + ' · ' + shortId(inst.id)
        : inst.type;
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
          type: inst.type,
          name: inst.name,
          label: inst.label,
          color: inst.color,
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
          dashed: false,
          primary: true
        };
      });
    }

    var only = scoped[0];
    return [
      {
        key: only.id + ':psi',
        name: 'PSI avg10',
        color: only.color,
        points: only.samples,
        value: function (s) { return s.psi; },
        area: true,
        dashed: false,
        primary: true
      },
      {
        key: only.id + ':avg',
        name: 'Rolling average',
        color: 'var(--series-avg)',
        points: only.samples,
        value: function (s) { return s.avgPsi; },
        area: false,
        dashed: true,
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
      if (s.dashed) {
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

  function renderChart(scoped) {
    var svg = $('#chart');
    var wrap = $('#plot-wrap');
    var empty = $('#empty-state');
    var multi = scoped.length > 1;
    var series = buildSeries(scoped, multi);

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
      top: 22,
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

    // Thresholds are calibrated per machine, so they only make sense when a
    // single instance is on screen.
    var thresholds = [];
    if (!multi) {
      var baseline = null;
      var points = scoped[0].samples;
      for (var b = points.length - 1; b >= 0; b--) {
        if (points[b].baseline !== null && points[b].baseline > 0) {
          baseline = points[b].baseline;
          break;
        }
      }
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
    }

    // Threshold rules may widen the scale, but never by more than 2x the data:
    // a far-away critical line must not squash the plot into the floor.
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
        if (s.dashed) {
          line.setAttribute('stroke-dasharray', '5 4');
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
      if (reading.series.dashed) {
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

  function renderSingleSummary(inst) {
    var latest = inst ? inst.all[inst.all.length - 1] : null;
    var key = latest ? stateKey(latest.state) : 'unknown';

    $('#hero-scope').textContent = inst ? inst.label : 'No instance reporting';
    $('#hero-psi').textContent = latest ? formatPsi(latest.psi) : '—';
    $('#status-text').textContent = latest ? latest.state : 'No data';
    $('#status-badge').setAttribute('data-state', key);
    $('#hero-reason').textContent = latest && latest.reason
      ? latest.reason
      : 'Waiting for the SysHealth agent to push its first sample.';

    $('#tile-avg').textContent = latest ? formatPsi(latest.avgPsi) : '—';
    $('#tile-scan').textContent = latest ? formatCount(latest.scan) : '—';
    $('#tile-steal').textContent = latest ? formatCount(latest.steal) : '—';
    $('#tile-count').textContent = formatCount(inst ? inst.all.length : 0);
  }

  function renderCompareSummary(scoped) {
    var grid = $('#overview-compare');
    clear(grid);

    scoped.forEach(function (inst) {
      var latest = inst.samples[inst.samples.length - 1];
      var peak = inst.samples.reduce(function (acc, s) {
        return Math.max(acc, s.psi);
      }, 0);
      var mean = inst.samples.reduce(function (acc, s) {
        return acc + s.psi;
      }, 0) / inst.samples.length;

      var tile = el('div', 'cmp-tile');

      var head = el('div', 'cmp-head');
      var key = el('span', 'legend-key');
      key.style.background = inst.color;
      head.appendChild(key);
      head.appendChild(el('span', 'cmp-type', inst.label));
      tile.appendChild(head);

      tile.appendChild(el('p', 'cmp-value', formatPsi(latest.psi)));

      var stateRow = el('p', 'cmp-state');
      var dot = el('span', 'state-dot');
      dot.setAttribute('data-state', stateKey(latest.state));
      stateRow.appendChild(dot);
      stateRow.appendChild(el('span', null, latest.state));
      tile.appendChild(stateRow);

      tile.appendChild(el('p', 'cmp-note',
        'mean ' + formatPsi(mean) + ' · peak ' + formatPsi(peak)));

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

      cell(formatPsi(sample.psi), 'num');
      cell(formatPsi(sample.avgPsi), 'num');

      var stateTd = document.createElement('td');
      var stateWrap = el('span', 'state-cell');
      var stateDot = el('span', 'state-dot');
      stateDot.setAttribute('data-state', stateKey(sample.state));
      stateWrap.appendChild(stateDot);
      stateWrap.appendChild(el('span', null, sample.state));
      stateTd.appendChild(stateWrap);
      tr.appendChild(stateTd);

      cell(formatCount(sample.scan), 'num');
      cell(formatCount(sample.steal), 'num');
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
    $('#chart-subtitle').textContent = multi
      ? sampleCount + ' samples across ' + scoped.length +
        ' instances · thresholds are calibrated per machine, so pick one to see them.'
      : (sampleCount
        ? sampleCount + ' samples · PSI avg10 per sample, with the analyser’s rolling average.'
        : 'PSI avg10 per sample, with the analyser’s rolling average.');

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
            id: String(row.id),
            type: String(row.type || 'unknown'),
            name: String(row.name || row.id),
            color: instanceColor(row.type),
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
