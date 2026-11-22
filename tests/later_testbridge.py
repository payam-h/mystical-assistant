"""An isolated test bridge for plugins/later (not collected by pytest: no test_ prefix).

The bridge's own entry point (claude_telegram_bridge.main) also does things outside its state
dir: it rewrites Kimi's user config (rules.sync_agents), fetches pinned agent CLIs into
~/.mystical, fetches upstream into the shared .git (whatsnew) and arms the nightly digest. A test
must not touch any of that while the live bridge runs, so this boots only the parts the plugin
uses: the store, the dashboard server (the gateway the claude child calls lives there), the
prompt queue, the runner, and the installed plugins. Shutdown is the entry point's, in the same
order. Start it with a CLEAN environment (env -i): the shell the live bridge hosts carries its
Telegram token and dashboard token.

    env -i HOME=$HOME PATH=$PATH LANG=en_US.UTF-8 BRIDGE_DB=/tmp/later-test/state/bridge.db \
        DASH_PORT=8899 DASH_TOKEN=later-test BASE_PATH=/tmp/later-test/base MINIAPP_ENABLE=0 \
        python3 tests/later_testbridge.py
"""

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for key in ("BRIDGE_DB", "DASH_PORT", "BASE_PATH"):
    if not os.environ.get(key):
        sys.exit(f"{key} must be set: this harness never falls back to the live bridge's state.")
if os.environ.get("TELEGRAM_BOT_TOKEN"):
    sys.exit("TELEGRAM_BOT_TOKEN is set: start this with env -i so the test bridge can't message anyone.")
if os.path.realpath(os.path.dirname(os.environ["BRIDGE_DB"])) == os.path.realpath(os.path.expanduser("~/.bridge_state")):
    sys.exit("BRIDGE_DB points at the live bridge's state dir.")

from bridge import config, envsettings, profiles, queue_manager, runner, state, store  # noqa: E402


def main() -> None:
    envsettings.apply()
    store.init()
    profiles.seed_defaults()
    from bridge.dashboard import server as dash
    dash.start()
    orphaned = store.claim_orphaned_turns()
    queue_manager.get().kick()
    from bridge import integrations, plugins_ext
    plugins_ext.start_installed(integrations.installed)
    print(f"test bridge up: pid {os.getpid()} dash :{config.DASH_PORT} db {config.BRIDGE_DB} "
          f"plugins {sorted(plugins_ext._live)} orphaned {len(orphaned)}", flush=True)

    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        state.shutting_down = True
        plugins_ext.stop_all()
        dash.stop()
        runner.stop_children()
        print("test bridge down", flush=True)


if __name__ == "__main__":
    main()
