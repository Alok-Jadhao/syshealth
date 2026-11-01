/* SysHealth dashboard — what every part of the page means.
   Any element with data-help="key" opens the explanation under that key. The
   handler sits on the document, so the buttons dashboard.js rebuilds on every
   poll need no wiring of their own. The guide card at the top is here too. */

(function () {
  'use strict';

  // The lines every state is judged by. dashboard.js passes the ones the
  // agents report; these are the agents' defaults (analyzer.py).
  var th = { degraded: 10, critical: 40, critical_full: 10 };

  // Each entry: a title, then paragraphs. An array inside the body is a
  // bulleted list. A function body is worked out when it opens.
  var HELP = {
    feed: {
      title: 'How fresh this is',
      body: [
        'Each machine runs a small SysHealth agent that sends one reading every 5 seconds. ' +
        'This page asks the server for new readings every 5 seconds too.',
        '“Updated 3s ago” means the newest reading from any machine is 3 seconds old. ' +
        '“Last sample 2 min ago” means nothing has arrived for a while: an agent has stopped, ' +
        'a machine is down, or this computer can no longer reach the dashboard (for example, ' +
        'on a new network: ./aws.sh up lets the new address in).'
      ]
    },

    'current-state': {
      title: 'Current state',
      body: [
        'The newest reading from each machine, refreshed every 5 seconds. It always shows ' +
        'now: the Range buttons further down do not change it.',
        'Each card, from the top:',
        [
          'The size, for example t3.micro, the smallest here. The dot is green while the machine ' +
          'is reporting, grey once it has been quiet for 15 seconds.',
          'The machine\'s name and how much RAM it has.',
          'The big number: the share of the last minute that programs on it spent waiting for ' +
          'memory. Lower is better.',
          'The coloured bar: where that number sits. Green healthy, amber degraded, red critical; ' +
          'the white mark is this machine.',
          'The state: HEALTHY, DEGRADED or CRITICAL.',
          'Memory in use: how full its RAM is, the number most dashboards show. The mark on ' +
          'the bar is 80%, where they usually alarm.',
          'Machine load: how busy it is, for context.',
          'The small print: the last 10 seconds, the highest reading in the chosen range, and ' +
          'during a test what the test is holding.'
        ],
        'Every underlined label and every ? opens an explanation like this one.'
      ]
    },

    'psi-minute': {
      title: 'Waiting for memory, last minute',
      body: function () {
        return [
          'The share of the last minute in which programs on this machine were stalled, ' +
          'waiting for memory they needed. The Linux kernel measures this itself. It is called ' +
          'PSI, Pressure Stall Information.',
          'Example: 20% means that out of every 10 seconds, 2 were lost to waiting. The work ' +
          'could have run about 20% faster with enough memory.',
          'It is not how full memory is. A machine can be 90% full and perfectly fine, as long ' +
          'as nothing has to wait. Memory in use cannot tell those apart; this number can, so ' +
          'SysHealth judges by it.',
          [
            'Under ' + th.degraded + '%: healthy.',
            th.degraded + '% to ' + th.critical + '%: degraded, noticeably slower.',
            th.critical + '% or more: critical, the machine is thrashing.'
          ],
          'Worked out from one reading every 5 seconds: the average of the last 12 readings of ' +
          'the 10-second figure.'
        ];
      }
    },

    state: {
      title: 'HEALTHY, DEGRADED, CRITICAL',
      body: function () {
        return [
          'The verdict on this machine, from the share of the last minute its programs spent ' +
          'waiting for memory.',
          [
            'HEALTHY: under ' + th.degraded + '%. Enough memory for the current load.',
            'DEGRADED: ' + th.degraded + '% to ' + th.critical + '%. Programs are noticeably ' +
            'slowed; users may see slower responses.',
            'CRITICAL: ' + th.critical + '% or more, or the whole machine stalled at least ' +
            th.critical_full + '% of the minute. The machine is thrashing: it spends its time ' +
            'freeing memory instead of working, and the kernel may start killing programs.',
            'RAN OUT OF MEMORY: during a load test, the kernel killed the test because it did ' +
            'not fit. The reading can fall afterwards, because killing it frees memory, but ' +
            'this size could not hold the load.'
          ],
          'A machine is only marked DEGRADED or CRITICAL after three readings in a row over the ' +
          'line, 15 seconds, so one short spike is not reported as a failure. It goes back to ' +
          'HEALTHY as soon as the minute drops under the line.',
          'For its first 30 seconds an agent is still collecting, and shows HEALTHY.',
          'With one machine selected, the lines under the state put the reading into words ' +
          'and give the next step. When the agent can see a likely cause, such as programs ' +
          'waiting on memory moved out to swap, the advice is about that cause. The grey line, when ' +
          'shown, is the analyser\'s own reason, for example that a new state is still being ' +
          'confirmed.',
          'The lines come from tools that act on this measurement in production: Android\'s ' +
          'low-memory killer, systemd-oomd and Meta\'s oomd (docs/thresholds.md).'
        ];
      }
    },

    'machine-load': {
      title: 'Machine load',
      body: [
        'How busy the machine is, for context:',
        [
          'CPU: the share of the last 5 seconds its processors were working.',
          'Load: the load average, as the uptime command shows it. Roughly how many programs ' +
          'wanted a processor at once over the last minute, next to how many vCPUs it has.'
        ],
        'Busy is not the same as short of memory. A machine can be flat out on CPU and read 0% ' +
        'waiting for memory. Only the waiting figure says whether it needs more RAM.'
      ]
    },

    'mem-in-use': {
      title: 'Memory in use',
      body: [
        'How full this machine\'s RAM is, as the free command, CloudWatch and most dashboards ' +
        'count it: all of it except what is still available to programs. The bar is in the ' +
        'machine\'s own colour; the mark on it is 80%, where most memory alarms are set.',
        'It is shown so you can compare it with the waiting figure above it. Full is not the ' +
        'same as too small: during a load test a size can pass the 80% mark and stay HEALTHY, ' +
        'because nothing has to wait yet. When programs do start to wait, the state changes, ' +
        'whatever this bar says.',
        'To see it over time, choose Show: Memory in use above the chart.'
      ]
    },

    'cmp-note': {
      title: 'The small print',
      body: [
        [
          'now: waiting for memory over just the last 10 seconds. It reacts fastest, so it ' +
          'jumps about; the state goes by the minute.',
          'peak … in range: the highest 10-second reading within the Range chosen below the ' +
          'cards (5 min, 15 min, 1 hour or all).',
          'app waiting: shown when the agent watches one app (SYSHEALTH_APP). The waiting ' +
          'inside that app\'s own group of processes, its cgroup.',
          'last seen: how long ago a machine that has gone quiet last reported.',
          'Holding …: while a load test runs, how much memory the test holds on this machine ' +
          'now, what it added (background work, extra swap) and which step it is on.'
        ]
      ]
    },

    'tile-now': {
      title: 'Right now',
      body: [
        'The same measure as the big number, waiting for memory, but over only the last ' +
        '10 seconds instead of a minute.',
        'It shows a change first, so it is the one to watch while a test runs. The state goes ' +
        'by the minute, so one spike here does not change it.'
      ]
    },

    'tile-full': {
      title: 'Whole machine stalled',
      body: function () {
        return [
          'The share of the last 10 seconds in which every program on the machine was waiting ' +
          'for memory at the same time, so no useful work was done at all. PSI calls this “full”.',
          'It is usually far lower than the big number, which counts the time at least one ' +
          'program waited. If its one-minute average reaches ' + th.critical_full + '%, the ' +
          'machine is CRITICAL even when the big number is not.'
        ];
      }
    },

    'tile-load': {
      title: 'Machine load',
      body: [
        'The big figure is CPU: the share of the last 5 seconds the processors were working. ' +
        'The note under it gives the load average beside the number of vCPUs.',
        'Neither says whether the machine is short of memory. The waiting figure does: a busy ' +
        'machine can read 0%.'
      ]
    },

    'tile-mem': {
      title: 'Memory in use',
      body: [
        'How full this machine\'s RAM is, as the free command, CloudWatch and most dashboards ' +
        'count it: all of it except what is still available to programs. The note gives the ' +
        'same in MB or GB.',
        'The mark on the bar is 80%, where most memory alarms are set. A machine past it can ' +
        'still be fine: the waiting figure, the big number on the left, says whether programs ' +
        'are held up, and the state goes by that.'
      ]
    },

    'tile-test': {
      title: 'Load test',
      body: [
        'How much memory a load test is holding on this machine right now, what the test added ' +
        '(background work, extra swap), and how far it has got.',
        'None means no test is running. Start one with Load test at the top of the page.'
      ]
    },

    'tile-app': {
      title: 'Your app',
      body: [
        'Shown when the agent is told to watch one app (SYSHEALTH_APP, set to the app\'s ' +
        'service or cgroup).',
        'The big figure is the app\'s own waiting for memory over the last 10 seconds, ' +
        'measured inside its cgroup. The note gives the memory it uses against its limit, if ' +
        'it has one, how often it was killed for going over, and its CPU use.',
        'It shows whether the app\'s own limit is the problem rather than the machine: an app ' +
        'can starve against its limit on a machine with plenty of memory free.'
      ]
    },

    'tile-scan': {
      title: 'Memory scanned',
      body: [
        'How many memory pages (4 KB each) programs had to make the kernel look through, since ' +
        'the last reading 5 seconds ago, to find some it could free. It rises when memory runs ' +
        'low and the kernel starts reclaiming.',
        'A clue to why programs wait, not a measure of harm: scanning that finds memory ' +
        'quickly costs little. The waiting figures say whether it hurts.'
      ]
    },

    'mem-where': {
      title: 'Where the memory is',
      body: [
        'This machine\'s RAM, split by what holds it:',
        [
          'Programs: memory programs asked for themselves.',
          'File cache: copies of files from disk. The kernel drops these when programs need ' +
          'the room, so a big cache is normal and healthy.',
          'Kernel and other: the kernel itself, shared memory and tmpfs.',
          'Free: not used for anything.'
        ],
        'Trouble starts when programs alone need more than the RAM. The cache is gone by then, ' +
        'and the kernel has to swap memory out to disk or kill something.',
        'Under the bar: memory in use as most dashboards count it, the swap in use, and how ' +
        'much programs are also waiting for the CPU and for the disk. Those do not set the ' +
        'state, but a machine that keeps re-reading files from disk can show memory trouble as ' +
        'disk waiting.',
        'The notes under the panel, when shown: the likely cause the agent found, a program ' +
        'whose memory keeps growing (a possible leak), and your app\'s verdict.'
      ]
    },

    'top-procs': {
      title: 'Holding the most',
      body: [
        'The programs holding the most memory right now (their resident memory, RSS), and the ' +
        'share of the RAM each one holds. The first suspects when memory runs short.',
        'While a load test runs, its workers appear here as stress.'
      ]
    },

    range: {
      title: 'Range',
      body: [
        'How much history the chart and the table show: the last 5 minutes, 15 minutes, hour, ' +
        'or all the server holds, up to 720 readings per machine (an hour, at one every ' +
        '5 seconds).',
        'It also sets “peak in range” on the cards. The big numbers on the cards always show now.'
      ]
    },

    instance: {
      title: 'Instance',
      body: [
        'Compare all: every machine on one chart, one line each, and one card each above.',
        'Pick one size to look at it on its own. The cards become one large reading with what ' +
        'it means, what to do, and tiles with the details; the chart adds the one-minute line ' +
        'its state is judged on; and a panel shows where its memory is and which programs ' +
        'hold it.'
      ]
    },

    view: {
      title: 'Chart or table',
      body: [
        'The same readings, drawn as a chart or listed as a table with the newest first. The ' +
        'table is easier for reading exact values.'
      ]
    },

    chart: {
      title: 'Memory pressure over time',
      body: function () {
        return [
          'Each line is one machine: the share of time its programs spent waiting for memory, ' +
          'one reading every 5 seconds (the 10-second figure). Higher means slower.',
          'Colour is size: the lightest blue is the smallest machine, the darkest the biggest. ' +
          'Two machines of the same size are told apart by dashes.',
          'The tinted bands and the labelled dashed lines are the states: under ' + th.degraded +
          '% healthy, ' + th.degraded + '% to ' + th.critical + '% degraded, ' + th.critical +
          '% and over critical.',
          'The Test load strip above the chart shows what a load test asked every machine to ' +
          'hold, step by step, lined up in time with the pressure it caused. Darker boxes are ' +
          'more memory.',
          'With one machine selected, the filled area is the 10-second reading and the grey ' +
          'dashed line is the one-minute average its state is judged on.',
          'Point at the chart to read every machine at one moment. With the keyboard, select ' +
          'the chart and use the left and right arrow keys.',
          'Show: Memory in use draws how full each machine is instead, from 0 to 100%.'
        ];
      }
    },

    'chart-metric': {
      title: 'Show',
      body: [
        'What the chart draws:',
        [
          'Waiting for memory: the share of time programs were held up waiting for memory. ' +
          'The states are judged on this, so it is the one with the healthy, degraded and ' +
          'critical lines.',
          'Memory in use: how full each machine\'s RAM is, from 0 to 100%, as most dashboards ' +
          'show it. The dashed line is 80%, where they usually alarm.'
        ],
        'Switch between them during a load test. The bigger sizes climb past 80% in use while ' +
        'their waiting stays flat at 0: full, but not too small. An 80% alarm cannot tell ' +
        'the two apart.'
      ]
    },

    'col-time': {
      title: 'Time',
      body: ['When the machine took the reading, in this computer\'s time zone.']
    },
    'col-instance': {
      title: 'Instance',
      body: ['Which machine sent the reading. Shown when more than one is in view.']
    },
    'col-now': {
      title: 'Now (10 s)',
      body: ['Waiting for memory over the 10 seconds before the reading. The figure the chart ' +
             'draws.']
    },
    'col-minute': {
      title: 'Last minute',
      body: ['Waiting for memory, averaged over the last minute. The state is judged on this.']
    },
    'col-full': {
      title: 'Whole machine',
      body: ['The share of the 10 seconds in which every program waited at once, so the ' +
             'whole machine was stalled.']
    },
    'col-in-use': {
      title: 'In use',
      body: ['How full the RAM was, as most dashboards count it. Shown for comparison: ' +
             'SysHealth does not judge by it.']
    },
    'col-state': {
      title: 'State',
      body: ['The state at that moment: HEALTHY, DEGRADED or CRITICAL.']
    },
    'col-test-load': {
      title: 'Test load',
      body: ['How much memory a load test was holding on the machine then. A dash when no ' +
             'test was running.']
    },
    'col-scanned': {
      title: 'Scanned Δ',
      body: ['How many memory pages the kernel looked through to free memory since the ' +
             'previous reading. Δ means “change since the last reading”.']
    },
    'col-reason': {
      title: 'Reason',
      body: ['The analyser\'s own words for the state, including whether a change was still ' +
             'being confirmed.']
    },

    loadtest: {
      title: 'Load test',
      body: [
        'Puts the same memory load on every reporting machine at the same moment, so you can ' +
        'see which sizes cope. Each machine\'s agent runs it itself: it starts workers that ' +
        'take the memory and keep using it, as a busy app would, for the time you set, then ' +
        'frees it.',
        [
          'Fill in the form, or press Show all three states.',
          'Check the plan drawn under the form: it shows the load against each machine\'s RAM ' +
          'before anything runs.',
          'Press Start. The cards and the chart show each machine react, and the results build ' +
          'up under the form.',
          'Stop test ends it at once on every machine.'
        ],
        'Nothing else on the machines changes. Extra swap, if you ask for it, is added for the ' +
        'test and removed after.',
        'Anyone with the watch-only code can watch. Only someone with the server\'s token can ' +
        'start or stop a test.'
      ]
    },

    'test-type': {
      title: 'Test type',
      body: [
        'Peak load: one amount of memory, held for the whole test. It answers: can each size ' +
        'take my app at its busiest?',
        'Variable load: the amount changes during the test, in a pattern you choose. It ' +
        'answers: how much can each size hold before it slows down, and does it recover?'
      ]
    },

    pattern: {
      title: 'Pattern',
      body: [
        [
          'Ramp up: climbs from From to To in even steps. Finds the most each size can hold ' +
          'before it slows down.',
          'Up and down: climbs, then comes back down, like a day of traffic. Shows whether each ' +
          'size recovers once the load drops.',
          'Bursts: switches between From and short spikes up to To. Shows how each size ' +
          'handles sudden peaks.'
        ]
      ]
    },

    'three-states': {
      title: 'Show all three states',
      body: function () {
        var spec = null;
        try {
          spec = JSON.parse(document.getElementById('lt-three-states').getAttribute('data-spec'));
        } catch (e) {
          spec = null;
        }
        var what = spec
          ? 'a ramp from ' + gb(spec.low_mb) + ' to ' + gb(spec.high_mb) + ' in ' + spec.steps +
            ' steps of ' + spec.step_sec + ' seconds, with ' + spec.workers + ' workers' +
            (spec.background_cpu ? ', background work' : '') +
            (spec.swap_mb ? ' and ' + gb(spec.swap_mb) + ' of extra swap' : '') +
            ', the same on every size'
          : 'a ramp tuned for the four sizes';
        return [
          'Fills in a test tuned on a real t3.micro to t3.large fleet, so that all three states ' +
          'show at the same moment: the micro goes CRITICAL, the small DEGRADED, and the medium ' +
          'and large stay HEALTHY. On AWS that picture held from about 1½ minutes in to the end ' +
          'of the test.',
          'What it runs: ' + what + '.',
          'It only fills in the form. Press Start to run it' +
          (spec ? ' (' + Math.round(spec.steps * spec.step_sec / 60) + ' minutes or so)' : '') + '.'
        ];
      }
    },

    memory_mb: {
      title: 'Memory to hold',
      body: [
        'How much memory the test takes on each machine, in MB (1024 MB is 1 GB). Set it to ' +
        'what your app uses at its busiest.',
        'Every machine gets the same amount, so the smallest ones feel it first.'
      ]
    },
    duration_sec: {
      title: 'Hold it for',
      body: ['How long to hold it, 10 to 1800 seconds. A minute or more lets the one-minute ' +
             'reading, which sets the state, settle.']
    },
    need_mb: {
      title: 'Your app at its busiest',
      body: [
        'The memory your app needs at its peak. The test can go past it, but every size is ' +
        'judged against this: the verdict picks the cheapest size that holds it, plus the ' +
        'spare you ask for, without slowing down.',
        'You can change it after the test, under Which size to choose, without running the ' +
        'test again.'
      ]
    },
    low_mb: {
      title: 'From',
      body: ['Where the load starts (Ramp up) or its low point (Up and down, Bursts). Your ' +
             'app\'s quiet-time memory; 0 for none.']
    },
    high_mb: {
      title: 'To',
      body: ['Where the load ends or peaks. Go past your app\'s busiest to see how much room ' +
             'each size leaves, and where each one breaks.']
    },
    steps: {
      title: 'Steps',
      body: [
        'Ramp up: how many levels the load climbs through from From to To. More steps find ' +
        'each size\'s limit more exactly, but take longer.',
        'Up and down: the levels on the way up; it comes back down the same way.',
        'Bursts: how many spikes.'
      ]
    },
    step_sec: {
      title: 'Each step lasts',
      body: ['How long each level is held. 60 seconds or more gives the clearest breaking ' +
             'point: the state is judged on a one-minute average, and the first 10 seconds of ' +
             'each step still carry the one before.']
    },
    workers: {
      title: 'Parallel workers',
      body: [
        'How many separate processes share the memory, split evenly between them, 1 to 16.',
        'A real server runs many programs at once. More workers make the load look like that, ' +
        'and press on memory harder.'
      ]
    },
    background_cpu: {
      title: 'Background work',
      body: [
        'Keeps every vCPU busy at the lowest priority for the whole test, like a server\'s ' +
        'other jobs. It gives way to anything else that wants the CPU.',
        'Why: with the test as the only work, a machine stalls as a whole whenever the test ' +
        'waits, so a size jumps from HEALTHY straight to CRITICAL. With background work it can ' +
        'be slowed but still working, so DEGRADED shows, as it would on a real server.'
      ]
    },
    swap_mb: {
      title: 'Extra swap',
      body: [
        'Disk space added as swap on each machine for the test, and removed when it ends. 0 ' +
        'adds none. Up to 4096 MB; the agent checks the disk has room first.',
        'Why: with only the machines\' own small swap, a size that cannot hold the load has the ' +
        'test killed within seconds, and its reading falls. With more swap it slows down ' +
        'instead and stays CRITICAL while it does, as a production server with swap would.'
      ]
    },

    plan: {
      title: 'The plan',
      body: [
        'What the test will do, before anything runs. The line at the top sums it up and says ' +
        'how long it takes in all.',
        'The drawing shows the memory held over time, with each machine\'s RAM as a line. ' +
        'Where the load goes over a line, that size cannot fit it.',
        'The list under it gives the peak as a share of each machine\'s RAM. Around 80% is ' +
        'already tight, because the operating system keeps a few hundred MB for itself; over ' +
        '100% means heavy swapping or the kernel killing the test.'
      ]
    },

    commands: {
      title: 'Commands each instance will run',
      body: ['The exact commands each machine\'s agent runs for this test, for anyone who ' +
             'wants to check it or repeat it by hand. stress is a standard Linux load tool.']
    },

    token: {
      title: 'Server token',
      body: [
        'A password for this dashboard\'s server. Starting or stopping a test needs it; ' +
        'watching needs it or the watch-only code.',
        'The page opened by ./aws.sh up or ./aws.sh open has it filled in already, and ' +
        './aws.sh token prints it. Otherwise it is SYSHEALTH_TOKEN on the server. It is kept ' +
        'in this browser only.'
      ]
    },

    access: {
      title: 'Access code',
      body: [
        'This dashboard\'s server shows its machines only to someone with a code, so a link ' +
        'passed around shows nothing on its own.',
        'Two codes work. The watch-only code shows everything and changes nothing: it is the ' +
        'one to give an audience. The server\'s token also starts and stops tests.',
        'Whoever runs the fleet gets both from the terminal with ./aws.sh token.'
      ]
    },

    start: {
      title: 'Start and stop',
      body: [
        'Start runs the test on every machine reporting now; the button says how many. They ' +
        'all start at the same moment.',
        'Stop test ends it on all of them and frees the memory.',
        'The line beside the buttons reports what each machine answered.'
      ]
    },

    results: {
      title: 'Results',
      body: [
        'How every size coped with the most recent load test, worked out on the server from ' +
        'the readings the machines sent. It fills in while the test runs (Test in progress) ' +
        'and settles when it ends (Last test). The line under the title says what the test was.'
      ]
    },

    'which-size': {
      title: 'Which size to choose',
      body: [
        'The verdict: the cheapest size that held your app at its busiest, plus the spare you ' +
        'ask for, without slowing down. Slowing down means going over the degraded line.',
        'The points under it set the pick against the others: what the next size down would ' +
        'save and why it is not enough, what it saves against the biggest size tested, and ' +
        'cheaper sizes with the same memory that the test did not cover.',
        'Change the boxes on the right and the verdict updates at once, without running the ' +
        'test again.',
        'The last line, when shown, sets this against the usual alarm rule, memory over 80% ' +
        'in use. It says whether that rule would have picked a bigger size, paying for memory ' +
        'nothing needed, or a smaller one that would have slowed down.'
      ]
    },
    'rs-need': {
      title: 'Your app at its busiest',
      body: ['What the verdict judges against, in MB. Left blank, it uses the value the test ' +
             'was started with.']
    },
    'rs-margin': {
      title: 'Keep spare',
      body: ['Room to leave above your app\'s busiest, for growth and surprises. 25% means a ' +
             'size must hold 1.25 times your app\'s memory to be chosen.']
    },
    'rs-cpu': {
      title: 'Its average CPU',
      body: [
        'Optional. How much CPU your app uses on average, in vCPUs: half of one vCPU busy all ' +
        'day is 0.5.',
        't3 sizes are burstable. They earn CPU credits at a fixed rate, and using more than ' +
        'that costs extra. With this filled in, Per month includes those credits, and sizes ' +
        'that cannot keep up at all are marked CPU too low.'
      ]
    },

    curve: {
      title: 'Pressure at each load level',
      body: [
        'For a variable test: one line per size, showing the pressure it reached at each ' +
        'amount of memory the test held. Each point is a step\'s average, skipping its first ' +
        '10 seconds while the reading settles.',
        'Where a line crosses the degraded line is the most that size can hold. A circle marks ' +
        'that point; it is the Holds up to figure in the table below.',
        'The solid upright line is your app at its busiest, and the dotted one adds the spare ' +
        'you asked for. A size is only chosen if its line stays under the degraded line until ' +
        'past the dotted one.',
        'A × above the chart marks a level at which the kernel killed the test on that size: ' +
        'it ran out of memory, so there is no pressure to plot.'
      ]
    },

    'col-r-instance': {
      title: 'Instance',
      body: ['The machine, by size. Recommended marks the size the verdict picked.']
    },
    'col-outcome': {
      title: 'Outcome',
      body: [
        'How the size coped with the whole test:',
        [
          'Coped comfortably: stayed HEALTHY.',
          'Coped, but slowed down: went DEGRADED. A variable test says from which load.',
          'Could not cope: went CRITICAL. A variable test says from which load.',
          'Ran out of memory: the kernel killed the test.',
          'Stopped reporting: it dropped out before the end, so it cannot be judged.'
        ]
      ]
    },
    'col-holds': {
      title: 'Holds up to',
      body: [
        'The most memory this size held while staying healthy, under the degraded line.',
        'Between two load levels it is estimated from the pressure at each, and the estimate ' +
        'errs low. Point at the figure to see the level it held and the level it slowed at.'
      ]
    },
    'col-spare': {
      title: 'Spare',
      body: [
        'How far Holds up to is above your app\'s busiest, as a share of it.',
        'Red: it slows down before your app\'s peak. Grey: it holds your app, but with less ' +
        'spare than you asked for, or the test did not go high enough to tell.'
      ]
    },
    'col-month': {
      title: 'Per month',
      body: [
        'The on-demand price of running this size for a month in its region, from the price ' +
        'list in data/prices.json.',
        'With your app\'s average CPU filled in, it adds the CPU credits a burstable size would ' +
        'burn above its baseline. The test itself loads memory, not CPU.'
      ]
    },
    'col-worst-minute': {
      title: 'Worst minute',
      body: ['The highest one-minute average of waiting for memory during the test. The figure ' +
             'the state is judged on.']
    },
    'col-worst-10s': {
      title: 'Worst 10 s',
      body: ['The sharpest spike: the highest 10-second reading during the test.']
    },
    'col-critical-from': {
      title: 'Critical from',
      body: ['For a variable test: the smallest load at which this size went CRITICAL. never ' +
             'if it did not.']
    }
  };

  function gb(mb) {
    return mb >= 1024 ? (Math.round(mb / 1024 * 10) / 10) + ' GB' : mb + ' MB';
  }

  var pop = null;
  var openFor = null;

  function build() {
    pop = document.createElement('div');
    pop.className = 'help-pop';
    pop.id = 'help-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-labelledby', 'help-pop-title');
    pop.hidden = true;
    document.body.appendChild(pop);
  }

  function render(entry) {
    while (pop.firstChild) {
      pop.removeChild(pop.firstChild);
    }
    var head = document.createElement('div');
    head.className = 'help-pop-head';
    var title = document.createElement('h3');
    title.className = 'help-pop-title';
    title.id = 'help-pop-title';
    title.textContent = entry.title;
    head.appendChild(title);
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'help-pop-close';
    close.setAttribute('aria-label', 'Close');
    close.textContent = '×';
    close.addEventListener('click', function () {
      hide(true);
    });
    head.appendChild(close);
    pop.appendChild(head);

    var body = typeof entry.body === 'function' ? entry.body() : entry.body;
    body.forEach(function (part) {
      if (Array.isArray(part)) {
        var list = document.createElement('ul');
        part.forEach(function (text) {
          var item = document.createElement('li');
          item.textContent = text;
          list.appendChild(item);
        });
        pop.appendChild(list);
      } else {
        var p = document.createElement('p');
        p.textContent = part;
        pop.appendChild(p);
      }
    });
  }

  // Under the button, or above it when there is no room below; never off
  // either side of the window.
  function place(trigger) {
    var gutter = 16;
    var rect = trigger.getBoundingClientRect();
    var width = Math.min(380, document.documentElement.clientWidth - gutter * 2);
    pop.style.width = width + 'px';
    var left = Math.max(gutter, Math.min(rect.left + rect.width / 2 - width / 2,
      document.documentElement.clientWidth - width - gutter));
    var top = rect.bottom + 8;
    var height = pop.offsetHeight;
    if (top + height > window.innerHeight - gutter && rect.top - 8 - height > gutter) {
      top = rect.top - 8 - height;
    }
    pop.style.left = (left + window.scrollX) + 'px';
    pop.style.top = (top + window.scrollY) + 'px';
  }

  function show(trigger) {
    var entry = HELP[trigger.getAttribute('data-help')];
    if (!entry) {
      return;
    }
    if (openFor) {
      openFor.setAttribute('aria-expanded', 'false');
    }
    render(entry);
    pop.hidden = false;
    place(trigger);
    openFor = trigger;
    trigger.setAttribute('aria-expanded', 'true');
  }

  function hide(refocus) {
    if (!pop || pop.hidden) {
      return;
    }
    pop.hidden = true;
    var trigger = openFor;
    openFor = null;
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
      // A card rebuilt since it opened has a new button; only a live one
      // can take the focus back.
      if (refocus && document.body.contains(trigger)) {
        trigger.focus();
      }
    }
  }

  /* ---------- the guide card ---------- */

  var GUIDE_KEY = 'syshealth.guide';

  function setGuide(open) {
    document.getElementById('guide').hidden = !open;
    document.getElementById('guide-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
    try {
      window.localStorage.setItem(GUIDE_KEY, open ? 'open' : 'closed');
    } catch (e) {
      /* Then it opens again on the next visit. */
    }
  }

  function wireGuide() {
    var saved = null;
    try {
      saved = window.localStorage.getItem(GUIDE_KEY);
    } catch (e) {
      saved = null;
    }
    // Open on a first visit, so nobody has to find it; closed once closed.
    setGuide(saved !== 'closed');
    document.getElementById('guide-toggle').addEventListener('click', function () {
      setGuide(document.getElementById('guide').hidden);
    });
    document.getElementById('guide-close').addEventListener('click', function () {
      setGuide(false);
      document.getElementById('guide-toggle').focus();
    });
  }

  function init() {
    build();
    wireGuide();
    document.addEventListener('click', function (event) {
      var trigger = event.target.closest('[data-help]');
      if (trigger) {
        // A ? inside a label must not also tick or focus the label's input.
        event.preventDefault();
        if (openFor === trigger && !pop.hidden) {
          hide(false);
        } else {
          show(trigger);
        }
        return;
      }
      if (!pop.hidden && !pop.contains(event.target)) {
        hide(false);
      }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        hide(true);
      }
    });
    // Phones resize the window as their address bar comes and goes: follow
    // the button rather than close.
    window.addEventListener('resize', function () {
      if (openFor && !pop.hidden) {
        if (document.body.contains(openFor)) {
          place(openFor);
        } else {
          hide(false);
        }
      }
    });
  }

  window.SysHealthHelp = {
    // The lines the agents report, so the explanations quote the real ones.
    setThresholds: function (value) {
      if (!value) {
        return;
      }
      th = value;
      Array.prototype.forEach.call(document.querySelectorAll('[data-th]'), function (node) {
        node.textContent = th[node.getAttribute('data-th')];
      });
    },
    // A ? for parts dashboard.js draws itself.
    button: function (key, what) {
      var node = trigger('help', key);
      node.setAttribute('aria-label', 'Explain ' + what);
      node.textContent = '?';
      return node;
    },
    // A label that explains itself, underlined: for cards that would
    // otherwise carry a ? on every line.
    term: function (key, text) {
      var node = trigger('term', key);
      node.textContent = text;
      return node;
    }
  };

  function trigger(className, key) {
    var node = document.createElement('button');
    node.type = 'button';
    node.className = className;
    node.setAttribute('data-help', key);
    node.setAttribute('aria-expanded', 'false');
    return node;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
