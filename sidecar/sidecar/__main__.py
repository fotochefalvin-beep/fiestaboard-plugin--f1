"""`python -m sidecar` runs the sidecar; `python -m sidecar replay ...` a replay."""

import sys


def run(argv: list[str]) -> int:
    if argv and argv[0] == "replay":
        from .replay import main as replay_main

        return replay_main(argv[1:])
    from .main import main

    return main()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
