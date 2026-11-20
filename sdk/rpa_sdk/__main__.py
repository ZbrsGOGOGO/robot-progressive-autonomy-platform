"""Local simulation demo and HTTP platform entry point."""

import argparse
import json
import os
from pathlib import Path
import threading
import time

from .dataset import EpisodeStore
from .http_api import make_server
from .runtime import Runtime
from .sim import SimRobot


def demo_runtime(root, input_timeout_ms=350):
    runtime = Runtime(EpisodeStore(root), input_timeout_ms=input_timeout_ms)
    runtime.register_controller("web-demo", "web")
    runtime.register_controller("pico-demo", "pico")
    runtime.register_controller("python-demo", "python")
    runtime.register_robot(SimRobot("arm-demo"))
    runtime.register_robot(SimRobot("base-demo", "sim-mobile"))
    return runtime


def run_demo(root):
    runtime = demo_runtime(root)
    try:
        session = runtime.open_session("python-demo", "arm-demo", "demo-station")["session_id"]
        runtime.arm(session)
        episode = runtime.record_start(session, "simulated_reach")
        runtime.record_mark(session, "begin")
        for sequence in range(10):
            runtime.input(session, {
                "sequence": sequence, "tracking": True, "capture_time_ns": time.monotonic_ns(),
                "poses": {"left": {"position": [0.01 * sequence, 0, 0], "rotation": [0, 0, 0, 1]}},
                "grip": [sequence > 0, False],
            })
            time.sleep(1 / 30)
        runtime.hold(session)
        result = runtime.record_stop(session)
        runtime.close(session)
        return {"recording": episode, "committed": result}
    finally:
        runtime.shutdown()


def serve(host, port, root, token, input_timeout_ms):
    runtime = demo_runtime(root, input_timeout_ms)
    stop_tick = threading.Event()

    def watchdog():
        while not stop_tick.wait(0.025):
            runtime.tick()

    worker = threading.Thread(target=watchdog, name="input-watchdog", daemon=True)
    try:
        with make_server(runtime, host, port, token=token) as server:
            worker.start()
            print(f"Simulation workbench: http://{host}:{server.server_port}/", flush=True)
            server.serve_forever(poll_interval=0.15)
    except KeyboardInterrupt:
        pass
    finally:
        stop_tick.set()
        if worker.is_alive():
            worker.join(timeout=2)
        faults = runtime.shutdown()
        if faults:
            print(json.dumps({"shutdown_faults": faults}, ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rpa")
    sub = parser.add_subparsers(dest="command", required=True)
    server_parser = sub.add_parser("serve", help="serve the simulation workbench and SDK API")
    server_parser.add_argument("--host", default="127.0.0.1")
    server_parser.add_argument("--port", type=int, default=8765)
    server_parser.add_argument("--episodes", type=Path, default=Path("episodes"))
    server_parser.add_argument("--input-timeout-ms", type=int, default=350,
                               help="simulation input deadline (20–5000 ms)")
    demo_parser = sub.add_parser("demo", help="record a short simulation without networking")
    demo_parser.add_argument("--episodes", type=Path, default=Path("episodes"))
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve(args.host, args.port, args.episodes, os.environ.get("RPA_TOKEN"), args.input_timeout_ms)
        else:
            print(json.dumps(run_demo(args.episodes), ensure_ascii=False, indent=2, default=str))
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
