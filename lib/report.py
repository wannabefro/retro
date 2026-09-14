"""Pure formatting for retro: the window guard, and plain-text tables."""

MAX_LINE = 100
COLUMN_GAP = 2
MIN_KEY_WIDTH = 24
OUTLIER_MARGIN = 2

_CLUSTER_HEADERS = ["COUNT", "PER-DAY", "SESSIONS", "SCOPE", "KEY"]
_CLUSTER_ALIGNS = ["right", "right", "right", "left", "left"]
_CLUSTER_KEY_IDX = 4
_LEDGER_HEADERS = ["ID", "SCOPE", "STATUS", "BASELINE/DAY", "CHECKPOINT", "VERDICT"]
_LEDGER_ALIGNS = ["left", "left", "left", "right", "left", "left"]
_VERDICT_HEADERS = ["ID", "STATUS", "DELTA", "BASELINE/DAY", "CURRENT/DAY"]
_VERDICT_ALIGNS = ["left", "left", "right", "right", "right"]
_STATUS_ORDER = ["probation", "kept", "reverted"]


def assert_window(extract_days, cluster_days):
    """Raise ValueError when the extract and cluster windows disagree."""
    if extract_days != cluster_days:
        raise ValueError(
            f"extraction window ({extract_days} days) and clustering window "
            f"({cluster_days} days) disagree; per_day would be meaningless"
        )


def _widths(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    return widths


def _cap_outliers(headers, rows, skip_idx):
    """Cap each non-skipped column at its second-longest value plus a margin."""
    widths = _widths(headers, rows)
    if len(rows) < 2:
        return widths
    for i in range(len(headers)):
        if i == skip_idx:
            continue
        lengths = sorted((len(row[i]) for row in rows), reverse=True)
        cap = max(len(headers[i]), lengths[1] + OUTLIER_MARGIN)
        widths[i] = min(widths[i], cap)
    return widths


def _fit_widths(widths, headers, max_line):
    """Shrink the widest column at a time, never below its own header length."""
    total = sum(widths) + COLUMN_GAP * (len(widths) - 1)
    while total > max_line:
        candidates = [i for i in range(len(widths)) if widths[i] > len(headers[i])]
        if not candidates:
            break
        widest = max(candidates, key=lambda i: widths[i])
        widths[widest] -= 1
        total -= 1


def _fit_key_widths(headers, rows, key_idx, max_line):
    """Cap non-KEY columns, then give KEY the remaining line budget."""
    widths = _cap_outliers(headers, rows, key_idx)
    gaps = COLUMN_GAP * (len(headers) - 1)
    other_idxs = [i for i in range(len(headers)) if i != key_idx]

    def remainder():
        return max_line - sum(widths[i] for i in other_idxs) - gaps

    while remainder() < MIN_KEY_WIDTH:
        candidates = [i for i in other_idxs if widths[i] > len(headers[i])]
        if not candidates:
            break
        widest = max(candidates, key=lambda i: widths[i])
        widths[widest] -= 1
    widths[key_idx] = max(remainder(), len(headers[key_idx]))
    return widths


def _truncate_end(cell, width):
    if width <= 3:
        return cell[:width]
    return cell[: width - 3] + "..."


def _truncate_middle(cell, width):
    """Cut from the middle so both the head and the tail of the value survive."""
    if width <= 1:
        return cell[:width]
    keep = width - 1
    left = (keep + 1) // 2
    right = keep - left
    tail = cell[-right:] if right > 0 else ""
    return cell[:left] + "…" + tail


def _render_row(cells, widths, aligns, middle_idx=None):
    parts = []
    for i, (cell, width, align) in enumerate(zip(cells, widths, aligns)):
        if len(cell) > width:
            cell = _truncate_middle(cell, width) if i == middle_idx else _truncate_end(cell, width)
        parts.append(cell.rjust(width) if align == "right" else cell.ljust(width))
    return (" " * COLUMN_GAP).join(parts).rstrip()


def _table(headers, rows, aligns, max_line=MAX_LINE, key_idx=None, middle_idx=None):
    if key_idx is not None:
        widths = _fit_key_widths(headers, rows, key_idx, max_line)
    else:
        widths = _widths(headers, rows)
        _fit_widths(widths, headers, max_line)
    lines = [_render_row(headers, widths, aligns, middle_idx)]
    for row in rows:
        lines.append(_render_row(row, widths, aligns, middle_idx))
    return "\n".join(lines)


def _cluster_rows(records):
    return [
        [str(r["count"]), f"{r['per_day']:.2f}", str(r["sessions"]), r["scope"], r["key"]]
        for r in records
    ]


def _cluster_table(records):
    rows = _cluster_rows(records)
    return _table(_CLUSTER_HEADERS, rows, _CLUSTER_ALIGNS, key_idx=_CLUSTER_KEY_IDX, middle_idx=_CLUSTER_KEY_IDX)


def format_clusters(clusters, rejected=None):
    """Render passed clusters as a table, with an optional rejected section."""
    lines = []
    if not clusters:
        lines.append("No clusters passed the threshold gate.")
    else:
        lines.append(_cluster_table(clusters))
    if rejected is not None:
        lines.append("")
        lines.append("Rejected (below threshold):")
        if not rejected:
            lines.append("None.")
        else:
            lines.append(_cluster_table(rejected))
    return "\n".join(lines)


def _status_rank(status):
    return _STATUS_ORDER.index(status) if status in _STATUS_ORDER else len(_STATUS_ORDER)


def _ledger_rows(entries):
    ordered = sorted(entries, key=lambda e: (_status_rank(e["status"]), e["id"]))
    rows = []
    for entry in ordered:
        result = entry.get("result") or {}
        verdict_str = ""
        if result:
            verdict_str = f"{result['status']} ({result['delta_pct']:+.1%})"
        rows.append([
            entry["id"],
            entry["scope"],
            entry["status"],
            f"{entry['baseline']['per_day']:.2f}",
            entry["checkpoint_at"][:10],
            verdict_str,
        ])
    return rows


def format_ledger(entries):
    """Render ledger entries as a table, probation entries listed first."""
    if not entries:
        return "No ledger entries."
    return _table(_LEDGER_HEADERS, _ledger_rows(entries), _LEDGER_ALIGNS)


def _fmt_pct(value):
    return f"{value:+.1%}" if value is not None else "n/a"


def _fmt_rate(value):
    return f"{value:.2f}" if value is not None else "n/a"


def _verdict_rows(rows):
    return [
        [
            row["id"],
            row["verdict"]["status"],
            _fmt_pct(row["verdict"]["delta_pct"]),
            _fmt_rate(row["verdict"]["baseline_per_day"]),
            _fmt_rate(row["verdict"]["current_per_day"]),
        ]
        for row in rows
    ]


def format_verdicts(rows):
    """Render verify verdicts, with a revert or unmeasurable hint under each such row."""
    if not rows:
        return "No entries due for verification."
    lines = [_table(_VERDICT_HEADERS, _verdict_rows(rows), _VERDICT_ALIGNS)]
    for row in rows:
        status = row["verdict"]["status"]
        if status == "reverted" and row.get("revert_hint"):
            lines.append(
                f"  {row['id']}: still live. Run this to revert it: {row['revert_hint']}"
            )
        elif status == "unmeasurable":
            reason = row["verdict"].get("reason", "")
            lines.append(f"  {row['id']}: unmeasurable. {reason}".rstrip())
    return "\n".join(lines)
