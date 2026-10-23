"""Deterministic ordered matching, with fixed manual anchors."""
from bisect import bisect_right


def match_manual(targets, overrides):
    """Minimum-distance monotone matching; timestamps are never changed."""
    manual = sorted(overrides.values())
    if len(manual) > len(targets):
        return {index: point for index, point in enumerate(manual, 1)}
    # state: matched manual count -> (distance, assigned target indices)
    states = {0: (0, ())}
    for target in targets:
        updated = dict(states)
        for count, (cost, indices) in states.items():
            if count == len(manual):
                continue
            value = (cost + abs(target['target_ms'] - manual[count]),
                     indices + (target['index'],))
            if count + 1 not in updated or value < updated[count + 1]:
                updated[count + 1] = value
        states = updated
    return dict(zip(states[len(manual)][1], manual))


def select_breaks(targets, candidates, overrides, minimum_ms, window_ms, allow_fallback, duration_ms):
    """Dynamic programming over target number and last selected source time."""
    # Objective: fill opportunities, preserve cue hierarchy, corroboration,
    # proximity, measured quality, then earlier timestamps. No greedy choices.
    zero = (0,) * 10
    states = {-minimum_ms: (zero, ())}
    all_options = []
    anchor_points = list(overrides.values())
    for target in targets:
        index, intended = target['index'], target['target_ms']
        options = []
        if index in overrides:
            options = [{'offset_ms': overrides[index], 'method': 'manual',
                        'score': 1.0, 'signals': {'manual': True}}]
        else:
            for candidate in candidates:
                method, signals = candidate['method'], candidate['signals']
                natural = method == 'fade' or (method == 'scene' and (
                    signals.get('silence') or signals.get('chapter_supported')))
                if not natural and abs(candidate['offset_ms'] - intended) > window_ms:
                    continue
                if method in {'quiet_pause', 'scene_fallback', 'midpoint'} and not allow_fallback:
                    continue
                options.append({**candidate, 'signals': dict(signals)})
            if allow_fallback:
                # Boundary alternatives allow a fallback to fit beside an anchor
                # without exceeding the normal window or inventing another target.
                points = {intended}
                for point in anchor_points:
                    points.update((point - minimum_ms, point + minimum_ms))
                for point in points:
                    if abs(point - intended) <= window_ms:
                        options.append({'offset_ms': point, 'method': 'midpoint', 'score': .25,
                            'signals': {'fallback': True, 'reason': 'no compatible stronger cue'}})
        options = [item for item in options if minimum_ms <= item['offset_ms'] <= duration_ms - minimum_ms and all(
            item['offset_ms'] - point >= minimum_ms if other < index
            else point - item['offset_ms'] >= minimum_ms
            for other, point in overrides.items() if other != index)]
        for item in options:
            item['signals'].update(window=index, target_ms=intended,
                target_role=target['role'], outside_preferred_window=
                abs(item['offset_ms'] - intended) > window_ms)
        all_options.extend(options)
        ordered = sorted(states)
        best = []
        for point in ordered:
            value = states[point]
            best.append(max(best[-1], value, key=lambda v: v[0]) if best else value)
        updated = {} if index in overrides else dict(states)
        for item in sorted(options, key=lambda c: (c['offset_ms'], c['method'])):
            point = item['offset_ms']
            before = bisect_right(ordered, point - minimum_ms) - 1
            if before < 0:
                continue
            score, path = best[before]
            method, signals = item['method'], item['signals']
            tier = {'manual': 0, 'fade': 0, 'scene': 0, 'still_card': 1,
                    'quiet_pause': 2, 'scene_fallback': 3, 'midpoint': 4}[method]
            quality = (int(signals.get('strict_black', False)) * 2000
                       + min(1500, round(signals.get('black_duration', 0) * 1000))
                       + signals.get('silence_duration_ms', 0)
                       + round(signals.get('scene_score', 0) * 1000))
            increment = (1, *(int(tier == n) for n in range(5)),
                         2 * int(bool(signals.get('chapter_supported'))) + int(bool(signals.get('silence'))),
                         -abs(point - intended), quality, -point)
            value = (tuple(a + b for a, b in zip(score, increment)), path + (item,))
            if point not in updated or value[0] > updated[point][0]:
                updated[point] = value
        states = updated
        if not states:
            raise ValueError('Manual breaks cannot fit the configured spacing')
    selected = list(max(states.values(), key=lambda v: v[0])[1])
    for item in selected:
        item['signals']['selection_reason'] = 'joint ordered selection; manual anchors, cue tier, corroboration, proximity, quality'
    # Unassigned evidence is retained even when outside every weaker-cue window.
    represented = {(c['offset_ms'], c['method']) for c in all_options}
    all_options.extend(c for c in candidates if (c['offset_ms'], c['method']) not in represented)
    return selected, all_options
