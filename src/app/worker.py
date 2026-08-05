"""Runner and sweeper entrypoint. Same image as the API, different role (D053).

@TODO claim work items with `FOR UPDATE SKIP LOCKED`, execute runs, heartbeat the lease, and
run the sweeper on a timer (docs/plan/07-distribution.md). This idles so that the second
entrypoint is real in compose before the engine exists.
"""

import logging
import time

logger = logging.getLogger(__name__)

IDLE_SECONDS = 5


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("worker up; no queue to claim from yet")
    while True:
        time.sleep(IDLE_SECONDS)


if __name__ == "__main__":
    main()
