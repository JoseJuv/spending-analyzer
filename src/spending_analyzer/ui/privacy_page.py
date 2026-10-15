"""Step 6: where the data is, what the AI was sent, and deleting everything."""

import json

import streamlit as st

from spending_analyzer.ui.state import (audit_log_path, data_files, delete_all_data, demo_mode,
                                        get_store)


def _size(path) -> str:
    if not path.exists():
        return "–"
    size = path.stat().st_size
    if size < 1024:
        return "< 1 KB"  # not "0 KB", which reads as empty
    return f"{size / 1024:.0f} KB" if size < 1024 ** 2 else f"{size / 1024 ** 2:.1f} MB"


def _audit_entries() -> list[dict]:
    path = audit_log_path()
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def render() -> None:
    st.title("Data & privacy")
    if done := st.session_state.pop("privacy_deleted", None):
        st.success(done)
    st.markdown(
        "- Uploaded PDFs are read in memory and **never saved**.\n"
        "- The AI model runs **on this computer** (Ollama) and keeps nothing between "
        "requests.\n"
        "- Everything that is kept is listed below, and you can delete all of it.")

    st.subheader("Stored data")
    counts = get_store().counts()
    st.markdown(f"{counts['statements']} statements · {counts['transactions']} transactions "
                f"· {counts['counterparties']} counterparties · {counts['overrides']} "
                "per-purchase confirmations")
    st.dataframe([{"What": what, "File": str(path), "Size": _size(path)}
                  for what, path in data_files().items()], hide_index=True)

    st.subheader("What the AI was sent")
    entries = _audit_entries()
    if not entries:
        st.caption("No AI requests logged yet.")
    else:
        models = sorted({e.get("model", "?") for e in entries})
        tokens_in = sum(e.get("input_tokens") or 0 for e in entries)
        tokens_out = sum(e.get("output_tokens") or 0 for e in entries)
        st.markdown(f"{len(entries)} requests to {', '.join(models)} · {tokens_in:,} tokens "
                    f"in, {tokens_out:,} out".replace(",", " "))
        st.caption("Every request is logged before it is sent, so you can check exactly "
                   "what the model saw.")
        for entry in reversed(entries[-20:]):
            label = f"{entry.get('time', '')[:19].replace('T', ' ')} · {entry.get('model', '?')}"
            if "error" in entry:
                label += " · failed"
            with st.expander(label):
                st.code(entry.get("prompt", ""), language=None)

    st.subheader("Delete")
    what = "the demo data" if demo_mode() else "all my data"
    sure = st.checkbox(f"I understand that deleting {what} cannot be undone")
    if st.button(f"Delete {what}", type="primary", disabled=not sure):
        deleted = delete_all_data()
        st.session_state.privacy_deleted = (
            f"Deleted {len(deleted)} file(s). Your settings (chosen model, demo mode) "
            "were kept.")
        st.rerun()  # redraw the counts above from the now-empty database
