"""Pure timing rules shared by program packaging and act-break analysis."""


def normal_package_ms(program_ms: int, clock_slot_ms: int, short_slot_ms: int) -> int:
    if program_ms <= short_slot_ms:
        return short_slot_ms
    return ((program_ms + clock_slot_ms - 1) // clock_slot_ms) * clock_slot_ms


def break_targets(duration_ms: int, package_ms: int, minimum_ms: int) -> list[dict]:
    # Each half-hour portion has an interior cut; adjacent portions share a cut.
    portions = min(max(1, (package_ms + 1_799_999) // 1_800_000),
                   duration_ms // (2 * minimum_ms))
    if portions < 1:
        return []
    boundaries = [0]
    for index in range(1, portions):
        lower = boundaries[-1] + 2 * minimum_ms
        upper = duration_ms - 2 * (portions - index) * minimum_ms
        boundaries.append(max(lower, min(index * 1_500_000, upper)))
    boundaries.append(duration_ms)
    result = []
    for index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
        result.append({"target_ms": (left + right) // 2, "role": "interior"})
        if index < portions - 1:
            result.append({"target_ms": right, "role": "portion_boundary"})
    return [{"index": index, **item} for index, item in enumerate(result, 1)]
