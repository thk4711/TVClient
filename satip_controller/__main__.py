import argparse
import asyncio
import fcntl
import logging
import os
import signal

from .config import Config, library, credentials
from .controller import Controller
from .input import Inputs
from .player import Player
from .state import State, read_json


async def run(config):
    os.umask(0o077)
    config.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    with (config.runtime_dir / "controller.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        channels, favourites = library(read_json(config.library))
        credentials(config.credentials)
        state = State(config.state_dir / "state.json", favourites)
        player = Player(config, state)
        controller = Controller(config, player, state)
        inputs = Inputs(config, controller.action, controller.local_press)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        tasks = [asyncio.create_task(controller.run()), asyncio.create_task(inputs.run())]
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stopper:
                    task.result()
        finally:
            for task in [*tasks, stopper]:
                task.cancel()
            await asyncio.gather(*tasks, stopper, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/satip-player/config.json")
    parser.add_argument("--check", action="store_true", help="Validate without starting playback or input")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Lounge query strings contain private session tokens.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = Config.load(args.config)
        library(read_json(config.library))
        credentials(config.credentials)
        if args.check:
            print("Configuration and playback credential permissions valid")
        else:
            asyncio.run(run(config))
    except (OSError, ValueError, KeyError, TypeError):
        logging.error("Configuration, device or runtime unavailable; check protected files and permissions")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
