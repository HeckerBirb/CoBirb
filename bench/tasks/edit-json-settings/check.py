import json, os, sys
got = json.load(open("settings.json"))
want = json.load(open(os.path.join(os.environ["BENCH_TASK"], "repo", "settings.json")))
want["logging"]["level"] = "DEBUG"
ok = (got.get("logging", {}).get("level") == "DEBUG"
      and sorted(got["plugins"]["enabled"]) == sorted(["auth", "search", "metrics"])
      and {k: v for k, v in got.items() if k not in ("logging", "plugins")}
          == {k: v for k, v in want.items() if k not in ("logging", "plugins")}
      and got["logging"].get("file") == "logs/app.log"
      and got["plugins"].get("disabled") == ["legacy"])
sys.exit(0 if ok else 1)
