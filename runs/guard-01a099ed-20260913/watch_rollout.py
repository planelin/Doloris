# -*- coding: utf-8 -*-
"""Rollout watcher for codex thread 01a099ed (guard mode).

Polls the rollout JSONL from a byte offset and exits with a code that tells
the ZCode guard loop what happened:
  0 = task_complete  (turn finished; last agent msg dumped to last-agent-msg.txt)
  1 = timeout        (max duration reached, turn still running -> restart me)
  3 = stall          (rollout not growing > stale_sec while turn active -> check GUI)
  4 = error event    (error/turn_aborted/stream_error in rollout)
  5 = app dead       (ChatGPT.exe process gone)
"""
import argparse, json, os, sys, time, io

def now_s():
    return time.strftime("%H:%M:%S")

def last_assistant_message(path):
    msg = None
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            p = j.get("payload")
            if j.get("type") == "response_item" and isinstance(p, dict) \
               and p.get("type") == "message" and p.get("role") == "assistant":
                txt = " ".join(c.get("text", "") for c in p.get("content", [])
                               if isinstance(c, dict))
                if txt.strip():
                    msg = txt.strip()
    return msg

def log_event(guard_dir, kind, detail):
    import datetime
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "kind": kind, "detail": detail}
    with io.open(os.path.join(guard_dir, "interventions.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("[%s] %s %s" % (now_s(), kind, detail), flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rollout", required=True)
    ap.add_argument("--guard-dir", required=True)
    ap.add_argument("--offset-file", required=True)
    ap.add_argument("--poll", type=float, default=15.0)
    ap.add_argument("--stale", type=float, default=420.0)
    ap.add_argument("--max-sec", type=float, default=1500.0)
    ap.add_argument("--app-pid", type=int, default=25648)
    args = ap.parse_args()

    off_path = args.offset_file
    offset = 0
    if os.path.exists(off_path):
        try:
            offset = int(io.open(off_path).read().strip() or 0)
        except Exception:
            offset = 0

    started = time.time()
    last_size = -1
    last_growth = time.time()
    turn_active = None  # None=unknown, True/False

    if offset == 0 and not os.path.exists(off_path):
        # first start of this watcher: fast-forward to current EOF unless the
        # offset file says otherwise, so we only react to FUTURE events
        pass

    log_event(args.guard_dir, "WATCH_START",
              "offset=%d poll=%.0fs stale=%.0fs max=%.0fs" %
              (offset, args.poll, args.stale, args.max_sec))

    while True:
        time.sleep(args.poll)
        try:
            size = os.path.getsize(args.rollout)
        except OSError:
            log_event(args.guard_dir, "ROLLOUT_GONE", "file unreadable")
            sys.exit(5)

        # app process check
        try:
            out = os.popen('tasklist /FI "PID eq %d" /NH' % args.app_pid).read()
            if str(args.app_pid) not in out:
                log_event(args.guard_dir, "APP_DEAD", "pid %d gone" % args.app_pid)
                sys.exit(5)
        except Exception:
            pass

        if size > last_size:
            if last_size >= 0:
                last_growth = time.time()
            last_size = size
            # read the new chunk, look for task boundary events
            with io.open(args.rollout, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            new_off = offset + len(chunk)
            events = []
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("type") == "event_msg" and isinstance(j.get("payload"), dict):
                    events.append(j["payload"].get("type"))
            if events:
                log_event(args.guard_dir, "EVENTS", ",".join(events))
            if "task_complete" in events:
                msg = last_assistant_message(args.rollout)
                with io.open(os.path.join(args.guard_dir, "last-agent-msg.txt"),
                             "w", encoding="utf-8") as f:
                    f.write(msg or "")
                log_event(args.guard_dir, "TASK_COMPLETE", (msg or "")[:200].replace("\n", " | "))
                with io.open(off_path, "w") as f:
                    f.write(str(new_off))
                sys.exit(0)
            if any(e in events for e in ("error", "turn_aborted", "stream_error")):
                msg = last_assistant_message(args.rollout)
                with io.open(os.path.join(args.guard_dir, "last-agent-msg.txt"),
                             "w", encoding="utf-8") as f:
                    f.write(msg or "")
                log_event(args.guard_dir, "TURN_ERROR", ",".join(events))
                with io.open(off_path, "w") as f:
                    f.write(str(new_off))
                sys.exit(4)
            if "task_started" in events:
                turn_active = True
                log_event(args.guard_dir, "TURN_STARTED", "turn active")
            offset = new_off
            with io.open(off_path, "w") as f:
                f.write(str(offset))

        # stall detection: no growth while turn believed active
        if turn_active is not False and (time.time() - last_growth) > args.stale:
            log_event(args.guard_dir, "STALL", "no rollout growth for %.0fs" %
                      (time.time() - last_growth))
            sys.exit(3)

        if time.time() - started > args.max_sec:
            log_event(args.guard_dir, "WATCH_TIMEOUT",
                      "%.0fs elapsed, still streaming (turn_active=%s)" %
                      (time.time() - started, turn_active))
            sys.exit(1)

if __name__ == "__main__":
    main()
