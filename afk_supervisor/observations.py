"""Read complete, new events; mtime/loop progress is never command acknowledgement."""
import json
from pathlib import Path


def file_size(path):
    try:
        return Path(path).stat().st_size if path else 0
    except OSError:
        return 0


def read_events(path, offset=0):
    if not path:
        return [], offset
    try:
        with Path(path).open("rb") as stream:
            if file_size(path) < offset:
                # Rotation/truncation is not fresh acceptance of a pending send.
                return [], offset
            stream.seek(offset)
            data = stream.read(2 * 1024 * 1024)
        end = data.rfind(b"\n") + 1
        if not end:
            return [], offset
        events = []
        for line in data[:end].splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except (ValueError, UnicodeDecodeError):
                continue
        return events, offset + end
    except OSError:
        return [], offset


def event_payload(event):
    data = event.get("payload") or event
    return data if isinstance(data, dict) else {}


def message_text(data):
    content = data.get("content") or data.get("message") or data.get("text") or ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
    return ""


def command_accepted(events, command, *, allow_turn_start=False):
    for event in events:
        data = event_payload(event)
        kind = data.get("type", "")
        if data.get("role") == "user" or kind in {"user_message", "UserMessage", "user"}:
            # Exact request text on this task, not a timestamp or another turn.
            if message_text(data) == command.strip():
                return True
        if allow_turn_start and kind in {"task_started", "turn_started", "turn.started"}:
            return True
    return False


def observe_worker(state, driver):
    path = getattr(driver, "jsonl", None)
    if path:
        target = str(Path(path).resolve())
        if state.event_path != target:
            state.event_path = target
            # Fresh fork history contains old events. Only the current command
            # or current stdout turn can acknowledge a new child launch.
            state.event_offset = state.dispatch_offset if state.dispatch_target == str(getattr(driver, "session_id", "")) else 0
        events, offset = read_events(path, state.event_offset)
        state.event_offset = offset
        # A fork copies historical turns; require the new stdout stream until
        # the child is bound, rather than accepting inherited task_started.
        accepted = command_accepted(events, state.pending_command,
                                    allow_turn_start=state.launch_kind != "fork") if state.pending_command else False
        state.worker_rollout = str(path)
    else:
        accepted = False
    stdout_events, stdout_offset = read_events(state.run_dir / "worker-stdout.log", state.dispatch_stdout_offset)
    state.dispatch_stdout_offset = stdout_offset
    if state.pending_command and command_accepted(stdout_events, state.pending_command, allow_turn_start=True):
        accepted = True
    if accepted:
        state.acknowledge_command()
    else:
        state.save()
    return accepted
